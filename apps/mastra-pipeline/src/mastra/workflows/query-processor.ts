import { createStep, createWorkflow } from "@mastra/core/workflows";
import { z } from "zod";
import {
	queryProcessorStateSchema,
	queryProcessorStep,
	queryProcessorStepOutputSchema,
} from "../steps/query-processor";

const queryProcessorBranchOutputSchema = queryProcessorStepOutputSchema.extend({
	branchPath: z.enum(["rag", "non_rag"]),
});

const queryProcessorBranchStateSchema = queryProcessorStateSchema.pick({
	query: true,
	conversationHistory: true,
	config: true,
	queryProcessor: true,
	branchPath: true,
});

const ragContinuationStep = createStep({
	id: "rag-continuation",
	description: "Continue to retrieval-oriented branch",
	inputSchema: queryProcessorStepOutputSchema,
	outputSchema: queryProcessorBranchOutputSchema,
	stateSchema: queryProcessorBranchStateSchema,
	execute: async ({ inputData, state, setState }) => {
		const branchPath: "rag" = "rag";
		await setState({ ...state, branchPath });
		return { ...inputData, branchPath };
	},
});

const nonRagShortCircuitStep = createStep({
	id: "non-rag-short-circuit",
	description: "Short-circuit on non-RAG intents",
	inputSchema: queryProcessorStepOutputSchema,
	outputSchema: queryProcessorBranchOutputSchema,
	stateSchema: queryProcessorBranchStateSchema,
	execute: async ({ inputData, state, setState }) => {
		const branchPath: "non_rag" = "non_rag";
		await setState({ ...state, branchPath });
		return { ...inputData, branchPath };
	},
});

export const queryProcessorWorkflow = createWorkflow({
	id: "query-processor-workflow",
	inputSchema: queryProcessorStep.inputSchema,
	outputSchema: queryProcessorBranchOutputSchema,
	stateSchema: queryProcessorStateSchema,
})
	.then(queryProcessorStep)
	.branch([
		[async ({ inputData }) => inputData.shouldProceed, ragContinuationStep],
		[async ({ inputData }) => !inputData.shouldProceed, nonRagShortCircuitStep],
	])
	.map(async ({ getStepResult }) => {
		const ragResult = getStepResult("rag-continuation");
		if (ragResult) {
			return ragResult;
		}

		const nonRagResult = getStepResult("non-rag-short-circuit");
		if (nonRagResult) {
			return nonRagResult;
		}

		throw new Error("No query-processor branch result available");
	})
	.commit();
