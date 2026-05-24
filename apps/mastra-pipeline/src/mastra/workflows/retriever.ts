import { createStep, createWorkflow } from "@mastra/core/workflows";
import { z } from "zod";
import {
	queryProcessorStateSchema,
	queryProcessorStep,
	queryProcessorStepOutputSchema,
} from "../steps/query-processor";
import {
	retrievedChunkSchema,
	retrieverStateSchema,
	retrieverStepInputSchema,
	retrieverStepOutputSchema,
	runRetriever,
} from "../steps/retriever";

export const retrieverWorkflowOutputSchema = queryProcessorStepOutputSchema.extend({
	branchPath: z.enum(["rag", "non_rag"]),
	chunks: z.array(retrievedChunkSchema),
	retrievalMeta: retrieverStepOutputSchema.shape.retrievalMeta.nullable(),
});

const retrieverWorkflowStateSchema = queryProcessorStateSchema.extend({
	retriever: retrieverStateSchema.shape.retriever,
});

const ragRetrieverStep = createStep({
	id: "rag-retriever",
	description: "Run retriever when query should continue to RAG",
	inputSchema: queryProcessorStepOutputSchema,
	outputSchema: retrieverWorkflowOutputSchema,
	stateSchema: retrieverWorkflowStateSchema,
	execute: async ({ inputData, state, setState }) => {
		const stateConfig = (state as { config?: { retrieval?: unknown } }).config;
		const parsedConfig = retrieverStepInputSchema.shape.config.safeParse(stateConfig?.retrieval);

		const retrieverResult = await runRetriever({
			queryForRetrieval: inputData.queryForRetrieval,
			needsLegalSearch: inputData.needsLegalSearch,
			config: parsedConfig.success ? parsedConfig.data : undefined,
		});

		const branchPath: "rag" = "rag";
		await setState({
			...state,
			branchPath,
			retriever: retrieverResult,
		});

		return {
			...inputData,
			branchPath,
			chunks: retrieverResult.chunks,
			retrievalMeta: retrieverResult.retrievalMeta,
		};
	},
});

const nonRagShortCircuitStep = createStep({
	id: "non-rag-short-circuit",
	description: "Skip retriever for non-RAG intents",
	inputSchema: queryProcessorStepOutputSchema,
	outputSchema: retrieverWorkflowOutputSchema,
	stateSchema: retrieverWorkflowStateSchema,
	execute: async ({ inputData, state, setState }) => {
		const branchPath: "non_rag" = "non_rag";
		await setState({
			...state,
			branchPath,
			retriever: undefined,
		});

		return {
			...inputData,
			branchPath,
			chunks: [],
			retrievalMeta: null,
		};
	},
});

export const retrieverWorkflow = createWorkflow({
	id: "retriever-workflow",
	inputSchema: queryProcessorStep.inputSchema,
	outputSchema: retrieverWorkflowOutputSchema,
	stateSchema: retrieverWorkflowStateSchema,
})
	.then(queryProcessorStep)
	.branch([
		[async ({ inputData }) => inputData.shouldProceed, ragRetrieverStep],
		[async ({ inputData }) => !inputData.shouldProceed, nonRagShortCircuitStep],
	])
	.map(async ({ getStepResult }) => {
		const ragResult = getStepResult("rag-retriever");
		if (ragResult) {
			return ragResult;
		}

		const nonRagResult = getStepResult("non-rag-short-circuit");
		if (nonRagResult) {
			return nonRagResult;
		}

		throw new Error("No retriever branch result available");
	})
	.commit();
