import { spawnSync } from "node:child_process";
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { isAbsolute, join } from "node:path";
import { fileURLToPath } from "node:url";
import { config as loadDotenv } from "dotenv";
import { type LlmReplayMode, LlmReplayStore } from "./lib/llm-replay";
import { runQueryProcessor } from "./steps/query-processor";

type ConformanceMode = "replay" | "live";

interface QueryFixture {
	id: string;
	query: string;
	conversation_history?: Array<{
		role: "user" | "assistant" | "system";
		content: string;
	}>;
}

interface QueryProcessorStageFile {
	input: {
		query: string;
		conversation_history?: Array<{
			role: "user" | "assistant" | "system";
			content: string;
		}>;
	};
	output: {
		intent?: string | null;
		theme?: string | null;
		needs_legal_search?: boolean | null;
	};
}

interface BaselineRowLive {
	id: string;
	query: string;
	python?: {
		metadata?: {
			intent?: string | null;
			theme?: string | null;
			needs_legal_search?: boolean | null;
		};
	} | null;
}

interface BaselineReference {
	input: {
		query: string;
		conversationHistory: Array<{ role: "user" | "assistant" | "system"; content: string }>;
	};
	expected: {
		intent: string | null;
		theme: string | null;
		needsLegalSearch: boolean | null;
	};
}

interface ParseArgsResult {
	queriesFile: string;
	outputFile: string | null;
	mode: ConformanceMode;
	baselineDir: string;
	llmReplayMode: LlmReplayMode;
	llmReplayCacheFile: string;
}

function parseArgs(argv: string[]): ParseArgsResult {
	const appRoot = fileURLToPath(new URL("../..", import.meta.url));
	const workspaceRoot = fileURLToPath(new URL("../../../..", import.meta.url));

	let queriesFile = join(workspaceRoot, "tests/conformance/queries.sample.jsonl");
	let outputFile: string | null = null;
	let mode: ConformanceMode = "replay";
	let baselineDir = join(workspaceRoot, "tests/conformance/baselines/queries-sample");
	let llmReplayMode: LlmReplayMode | null = null;
	let llmReplayCacheFile = join(
		workspaceRoot,
		"tests/conformance/replay-cache/query-processor.intent.v1.json",
	);

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

		if (arg === "--llm-replay-mode") {
			const value = argv[i + 1];
			if (!value || (value !== "off" && value !== "replay" && value !== "record")) {
				throw new Error("Invalid value for --llm-replay-mode (expected off|replay|record)");
			}
			llmReplayMode = value;
			i += 1;
			continue;
		}

		if (arg === "--llm-replay-cache-file") {
			const value = argv[i + 1];
			if (!value) {
				throw new Error("Missing value for --llm-replay-cache-file");
			}
			llmReplayCacheFile = value;
			i += 1;
			continue;
		}

		// Backward compatibility with old scripts.
		if (arg === "--skip-python-baseline") {
			mode = "replay";
		}
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

	if (!isAbsolute(llmReplayCacheFile)) {
		llmReplayCacheFile = join(workspaceRoot, llmReplayCacheFile);
	}

	loadDotenv({ path: join(workspaceRoot, ".env"), quiet: true });
	loadDotenv({ path: join(appRoot, ".env"), quiet: true, override: true });

	return {
		queriesFile,
		outputFile,
		mode,
		baselineDir,
		llmReplayMode: llmReplayMode ?? (mode === "replay" ? "replay" : "off"),
		llmReplayCacheFile,
	};
}

function readJsonlFixtures(path: string): QueryFixture[] {
	const content = readFileSync(path, "utf-8");
	return content
		.split("\n")
		.map((line) => line.trim())
		.filter((line) => line.length > 0)
		.map((line) => JSON.parse(line) as QueryFixture);
}

function loadReplayBaseline(fixtureId: string, baselineDir: string): BaselineReference {
	const stagePath = join(baselineDir, fixtureId, "01_query_processor.json");
	if (!existsSync(stagePath)) {
		throw new Error(`Missing replay baseline file: ${stagePath}`);
	}

	const stage = JSON.parse(readFileSync(stagePath, "utf-8")) as QueryProcessorStageFile;
	return {
		input: {
			query: stage.input.query,
			conversationHistory: stage.input.conversation_history ?? [],
		},
		expected: {
			intent: stage.output.intent ?? null,
			theme: stage.output.theme ?? null,
			needsLegalSearch:
				typeof stage.output.needs_legal_search === "boolean"
					? stage.output.needs_legal_search
					: null,
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
	stdout: string;
	stderr: string;
	errorCount: number;
} {
	const outputFile = join(tmpdir(), `mastra-conformance-live-qp-${Date.now()}.json`);

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
			`Python baseline output was not created (status ${run.status ?? -1}).\nSTDOUT:\n${run.stdout}\nSTDERR:\n${run.stderr}`,
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
		stdout: run.stdout ?? "",
		stderr: run.stderr ?? "",
		errorCount: Array.isArray(report.errors) ? report.errors.length : 0,
	};
}

async function main(): Promise<void> {
	const { queriesFile, outputFile, mode, baselineDir, llmReplayMode, llmReplayCacheFile } =
		parseArgs(process.argv.slice(2));
	const workspaceRoot = fileURLToPath(new URL("../../../..", import.meta.url));
	const fixtures = readJsonlFixtures(queriesFile);
	const llmReplayStore = llmReplayMode === "off" ? null : new LlmReplayStore(llmReplayCacheFile);
	const llmReplayStats = {
		hits: 0,
		misses: 0,
		recorded: 0,
	};

	const baselineLive =
		mode === "live"
			? runPythonBaseline(workspaceRoot, queriesFile)
			: {
					rows: [] as BaselineRowLive[],
					status: 0,
					outputFile: null,
					stdout: "",
					stderr: "",
					errorCount: 0,
				};

	const baselineLiveById = new Map(baselineLive.rows.map((row) => [row.id, row]));

	const details: Array<{
		id: string;
		intent: { python: string | null; candidate: string | null; match: boolean | null };
		theme: { python: string | null; candidate: string | null; match: boolean | null };
		needsLegalSearch: { python: boolean | null; candidate: boolean | null; match: boolean | null };
		branchPath: string;
		shouldProceed: boolean;
	}> = [];

	let intentTotal = 0;
	let intentMatched = 0;
	let themeTotal = 0;
	let themeMatched = 0;
	let legalTotal = 0;
	let legalMatched = 0;

	const workflowErrors: Array<{ id: string; error: string }> = [];

	for (const fixture of fixtures) {
		try {
			const replayBaseline = mode === "replay" ? loadReplayBaseline(fixture.id, baselineDir) : null;
			const input = replayBaseline
				? replayBaseline.input
				: {
						query: fixture.query,
						conversationHistory: fixture.conversation_history ?? [],
					};

			const llmReplayRequest = {
				stage: "query-processor.intent",
				payload: {
					fixtureId: fixture.id,
					query: input.query,
					conversationHistory: input.conversationHistory,
				},
			};

			let result: Awaited<ReturnType<typeof runQueryProcessor>>;
			if (llmReplayMode === "replay") {
				const replayEntry = llmReplayStore?.get(llmReplayRequest);
				if (!replayEntry) {
					llmReplayStats.misses += 1;
					throw new Error(
						`Missing LLM replay cache entry for fixture ${fixture.id} in ${llmReplayCacheFile}`,
					);
				}

				llmReplayStats.hits += 1;
				result = await runQueryProcessor(
					{
						query: input.query,
						conversationHistory: input.conversationHistory,
					},
					undefined,
					{
						forcedIntentRawResponse: replayEntry.response,
					},
				);
			} else {
				result = await runQueryProcessor({
					query: input.query,
					conversationHistory: input.conversationHistory,
				});

				if (llmReplayMode === "record") {
					if (!result.intentRawResponse || result.intentRawResponse.trim().length === 0) {
						throw new Error(
							`Cannot record replay cache: empty intentRawResponse for fixture ${fixture.id}`,
						);
					}

					llmReplayStore?.upsert(llmReplayRequest, result.intentRawResponse, {
						providerUsed: result.providerUsed,
						promptNameUsed: result.promptNameUsed,
						intent: result.intent,
						theme: result.theme,
						needsLegalSearch: result.needsLegalSearch,
					});
					llmReplayStats.recorded += 1;
				}
			}

			const pyMetadata = baselineLiveById.get(fixture.id)?.python?.metadata;
			const expected = replayBaseline?.expected ?? {
				intent: pyMetadata?.intent ?? null,
				theme: pyMetadata?.theme ?? null,
				needsLegalSearch:
					typeof pyMetadata?.needs_legal_search === "boolean"
						? pyMetadata.needs_legal_search
						: null,
			};

			const intentMatch = expected.intent === null ? null : expected.intent === result.intent;
			if (intentMatch !== null) {
				intentTotal += 1;
				if (intentMatch) {
					intentMatched += 1;
				}
			}

			const themeMatch = expected.theme === null ? null : expected.theme === result.theme;
			if (themeMatch !== null) {
				themeTotal += 1;
				if (themeMatch) {
					themeMatched += 1;
				}
			}

			const legalMatch =
				expected.needsLegalSearch === null
					? null
					: expected.needsLegalSearch === result.needsLegalSearch;

			if (legalMatch !== null) {
				legalTotal += 1;
				if (legalMatch) {
					legalMatched += 1;
				}
			}

			details.push({
				id: fixture.id,
				intent: {
					python: expected.intent,
					candidate: result.intent,
					match: intentMatch,
				},
				theme: {
					python: expected.theme,
					candidate: result.theme,
					match: themeMatch,
				},
				needsLegalSearch: {
					python: expected.needsLegalSearch,
					candidate: result.needsLegalSearch,
					match: legalMatch,
				},
				branchPath: result.shouldProceed ? "rag" : "non_rag",
				shouldProceed: result.shouldProceed,
			});
		} catch (error) {
			const message = error instanceof Error ? error.message : String(error);
			workflowErrors.push({ id: fixture.id, error: message });
		}
	}

	llmReplayStore?.saveIfDirty();

	const report = {
		generatedAt: new Date().toISOString(),
		mode,
		baselineDir: mode === "replay" ? baselineDir : null,
		queriesFile,
		queryCount: fixtures.length,
		succeededCount: details.length,
		failedCount: workflowErrors.length,
		pythonBaseline: {
			skipped: mode === "replay",
			status: baselineLive.status,
			outputFile: baselineLive.outputFile,
			errorCount: baselineLive.errorCount,
			stderrPreview: baselineLive.stderr.slice(0, 2000),
		},
		llmReplay: {
			mode: llmReplayMode,
			cacheFile: llmReplayMode === "off" ? null : llmReplayCacheFile,
			hits: llmReplayStats.hits,
			misses: llmReplayStats.misses,
			recorded: llmReplayStats.recorded,
			entryCount: llmReplayStore?.entryCount ?? null,
		},
		summary: {
			intentMatchRate: intentTotal === 0 ? null : intentMatched / intentTotal,
			themeMatchRate: themeTotal === 0 ? null : themeMatched / themeTotal,
			needsLegalSearchMatchRate: legalTotal === 0 ? null : legalMatched / legalTotal,
		},
		details,
		errors: workflowErrors,
	};

	const serialized = JSON.stringify(report, null, 2);
	if (outputFile) {
		writeFileSync(outputFile, serialized, "utf-8");
	}

	console.log(serialized);
}

void main();
