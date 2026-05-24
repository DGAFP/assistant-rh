import { spawnSync } from "node:child_process";
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { isAbsolute, join } from "node:path";
import { fileURLToPath } from "node:url";
import { config as loadDotenv } from "dotenv";
import { getDbPool } from "./lib/db";
import { runContextBuilder } from "./steps/context-builder";
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
		}>;
	};
}

interface ContextSelectorReplayStage {
	output: {
		selected_section_ids: string[];
		selector_all_rejected?: boolean;
	};
}

interface ContextBuilderReplayStage {
	output: {
		context_items_ref: Array<{ section_id?: string | null }>;
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
	heading: string | null;
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

function normalizeSectionIds(items: Array<{ section_id?: string | null }>): string[] {
	return items
		.map((item) => String(item.section_id ?? "").trim())
		.filter((sectionId) => sectionId.length > 0);
}

async function hydrateSectionsForReplay(args: {
	selectedSectionIds: string[];
	scoreBySectionId: Map<string, number>;
	headingBySectionId: Map<string, string>;
	publisherBySectionId: Map<string, string>;
}) {
	if (args.selectedSectionIds.length === 0) {
		return [];
	}

	const db = getDbPool();
	const result = await db.query<HydratedSectionRow>(
		`
      SELECT
        s.section_id::text AS section_id,
        s.heading,
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
		[args.selectedSectionIds],
	);

	const byId = new Map(result.rows.map((row) => [row.section_id, row]));

	return args.selectedSectionIds.map((sectionId) => {
		const row = byId.get(sectionId);
		return {
			sectionId,
			heading: row?.heading ?? args.headingBySectionId.get(sectionId) ?? `(section ${sectionId})`,
			markdown: row?.section_markdown ?? "",
			chunks: [],
			score: args.scoreBySectionId.get(sectionId) ?? 0,
			documentId: row?.doc_id ?? null,
			publisher: row?.doc_publisher ?? args.publisherBySectionId.get(sectionId) ?? null,
			referencesJuridiques: row?.references_juridiques ?? null,
			headingPath: row?.heading_path ?? null,
			metadata: {
				doc_id: row?.doc_id ?? "",
				doc_short_id: row?.doc_short_id ?? "",
				doc_title: row?.doc_title ?? "",
				doc_url: row?.doc_url ?? null,
				doc_publisher: row?.doc_publisher ?? args.publisherBySectionId.get(sectionId) ?? "",
				doc_date: "",
				doc_token_count: row?.doc_token_count ?? 0,
				chunk_count: 0,
				max_chunk_score: args.scoreBySectionId.get(sectionId) ?? 0,
				mean_chunk_score: args.scoreBySectionId.get(sectionId) ?? 0,
			},
		};
	});
}

function loadReplayBaseline(
	fixtureId: string,
	baselineDir: string,
): {
	shouldProceed: boolean;
	selectedSectionIds: string[];
	selectorAllRejected: boolean;
	pythonContextSectionIds: string[];
	scoreBySectionId: Map<string, number>;
	headingBySectionId: Map<string, string>;
	publisherBySectionId: Map<string, string>;
} {
	const qpPath = join(baselineDir, fixtureId, "01_query_processor.json");
	const sectionPath = join(baselineDir, fixtureId, "03_section_aggregator.json");
	const selectorPath = join(baselineDir, fixtureId, "04_context_selector.json");
	const contextPath = join(baselineDir, fixtureId, "05_context_builder.json");

	for (const path of [qpPath, sectionPath, selectorPath, contextPath]) {
		if (!existsSync(path)) {
			throw new Error(`Missing replay baseline file: ${path}`);
		}
	}

	const qp = JSON.parse(readFileSync(qpPath, "utf-8")) as QueryProcessorReplayStage;
	const section = JSON.parse(readFileSync(sectionPath, "utf-8")) as SectionAggregatorReplayStage;
	const selector = JSON.parse(readFileSync(selectorPath, "utf-8")) as ContextSelectorReplayStage;
	const context = JSON.parse(readFileSync(contextPath, "utf-8")) as ContextBuilderReplayStage;

	const scoreBySectionId = new Map<string, number>();
	const headingBySectionId = new Map<string, string>();
	const publisherBySectionId = new Map<string, string>();

	for (const row of section.output.aggregated_sections) {
		const sectionId = String(row.section_id ?? "").trim();
		if (!sectionId) {
			continue;
		}

		if (typeof row.score === "number") {
			scoreBySectionId.set(sectionId, row.score);
		}
		if (typeof row.heading === "string" && row.heading.trim().length > 0) {
			headingBySectionId.set(sectionId, row.heading.trim());
		}
		if (typeof row.publisher === "string" && row.publisher.trim().length > 0) {
			publisherBySectionId.set(sectionId, row.publisher.trim());
		}
	}

	return {
		shouldProceed: Boolean(qp.output.should_proceed),
		selectedSectionIds: selector.output.selected_section_ids ?? [],
		selectorAllRejected: Boolean(selector.output.selector_all_rejected),
		pythonContextSectionIds: normalizeSectionIds(context.output.context_items_ref),
		scoreBySectionId,
		headingBySectionId,
		publisherBySectionId,
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
	const outputFile = join(tmpdir(), `mastra-conformance-live-context-builder-${Date.now()}.json`);

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
		pythonTopContextSectionIds: string[];
		candidateTopContextSectionIds: string[];
		branchPath: "rag" | "non_rag";
		shortCircuit: boolean;
		selectorAllRejectedPython: boolean | null;
		contextMode: "standard" | "wide" | null;
		contextTokenCount: number | null;
		selectedSectionsCount: number;
		contextItemsCount: number;
	}> = [];

	for (const fixture of fixtures) {
		try {
			if (mode === "replay") {
				const replay = loadReplayBaseline(fixture.id, baselineDir);

				if (!replay.shouldProceed) {
					details.push({
						id: fixture.id,
						overlapTopK: topKJaccard(replay.pythonContextSectionIds, [], topK),
						pythonTopContextSectionIds: replay.pythonContextSectionIds.slice(0, topK),
						candidateTopContextSectionIds: [],
						branchPath: "non_rag",
						shortCircuit: true,
						selectorAllRejectedPython: replay.selectorAllRejected,
						contextMode: null,
						contextTokenCount: null,
						selectedSectionsCount: 0,
						contextItemsCount: 0,
					});
					continue;
				}

				const sections = await hydrateSectionsForReplay({
					selectedSectionIds: replay.selectedSectionIds,
					scoreBySectionId: replay.scoreBySectionId,
					headingBySectionId: replay.headingBySectionId,
					publisherBySectionId: replay.publisherBySectionId,
				});

				const contextBuilder = await runContextBuilder({
					sections,
				});

				const candidateSectionIds = contextBuilder.contextItems
					.map((item) => item.sectionId ?? "")
					.filter((sectionId) => sectionId.length > 0);

				details.push({
					id: fixture.id,
					overlapTopK: topKJaccard(replay.pythonContextSectionIds, candidateSectionIds, topK),
					pythonTopContextSectionIds: replay.pythonContextSectionIds.slice(0, topK),
					candidateTopContextSectionIds: candidateSectionIds.slice(0, topK),
					branchPath: "rag",
					shortCircuit: replay.selectedSectionIds.length === 0,
					selectorAllRejectedPython: replay.selectorAllRejected,
					contextMode: contextBuilder.contextMeta?.contextMode ?? null,
					contextTokenCount: contextBuilder.contextMeta?.tokenCount ?? null,
					selectedSectionsCount: replay.selectedSectionIds.length,
					contextItemsCount: contextBuilder.contextItems.length,
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

			const candidateSectionIds = result.result.contextItems
				.map((item) => item.sectionId ?? "")
				.filter((sectionId) => sectionId.length > 0);

			const pyRow = baselineLiveById.get(fixture.id);
			const pythonSectionIds =
				pyRow?.python?.metadata?.context_items_ref
					?.map((item) => String(item.section_id ?? "").trim())
					.filter((sectionId) => sectionId.length > 0) ?? [];

			details.push({
				id: fixture.id,
				overlapTopK: topKJaccard(pythonSectionIds, candidateSectionIds, topK),
				pythonTopContextSectionIds: pythonSectionIds.slice(0, topK),
				candidateTopContextSectionIds: candidateSectionIds.slice(0, topK),
				branchPath: result.result.branchPath,
				shortCircuit: result.result.shortCircuit,
				selectorAllRejectedPython:
					typeof pyRow?.python?.metadata?.selector_all_rejected === "boolean"
						? pyRow.python.metadata.selector_all_rejected
						: null,
				contextMode: result.result.contextMeta?.contextMode ?? null,
				contextTokenCount: result.result.contextMeta?.tokenCount ?? null,
				selectedSectionsCount: result.result.selectedSections.length,
				contextItemsCount: result.result.contextItems.length,
			});
		} catch (error) {
			const message = error instanceof Error ? error.message : String(error);
			errors.push({ id: fixture.id, error: message });
		}
	}

	const overlaps = details
		.map((detail) => detail.overlapTopK)
		.filter((value): value is number => value !== null);

	const contextOverlapTopKAvg =
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
			contextOverlapTopKAvg,
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
