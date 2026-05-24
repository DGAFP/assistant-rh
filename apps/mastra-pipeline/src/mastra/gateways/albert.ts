import { createOpenAICompatible } from "@ai-sdk/openai-compatible";
import {
	type GatewayLanguageModel,
	MastraModelGateway,
	type ProviderConfig,
} from "@mastra/core/llm";

const DEFAULT_BASE_URL = "https://albert.api.etalab.gouv.fr/v1";

/**
 * DINUM Gateway — French sovereign LLM service (Albert API).
 *
 * Provides access to openweight-* chat models via an OpenAI-compatible API
 * at albert.api.etalab.gouv.fr. Embedding, reranking, and audio models are
 * available on the API but not yet wired through the gateway resolver.
 *
 * Models are referenced as `dinum/albert/<model-alias>`, e.g.:
 *   - `dinum/albert/openweight-medium`  (Mistral-Small-3.2-24B)
 *   - `dinum/albert/openweight-large`   (gpt-oss-120b)
 *   - `dinum/albert/openweight-small`   (Ministral-3-8B)
 *
 * Environment variables:
 *   - ALBERT_API_KEY  — required
 *   - ALBERT_BASE_URL — optional (defaults to https://albert.api.etalab.gouv.fr/v1)
 */
export class AlbertAPIGateway extends MastraModelGateway {
	readonly id = "dinum";
	readonly name = "Albert API (DINUM)";

	async fetchProviders(): Promise<Record<string, ProviderConfig>> {
		return {
			albert: {
				name: "Albert API",
				models: [
					// Chat models (text generation) — routed via resolveLanguageModel
					"openweight-large", // openai/gpt-oss-120b — complex tasks
					"openweight-medium", // mistralai/Mistral-Small-3.2-24B — moderate tasks + vision
					"openweight-small", // mistralai/Ministral-3-8B — simple tasks
					"openweight-code", // Qwen/Qwen3-Coder-30B-A3B — code specialization

					// TODO: wire embedding/reranking resolvers when needed
					// "openweight-embeddings", // BAAI/bge-m3 (1024d)
					// "openweight-rerank",     // BAAI/bge-reranker-v2-m3
					// "openweight-audio",      // openai/whisper-large-v3
				],
				apiKeyEnvVar: "ALBERT_API_KEY",
				gateway: this.id,
				url: this.getBaseUrl(),
				docUrl: "https://albert.api.etalab.gouv.fr/documentation",
			},
		};
	}

	buildUrl(_modelId: string, envVars?: Record<string, string>): string {
		return envVars?.ALBERT_BASE_URL || this.getBaseUrl();
	}

	async getApiKey(_modelId: string): Promise<string> {
		const apiKey = process.env.ALBERT_API_KEY;
		if (!apiKey) {
			throw new Error(
				"Missing ALBERT_API_KEY environment variable. " +
					"Get your key at https://albert.api.etalab.gouv.fr",
			);
		}
		return apiKey;
	}

	async resolveLanguageModel({
		modelId,
		providerId,
		apiKey,
		headers,
	}: {
		modelId: string;
		providerId: string;
		apiKey: string;
		headers?: Record<string, string>;
	}): Promise<GatewayLanguageModel> {
		const baseURL = this.getBaseUrl();

		return createOpenAICompatible({
			name: providerId,
			apiKey,
			baseURL,
			headers,
		}).chatModel(modelId);
	}

	private getBaseUrl(): string {
		return process.env.ALBERT_BASE_URL || DEFAULT_BASE_URL;
	}
}
