import { spawnSync } from "node:child_process";
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { isAbsolute, join } from "node:path";
import { fileURLToPath } from "node:url";
import { config as loadDotenv } from "dotenv";
import { getDbPool } from "./lib/db";
import { runContextSelector } from "./steps/context-selector";
import { contextBuilderWorkflow } from "./workflows/context-builder";

type ConformanceMode = "replay" | "live";

interface QueryFixture {
	id: string;
	query: string;
	conversation_history?: Array<{
		role: "user" | "assistant" | "system";
		content: string;
	}>;
}

interface BaselineRowLive {
	id: string;
	python?: {
		metadata?: {
			context_items_ref?: Array<{ section_id?: string | null }>;
			selector_all_rejected?: boolean | null;
		};
	} | null;
}

interface QueryProcessorReplayStage {
	output: {
		should_proceed?: boolean;
	};
}

interface SectionAggregatorReplayStage {
	output: {
		aggregated_sections: Array<{
			section_id?: string | null;
			score?: number | null;
			publisher?: string | null;
			heading?: string | null;
			chunk_count?: number | null;
		}>;
	};
}

interface ContextSelectorReplayStage {
	input: {
		query: string;
	};
	output: {
		selected_section_ids: string[];
		selector_all_rejected?: boolean;
	};
}

interface ParseArgsResult {
	queriesFile: string;
	outputFile: string | null;
	topK: number;
	mode: ConformanceMode;
	baselineDir: string;
}

interface HydratedSectionRow {
	section_id: string;
	section_markdown: string | null;
	references_juridiques: unknown;
	heading_path: string | null;
	doc_id: string | null;
	doc_short_id: string | null;
	doc_title: string | null;
	doc_url: string | null;
	doc_publisher: string | null;
	doc_token_count: number | null;
}

function parseArgs(argv: string[]): ParseArgsResult {
	const appRoot = fileURLToPath(new URL("../..", import.meta.url));
	const workspaceRoot = fileURLToPath(new URL("../../../..", import.meta.url));

	let queriesFile = join(workspaceRoot, "tests/conformance/queries.sample.jsonl");
	let outputFile: string | null = null;
	let topK = 10;
	let mode: ConformanceMode = "replay";
	let baselineDir = join(workspaceRoot, "tests/conformance/baselines/queries-sample");

	for (let i = 0; i < argv.length; i += 1) {
		const arg = argv[i];

		if (arg === "--queries-file") {
			const value = argv[i + 1];
			if (!value) {
				throw new Error("Missing value for --queries-file");
			}
			queriesFile = value;
			i += 1;
			continue;
		}

		if (arg === "--output") {
			const value = argv[i + 1];
			if (!value) {
				throw new Error("Missing value for --output");
			}
			outputFile = value;
			i += 1;
			continue;
		}

		if (arg === "--top-k") {
			const value = argv[i + 1];
			if (!value) {
				throw new Error("Missing value for --top-k");
			}
			topK = Number.parseInt(value, 10);
			i += 1;
			continue;
		}

		if (arg === "--mode") {
			const value = argv[i + 1];
			if (!value || (value !== "replay" && value !== "live")) {
				throw new Error("Invalid value for --mode (expected replay|live)");
			}
			mode = value;
			i += 1;
			continue;
		}

		if (arg === "--baseline-dir") {
			const value = argv[i + 1];
			if (!value) {
				throw new Error("Missing value for --baseline-dir");
			}
			baselineDir = value;
			i += 1;
			continue;
		}

		if (arg === "--skip-python-baseline") {
			mode = "replay";
		}
	}

	if (!Number.isFinite(topK) || topK <= 0) {
		throw new Error(`Invalid --top-k value: ${topK}`);
	}

	if (!isAbsolute(queriesFile)) {
		queriesFile = join(workspaceRoot, queriesFile);
	}

	if (outputFile && !isAbsolute(outputFile)) {
		outputFile = join(workspaceRoot, outputFile);
	}

	if (!isAbsolute(baselineDir)) {
		baselineDir = join(workspaceRoot, baselineDir);
	}

	loadDotenv({ path: join(workspaceRoot, ".env"), quiet: true });
	loadDotenv({ path: join(appRoot, ".env"), quiet: true, override: true });

	return { queriesFile, outputFile, topK, mode, baselineDir };
}

function readJsonlFixtures(path: string): QueryFixture[] {
	const content = readFileSync(path, "utf-8");
	return content
		.split("\n")
		.map((line) => line.trim())
		.filter((line) => line.length > 0)
		.map((line) => JSON.parse(line) as QueryFixture);
}

function uniqueSectionIdsFromContextItems(items: Array<{ sectionId: string | null }>): string[] {
	const out: string[] = [];
	for (const item of items) {
		const sectionId = String(item.sectionId ?? "").trim();
		if (!sectionId || out.includes(sectionId)) {
			continue;
		}
		out.push(sectionId);
	}
	return out;
}

function uniqueSectionIdsFromSelectedSections(
	sections: Array<{ sectionId: string | null }>,
): string[] {
	const out: string[] = [];
	for (const section of sections) {
		const sectionId = String(section.sectionId ?? "").trim();
		if (!sectionId || out.includes(sectionId)) {
			continue;
		}
		out.push(sectionId);
	}
	return out;
}

function buildDeterministicSelectorResponse(args: {
	sections: Array<{ sectionId: string | null }>;
	selectedSectionIds: string[];
}): string {
	const selectedSectionIdSet = new Set(
		args.selectedSectionIds.map((value) => value.trim()).filter((value) => value.length > 0),
	);

	const selectedIndices = args.sections
		.map((section, idx) => ({
			idx,
			sectionId: String(section.sectionId ?? "").trim(),
		}))
		.filter((item) => item.sectionId.length > 0 && selectedSectionIdSet.has(item.sectionId))
		.map((item) => item.idx);

	return JSON.stringify({
		selected_ids: selectedIndices,
		reason: "Deterministic replay selector response",
	});
}

async function hydrateAggregatedSectionsForReplay(
	rows: SectionAggregatorReplayStage["output"]["aggregated_sections"],
) {
	const nonEmptySectionIds = Array.from(
		new Set(
			rows
				.map((row) => String(row.section_id ?? "").trim())
				.filter((sectionId) => sectionId.length > 0),
		),
	);

	const byId = new Map<string, HydratedSectionRow>();

	if (nonEmptySectionIds.length > 0) {
		const db = getDbPool();
		const result = await db.query<HydratedSectionRow>(
			`
        SELECT
          s.section_id::text AS section_id,
          s.section_markdown,
          s.references_juridiques,
          s.heading_path,
          s.doc_id::text AS doc_id,
          d.short_id AS doc_short_id,
          d.title AS doc_title,
          d.source_url AS doc_url,
          d.publisher AS doc_publisher,
          d.token_count AS doc_token_count
        FROM rag_sections s
        LEFT JOIN rag_documents d ON d.doc_id = s.doc_id
        WHERE s.section_id = ANY($1::uuid[])
      `,
			[nonEmptySectionIds],
		);

		for (const row of result.rows) {
			byId.set(row.section_id, row);
		}
	}

	return rows.map((row) => {
		const sectionId = String(row.section_id ?? "").trim();
		const hydrated = sectionId ? byId.get(sectionId) : undefined;
		const score = typeof row.score === "number" ? row.score : 0;
		const publisher =
			(typeof row.publisher === "string" && row.publisher.trim().length > 0
				? row.publisher.trim()
				: null) ??
			hydrated?.doc_publisher ??
			null;

		return {
			sectionId: sectionId || null,
			heading:
				(typeof row.heading === "string" && row.heading.trim().length > 0
					? row.heading.trim()
					: "") || `section ${sectionId || "unknown"}`,
			markdown: hydrated?.section_markdown ?? "",
			chunks: [],
			score,
			documentId: hydrated?.doc_id ?? null,
			publisher,
			referencesJuridiques: hydrated?.references_juridiques ?? null,
			headingPath: hydrated?.heading_path ?? null,
			metadata: {
				doc_id: hydrated?.doc_id ?? "",
				doc_short_id: hydrated?.doc_short_id ?? "",
				doc_title: hydrated?.doc_title ?? "",
				doc_url: hydrated?.doc_url ?? null,
				doc_publisher: publisher ?? "",
				doc_date: "",
				doc_token_count: hydrated?.doc_token_count ?? 0,
				chunk_count: typeof row.chunk_count === "number" ? row.chunk_count : 0,
				max_chunk_score: score,
				mean_chunk_score: score,
			},
		};
	});
}

function loadReplayBaseline(
	fixtureId: string,
	baselineDir: string,
): {
	shouldProceed: boolean;
	queryForRetrieval: string;
	aggregatedSections: SectionAggregatorReplayStage["output"]["aggregated_sections"];
	expectedSelectedSectionIds: string[];
	selectorAllRejected: boolean;
} {
	const qpPath = join(baselineDir, fixtureId, "01_query_processor.json");
	const sectionPath = join(baselineDir, fixtureId, "03_section_aggregator.json");
	const selectorPath = join(baselineDir, fixtureId, "04_context_selector.json");

	for (const path of [qpPath, sectionPath, selectorPath]) {
		if (!existsSync(path)) {
			throw new Error(`Missing replay baseline file: ${path}`);
		}
	}

	const qp = JSON.parse(readFileSync(qpPath, "utf-8")) as QueryProcessorReplayStage;
	const section = JSON.parse(readFileSync(sectionPath, "utf-8")) as SectionAggregatorReplayStage;
	const selector = JSON.parse(readFileSync(selectorPath, "utf-8")) as ContextSelectorReplayStage;

	return {
		shouldProceed: Boolean(qp.output.should_proceed),
		queryForRetrieval: selector.input.query,
		aggregatedSections: section.output.aggregated_sections,
		expectedSelectedSectionIds: (selector.output.selected_section_ids ?? [])
			.map((value) => String(value).trim())
			.filter((value) => value.length > 0),
		selectorAllRejected: Boolean(selector.output.selector_all_rejected),
	};
}

function runPythonBaseline(
	workspaceRoot: string,
	queriesFile: string,
): {
	rows: BaselineRowLive[];
	status: number;
	outputFile: string;
	stderr: string;
	errorCount: number;
} {
	const outputFile = join(tmpdir(), `mastra-conformance-live-context-selector-${Date.now()}.json`);

	const run = spawnSync(
		"uv",
		[
			"run",
			"python",
			"scripts/run_mastra_conformance.py",
			"--queries-file",
			queriesFile,
			"--output",
			outputFile,
		],
		{
			cwd: workspaceRoot,
			encoding: "utf-8",
			stdio: "pipe",
		},
	);

	if (!existsSync(outputFile)) {
		throw new Error(
			`Python baseline output was not created (status ${run.status ?? -1}).\nSTDERR:\n${run.stderr}`,
		);
	}

	const report = JSON.parse(readFileSync(outputFile, "utf-8")) as {
		results?: BaselineRowLive[];
		errors?: unknown[];
	};

	return {
		rows: Array.isArray(report.results) ? report.results : [],
		status: run.status ?? -1,
		outputFile,
		stderr: run.stderr ?? "",
		errorCount: Array.isArray(report.errors) ? report.errors.length : 0,
	};
}

function topKJaccard(left: string[], right: string[], topK: number): number | null {
	const leftTopK = left.slice(0, topK).filter((value) => value.length > 0);
	const rightTopK = right.slice(0, topK).filter((value) => value.length > 0);

	const leftSet = new Set(leftTopK);
	const rightSet = new Set(rightTopK);

	if (leftSet.size === 0 && rightSet.size === 0) {
		return null;
	}

	const union = new Set([...leftSet, ...rightSet]);
	const intersection = Array.from(leftSet).filter((value) => rightSet.has(value));
	return intersection.length / union.size;
}

async function main(): Promise<void> {
	const { queriesFile, outputFile, topK, mode, baselineDir } = parseArgs(process.argv.slice(2));
	const workspaceRoot = fileURLToPath(new URL("../../../..", import.meta.url));

	const fixtures = readJsonlFixtures(queriesFile);

	const baselineLive =
		mode === "live"
			? runPythonBaseline(workspaceRoot, queriesFile)
			: {
					rows: [] as BaselineRowLive[],
					status: 0,
					outputFile: null,
					stderr: "",
					errorCount: 0,
				};

	const baselineLiveById = new Map(baselineLive.rows.map((row) => [row.id, row]));

	const errors: Array<{ id: string; error: string }> = [];
	const details: Array<{
		id: string;
		overlapTopK: number | null;
		pythonSelectedSectionIds: string[];
		candidateSelectedSectionIds: string[];
		branchPath: "rag" | "non_rag";
		selectorEnabled: boolean | null;
		selectorAllRejected: boolean | null;
		selectorItemsBefore: number | null;
		selectorItemsAfter: number | null;
	}> = [];

	for (const fixture of fixtures) {
		try {
			if (mode === "replay") {
				const replay = loadReplayBaseline(fixture.id, baselineDir);

				if (!replay.shouldProceed) {
					details.push({
						id: fixture.id,
						overlapTopK: topKJaccard(replay.expectedSelectedSectionIds, [], topK),
						pythonSelectedSectionIds: replay.expectedSelectedSectionIds.slice(0, topK),
						candidateSelectedSectionIds: [],
						branchPath: "non_rag",
						selectorEnabled: null,
						selectorAllRejected: replay.selectorAllRejected,
						selectorItemsBefore: null,
						selectorItemsAfter: null,
					});
					continue;
				}

				const hydratedSections = await hydrateAggregatedSectionsForReplay(
					replay.aggregatedSections,
				);

				const forcedRawResponse = buildDeterministicSelectorResponse({
					sections: hydratedSections,
					selectedSectionIds: replay.expectedSelectedSectionIds,
				});

				const selectorResult = await runContextSelector(
					{
						queryForRetrieval: replay.queryForRetrieval,
						sections: hydratedSections,
						config: {
							enabled: true,
						},
					},
					undefined,
					{ forcedRawResponse },
				);

				const candidateSelectedSectionIds = uniqueSectionIdsFromSelectedSections(
					selectorResult.sections,
				);

				details.push({
					id: fixture.id,
					overlapTopK: topKJaccard(
						replay.expectedSelectedSectionIds,
						candidateSelectedSectionIds,
						topK,
					),
					pythonSelectedSectionIds: replay.expectedSelectedSectionIds.slice(0, topK),
					candidateSelectedSectionIds: candidateSelectedSectionIds.slice(0, topK),
					branchPath: "rag",
					selectorEnabled: selectorResult.selectorMeta.enabled,
					selectorAllRejected: selectorResult.selectorMeta.allRejected,
					selectorItemsBefore:
						selectorResult.selectorMeta.selectedCount + selectorResult.selectorMeta.removedCount,
					selectorItemsAfter: selectorResult.selectorMeta.selectedCount,
				});

				continue;
			}

			const run = await contextBuilderWorkflow.createRun();
			const result = await run.start({
				inputData: {
					query: fixture.query,
					conversationHistory: fixture.conversation_history,
				},
			});

			if (result.status !== "success") {
				errors.push({
					id: fixture.id,
					error: `Context builder workflow failed for ${fixture.id} with status ${result.status}`,
				});
				continue;
			}

			const candidateSelectedSectionIds = uniqueSectionIdsFromContextItems(
				result.result.contextItems,
			);

			const pyRow = baselineLiveById.get(fixture.id);
			const pythonSectionIds =
				pyRow?.python?.metadata?.context_items_ref
					?.map((item) => String(item.section_id ?? "").trim())
					.filter((sectionId) => sectionId.length > 0) ?? [];

			details.push({
				id: fixture.id,
				overlapTopK: topKJaccard(pythonSectionIds, candidateSelectedSectionIds, topK),
				pythonSelectedSectionIds: pythonSectionIds.slice(0, topK),
				candidateSelectedSectionIds: candidateSelectedSectionIds.slice(0, topK),
				branchPath: result.result.branchPath,
				selectorEnabled: result.result.selectorMeta?.enabled ?? null,
				selectorAllRejected: result.result.selectorMeta?.allRejected ?? null,
				selectorItemsBefore:
					result.result.selectorMeta === null
						? null
						: result.result.selectorMeta.selectedCount + result.result.selectorMeta.removedCount,
				selectorItemsAfter: result.result.selectorMeta?.selectedCount ?? null,
			});
		} catch (error) {
			const message = error instanceof Error ? error.message : String(error);
			errors.push({ id: fixture.id, error: message });
		}
	}

	const overlaps = details
		.map((detail) => detail.overlapTopK)
		.filter((value): value is number => value !== null);

	const selectorOverlapTopKAvg =
		overlaps.length === 0
			? null
			: overlaps.reduce((sum, value) => sum + value, 0) / overlaps.length;

	const report = {
		generatedAt: new Date().toISOString(),
		mode,
		baselineDir: mode === "replay" ? baselineDir : null,
		queriesFile,
		topK,
		queryCount: fixtures.length,
		succeededCount: details.length,
		failedCount: errors.length,
		pythonBaseline: {
			skipped: mode === "replay",
			status: baselineLive.status,
			outputFile: baselineLive.outputFile,
			errorCount: baselineLive.errorCount,
			stderrPreview: baselineLive.stderr.slice(0, 2000),
		},
		summary: {
			selectorOverlapTopKAvg,
		},
		details,
		errors,
	};

	const serialized = JSON.stringify(report, null, 2);
	if (outputFile) {
		writeFileSync(outputFile, serialized, "utf-8");
	}

	console.log(serialized);
}

void main();
