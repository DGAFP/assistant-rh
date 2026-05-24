import { createOpenAICompatible } from "@ai-sdk/openai-compatible";
import { CircuitBreaker } from "./circuit-breaker";

export type ProviderName = "albert" | "scaleway";

type OpenAiCompatibleProvider = ReturnType<typeof createOpenAICompatible>;
type ChatModel = ReturnType<OpenAiCompatibleProvider["chatModel"]>;

const DEFAULT_ALBERT_BASE_URL = "https://albert.api.etalab.gouv.fr/v1";
const DEFAULT_SCALEWAY_BASE_URL = "https://api.scaleway.ai/v1";

const CHAT_BREAKER = new CircuitBreaker({ cooldownMs: 60_000 });

export const ALBERT_MODELS = {
	chat: "openweight-large",
	embeddings: "openweight-embeddings",
	reranker: "openweight-rerank",
} as const;

export const SCALEWAY_MODELS = {
	chat: "llama-3.1-70b-instruct",
	embeddings: "bge-multilingual-gemma2",
} as const;

function requiredEnv(name: string): string {
	const value = process.env[name];
	if (!value) {
		throw new Error(`Missing required environment variable: ${name}`);
	}
	return value;
}

export function getAlbertBaseUrl(): string {
	return process.env.ALBERT_BASE_URL ?? DEFAULT_ALBERT_BASE_URL;
}

export function getScalewayBaseUrl(): string {
	return (
		process.env.SCALEWAY_BASE_URL ??
		process.env.SCW_GENERATIVE_APIs_ENDPOINT ??
		DEFAULT_SCALEWAY_BASE_URL
	);
}

export function getAlbertProvider(): OpenAiCompatibleProvider {
	return createOpenAICompatible({
		name: "albert",
		apiKey: requiredEnv("ALBERT_API_KEY"),
		baseURL: getAlbertBaseUrl(),
	});
}

/**
 * Uses Mastra-compatible OpenAI endpoint settings for Scaleway without creating
 * a dedicated custom gateway.
 */
export function getScalewayProvider(): OpenAiCompatibleProvider {
	return createOpenAICompatible({
		name: "scaleway",
		apiKey: requiredEnv("SCALEWAY_API_KEY"),
		baseURL: getScalewayBaseUrl(),
	});
}

export interface ChatModelOptions {
	provider?: ProviderName;
	modelId?: string;
}

export interface ResolvedChatModel {
	provider: ProviderName;
	model: ChatModel;
}

export function resolveChatModel(options: ChatModelOptions = {}): ResolvedChatModel {
	const providerName = options.provider ?? "albert";

	if (providerName === "scaleway") {
		const scalewayProvider = getScalewayProvider();
		return {
			provider: "scaleway",
			model: scalewayProvider.chatModel(options.modelId ?? SCALEWAY_MODELS.chat),
		};
	}

	const albertProvider = getAlbertProvider();
	return {
		provider: "albert",
		model: albertProvider.chatModel(options.modelId ?? ALBERT_MODELS.chat),
	};
}

export async function withAlbertFallback<T>(handlers: {
	runAlbert: () => Promise<T>;
	runScaleway: () => Promise<T>;
}): Promise<{ value: T; provider: ProviderName }> {
	if (CHAT_BREAKER.shouldSkip()) {
		return { value: await handlers.runScaleway(), provider: "scaleway" };
	}

	try {
		const value = await handlers.runAlbert();
		CHAT_BREAKER.recordSuccess();
		return { value, provider: "albert" };
	} catch {
		CHAT_BREAKER.recordFailure();
		return { value: await handlers.runScaleway(), provider: "scaleway" };
	}
}

export function getChatFallbackState() {
	return CHAT_BREAKER.snapshot();
}

export interface RerankRequest {
	query: string;
	documents: string[];
	topN?: number;
}

export interface RerankResult {
	index: number;
	score: number;
}

export async function rerankWithAlbert(request: RerankRequest): Promise<RerankResult[]> {
	const response = await fetch(`${getAlbertBaseUrl().replace(/\/$/, "")}/rerank`, {
		method: "POST",
		headers: {
			"Content-Type": "application/json",
			Authorization: `Bearer ${requiredEnv("ALBERT_API_KEY")}`,
		},
		body: JSON.stringify({
			model: ALBERT_MODELS.reranker,
			query: request.query,
			documents: request.documents,
			top_n: request.topN ?? request.documents.length,
		}),
		signal: AbortSignal.timeout(30_000),
	});

	if (!response.ok) {
		throw new Error(`Albert rerank call failed with status ${response.status}`);
	}

	const body = (await response.json()) as {
		data?: Array<{ index?: number; relevance_score?: number }>;
	};

	const rows = Array.isArray(body.data) ? body.data : [];
	return rows
		.filter((row) => Number.isInteger(row.index) && typeof row.relevance_score === "number")
		.map((row) => ({ index: row.index as number, score: row.relevance_score as number }));
}
