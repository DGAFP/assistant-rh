import { registerApiRoute } from "@mastra/core/server";
import { ALBERT_MODELS, SCALEWAY_MODELS } from "../lib/albert";

/**
 * Albert model catalog — derived from the gateway's fetchProviders().
 * The gateway (gateways/albert.ts) is the single source of truth for
 * available models; this list mirrors it for the /v1/models endpoint.
 */
const ALBERT_CHAT_MODELS = [
	ALBERT_MODELS.chat, // openweight-large
	"openweight-medium",
	"openweight-small",
	"openweight-code",
] as const;

/**
 * Models list endpoint — OpenAI-compatible API.
 *
 * GET /v1/models
 *
 * Returns available models for the RAG pipeline.
 */
export const modelsRoute = registerApiRoute("/v1/models", {
	method: "GET",
	handler: async (c) => {
		const created = Math.floor(Date.now() / 1000);

		const models = [
			...ALBERT_CHAT_MODELS.map((id) => ({
				id,
				object: "model" as const,
				created,
				owned_by: "albert",
			})),
			{
				id: SCALEWAY_MODELS.chat,
				object: "model" as const,
				created,
				owned_by: "scaleway",
			},
		];

		return c.json({
			object: "list",
			data: models,
		});
	},
});
