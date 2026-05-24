import { spawnSync } from "node:child_process";
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { isAbsolute, join } from "node:path";
import { fileURLToPath } from "node:url";
import { config as loadDotenv } from "dotenv";
import { runQueryProcessor } from "./steps/query-processor";
import { runRetriever } from "./steps/retriever";

type ConformanceMode = "replay" | "live";

interface QueryFixture {
	id: string;
	query: string;
	conversation_history?: Array<{ role: "user" | "assistant" | "system"; content: string }>;
}

interface BaselineRowLive {
	id: string;
	python?: {
		metadata?: {
			retrieved_chunks?: Array<{ chunk_id?: string | null }>;
		};
	} | null;
}

interface QueryProcessorReplayStage {
	output: {
		should_proceed?: boolean;
	};
}

interface RetrieverReplayStage {
	input: {
		query: string;
		needs_legal_search: boolean;
	};
	output: {
		retrieved_chunks: Array<{ chunk_id?: string | null }>;
	};
}

interface ReplayRetrieverBaseline {
	shouldProceed: boolean;
	input: {
		queryForRetrieval: string;
		needsLegalSearch: boolean;
	};
	expectedChunkIds: string[];
}

interface ParseArgsResult {
	queriesFile: string;
	outputFile: string | null;
	topK: number;
	mode: ConformanceMode;
	baselineDir: string;
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

function normalizeChunkIds(chunks: Array<{ chunk_id?: string | null }>): string[] {
	return chunks
		.map((chunk) => String(chunk.chunk_id ?? "").trim())
		.filter((chunkId) => chunkId.length > 0);
}

function loadReplayBaseline(fixtureId: string, baselineDir: string): ReplayRetrieverBaseline {
	const qpPath = join(baselineDir, fixtureId, "01_query_processor.json");
	const retrieverPath = join(baselineDir, fixtureId, "02_retriever.json");

	if (!existsSync(qpPath)) {
		throw new Error(`Missing replay baseline file: ${qpPath}`);
	}
	if (!existsSync(retrieverPath)) {
		throw new Error(`Missing replay baseline file: ${retrieverPath}`);
	}

	const qp = JSON.parse(readFileSync(qpPath, "utf-8")) as QueryProcessorReplayStage;
	const stage = JSON.parse(readFileSync(retrieverPath, "utf-8")) as RetrieverReplayStage;

	return {
		shouldProceed: Boolean(qp.output.should_proceed),
		input: {
			queryForRetrieval: stage.input.query,
			needsLegalSearch: stage.input.needs_legal_search,
		},
		expectedChunkIds: normalizeChunkIds(stage.output.retrieved_chunks),
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
	const outputFile = join(tmpdir(), `mastra-conformance-live-retriever-${Date.now()}.json`);

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
		pythonTopChunkIds: string[];
		candidateTopChunkIds: string[];
		branchPath: "rag" | "non_rag";
		retrievalModeByPublisher: Record<string, string>;
		chunkCount: number;
	}> = [];

	for (const fixture of fixtures) {
		try {
			let shouldProceed: boolean;
			let queryForRetrieval: string;
			let needsLegalSearch: boolean;
			let pythonChunkIds: string[];

			if (mode === "replay") {
				const replay = loadReplayBaseline(fixture.id, baselineDir);
				shouldProceed = replay.shouldProceed;
				queryForRetrieval = replay.input.queryForRetrieval;
				needsLegalSearch = replay.input.needsLegalSearch;
				pythonChunkIds = replay.expectedChunkIds;
			} else {
				const qp = await runQueryProcessor({
					query: fixture.query,
					conversationHistory: fixture.conversation_history ?? [],
				});

				shouldProceed = qp.shouldProceed;
				queryForRetrieval = qp.queryForRetrieval;
				needsLegalSearch = qp.needsLegalSearch;
				const pyChunks = baselineLiveById.get(fixture.id)?.python?.metadata?.retrieved_chunks;
				pythonChunkIds = pyChunks ? normalizeChunkIds(pyChunks) : [];
			}

			if (!shouldProceed) {
				details.push({
					id: fixture.id,
					overlapTopK: topKJaccard(pythonChunkIds, [], topK),
					pythonTopChunkIds: pythonChunkIds.slice(0, topK),
					candidateTopChunkIds: [],
					branchPath: "non_rag",
					retrievalModeByPublisher: {},
					chunkCount: 0,
				});
				continue;
			}

			const retrieverResult = await runRetriever({
				queryForRetrieval,
				needsLegalSearch,
			});

			const candidateChunkIds = retrieverResult.chunks.map((chunk) => chunk.chunkId);

			details.push({
				id: fixture.id,
				overlapTopK: topKJaccard(pythonChunkIds, candidateChunkIds, topK),
				pythonTopChunkIds: pythonChunkIds.slice(0, topK),
				candidateTopChunkIds: candidateChunkIds.slice(0, topK),
				branchPath: "rag",
				retrievalModeByPublisher: retrieverResult.retrievalMeta?.modeByPublisher ?? {},
				chunkCount: retrieverResult.chunks.length,
			});
		} catch (error) {
			const message = error instanceof Error ? error.message : String(error);
			errors.push({ id: fixture.id, error: message });
		}
	}

	const overlaps = details
		.map((detail) => detail.overlapTopK)
		.filter((value): value is number => value !== null);

	const retrievalOverlapTopKAvg =
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
			retrievalOverlapTopKAvg,
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
