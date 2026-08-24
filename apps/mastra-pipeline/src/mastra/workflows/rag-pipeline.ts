import { createStep, createWorkflow } from "@mastra/core/workflows";
import { z } from "zod";
import { DEFAULT_RUNTIME_RAG_CONFIG, getRuntimeRagConfig } from "../lib/config";
import {
	contextBuilderStateSchema,
	contextBuilderStepInputSchema,
	contextBuilderStepOutputSchema,
	runContextBuilder,
} from "../steps/context-builder";
import {
	contextSelectorStepInputSchema,
	contextSelectorStepOutputSchema,
	runContextSelector,
} from "../steps/context-selector";
import {
	generationConfigSchema,
	generatorStepInputSchema,
	generatorStepOutputSchema,
	runGenerator,
} from "../steps/generator";
import { queryProcessorStep, queryProcessorStepOutputSchema } from "../steps/query-processor";
import {
	retrievedChunkSchema,
	retrieverStepInputSchema,
	retrieverStepOutputSchema,
	runRetriever,
	type SearchMode,
} from "../steps/retriever";
import {
	aggregatedSectionSchema,
	runSectionAggregator,
	sectionAggregatorStepInputSchema,
	sectionAggregatorStepOutputSchema,
} from "../steps/section-aggregator";

const timingSchema = z.record(z.string(), z.number());
const metadataSchema = z.record(z.string(), z.unknown());

export const ragPipelineWorkflowOutputSchema = queryProcessorStepOutputSchema.extend({
	branchPath: z.enum(["rag", "non_rag"]),
	answer: z.string(),
	chunks: z.array(retrievedChunkSchema),
	retrievalMeta: retrieverStepOutputSchema.shape.retrievalMeta.nullable(),
	sections: z.array(aggregatedSectionSchema),
	aggregationMeta: sectionAggregatorStepOutputSchema.shape.aggregationMeta.nullable(),
	selectedSections: z.array(aggregatedSectionSchema),
	selectorMeta: contextSelectorStepOutputSchema.shape.selectorMeta.nullable(),
	shortCircuit: z.boolean(),
	shortCircuitMessage: z.string().nullable(),
	contextItems: contextBuilderStepOutputSchema.shape.contextItems,
	context: z.string(),
	contextMeta: contextBuilderStepOutputSchema.shape.contextMeta.nullable(),
	generationMeta: generatorStepOutputSchema.shape.generationMeta.nullable(),
	timing: timingSchema,
	metadata: metadataSchema,
});

const ragPipelineStateSchema = contextBuilderStateSchema.extend({
	config: z
		.object({
			retrieval: retrieverStepInputSchema.shape.config.optional(),
			aggregation: sectionAggregatorStepInputSchema.shape.config.optional(),
			selector: contextSelectorStepInputSchema.shape.config.optional(),
			context: contextBuilderStepInputSchema.shape.config.optional(),
			generation: generationConfigSchema.optional(),
		})
		.passthrough()
		.optional(),
	generator: generatorStepOutputSchema.optional(),
});

function estimateTokens(text: string): number {
	return Math.floor((text ?? "").length / 4);
}

function toRetrievedChunkRefs(chunks: z.infer<typeof retrievedChunkSchema>[]) {
	return chunks.map((chunk) => ({
		chunk_id: chunk.chunkId,
		table: chunk.tableSource,
		score: Number(chunk.score.toFixed(4)),
		section_id: chunk.sectionId ?? "",
	}));
}

function toAggregatedSectionRefs(sections: z.infer<typeof aggregatedSectionSchema>[]) {
	return sections.map((section) => ({
		section_id: section.sectionId ?? "",
		heading: (section.heading ?? "").slice(0, 80),
		score: Number(section.score.toFixed(4)),
		publisher: section.publisher ?? "",
		chunk_count: section.chunks.length,
	}));
}

function toContextItemRefs(
	contextItems: z.infer<typeof contextBuilderStepOutputSchema.shape.contextItems>,
) {
	return contextItems.map((item) => ({
		section_id: item.sectionId ?? "",
		doc_id: String(item.metadata.doc_id ?? ""),
		heading: (item.heading ?? "").slice(0, 80),
		publisher: item.publisher ?? "",
		tokens: item.tokenEstimate,
		score: Number(item.score.toFixed(4)),
		is_doc_entire: Boolean(item.metadata.is_doc_entire),
	}));
}

type AttemptName = "initial" | "selector_retry";

interface AttemptMetadata {
	name: AttemptName;
	search_mode: SearchMode;
	top_k: number;
	chunk_count: number;
	section_count: number;
	selected_section_count: number;
	selector_all_rejected: boolean;
}

function buildSharedMetadata(args: {
	intent: string;
	intentConfidence: number;
	theme: string | null;
	wasExpanded: boolean;
	expandedAcronyms: string[];
	queryForRetrieval: string;
	needsLegalSearch: boolean;
	tablesSearched: string[];
	selectorMeta: z.infer<typeof contextSelectorStepOutputSchema.shape.selectorMeta> | null;
	retrievedChunks: ReturnType<typeof toRetrievedChunkRefs>;
	aggregatedSections: ReturnType<typeof toAggregatedSectionRefs>;
	contextItemsRef: ReturnType<typeof toContextItemRefs>;
	generationMeta: z.infer<typeof generatorStepOutputSchema.shape.generationMeta> | null;
	selectorRetryEnabled?: boolean;
	selectorRetryTriggered?: boolean;
	selectorRetrySucceeded?: boolean;
	selectedAttemptName?: AttemptName;
	retrievalAttempts?: AttemptMetadata[];
}) {
	return {
		intent: args.intent,
		intent_confidence: args.intentConfidence,
		theme: args.theme,
		was_expanded: args.wasExpanded,
		expanded_acronyms: args.expandedAcronyms,
		query_for_retrieval: args.queryForRetrieval,
		needs_legal_search: args.needsLegalSearch,
		tables_searched: args.tablesSearched,
		selector_enabled: args.selectorMeta?.enabled ?? false,
		generator_model: args.generationMeta?.modelUsed ?? null,
		generator_provider: args.generationMeta?.providerUsed ?? null,
		retrieved_chunks: args.retrievedChunks,
		aggregated_sections: args.aggregatedSections,
		context_items_ref: args.contextItemsRef,
		selector_decisions: args.selectorMeta?.kept ?? [],
		selector_reasoning: args.selectorMeta?.reason ?? "",
		selector_raw_response: args.selectorMeta?.rawResponse ?? "",
		selector_items_before:
			(args.selectorMeta?.selectedCount ?? 0) + (args.selectorMeta?.removedCount ?? 0),
		selector_items_after: args.selectorMeta?.selectedCount ?? 0,
		selector_all_rejected: args.selectorMeta?.allRejected ?? false,
		selector_retry_enabled: args.selectorRetryEnabled ?? false,
		selector_retry_triggered: args.selectorRetryTriggered ?? false,
		selector_retry_succeeded: args.selectorRetrySucceeded ?? false,
		selected_retrieval_attempt: args.selectedAttemptName ?? "initial",
		retrieval_attempts: args.retrievalAttempts ?? [],
	};
}

type RagPipelineInputData = z.infer<typeof queryProcessorStepOutputSchema>;
type RagPipelineState = z.infer<typeof ragPipelineStateSchema>;
type RagPipelineOutput = z.infer<typeof ragPipelineWorkflowOutputSchema>;

type RuntimeConfig = Awaited<ReturnType<typeof getRuntimeRagConfig>>;

interface RagPipelineAttemptResult {
	name: AttemptName;
	searchMode: SearchMode;
	topK: number;
	retrieverResult: z.infer<typeof retrieverStepOutputSchema>;
	sectionAggregationResult: z.infer<typeof sectionAggregatorStepOutputSchema>;
	contextSelectorResult: z.infer<typeof contextSelectorStepOutputSchema>;
}

export interface RagPipelineExecutionDependencies {
	getRuntimeRagConfig: typeof getRuntimeRagConfig;
	runRetriever: typeof runRetriever;
	runSectionAggregator: typeof runSectionAggregator;
	runContextSelector: typeof runContextSelector;
	runContextBuilder: typeof runContextBuilder;
	runGenerator: typeof runGenerator;
}

const DEFAULT_RAG_PIPELINE_EXECUTION_DEPENDENCIES: RagPipelineExecutionDependencies = {
	getRuntimeRagConfig,
	runRetriever,
	runSectionAggregator,
	runContextSelector,
	runContextBuilder,
	runGenerator,
};

async function loadRuntimeRagConfigSafe(
	dependencies: RagPipelineExecutionDependencies,
): Promise<RuntimeConfig> {
	try {
		return await dependencies.getRuntimeRagConfig();
	} catch {
		return DEFAULT_RUNTIME_RAG_CONFIG;
	}
}

function addTiming(timings: Record<string, number>, key: string, elapsedMs: number): void {
	timings[key] = (timings[key] ?? 0) + elapsedMs;
}

function toAttemptMetadata(attempt: RagPipelineAttemptResult): AttemptMetadata {
	return {
		name: attempt.name,
		search_mode: attempt.searchMode,
		top_k: attempt.topK,
		chunk_count: attempt.retrieverResult.chunks.length,
		section_count: attempt.sectionAggregationResult.sections.length,
		selected_section_count: attempt.contextSelectorResult.sections.length,
		selector_all_rejected: attempt.contextSelectorResult.selectorMeta.allRejected,
	};
}

const NO_CONTEXT_AFTER_SELECTION_MESSAGE =
	"Je n'ai pas trouvé d'informations suffisamment pertinentes dans ma base de connaissances " +
	"pour répondre à cette question. N'hésitez pas à reformuler votre question ou à contacter " +
	"votre service RH pour obtenir une réponse précise.";

function resolveEffectiveRuntimeConfig(args: {
	runtimeConfig: RuntimeConfig;
	stateConfig: RagPipelineState["config"];
}) {
	const runtimeRetrievalConfig = retrieverStepInputSchema.shape.config.safeParse(
		args.runtimeConfig.retrieval,
	);
	const rawRetrievalConfig = {
		...args.runtimeConfig.retrieval,
		...(args.stateConfig?.retrieval ?? {}),
	};
	const retrievalConfig = retrieverStepInputSchema.shape.config.safeParse(rawRetrievalConfig);
	const baseRetrievalConfig = retrievalConfig.success
		? retrievalConfig.data
		: runtimeRetrievalConfig.success
			? runtimeRetrievalConfig.data
			: DEFAULT_RUNTIME_RAG_CONFIG.retrieval;

	const runtimeAggregationConfig = sectionAggregatorStepInputSchema.shape.config.safeParse(
		args.runtimeConfig.aggregation,
	);
	const rawAggregationConfig = {
		...args.runtimeConfig.aggregation,
		...(args.stateConfig?.aggregation ?? {}),
	};
	const aggregationConfig =
		sectionAggregatorStepInputSchema.shape.config.safeParse(rawAggregationConfig);

	const runtimeSelectorConfig = contextSelectorStepInputSchema.shape.config.safeParse(
		args.runtimeConfig.selector,
	);
	const rawSelectorConfig = {
		...args.runtimeConfig.selector,
		...(args.stateConfig?.selector ?? {}),
	};
	const selectorConfig = contextSelectorStepInputSchema.shape.config.safeParse(rawSelectorConfig);

	const runtimeContextBuildConfig = contextBuilderStepInputSchema.shape.config.safeParse(
		args.runtimeConfig.context,
	);
	const rawContextConfig = {
		...args.runtimeConfig.context,
		...(args.stateConfig?.context ?? {}),
	};
	const contextBuildConfig = contextBuilderStepInputSchema.shape.config.safeParse(rawContextConfig);

	const runtimeGenerationConfig = generatorStepInputSchema.shape.config.safeParse(
		args.runtimeConfig.generation,
	);
	const rawGenerationConfig = {
		...args.runtimeConfig.generation,
		...(args.stateConfig?.generation ?? {}),
	};
	const generationConfig = generatorStepInputSchema.shape.config.safeParse(rawGenerationConfig);

	return {
		baseRetrievalConfig: baseRetrievalConfig ?? DEFAULT_RUNTIME_RAG_CONFIG.retrieval,
		aggregationConfig: aggregationConfig.success
			? aggregationConfig.data
			: runtimeAggregationConfig.success
				? runtimeAggregationConfig.data
				: DEFAULT_RUNTIME_RAG_CONFIG.aggregation,
		selectorConfig: selectorConfig.success
			? selectorConfig.data
			: runtimeSelectorConfig.success
				? runtimeSelectorConfig.data
				: DEFAULT_RUNTIME_RAG_CONFIG.selector,
		contextBuildConfig: contextBuildConfig.success
			? contextBuildConfig.data
			: runtimeContextBuildConfig.success
				? runtimeContextBuildConfig.data
				: DEFAULT_RUNTIME_RAG_CONFIG.context,
		generationConfig: generationConfig.success
			? generationConfig.data
			: runtimeGenerationConfig.success
				? runtimeGenerationConfig.data
				: DEFAULT_RUNTIME_RAG_CONFIG.generation,
	};
}

export async function runRagPipelineRagBranch(args: {
	inputData: RagPipelineInputData;
	state: RagPipelineState;
	setState: (state: RagPipelineState) => Promise<void>;
	dependencies?: Partial<RagPipelineExecutionDependencies>;
}): Promise<RagPipelineOutput> {
	const dependencies: RagPipelineExecutionDependencies = {
		...DEFAULT_RAG_PIPELINE_EXECUTION_DEPENDENCIES,
		...(args.dependencies ?? {}),
	};

	const pipelineStart = Date.now();
	const timings: Record<string, number> = {};
	const branchPath: "rag" = "rag";

	const runtimeConfig = await loadRuntimeRagConfigSafe(dependencies);
	const {
		baseRetrievalConfig,
		aggregationConfig,
		selectorConfig,
		contextBuildConfig,
		generationConfig,
	} = resolveEffectiveRuntimeConfig({
		runtimeConfig,
		stateConfig: args.state.config,
	});

	async function runAttempt({
		name,
		searchMode,
		topK,
	}: {
		name: AttemptName;
		searchMode: SearchMode;
		topK: number;
	}): Promise<RagPipelineAttemptResult> {
		const retrievalStart = Date.now();
		const retrieverResult = await dependencies.runRetriever({
			queryForRetrieval: args.inputData.queryForRetrieval,
			needsLegalSearch: args.inputData.needsLegalSearch,
			config: {
				...baseRetrievalConfig,
				search_mode: searchMode,
				initial_top_k: topK,
			},
		});
		addTiming(timings, "retrieval_ms", Date.now() - retrievalStart);

		const aggregationStart = Date.now();
		const sectionAggregationResult = await dependencies.runSectionAggregator({
			chunks: retrieverResult.chunks,
			queryForRetrieval: args.inputData.queryForRetrieval,
			reformulatedQuery: args.inputData.reformulatedQuery ?? undefined,
			config: aggregationConfig,
		});
		addTiming(timings, "aggregation_ms", Date.now() - aggregationStart);

		const selectorStart = Date.now();
		const contextSelectorResult = await dependencies.runContextSelector({
			queryForRetrieval: args.inputData.queryForRetrieval,
			sections: sectionAggregationResult.sections,
			config: selectorConfig,
		});
		addTiming(timings, "selector_ms", Date.now() - selectorStart);

		return {
			name,
			searchMode,
			topK,
			retrieverResult,
			sectionAggregationResult,
			contextSelectorResult,
		};
	}

	const initialSearchMode = baseRetrievalConfig.search_mode ?? "semantic";
	const initialTopK = baseRetrievalConfig.initial_top_k ?? 30;
	const selectorRetryEnabled = baseRetrievalConfig.enable_selector_retry ?? true;
	const selectorRetrySearchMode = baseRetrievalConfig.selector_retry_search_mode ?? "hybrid";
	const selectorRetryTopK = baseRetrievalConfig.selector_retry_top_k ?? 30;

	const initialAttempt = await runAttempt({
		name: "initial",
		searchMode: initialSearchMode,
		topK: initialTopK,
	});
	const attempts = [initialAttempt];

	let selectedAttempt = initialAttempt;
	let selectorRetryTriggered = false;
	let selectorRetrySucceeded = false;

	if (initialAttempt.contextSelectorResult.shortCircuit && selectorRetryEnabled) {
		selectorRetryTriggered = true;
		const retryAttempt = await runAttempt({
			name: "selector_retry",
			searchMode: selectorRetrySearchMode,
			topK: selectorRetryTopK,
		});
		attempts.push(retryAttempt);
		selectedAttempt = retryAttempt;
		selectorRetrySucceeded = !retryAttempt.contextSelectorResult.shortCircuit;
	}

	const retrievalAttempts = attempts.map(toAttemptMetadata);

	if (selectedAttempt.contextSelectorResult.shortCircuit) {
		const answer = selectedAttempt.contextSelectorResult.shortCircuitMessage ?? "";
		timings.context_build_ms = 0;
		timings.generation_ms = 0;
		timings.ttft_ms = 0;
		timings.chars_per_second = 0;
		timings.response_length_tokens = estimateTokens(answer);
		timings.pipeline_total_ms = Date.now() - pipelineStart;

		const metadata = buildSharedMetadata({
			intent: args.inputData.intent,
			intentConfidence: args.inputData.intentConfidence,
			theme: args.inputData.theme,
			wasExpanded: args.inputData.wasExpanded,
			expandedAcronyms: args.inputData.expandedAcronyms,
			queryForRetrieval: args.inputData.queryForRetrieval,
			needsLegalSearch: args.inputData.needsLegalSearch,
			tablesSearched: selectedAttempt.retrieverResult.retrievalMeta.publishersSearched,
			selectorMeta: selectedAttempt.contextSelectorResult.selectorMeta,
			retrievedChunks: toRetrievedChunkRefs(selectedAttempt.retrieverResult.chunks),
			aggregatedSections: toAggregatedSectionRefs(
				selectedAttempt.sectionAggregationResult.sections,
			),
			contextItemsRef: [],
			generationMeta: null,
			selectorRetryEnabled,
			selectorRetryTriggered,
			selectorRetrySucceeded,
			selectedAttemptName: selectedAttempt.name,
			retrievalAttempts,
		});

		await args.setState({
			...args.state,
			branchPath,
			retriever: selectedAttempt.retrieverResult,
			sectionAggregator: selectedAttempt.sectionAggregationResult,
			contextSelector: selectedAttempt.contextSelectorResult,
			contextBuilder: undefined,
			generator: undefined,
		});

		return {
			...args.inputData,
			branchPath,
			answer,
			chunks: selectedAttempt.retrieverResult.chunks,
			retrievalMeta: selectedAttempt.retrieverResult.retrievalMeta,
			sections: selectedAttempt.sectionAggregationResult.sections,
			aggregationMeta: selectedAttempt.sectionAggregationResult.aggregationMeta,
			selectedSections: selectedAttempt.contextSelectorResult.sections,
			selectorMeta: selectedAttempt.contextSelectorResult.selectorMeta,
			shortCircuit: true,
			shortCircuitMessage: selectedAttempt.contextSelectorResult.shortCircuitMessage,
			contextItems: [],
			context: "",
			contextMeta: null,
			generationMeta: null,
			timing: timings,
			metadata,
		};
	}

	const contextBuildStart = Date.now();
	const contextBuilderResult = await dependencies.runContextBuilder({
		sections: selectedAttempt.contextSelectorResult.sections,
		config: contextBuildConfig,
	});
	timings.context_build_ms = Date.now() - contextBuildStart;

	if (contextBuilderResult.contextItems.length === 0) {
		const answer =
			initialAttempt.contextSelectorResult.shortCircuitMessage ??
			selectedAttempt.contextSelectorResult.shortCircuitMessage ??
			NO_CONTEXT_AFTER_SELECTION_MESSAGE;
		timings.generation_ms = 0;
		timings.ttft_ms = 0;
		timings.chars_per_second = 0;
		timings.response_length_tokens = estimateTokens(answer);
		timings.pipeline_total_ms = Date.now() - pipelineStart;

		const metadata = buildSharedMetadata({
			intent: args.inputData.intent,
			intentConfidence: args.inputData.intentConfidence,
			theme: args.inputData.theme,
			wasExpanded: args.inputData.wasExpanded,
			expandedAcronyms: args.inputData.expandedAcronyms,
			queryForRetrieval: args.inputData.queryForRetrieval,
			needsLegalSearch: args.inputData.needsLegalSearch,
			tablesSearched: selectedAttempt.retrieverResult.retrievalMeta.publishersSearched,
			selectorMeta: selectedAttempt.contextSelectorResult.selectorMeta,
			retrievedChunks: toRetrievedChunkRefs(selectedAttempt.retrieverResult.chunks),
			aggregatedSections: toAggregatedSectionRefs(
				selectedAttempt.sectionAggregationResult.sections,
			),
			contextItemsRef: [],
			generationMeta: null,
			selectorRetryEnabled,
			selectorRetryTriggered,
			selectorRetrySucceeded: false,
			selectedAttemptName: selectedAttempt.name,
			retrievalAttempts,
		});

		await args.setState({
			...args.state,
			branchPath,
			retriever: selectedAttempt.retrieverResult,
			sectionAggregator: selectedAttempt.sectionAggregationResult,
			contextSelector: selectedAttempt.contextSelectorResult,
			contextBuilder: contextBuilderResult,
			generator: undefined,
		});

		return {
			...args.inputData,
			branchPath,
			answer,
			chunks: selectedAttempt.retrieverResult.chunks,
			retrievalMeta: selectedAttempt.retrieverResult.retrievalMeta,
			sections: selectedAttempt.sectionAggregationResult.sections,
			aggregationMeta: selectedAttempt.sectionAggregationResult.aggregationMeta,
			selectedSections: selectedAttempt.contextSelectorResult.sections,
			selectorMeta: selectedAttempt.contextSelectorResult.selectorMeta,
			shortCircuit: true,
			shortCircuitMessage: answer,
			contextItems: [],
			context: "",
			contextMeta: contextBuilderResult.contextMeta,
			generationMeta: null,
			timing: timings,
			metadata,
		};
	}

	const generatorResult = await dependencies.runGenerator({
		queryForRetrieval: args.inputData.queryForRetrieval,
		context: contextBuilderResult.context,
		contextItems: contextBuilderResult.contextItems,
		conversationHistory: args.state.conversationHistory ?? [],
		config: generationConfig,
	});

	timings.generation_ms = generatorResult.generationMeta.generationMs;
	timings.ttft_ms = generatorResult.generationMeta.ttftMs;
	timings.chars_per_second = generatorResult.generationMeta.charsPerSecond;
	timings.response_length_tokens = generatorResult.generationMeta.responseLengthTokens;
	timings.pipeline_total_ms = Date.now() - pipelineStart;

	const metadata = buildSharedMetadata({
		intent: args.inputData.intent,
		intentConfidence: args.inputData.intentConfidence,
		theme: args.inputData.theme,
		wasExpanded: args.inputData.wasExpanded,
		expandedAcronyms: args.inputData.expandedAcronyms,
		queryForRetrieval: args.inputData.queryForRetrieval,
		needsLegalSearch: args.inputData.needsLegalSearch,
		tablesSearched: selectedAttempt.retrieverResult.retrievalMeta.publishersSearched,
		selectorMeta: selectedAttempt.contextSelectorResult.selectorMeta,
		retrievedChunks: toRetrievedChunkRefs(selectedAttempt.retrieverResult.chunks),
		aggregatedSections: toAggregatedSectionRefs(selectedAttempt.sectionAggregationResult.sections),
		contextItemsRef: toContextItemRefs(contextBuilderResult.contextItems),
		generationMeta: generatorResult.generationMeta,
		selectorRetryEnabled,
		selectorRetryTriggered,
		selectorRetrySucceeded,
		selectedAttemptName: selectedAttempt.name,
		retrievalAttempts,
	});

	await args.setState({
		...args.state,
		branchPath,
		retriever: selectedAttempt.retrieverResult,
		sectionAggregator: selectedAttempt.sectionAggregationResult,
		contextSelector: selectedAttempt.contextSelectorResult,
		contextBuilder: contextBuilderResult,
		generator: generatorResult,
	});

	return {
		...args.inputData,
		branchPath,
		answer: generatorResult.answer,
		chunks: selectedAttempt.retrieverResult.chunks,
		retrievalMeta: selectedAttempt.retrieverResult.retrievalMeta,
		sections: selectedAttempt.sectionAggregationResult.sections,
		aggregationMeta: selectedAttempt.sectionAggregationResult.aggregationMeta,
		selectedSections: selectedAttempt.contextSelectorResult.sections,
		selectorMeta: selectedAttempt.contextSelectorResult.selectorMeta,
		shortCircuit: false,
		shortCircuitMessage: null,
		contextItems: contextBuilderResult.contextItems,
		context: contextBuilderResult.context,
		contextMeta: contextBuilderResult.contextMeta,
		generationMeta: generatorResult.generationMeta,
		timing: timings,
		metadata,
	};
}

const ragPipelineStep = createStep({
	id: "rag-pipeline-rag-branch",
	description: "Run full RAG chain from retrieval to generation",
	inputSchema: queryProcessorStepOutputSchema,
	outputSchema: ragPipelineWorkflowOutputSchema,
	stateSchema: ragPipelineStateSchema,
	execute: async ({ inputData, state, setState }) =>
		runRagPipelineRagBranch({ inputData, state, setState }),
});

const nonRagShortCircuitStep = createStep({
	id: "rag-pipeline-non-rag-short-circuit",
	description: "Short-circuit full RAG workflow for non-RAG intents",
	inputSchema: queryProcessorStepOutputSchema,
	outputSchema: ragPipelineWorkflowOutputSchema,
	stateSchema: ragPipelineStateSchema,
	execute: async ({ inputData, state, setState }) => {
		const start = Date.now();
		const branchPath: "non_rag" = "non_rag";
		const answer = inputData.directResponse ?? "";

		const timings: Record<string, number> = {
			retrieval_ms: 0,
			aggregation_ms: 0,
			selector_ms: 0,
			context_build_ms: 0,
			generation_ms: 0,
			ttft_ms: 0,
			chars_per_second: 0,
			response_length_tokens: estimateTokens(answer),
			pipeline_total_ms: Date.now() - start,
		};

		const metadata = buildSharedMetadata({
			intent: inputData.intent,
			intentConfidence: inputData.intentConfidence,
			theme: inputData.theme,
			wasExpanded: inputData.wasExpanded,
			expandedAcronyms: inputData.expandedAcronyms,
			queryForRetrieval: inputData.queryForRetrieval,
			needsLegalSearch: inputData.needsLegalSearch,
			tablesSearched: [],
			selectorMeta: null,
			retrievedChunks: [],
			aggregatedSections: [],
			contextItemsRef: [],
			generationMeta: null,
		});

		await setState({
			...state,
			branchPath,
			retriever: undefined,
			sectionAggregator: undefined,
			contextSelector: undefined,
			contextBuilder: undefined,
			generator: undefined,
		});

		return {
			...inputData,
			branchPath,
			answer,
			chunks: [],
			retrievalMeta: null,
			sections: [],
			aggregationMeta: null,
			selectedSections: [],
			selectorMeta: null,
			shortCircuit: false,
			shortCircuitMessage: null,
			contextItems: [],
			context: "",
			contextMeta: null,
			generationMeta: null,
			timing: timings,
			metadata,
		};
	},
});

export const ragPipelineWorkflow = createWorkflow({
	id: "rag-pipeline-workflow",
	inputSchema: queryProcessorStep.inputSchema,
	outputSchema: ragPipelineWorkflowOutputSchema,
	stateSchema: ragPipelineStateSchema,
})
	.then(queryProcessorStep)
	.branch([
		[async ({ inputData }) => inputData.shouldProceed, ragPipelineStep],
		[async ({ inputData }) => !inputData.shouldProceed, nonRagShortCircuitStep],
	])
	.map(async ({ getStepResult }) => {
		const ragResult = getStepResult("rag-pipeline-rag-branch");
		if (ragResult) {
			return ragResult;
		}

		const nonRagResult = getStepResult("rag-pipeline-non-rag-short-circuit");
		if (nonRagResult) {
			return nonRagResult;
		}

		throw new Error("No rag-pipeline branch result available");
	})
	.commit();
