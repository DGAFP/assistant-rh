import { createStep, createWorkflow } from "@mastra/core/workflows";
import { z } from "zod";
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
	sectionAggregatorStateSchema,
	sectionAggregatorStepInputSchema,
	sectionAggregatorStepOutputSchema,
} from "../steps/section-aggregator";

export const sectionAggregatorWorkflowOutputSchema = queryProcessorStepOutputSchema.extend({
	branchPath: z.enum(["rag", "non_rag"]),
	chunks: z.array(retrievedChunkSchema),
	retrievalMeta: retrieverStepOutputSchema.shape.retrievalMeta.nullable(),
	sections: z.array(aggregatedSectionSchema),
	aggregationMeta: sectionAggregatorStepOutputSchema.shape.aggregationMeta.nullable(),
});

const ragSectionAggregatorStep = createStep({
	id: "rag-section-aggregator",
	description: "Run retriever then section aggregation for RAG intents",
	inputSchema: queryProcessorStepOutputSchema,
	outputSchema: sectionAggregatorWorkflowOutputSchema,
	stateSchema: sectionAggregatorStateSchema,
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

		const branchPath: "rag" = "rag";
		await setState({
			...state,
			branchPath,
			retriever: retrieverResult,
			sectionAggregator: sectionAggregationResult,
		});

		return {
			...inputData,
			branchPath,
			chunks: retrieverResult.chunks,
			retrievalMeta: retrieverResult.retrievalMeta,
			sections: sectionAggregationResult.sections,
			aggregationMeta: sectionAggregationResult.aggregationMeta,
		};
	},
});

const nonRagShortCircuitStep = createStep({
	id: "section-aggregator-non-rag-short-circuit",
	description: "Skip retriever and section aggregation for non-RAG intents",
	inputSchema: queryProcessorStepOutputSchema,
	outputSchema: sectionAggregatorWorkflowOutputSchema,
	stateSchema: sectionAggregatorStateSchema,
	execute: async ({ inputData, state, setState }) => {
		const branchPath: "non_rag" = "non_rag";

		await setState({
			...state,
			branchPath,
			retriever: undefined,
			sectionAggregator: undefined,
		});

		return {
			...inputData,
			branchPath,
			chunks: [],
			retrievalMeta: null,
			sections: [],
			aggregationMeta: null,
		};
	},
});

export const sectionAggregatorWorkflow = createWorkflow({
	id: "section-aggregator-workflow",
	inputSchema: queryProcessorStep.inputSchema,
	outputSchema: sectionAggregatorWorkflowOutputSchema,
	stateSchema: sectionAggregatorStateSchema,
})
	.then(queryProcessorStep)
	.branch([
		[async ({ inputData }) => inputData.shouldProceed, ragSectionAggregatorStep],
		[async ({ inputData }) => !inputData.shouldProceed, nonRagShortCircuitStep],
	])
	.map(async ({ getStepResult }) => {
		const ragResult = getStepResult("rag-section-aggregator");
		if (ragResult) {
			return ragResult;
		}

		const nonRagResult = getStepResult("section-aggregator-non-rag-short-circuit");
		if (nonRagResult) {
			return nonRagResult;
		}

		throw new Error("No section aggregator branch result available");
	})
	.commit();
