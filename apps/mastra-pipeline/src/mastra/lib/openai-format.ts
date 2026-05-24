import { z } from "zod";
import type { ragPipelineWorkflowOutputSchema } from "../workflows/rag-pipeline";
import { ALBERT_MODELS } from "./albert";

// OpenAI message format
const openaiMessageSchema = z.object({
	role: z.enum(["system", "user", "assistant"]),
	content: z.string(),
});

export type OpenAIMessage = z.infer<typeof openaiMessageSchema>;

// OpenAI request format (subset we accept)
export const chatCompletionsRequestSchema = z.object({
	model: z.string().optional(),
	messages: z.array(openaiMessageSchema).min(1),
	stream: z.boolean().optional().default(false),
	temperature: z.number().optional(),
	// Non-standard extensions for RAG pipeline control
	conversation_history: z
		.array(
			z.object({
				role: z.enum(["user", "assistant", "system"]),
				content: z.string(),
			}),
		)
		.optional(),
	config: z
		.object({
			retrieval: z.unknown().optional(),
			aggregation: z.unknown().optional(),
			selector: z.unknown().optional(),
			context: z.unknown().optional(),
			generation: z.unknown().optional(),
		})
		.passthrough()
		.optional(),
});

export type ChatCompletionsRequest = z.infer<typeof chatCompletionsRequestSchema>;

// OpenAI response format
export interface ChatCompletionChoice {
	index: number;
	message: {
		role: "assistant";
		content: string;
	};
	finish_reason: "stop" | "length" | null;
}

export interface ChatCompletionUsage {
	prompt_tokens: number;
	completion_tokens: number;
	total_tokens: number;
}

export interface ChatCompletionResponse {
	id: string;
	object: "chat.completion";
	created: number;
	model: string;
	choices: ChatCompletionChoice[];
	usage: ChatCompletionUsage;
}

// OpenAI streaming chunk format
export interface ChatCompletionChunk {
	id: string;
	object: "chat.completion.chunk";
	created: number;
	model: string;
	choices: Array<{
		index: number;
		delta: {
			role?: "assistant";
			content?: string;
		};
		finish_reason: "stop" | "length" | null;
	}>;
}

/**
 * Generate a unique chat completion ID.
 */
export function generateCompletionId(): string {
	const uuid = crypto.randomUUID();
	return `chatcmpl-${uuid.slice(0, 8)}${uuid.slice(9, 13)}${uuid.slice(14, 18)}`;
}

/**
 * Estimate token count from text.
 * Uses the 4-char-per-token heuristic from the existing pipeline.
 */
export function estimateTokens(text: string): number {
	return Math.floor((text ?? "").length / 4);
}

/**
 * Convert OpenAI messages format to RAG pipeline input.
 *
 * - Last user message becomes the query
 * - Prior messages become conversation history (for follow-up context)
 * - System messages are ignored (RAG pipeline has its own system prompt)
 */
export function messagesToPipelineInput(messages: OpenAIMessage[]): {
	query: string;
	conversationHistory: Array<{ role: "user" | "assistant"; content: string }>;
} {
	// Find the last user message
	let query = "";
	const conversationHistory: Array<{
		role: "user" | "assistant";
		content: string;
	}> = [];

	for (const msg of messages) {
		if (msg.role === "user") {
			// If we already have a query, add it to history
			if (query.length > 0) {
				conversationHistory.push({ role: "user", content: query });
			}
			query = msg.content;
		} else if (msg.role === "assistant" && query.length > 0) {
			conversationHistory.push({ role: "assistant", content: msg.content });
		}
		// System messages are ignored - RAG pipeline controls its own system prompt
	}

	if (query.length === 0) {
		throw new Error("No user message found in request");
	}

	return { query, conversationHistory };
}

/**
 * Build a non-streaming response from pipeline output.
 */
export function buildCompletionResponse(
	completionId: string,
	model: string,
	answer: string,
	contextTokens: number,
): ChatCompletionResponse {
	const completionTokens = estimateTokens(answer);
	const created = Math.floor(Date.now() / 1000);

	return {
		id: completionId,
		object: "chat.completion",
		created,
		model,
		choices: [
			{
				index: 0,
				message: {
					role: "assistant",
					content: answer,
				},
				finish_reason: "stop",
			},
		],
		usage: {
			prompt_tokens: contextTokens,
			completion_tokens: completionTokens,
			total_tokens: contextTokens + completionTokens,
		},
	};
}

/**
 * Build a streaming chunk for the start of a response.
 */
export function buildStreamStartChunk(completionId: string, model: string): ChatCompletionChunk {
	return {
		id: completionId,
		object: "chat.completion.chunk",
		created: Math.floor(Date.now() / 1000),
		model,
		choices: [
			{
				index: 0,
				delta: {
					role: "assistant",
				},
				finish_reason: null,
			},
		],
	};
}

/**
 * Build a streaming chunk for content delta.
 */
export function buildStreamContentChunk(
	completionId: string,
	model: string,
	content: string,
): ChatCompletionChunk {
	return {
		id: completionId,
		object: "chat.completion.chunk",
		created: Math.floor(Date.now() / 1000),
		model,
		choices: [
			{
				index: 0,
				delta: {
					content,
				},
				finish_reason: null,
			},
		],
	};
}

/**
 * Build the final streaming chunk.
 */
export function buildStreamEndChunk(completionId: string, model: string): ChatCompletionChunk {
	return {
		id: completionId,
		object: "chat.completion.chunk",
		created: Math.floor(Date.now() / 1000),
		model,
		choices: [
			{
				index: 0,
				delta: {},
				finish_reason: "stop",
			},
		],
	};
}

/**
 * Format a chunk as SSE data line.
 */
export function formatSseChunk(chunk: ChatCompletionChunk): string {
	return `data: ${JSON.stringify(chunk)}\n\n`;
}

/**
 * Format the SSE stream terminator.
 */
export function formatSseDone(): string {
	return "data: [DONE]\n\n";
}

/**
 * Extract model name from pipeline result.
 * Falls back to a sensible default if metadata is incomplete.
 */
export function resolveModelName(result: z.infer<typeof ragPipelineWorkflowOutputSchema>): string {
	const meta = result.generationMeta;
	if (meta?.modelUsed) {
		return meta.modelUsed;
	}
	return ALBERT_MODELS.chat;
}
