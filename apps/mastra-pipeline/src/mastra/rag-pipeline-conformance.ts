import { spawnSync } from "node:child_process";
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { isAbsolute, join } from "node:path";
import { fileURLToPath } from "node:url";
import { config as loadDotenv } from "dotenv";
import { ragPipelineWorkflow } from "./workflows/rag-pipeline";

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
		answer?: string;
		metadata?: {
			retrieved_chunks?: Array<{ chunk_id?: string | null }>;
			aggregated_sections?: Array<{ section_id?: string | null }>;
			context_items_ref?: Array<{ section_id?: string | null }>;
		};
	} | null;
}

interface ReplayInputStage {
	query: string;
	conversation_history?: Array<{
		role: "user" | "assistant" | "system";
		content: string;
	}>;
}

interface ReplayPipelineResultStage {
	answer?: string;
	metadata?: {
		retrieved_chunks?: Array<{ chunk_id?: string | null }>;
		aggregated_sections?: Array<{ section_id?: string | null }>;
		context_items_ref?: Array<{ section_id?: string | null }>;
	};
}

interface ParseArgsResult {
	queriesFile: string;
	outputFile: string | null;
	topK: number;
	mode: ConformanceMode;
	baselineDir: string;
}

interface ReplayBaseline {
	input: {
		query: string;
		conversationHistory: Array<{ role: "user" | "assistant" | "system"; content: string }>;
	};
	expected: {
		answer: string;
		retrievedChunkIds: string[];
		aggregatedSectionIds: string[];
		contextSectionIds: string[];
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

function normalizeChunkIds(items: Array<{ chunk_id?: string | null }>): string[] {
	return items
		.map((item) => String(item.chunk_id ?? "").trim())
		.filter((value) => value.length > 0);
}

function normalizeSectionIds(items: Array<{ section_id?: string | null }>): string[] {
	return items
		.map((item) => String(item.section_id ?? "").trim())
		.filter((value) => value.length > 0);
}

function loadReplayBaseline(fixtureId: string, baselineDir: string): ReplayBaseline {
	const inputPath = join(baselineDir, fixtureId, "00_input.json");
	const pipelinePath = join(baselineDir, fixtureId, "07_pipeline_result.json");

	if (!existsSync(inputPath)) {
		throw new Error(`Missing replay baseline file: ${inputPath}`);
	}
	if (!existsSync(pipelinePath)) {
		throw new Error(`Missing replay baseline file: ${pipelinePath}`);
	}

	const inputStage = JSON.parse(readFileSync(inputPath, "utf-8")) as ReplayInputStage;
	const pipelineStage = JSON.parse(
		readFileSync(pipelinePath, "utf-8"),
	) as ReplayPipelineResultStage;

	return {
		input: {
			query: inputStage.query,
			conversationHistory: inputStage.conversation_history ?? [],
		},
		expected: {
			answer: pipelineStage.answer ?? "",
			retrievedChunkIds: normalizeChunkIds(pipelineStage.metadata?.retrieved_chunks ?? []),
			aggregatedSectionIds: normalizeSectionIds(pipelineStage.metadata?.aggregated_sections ?? []),
			contextSectionIds: normalizeSectionIds(pipelineStage.metadata?.context_items_ref ?? []),
		},
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
	const outputFile = join(tmpdir(), `mastra-conformance-live-rag-pipeline-${Date.now()}.json`);

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

function normalizeTokens(text: string): string[] {
	return text
		.toLowerCase()
		.split(/\s+/)
		.map((token) => token.trim())
		.filter((token) => token.length > 0);
}

function tokenJaccard(left: string, right: string): number | null {
	const leftSet = new Set(normalizeTokens(left));
	const rightSet = new Set(normalizeTokens(right));

	if (leftSet.size === 0 && rightSet.size === 0) {
		return null;
	}

	const union = new Set([...leftSet, ...rightSet]);
	const intersection = Array.from(leftSet).filter((value) => rightSet.has(value));
	return intersection.length / union.size;
}

function averageNullable(values: Array<number | null>): number | null {
	const present = values.filter((value): value is number => value !== null);
	if (present.length === 0) {
		return null;
	}
	return present.reduce((sum, value) => sum + value, 0) / present.length;
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
		answerTokenJaccard: number | null;
		retrievalOverlapTopK: number | null;
		sectionOverlapTopK: number | null;
		contextOverlapTopK: number | null;
		pythonTopChunkIds: string[];
		candidateTopChunkIds: string[];
		pythonTopSectionIds: string[];
		candidateTopSectionIds: string[];
		pythonTopContextIds: string[];
		candidateTopContextIds: string[];
		branchPath: "rag" | "non_rag";
		shortCircuit: boolean;
		answerLength: number;
		pipelineTotalMs: number;
	}> = [];

	for (const fixture of fixtures) {
		try {
			const baseline =
				mode === "replay"
					? loadReplayBaseline(fixture.id, baselineDir)
					: {
							input: {
								query: fixture.query,
								conversationHistory: fixture.conversation_history ?? [],
							},
							expected: {
								answer: baselineLiveById.get(fixture.id)?.python?.answer ?? "",
								retrievedChunkIds: normalizeChunkIds(
									baselineLiveById.get(fixture.id)?.python?.metadata?.retrieved_chunks ?? [],
								),
								aggregatedSectionIds: normalizeSectionIds(
									baselineLiveById.get(fixture.id)?.python?.metadata?.aggregated_sections ?? [],
								),
								contextSectionIds: normalizeSectionIds(
									baselineLiveById.get(fixture.id)?.python?.metadata?.context_items_ref ?? [],
								),
							},
						};

			const run = await ragPipelineWorkflow.createRun();
			const startedAt = Date.now();
			const result = await run.start({
				inputData: {
					query: baseline.input.query,
					conversationHistory: baseline.input.conversationHistory,
				},
			});
			const elapsedMs = Date.now() - startedAt;

			if (result.status !== "success") {
				errors.push({
					id: fixture.id,
					error: `RAG pipeline workflow failed for ${fixture.id} with status ${result.status}`,
				});
				continue;
			}

			const candidateChunkIds = result.result.chunks
				.map((chunk) => chunk.chunkId)
				.filter((chunkId) => chunkId.length > 0);
			const candidateSectionIds = result.result.sections
				.map((section) => section.sectionId ?? "")
				.filter((sectionId) => sectionId.length > 0);
			const candidateContextIds = result.result.contextItems
				.map((item) => item.sectionId ?? "")
				.filter((sectionId) => sectionId.length > 0);

			details.push({
				id: fixture.id,
				answerTokenJaccard: tokenJaccard(baseline.expected.answer, result.result.answer),
				retrievalOverlapTopK: topKJaccard(
					baseline.expected.retrievedChunkIds,
					candidateChunkIds,
					topK,
				),
				sectionOverlapTopK: topKJaccard(
					baseline.expected.aggregatedSectionIds,
					candidateSectionIds,
					topK,
				),
				contextOverlapTopK: topKJaccard(
					baseline.expected.contextSectionIds,
					candidateContextIds,
					topK,
				),
				pythonTopChunkIds: baseline.expected.retrievedChunkIds.slice(0, topK),
				candidateTopChunkIds: candidateChunkIds.slice(0, topK),
				pythonTopSectionIds: baseline.expected.aggregatedSectionIds.slice(0, topK),
				candidateTopSectionIds: candidateSectionIds.slice(0, topK),
				pythonTopContextIds: baseline.expected.contextSectionIds.slice(0, topK),
				candidateTopContextIds: candidateContextIds.slice(0, topK),
				branchPath: result.result.branchPath,
				shortCircuit: result.result.shortCircuit,
				answerLength: result.result.answer.length,
				pipelineTotalMs: elapsedMs,
			});
		} catch (error) {
			const message = error instanceof Error ? error.message : String(error);
			errors.push({ id: fixture.id, error: message });
		}
	}

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
			answerTokenJaccardAvg: averageNullable(details.map((detail) => detail.answerTokenJaccard)),
			retrievalOverlapTopKAvg: averageNullable(
				details.map((detail) => detail.retrievalOverlapTopK),
			),
			sectionOverlapTopKAvg: averageNullable(details.map((detail) => detail.sectionOverlapTopK)),
			contextOverlapTopKAvg: averageNullable(details.map((detail) => detail.contextOverlapTopK)),
			pipelineTotalMsAvg:
				details.length === 0
					? null
					: details.reduce((sum, detail) => sum + detail.pipelineTotalMs, 0) / details.length,
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
