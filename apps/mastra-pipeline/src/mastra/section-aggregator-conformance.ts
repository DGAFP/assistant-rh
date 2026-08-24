import { spawnSync } from "node:child_process";
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { isAbsolute, join } from "node:path";
import { fileURLToPath } from "node:url";
import { config as loadDotenv } from "dotenv";
import { runQueryProcessor } from "./steps/query-processor";
import { runRetriever } from "./steps/retriever";
import { runSectionAggregator } from "./steps/section-aggregator";

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
			aggregated_sections?: Array<{ section_id?: string | null }>;
		};
	} | null;
}

interface QueryProcessorReplayStage {
	output: {
		should_proceed?: boolean;
	};
}

interface RetrieverReplayStage {
	output: {
		retrieved_chunks: Array<{
			chunk_id?: string | null;
			section_id?: string | null;
			score?: number | null;
			source_table?: string | null;
		}>;
	};
}

interface SectionAggregatorReplayStage {
	output: {
		aggregated_sections: Array<{ section_id?: string | null }>;
	};
}

interface ParseArgsResult {
	queriesFile: string;
	outputFile: string | null;
	topK: number;
	mode: ConformanceMode;
	baselineDir: string;
}

type SectionAggregatorChunk = Parameters<typeof runSectionAggregator>[0]["chunks"][number];
type SectionAggregatorConfig = NonNullable<Parameters<typeof runSectionAggregator>[0]["config"]>;

interface ReplayManifest {
	pipeline_config?: {
		aggregation?: SectionAggregatorConfig;
	};
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

function loadReplayAggregationConfig(baselineDir: string): SectionAggregatorConfig {
	const manifestPath = join(baselineDir, "manifest.json");
	if (!existsSync(manifestPath)) {
		throw new Error(`Missing replay baseline manifest: ${manifestPath}`);
	}

	const manifest = JSON.parse(readFileSync(manifestPath, "utf-8")) as ReplayManifest;
	const aggregationConfig = manifest.pipeline_config?.aggregation;
	if (!aggregationConfig) {
		throw new Error(`Missing pipeline_config.aggregation in replay manifest: ${manifestPath}`);
	}
	return aggregationConfig;
}

function normalizeSectionIds(rows: Array<{ section_id?: string | null }>): string[] {
	return rows
		.map((row) => String(row.section_id ?? "").trim())
		.filter((sectionId) => sectionId.length > 0);
}

function publisherKeyFromSource(sourceTable: string): string {
	const normalized = sourceTable.toLowerCase();
	if (normalized === "matte") {
		return "matte";
	}
	if (normalized === "service-public" || normalized === "service_public") {
		return "service_public";
	}
	if (normalized === "rgrh") {
		return "rgrh";
	}
	if (normalized === "dgafp") {
		return "dgafp";
	}
	return normalized.replace(/[^a-z0-9_]/g, "_");
}

function toReplayRetrievedChunks(
	rows: RetrieverReplayStage["output"]["retrieved_chunks"],
): SectionAggregatorChunk[] {
	return rows.map((row) => {
		const sourceTable = String(row.source_table ?? "unknown").trim() || "unknown";
		const chunkId = String(row.chunk_id ?? "").trim();
		const sectionId = String(row.section_id ?? "").trim();

		return {
			chunkId,
			text: "",
			score: typeof row.score === "number" ? row.score : 0,
			tableSource: sourceTable,
			publisher: sourceTable,
			publisherKey: publisherKeyFromSource(sourceTable),
			sectionId: sectionId.length > 0 ? sectionId : null,
			metadata: {
				source_name: sourceTable,
			},
			embeddingModelUsed: "albert" as const,
			retrievalMode: "semantic" as const,
			sourceIndex: "rag_chunks_albert" as const,
		};
	});
}

function loadReplayBaseline(
	fixtureId: string,
	baselineDir: string,
): {
	shouldProceed: boolean;
	chunks: ReturnType<typeof toReplayRetrievedChunks>;
	pythonSectionIds: string[];
} {
	const qpPath = join(baselineDir, fixtureId, "01_query_processor.json");
	const retrieverPath = join(baselineDir, fixtureId, "02_retriever.json");
	const sectionPath = join(baselineDir, fixtureId, "03_section_aggregator.json");

	for (const path of [qpPath, retrieverPath, sectionPath]) {
		if (!existsSync(path)) {
			throw new Error(`Missing replay baseline file: ${path}`);
		}
	}

	const qp = JSON.parse(readFileSync(qpPath, "utf-8")) as QueryProcessorReplayStage;
	const retriever = JSON.parse(readFileSync(retrieverPath, "utf-8")) as RetrieverReplayStage;
	const section = JSON.parse(readFileSync(sectionPath, "utf-8")) as SectionAggregatorReplayStage;

	return {
		shouldProceed: Boolean(qp.output.should_proceed),
		chunks: toReplayRetrievedChunks(retriever.output.retrieved_chunks),
		pythonSectionIds: normalizeSectionIds(section.output.aggregated_sections),
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
	const outputFile = join(
		tmpdir(),
		`mastra-conformance-live-section-aggregator-${Date.now()}.json`,
	);

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
	const replayAggregationConfig =
		mode === "replay" ? loadReplayAggregationConfig(baselineDir) : undefined;

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
		pythonTopSectionIds: string[];
		candidateTopSectionIds: string[];
		branchPath: "rag" | "non_rag";
		sectionCountBeforeRerank: number | null;
		sectionCountAfterRerank: number | null;
		rerankerApplied: boolean | null;
	}> = [];

	for (const fixture of fixtures) {
		try {
			let shouldProceed: boolean;
			let chunks: ReturnType<typeof toReplayRetrievedChunks>;
			let pythonSectionIds: string[];

			if (mode === "replay") {
				const replay = loadReplayBaseline(fixture.id, baselineDir);
				shouldProceed = replay.shouldProceed;
				chunks = replay.chunks;
				pythonSectionIds = replay.pythonSectionIds;
			} else {
				const qp = await runQueryProcessor({
					query: fixture.query,
					conversationHistory: fixture.conversation_history ?? [],
				});
				shouldProceed = qp.shouldProceed;

				if (shouldProceed) {
					const retriever = await runRetriever({
						queryForRetrieval: qp.queryForRetrieval,
						needsLegalSearch: qp.needsLegalSearch,
					});
					chunks = retriever.chunks as SectionAggregatorChunk[];
				} else {
					chunks = [];
				}

				const pySections = baselineLiveById.get(fixture.id)?.python?.metadata?.aggregated_sections;
				pythonSectionIds = pySections ? normalizeSectionIds(pySections) : [];
			}

			if (!shouldProceed) {
				details.push({
					id: fixture.id,
					overlapTopK: topKJaccard(pythonSectionIds, [], topK),
					pythonTopSectionIds: pythonSectionIds.slice(0, topK),
					candidateTopSectionIds: [],
					branchPath: "non_rag",
					sectionCountBeforeRerank: null,
					sectionCountAfterRerank: null,
					rerankerApplied: null,
				});
				continue;
			}

			const sectionAggregation = await runSectionAggregator({
				chunks,
				config: replayAggregationConfig,
			});

			const candidateSectionIds = sectionAggregation.sections
				.map((section) => section.sectionId ?? "")
				.filter((sectionId) => sectionId.length > 0);

			details.push({
				id: fixture.id,
				overlapTopK: topKJaccard(pythonSectionIds, candidateSectionIds, topK),
				pythonTopSectionIds: pythonSectionIds.slice(0, topK),
				candidateTopSectionIds: candidateSectionIds.slice(0, topK),
				branchPath: "rag",
				sectionCountBeforeRerank:
					sectionAggregation.aggregationMeta?.sectionCountBeforeRerank ?? null,
				sectionCountAfterRerank:
					sectionAggregation.aggregationMeta?.sectionCountAfterRerank ?? null,
				rerankerApplied: sectionAggregation.aggregationMeta?.rerankerApplied ?? null,
			});
		} catch (error) {
			const message = error instanceof Error ? error.message : String(error);
			errors.push({ id: fixture.id, error: message });
		}
	}

	const overlaps = details
		.map((detail) => detail.overlapTopK)
		.filter((value): value is number => value !== null);

	const sectionOverlapTopKAvg =
		overlaps.length === 0
			? null
			: overlaps.reduce((sum, value) => sum + value, 0) / overlaps.length;

	const report = {
		generatedAt: new Date().toISOString(),
		mode,
		baselineDir: mode === "replay" ? baselineDir : null,
		replayAggregationConfig: replayAggregationConfig ?? null,
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
			sectionOverlapTopKAvg,
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
