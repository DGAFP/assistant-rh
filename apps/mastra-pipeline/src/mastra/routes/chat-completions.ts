import { registerApiRoute } from "@mastra/core/server";
import { ensureObservabilityTable, logMastraRun } from "../lib/observability";
import {
	buildCompletionResponse,
	buildStreamContentChunk,
	buildStreamEndChunk,
	buildStreamStartChunk,
	chatCompletionsRequestSchema,
	estimateTokens,
	formatSseChunk,
	formatSseDone,
	generateCompletionId,
	messagesToPipelineInput,
	resolveModelName,
} from "../lib/openai-format";
import { ragPipelineWorkflow } from "../workflows/rag-pipeline";

/**
 * Chat completions endpoint — OpenAI-compatible API for the RAG pipeline.
 *
 * POST /v1/chat/completions
 *
 * Supports both streaming and non-streaming responses.
 * The endpoint calls the RAG pipeline workflow directly (not an agent).
 *
 * Note: Config overrides (model, temperature) are not yet supported.
 * The workflow uses config loaded from the database (rag_config table).
 */
export const chatCompletionsRoute = registerApiRoute("/v1/chat/completions", {
	method: "POST",
	handler: async (c) => {
		// Parse and validate request body
		const body = await c.req.json();
		const parsed = chatCompletionsRequestSchema.safeParse(body);

		if (!parsed.success) {
			return c.json(
				{
					error: {
						message: "Invalid request body",
						type: "invalid_request_error",
						details: parsed.error.issues,
					},
				},
				400,
			);
		}

		const request = parsed.data;
		const completionId = generateCompletionId();

		// Convert messages to pipeline input
		let pipelineInput: {
			query: string;
			conversationHistory: Array<{ role: "user" | "assistant"; content: string }>;
		};
		try {
			pipelineInput = messagesToPipelineInput(request.messages);
		} catch (error) {
			return c.json(
				{
					error: {
						message: error instanceof Error ? error.message : "Failed to parse messages",
						type: "invalid_request_error",
					},
				},
				400,
			);
		}

		// Merge conversation_history from request if provided
		const conversationHistory = request.conversation_history ?? pipelineInput.conversationHistory;

		// Execute the RAG pipeline workflow
		try {
			const run = await ragPipelineWorkflow.createRun();
			const result = await run.start({
				inputData: {
					query: pipelineInput.query,
					conversationHistory,
				},
			});

			if (result.status !== "success") {
				return c.json(
					{
						error: {
							message: `Pipeline failed with status: ${result.status}`,
							type: "pipeline_error",
						},
					},
					500,
				);
			}

			const workflowResult = result.result;
			const model = resolveModelName(workflowResult);

			// Ensure observability table exists (lazy init)
			await ensureObservabilityTable();

			// Log the run (fire and forget - don't block response)
			const turnId = completionId;
			void logMastraRun(turnId, pipelineInput.query, workflowResult).catch((err) => {
				console.error("Failed to log Mastra run:", err);
			});

			// Handle streaming vs non-streaming
			if (request.stream) {
				return handleStreamingResponse(completionId, model, workflowResult.answer);
			}

			// Non-streaming response
			const contextTokens =
				workflowResult.contextMeta?.tokenCount ?? estimateTokens(workflowResult.context);
			const response = buildCompletionResponse(
				completionId,
				model,
				workflowResult.answer,
				contextTokens,
			);

			return c.json(response);
		} catch (error) {
			console.error("Pipeline execution error:", error);
			return c.json(
				{
					error: {
						message: error instanceof Error ? error.message : "Pipeline execution failed",
						type: "pipeline_error",
					},
				},
				500,
			);
		}
	},
});

/**
 * Handle streaming response using SSE.
 *
 * Note: This streams the final answer after the workflow completes.
 * True token-by-token streaming through the pipeline would require
 * deeper integration with the generator's stream.
 */
function handleStreamingResponse(completionId: string, model: string, answer: string): Response {
	// Create a ReadableStream for SSE
	const stream = new ReadableStream({
		async start(controller) {
			const encoder = new TextEncoder();

			// Send start chunk
			const startChunk = buildStreamStartChunk(completionId, model);
			controller.enqueue(encoder.encode(formatSseChunk(startChunk)));

			// Stream content in chunks (split on reasonable boundaries)
			// For now, we send the whole answer as one chunk since the workflow
			// already completed. Future optimization: integrate with generator stream.
			if (answer.length > 0) {
				const contentChunk = buildStreamContentChunk(completionId, model, answer);
				controller.enqueue(encoder.encode(formatSseChunk(contentChunk)));
			}

			// Send end chunk
			const endChunk = buildStreamEndChunk(completionId, model);
			controller.enqueue(encoder.encode(formatSseChunk(endChunk)));

			// Send done marker
			controller.enqueue(encoder.encode(formatSseDone()));

			controller.close();
		},
	});

	return new Response(stream, {
		headers: {
			"Content-Type": "text/event-stream",
			"Cache-Control": "no-cache",
			Connection: "keep-alive",
		},
	});
}
