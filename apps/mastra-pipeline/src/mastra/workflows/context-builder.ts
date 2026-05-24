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

export const contextBuilderWorkflowOutputSchema = queryProcessorStepOutputSchema.extend({
	branchPath: z.enum(["rag", "non_rag"]),
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
});

const ragContextBuilderStep = createStep({
	id: "rag-context-builder",
	description: "Run retriever, section aggregation, selector, and context builder for RAG intents",
	inputSchema: queryProcessorStepOutputSchema,
	outputSchema: contextBuilderWorkflowOutputSchema,
	stateSchema: contextBuilderStateSchema,
	execute: async ({ inputData, state, setState }) => {
		const stateConfig = state.config;

		const retrievalConfig = retrieverStepInputSchema.shape.config.safeParse(stateConfig?.retrieval);

		const retrieverResult = await runRetriever({
			queryForRetrieval: inputData.queryForRetrieval,
			needsLegalSearch: inputData.needsLegalSearch,
			config: retrievalConfig.success ? retrievalConfig.data : undefined,
		});

		const aggregationConfig = sectionAggregatorStepInputSchema.shape.config.safeParse(
			stateConfig?.aggregation,
		);

		const sectionAggregationResult = await runSectionAggregator({
			chunks: retrieverResult.chunks,
			queryForRetrieval: inputData.queryForRetrieval,
			reformulatedQuery: inputData.reformulatedQuery ?? undefined,
			config: aggregationConfig.success ? aggregationConfig.data : undefined,
		});

		const selectorConfig = contextSelectorStepInputSchema.shape.config.safeParse(
			stateConfig?.selector,
		);

		const contextSelectorResult = await runContextSelector({
			queryForRetrieval: inputData.queryForRetrieval,
			sections: sectionAggregationResult.sections,
			config: selectorConfig.success ? selectorConfig.data : undefined,
		});

		const branchPath: "rag" = "rag";

		if (contextSelectorResult.shortCircuit) {
			await setState({
				...state,
				branchPath,
				retriever: retrieverResult,
				sectionAggregator: sectionAggregationResult,
				contextSelector: contextSelectorResult,
				contextBuilder: undefined,
			});

			return {
				...inputData,
				branchPath,
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
			};
		}

		const contextBuildConfig = contextBuilderStepInputSchema.shape.config.safeParse(
			stateConfig?.context,
		);

		const contextBuilderResult = await runContextBuilder({
			sections: contextSelectorResult.sections,
			config: contextBuildConfig.success ? contextBuildConfig.data : undefined,
		});

		await setState({
			...state,
			branchPath,
			retriever: retrieverResult,
			sectionAggregator: sectionAggregationResult,
			contextSelector: contextSelectorResult,
			contextBuilder: contextBuilderResult,
		});

		return {
			...inputData,
			branchPath,
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
		};
	},
});

const nonRagShortCircuitStep = createStep({
	id: "context-builder-non-rag-short-circuit",
	description: "Skip retrieval/context stages for non-RAG intents",
	inputSchema: queryProcessorStepOutputSchema,
	outputSchema: contextBuilderWorkflowOutputSchema,
	stateSchema: contextBuilderStateSchema,
	execute: async ({ inputData, state, setState }) => {
		const branchPath: "non_rag" = "non_rag";

		await setState({
			...state,
			branchPath,
			retriever: undefined,
			sectionAggregator: undefined,
			contextSelector: undefined,
			contextBuilder: undefined,
		});

		return {
			...inputData,
			branchPath,
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
		};
	},
});

export const contextBuilderWorkflow = createWorkflow({
	id: "context-builder-workflow",
	inputSchema: queryProcessorStep.inputSchema,
	outputSchema: contextBuilderWorkflowOutputSchema,
	stateSchema: contextBuilderStateSchema,
})
	.then(queryProcessorStep)
	.branch([
		[async ({ inputData }) => inputData.shouldProceed, ragContextBuilderStep],
		[async ({ inputData }) => !inputData.shouldProceed, nonRagShortCircuitStep],
	])
	.map(async ({ getStepResult }) => {
		const ragResult = getStepResult("rag-context-builder");
		if (ragResult) {
			return ragResult;
		}

		const nonRagResult = getStepResult("context-builder-non-rag-short-circuit");
		if (nonRagResult) {
			return nonRagResult;
		}

		throw new Error("No context-builder branch result available");
	})
	.commit();
