import { createStep } from "@mastra/core/workflows";
import { z } from "zod";
import { getAlbertBaseUrl, getScalewayBaseUrl } from "../lib/albert";
import {
	DEFAULT_RUNTIME_RAG_CONFIG,
	getRuntimeRagConfig,
	type RuntimeRagConfig,
	resolvePrompt,
} from "../lib/config";
import { contextBuilderStateSchema, contextBuilderStepOutputSchema } from "./context-builder";

const DEFAULT_GENERATOR_SYSTEM_PROMPT =
	"Tu es un assistant RH expert pour le Ministere de la Transition Ecologique. " +
	"Reponds aux questions des agents publics sur les ressources humaines.";

const USER_PROMPT_TEMPLATE = `Voici le contexte documentaire pour repondre a la question :

{context}

---

**Question de l'utilisateur :** {question}

---

En vous appuyant uniquement sur les sources ci-dessus, repondez de maniere claire et operationnelle.
Si les sources ne permettent pas de repondre, dites-le explicitement et n'inventez pas.`;

const STREAM_PARTIAL_ERROR_SUFFIX = "\n\n_Erreur de connexion ({provider}). Reponse partielle._";

const GENERATION_FAILURE_MESSAGE =
	"Je rencontre actuellement une indisponibilite technique pour generer une reponse complete. " +
	"Merci de reessayer dans quelques instants.";

const generationConfigSchema = z
	.object({
		provider: z.enum(["albert", "scaleway", "mistral"]),
		model: z.string(),
		temperature: z.number(),
		system_prompt_name: z.string(),
		fallback_provider: z.enum(["albert", "scaleway", "mistral"]),
		fallback_model: z.string(),
	})
	.partial();

const conversationMessageSchema = z.object({
	role: z.enum(["user", "assistant", "system"]),
	content: z.string(),
});

interface NormalizedGenerationConfig {
	provider: "albert" | "scaleway" | "mistral";
	model: string;
	temperature: number;
	system_prompt_name: string;
	fallback_provider: "albert" | "scaleway" | "mistral";
	fallback_model: string;
}

const generationMetaSchema = z.object({
	providerConfigured: z.enum(["albert", "scaleway", "mistral"]),
	modelConfigured: z.string(),
	fallbackProviderConfigured: z.enum(["albert", "scaleway", "mistral"]),
	fallbackModelConfigured: z.string(),
	providerUsed: z.enum(["albert", "scaleway", "mistral"]),
	modelUsed: z.string(),
	fallbackTriggered: z.boolean(),
	promptNameUsed: z.string(),
	systemPromptUsed: z.string(),
	fullPrompt: z.string(),
	generationMs: z.number().nonnegative(),
	ttftMs: z.number().nonnegative(),
	charsPerSecond: z.number().nonnegative(),
	responseLengthTokens: z.number().int().nonnegative(),
	warnings: z.array(z.string()),
});

export const generatorStepInputSchema = z.object({
	queryForRetrieval: z.string(),
	context: z.string(),
	contextItems: contextBuilderStepOutputSchema.shape.contextItems,
	conversationHistory: z.array(conversationMessageSchema).optional(),
	config: generationConfigSchema.optional(),
});

export const generatorStepOutputSchema = z.object({
	answer: z.string(),
	generationMeta: generationMetaSchema,
});

export const generatorStateSchema = contextBuilderStateSchema.extend({
	config: z
		.object({
			retrieval: z.unknown().optional(),
			aggregation: z.unknown().optional(),
			selector: z.unknown().optional(),
			context: z.unknown().optional(),
			generation: generationConfigSchema.optional(),
		})
		.passthrough()
		.optional(),
	generator: generatorStepOutputSchema.optional(),
});

type GeneratorStepInput = z.infer<typeof generatorStepInputSchema>;
type GeneratorStepOutput = z.infer<typeof generatorStepOutputSchema>;
type GenerationConfigInput = z.infer<typeof generationConfigSchema>;
type ConversationMessage = z.infer<typeof conversationMessageSchema>;

interface ChatProviderConfig {
	provider: "albert" | "scaleway" | "mistral";
	baseUrl: string;
	apiKey: string;
	model: string;
	temperature: number;
}

interface StreamResult {
	text: string;
	ttftMs: number;
}

class StreamFailureAfterTokensError extends Error {
	constructor(
		message: string,
		readonly partialText: string,
		readonly ttftMs: number,
	) {
		super(message);
		this.name = "StreamFailureAfterTokensError";
	}
}

function requiredEnv(name: string): string {
	const value = process.env[name];
	if (!value) {
		throw new Error(`Missing required environment variable: ${name}`);
	}
	return value;
}

function estimateTokens(text: string): number {
	return Math.floor((text ?? "").length / 4);
}

function asDateForPrompt(date: Date): string {
	return date.toISOString().slice(0, 10);
}

function normalizeGenerationConfig(
	runtimeConfig: RuntimeRagConfig["generation"],
	override?: GenerationConfigInput,
): NormalizedGenerationConfig {
	const base: NormalizedGenerationConfig = {
		provider: "albert",
		model: "openweight-large",
		temperature: 0,
		system_prompt_name: "system_prompt_V6_optimized.md",
		fallback_provider: "scaleway",
		fallback_model: "llama-3.1-70b-instruct",
	};

	return {
		provider: override?.provider ?? runtimeConfig?.provider ?? base.provider,
		model: override?.model ?? runtimeConfig?.model ?? base.model,
		temperature: override?.temperature ?? runtimeConfig?.temperature ?? base.temperature,
		system_prompt_name:
			override?.system_prompt_name ?? runtimeConfig?.system_prompt_name ?? base.system_prompt_name,
		fallback_provider:
			override?.fallback_provider ?? runtimeConfig?.fallback_provider ?? base.fallback_provider,
		fallback_model:
			override?.fallback_model ?? runtimeConfig?.fallback_model ?? base.fallback_model,
	};
}

async function loadRuntimeGenerationConfigSafe(): Promise<RuntimeRagConfig["generation"]> {
	try {
		const runtimeConfig = await getRuntimeRagConfig();
		return runtimeConfig.generation;
	} catch {
		return DEFAULT_RUNTIME_RAG_CONFIG.generation;
	}
}

function resolveProviderConfig(args: {
	provider: "albert" | "scaleway" | "mistral";
	model: string;
	temperature: number;
}): ChatProviderConfig {
	if (args.provider === "scaleway") {
		return {
			provider: "scaleway",
			baseUrl: getScalewayBaseUrl(),
			apiKey: requiredEnv("SCALEWAY_API_KEY"),
			model: args.model,
			temperature: args.temperature,
		};
	}

	if (args.provider === "mistral") {
		return {
			provider: "mistral",
			baseUrl: process.env.OPENAI_BASE_URL ?? "https://api.openai.com/v1",
			apiKey: requiredEnv("OPENAI_API_KEY"),
			model: args.model,
			temperature: args.temperature,
		};
	}

	return {
		provider: "albert",
		baseUrl: getAlbertBaseUrl(),
		apiKey: requiredEnv("ALBERT_API_KEY"),
		model: args.model,
		temperature: args.temperature,
	};
}

function buildMessages(args: {
	systemPrompt: string;
	userPrompt: string;
	conversationHistory: ConversationMessage[];
}): Array<{ role: "system" | "user" | "assistant"; content: string }> {
	const messages: Array<{ role: "system" | "user" | "assistant"; content: string }> = [];

	if (args.systemPrompt.trim().length > 0) {
		messages.push({ role: "system", content: args.systemPrompt });
	}

	for (const item of args.conversationHistory) {
		messages.push({ role: item.role, content: item.content });
	}

	messages.push({ role: "user", content: args.userPrompt });
	return messages;
}

function extractSseDataEvents(rawEvent: string): string[] {
	const lines = rawEvent.replaceAll("\r", "").split("\n");
	const dataLines: string[] = [];
	for (const line of lines) {
		const trimmed = line.trim();
		if (trimmed.startsWith("data:")) {
			const payload = trimmed.slice(5).trim();
			if (payload.length > 0) {
				dataLines.push(payload);
			}
		}
	}

	return dataLines;
}

function consumeSseBuffer(buffer: string): {
	rest: string;
	events: string[];
} {
	const events: string[] = [];
	let rest = buffer;

	for (;;) {
		const separatorIndex = rest.indexOf("\n\n");
		if (separatorIndex < 0) {
			break;
		}

		const event = rest.slice(0, separatorIndex);
		rest = rest.slice(separatorIndex + 2);
		events.push(event);
	}

	return { rest, events };
}

async function requestStreamingCompletion(args: {
	provider: ChatProviderConfig;
	messages: Array<{ role: "system" | "user" | "assistant"; content: string }>;
}): Promise<StreamResult> {
	const startTs = Date.now();
	const response = await fetch(`${args.provider.baseUrl.replace(/\/$/, "")}/chat/completions`, {
		method: "POST",
		headers: {
			"Content-Type": "application/json",
			Authorization: `Bearer ${args.provider.apiKey}`,
		},
		body: JSON.stringify({
			model: args.provider.model,
			temperature: args.provider.temperature,
			stream: true,
			messages: args.messages,
		}),
		signal: AbortSignal.timeout(120_000),
	});

	if (!response.ok) {
		throw new Error(`Generator completion failed with HTTP ${response.status}`);
	}

	const body = response.body;
	if (!body) {
		throw new Error("Generator completion returned no response body");
	}

	const reader = body.getReader();
	const decoder = new TextDecoder();

	let buffer = "";
	let text = "";
	let ttftMs: number | null = null;

	try {
		for (;;) {
			const chunk = await reader.read();
			if (chunk.done) {
				break;
			}

			buffer += decoder.decode(chunk.value, { stream: true });
			const consumed = consumeSseBuffer(buffer);
			buffer = consumed.rest;

			for (const rawEvent of consumed.events) {
				const dataEvents = extractSseDataEvents(rawEvent);
				for (const eventData of dataEvents) {
					if (eventData === "[DONE]") {
						return {
							text,
							ttftMs: ttftMs ?? 0,
						};
					}

					let parsed: unknown;
					try {
						parsed = JSON.parse(eventData) as unknown;
					} catch {
						continue;
					}

					const delta =
						typeof parsed === "object" &&
						parsed !== null &&
						!Array.isArray(parsed) &&
						Array.isArray((parsed as { choices?: unknown }).choices)
							? (parsed as { choices: Array<{ delta?: { content?: unknown } }> }).choices[0]?.delta
									?.content
							: null;

					if (typeof delta === "string" && delta.length > 0) {
						if (ttftMs === null) {
							ttftMs = Date.now() - startTs;
						}
						text += delta;
					}
				}
			}
		}

		return {
			text,
			ttftMs: ttftMs ?? 0,
		};
	} catch (error) {
		if (text.length > 0) {
			const message = error instanceof Error ? error.message : String(error);
			throw new StreamFailureAfterTokensError(message, text, ttftMs ?? 0);
		}

		throw error;
	} finally {
		reader.releaseLock();
	}
}

function withPartialStreamError(provider: "albert" | "scaleway" | "mistral", text: string): string {
	return `${text}${STREAM_PARTIAL_ERROR_SUFFIX.replace("{provider}", provider)}`;
}

export async function runGenerator(
	input: GeneratorStepInput,
	runtimeGenerationConfigOverride?: RuntimeRagConfig["generation"],
): Promise<GeneratorStepOutput> {
	const runtimeGenerationConfig =
		runtimeGenerationConfigOverride ?? (await loadRuntimeGenerationConfigSafe());
	const config = normalizeGenerationConfig(runtimeGenerationConfig, input.config);

	const today = asDateForPrompt(new Date());
	const rawSystemPrompt =
		(await resolvePrompt(config.system_prompt_name, {
			fallbackContent: DEFAULT_GENERATOR_SYSTEM_PROMPT,
		})) ?? DEFAULT_GENERATOR_SYSTEM_PROMPT;

	const systemPrompt = rawSystemPrompt.replaceAll("{today}", today);
	const fullPrompt = USER_PROMPT_TEMPLATE.replaceAll("{context}", input.context).replaceAll(
		"{question}",
		input.queryForRetrieval,
	);

	const conversationHistory = input.conversationHistory ?? [];
	const messages = buildMessages({
		systemPrompt,
		userPrompt: fullPrompt,
		conversationHistory,
	});

	const warnings: string[] = [];
	const generationStart = Date.now();

	const primaryProvider = resolveProviderConfig({
		provider: config.provider,
		model: config.model,
		temperature: config.temperature,
	});

	const fallbackProvider = resolveProviderConfig({
		provider: config.fallback_provider,
		model: config.fallback_model,
		temperature: config.temperature,
	});

	let answer = "";
	let providerUsed: "albert" | "scaleway" | "mistral" = primaryProvider.provider;
	let modelUsed = primaryProvider.model;
	let fallbackTriggered = false;
	let ttftMs = 0;

	try {
		const primary = await requestStreamingCompletion({
			provider: primaryProvider,
			messages,
		});

		answer = primary.text;
		ttftMs = primary.ttftMs;
	} catch (error) {
		if (error instanceof StreamFailureAfterTokensError) {
			providerUsed = primaryProvider.provider;
			modelUsed = primaryProvider.model;
			answer = withPartialStreamError(primaryProvider.provider, error.partialText);
			ttftMs = error.ttftMs;
			warnings.push(
				`Primary stream interrupted after first tokens (${primaryProvider.provider}); returned partial response.`,
			);
		} else {
			const primaryError = error instanceof Error ? error.message : String(error);
			warnings.push(
				`Primary provider failed before first token (${primaryProvider.provider}): ${primaryError}`,
			);

			if (
				fallbackProvider.provider === primaryProvider.provider &&
				fallbackProvider.model === primaryProvider.model
			) {
				warnings.push(
					`Fallback provider is identical to primary (${primaryProvider.provider}/${primaryProvider.model}); returning graceful failure message.`,
				);
				answer = GENERATION_FAILURE_MESSAGE;
			} else {
				fallbackTriggered = true;
				try {
					const fallback = await requestStreamingCompletion({
						provider: fallbackProvider,
						messages,
					});

					providerUsed = fallbackProvider.provider;
					modelUsed = fallbackProvider.model;
					answer = fallback.text;
					ttftMs = fallback.ttftMs;
				} catch (fallbackError) {
					const fallbackErrorMessage =
						fallbackError instanceof Error ? fallbackError.message : String(fallbackError);
					warnings.push(
						`Fallback provider failed before first token (${fallbackProvider.provider}): ${fallbackErrorMessage}`,
					);
					providerUsed = fallbackProvider.provider;
					modelUsed = fallbackProvider.model;
					answer = GENERATION_FAILURE_MESSAGE;
					ttftMs = 0;
				}
			}
		}
	}

	const generationMs = Date.now() - generationStart;
	const charsPerSecond = generationMs > 0 ? answer.length / (generationMs / 1000) : 0;

	return {
		answer,
		generationMeta: {
			providerConfigured: config.provider,
			modelConfigured: config.model,
			fallbackProviderConfigured: config.fallback_provider,
			fallbackModelConfigured: config.fallback_model,
			providerUsed,
			modelUsed,
			fallbackTriggered,
			promptNameUsed: config.system_prompt_name,
			systemPromptUsed: systemPrompt,
			fullPrompt,
			generationMs,
			ttftMs,
			charsPerSecond,
			responseLengthTokens: estimateTokens(answer),
			warnings,
		},
	};
}

export const generatorStep = createStep({
	id: "generator",
	description: "Generate final answer from built context with streaming-compatible fallback",
	inputSchema: generatorStepInputSchema,
	outputSchema: generatorStepOutputSchema,
	stateSchema: generatorStateSchema,
	execute: async ({ inputData, state, setState }) => {
		const runtimeGenerationConfig = await loadRuntimeGenerationConfigSafe();
		const result = await runGenerator(inputData, runtimeGenerationConfig);

		await setState({
			...state,
			generator: result,
		});

		return result;
	},
});

export { generationConfigSchema };
