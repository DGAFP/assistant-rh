import { createStep, createWorkflow } from "@mastra/core/workflows";
import { z } from "zod";
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
	};
}

const ragPipelineStep = createStep({
	id: "rag-pipeline-rag-branch",
	description: "Run full RAG chain from retrieval to generation",
	inputSchema: queryProcessorStepOutputSchema,
	outputSchema: ragPipelineWorkflowOutputSchema,
	stateSchema: ragPipelineStateSchema,
	execute: async ({ inputData, state, setState }) => {
		const pipelineStart = Date.now();
		const timings: Record<string, number> = {};

		const stateConfig = state.config;

		const retrievalConfig = retrieverStepInputSchema.shape.config.safeParse(stateConfig?.retrieval);

		const retrievalStart = Date.now();
		const retrieverResult = await runRetriever({
			queryForRetrieval: inputData.queryForRetrieval,
			needsLegalSearch: inputData.needsLegalSearch,
			config: retrievalConfig.success ? retrievalConfig.data : undefined,
		});
		timings.retrieval_ms = Date.now() - retrievalStart;

		const aggregationConfig = sectionAggregatorStepInputSchema.shape.config.safeParse(
			stateConfig?.aggregation,
		);

		const aggregationStart = Date.now();
		const sectionAggregationResult = await runSectionAggregator({
			chunks: retrieverResult.chunks,
			queryForRetrieval: inputData.queryForRetrieval,
			reformulatedQuery: inputData.reformulatedQuery ?? undefined,
			config: aggregationConfig.success ? aggregationConfig.data : undefined,
		});
		timings.aggregation_ms = Date.now() - aggregationStart;

		const selectorConfig = contextSelectorStepInputSchema.shape.config.safeParse(
			stateConfig?.selector,
		);

		const selectorStart = Date.now();
		const contextSelectorResult = await runContextSelector({
			queryForRetrieval: inputData.queryForRetrieval,
			sections: sectionAggregationResult.sections,
			config: selectorConfig.success ? selectorConfig.data : undefined,
		});
		timings.selector_ms = Date.now() - selectorStart;

		const branchPath: "rag" = "rag";

		if (contextSelectorResult.shortCircuit) {
			const answer = contextSelectorResult.shortCircuitMessage ?? "";
			timings.context_build_ms = 0;
			timings.generation_ms = 0;
			timings.ttft_ms = 0;
			timings.chars_per_second = 0;
			timings.response_length_tokens = estimateTokens(answer);
			timings.pipeline_total_ms = Date.now() - pipelineStart;

			const metadata = buildSharedMetadata({
				intent: inputData.intent,
				intentConfidence: inputData.intentConfidence,
				theme: inputData.theme,
				wasExpanded: inputData.wasExpanded,
				expandedAcronyms: inputData.expandedAcronyms,
				queryForRetrieval: inputData.queryForRetrieval,
				needsLegalSearch: inputData.needsLegalSearch,
				tablesSearched: retrieverResult.retrievalMeta.publishersSearched,
				selectorMeta: contextSelectorResult.selectorMeta,
				retrievedChunks: toRetrievedChunkRefs(retrieverResult.chunks),
				aggregatedSections: toAggregatedSectionRefs(sectionAggregationResult.sections),
				contextItemsRef: [],
				generationMeta: null,
			});

			await setState({
				...state,
				branchPath,
				retriever: retrieverResult,
				sectionAggregator: sectionAggregationResult,
				contextSelector: contextSelectorResult,
				contextBuilder: undefined,
				generator: undefined,
			});

			return {
				...inputData,
				branchPath,
				answer,
				chunks: retrieverResult.chunks,
				retrievalMeta: retrieverResult.retrievalMeta,
				sections: sectionAggregationResult.sections,
				aggregationMeta: sectionAggregationResult.aggregationMeta,
				selectedSections: contextSelectorResult.sections,
				selectorMeta: contextSelectorResult.selectorMeta,
				shortCircuit: true,
				shortCircuitMessage: contextSelectorResult.shortCircuitMessage,
				contextItems: [],
				context: "",
				contextMeta: null,
				generationMeta: null,
				timing: timings,
				metadata,
			};
		}

		const contextBuildConfig = contextBuilderStepInputSchema.shape.config.safeParse(
			stateConfig?.context,
		);

		const contextBuildStart = Date.now();
		const contextBuilderResult = await runContextBuilder({
			sections: contextSelectorResult.sections,
			config: contextBuildConfig.success ? contextBuildConfig.data : undefined,
		});
		timings.context_build_ms = Date.now() - contextBuildStart;

		const generationConfig = generatorStepInputSchema.shape.config.safeParse(
			stateConfig?.generation,
		);

		const generatorResult = await runGenerator({
			queryForRetrieval: inputData.queryForRetrieval,
			context: contextBuilderResult.context,
			contextItems: contextBuilderResult.contextItems,
			conversationHistory: state.conversationHistory ?? [],
			config: generationConfig.success ? generationConfig.data : undefined,
		});

		timings.generation_ms = generatorResult.generationMeta.generationMs;
		timings.ttft_ms = generatorResult.generationMeta.ttftMs;
		timings.chars_per_second = generatorResult.generationMeta.charsPerSecond;
		timings.response_length_tokens = generatorResult.generationMeta.responseLengthTokens;
		timings.pipeline_total_ms = Date.now() - pipelineStart;

		const metadata = buildSharedMetadata({
			intent: inputData.intent,
			intentConfidence: inputData.intentConfidence,
			theme: inputData.theme,
			wasExpanded: inputData.wasExpanded,
			expandedAcronyms: inputData.expandedAcronyms,
			queryForRetrieval: inputData.queryForRetrieval,
			needsLegalSearch: inputData.needsLegalSearch,
			tablesSearched: retrieverResult.retrievalMeta.publishersSearched,
			selectorMeta: contextSelectorResult.selectorMeta,
			retrievedChunks: toRetrievedChunkRefs(retrieverResult.chunks),
			aggregatedSections: toAggregatedSectionRefs(sectionAggregationResult.sections),
			contextItemsRef: toContextItemRefs(contextBuilderResult.contextItems),
			generationMeta: generatorResult.generationMeta,
		});

		await setState({
			...state,
			branchPath,
			retriever: retrieverResult,
			sectionAggregator: sectionAggregationResult,
			contextSelector: contextSelectorResult,
			contextBuilder: contextBuilderResult,
			generator: generatorResult,
		});

		return {
			...inputData,
			branchPath,
			answer: generatorResult.answer,
			chunks: retrieverResult.chunks,
			retrievalMeta: retrieverResult.retrievalMeta,
			sections: sectionAggregationResult.sections,
			aggregationMeta: sectionAggregationResult.aggregationMeta,
			selectedSections: contextSelectorResult.sections,
			selectorMeta: contextSelectorResult.selectorMeta,
			shortCircuit: false,
			shortCircuitMessage: null,
			contextItems: contextBuilderResult.contextItems,
			context: contextBuilderResult.context,
			contextMeta: contextBuilderResult.contextMeta,
			generationMeta: generatorResult.generationMeta,
			timing: timings,
			metadata,
		};
	},
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
