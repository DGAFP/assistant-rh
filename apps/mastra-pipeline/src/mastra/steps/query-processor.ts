import { createStep } from "@mastra/core/workflows";
import { z } from "zod";
import { getAlbertBaseUrl, getScalewayBaseUrl, withAlbertFallback } from "../lib/albert";
import {
	DEFAULT_RUNTIME_RAG_CONFIG,
	getAcronymDictionary,
	getRuntimeRagConfig,
	resolvePrompt,
} from "../lib/config";

const FALLBACK_INTENT_PROMPT = `Tu es un classificateur d'intention RH.
Historique:
{history}

Question:
"{query}"

Acronymes détectés:
{acronyms_section}

Réponds uniquement en JSON avec ces clés:
intent, theme, needs_legal_search, reformulated_query, query_for_retrieval, confidence, reasoning.`;

const INTENTS = [
	"rag_query",
	"chit_chat",
	"out_of_scope",
	"clarification",
	"follow_up",
	"document_request",
] as const;

const THEMES = [
	"recrutement",
	"typologie_contrats",
	"remuneration",
	"renouvellement_mobilite",
	"fin_contrat_licenciement",
	"temps_de_travail",
	"conges",
	"formation",
	"action_sociale",
	"psc",
	"sante_securite",
	"retraite",
	"apprentis",
	"deontologie",
	"autre",
] as const;

const BETA_EXCLUDED_THEMES = new Set<string>(["action_sociale", "psc", "retraite", "apprentis"]);

const DIRECT_RESPONSES: Record<string, string> = {
	chit_chat:
		"Bonjour, je suis l'Assistant RH specialise sur les questions liees aux contractuels de la fonction publique d'Etat (FPE). Comment puis-je vous aider ?",
	out_of_scope:
		"Je suis specialise sur les questions liees aux contractuels de la fonction publique d'Etat (FPE). Puis-je vous aider sur un sujet RH (contrats, conges, remuneration, fin de contrat...) ?",
	clarification:
		"Je n'ai pas bien compris votre question. Pourriez-vous la preciser ? Par exemple : sur quel type de contrat, de conge, ou de situation vous souhaitez des informations ?",
	document_request:
		"Je ne suis pas en mesure de vous donner directement acces aux documents. Posez-moi plutot une question RH et je pourrai vous guider vers les bonnes sources.",
};

const conversationMessageSchema = z.object({
	role: z.enum(["user", "assistant", "system"]),
	content: z.string(),
});

const intentSchema = z.enum(INTENTS);
const themeSchema = z.enum(THEMES);

export const queryProcessorStepInputSchema = z.object({
	query: z.string().min(1),
	conversationHistory: z.array(conversationMessageSchema).optional(),
});

export const queryProcessorStepOutputSchema = z.object({
	originalQuery: z.string(),
	processedQuery: z.string(),
	reformulatedQuery: z.string().nullable(),
	queryForRetrieval: z.string(),
	expandedAcronyms: z.array(z.string()),
	detectedAcronyms: z.record(z.string(), z.string()),
	wasExpanded: z.boolean(),
	isInScope: z.boolean(),
	shouldProceed: z.boolean(),
	intent: intentSchema,
	intentConfidence: z.number(),
	intentReason: z.string().nullable(),
	needsLegalSearch: z.boolean(),
	theme: themeSchema.nullable(),
	isBetaExcludedTheme: z.boolean(),
	requestedSource: z.enum(["MATTE", "Service-Public"]).nullable(),
	isCatalogQuery: z.boolean(),
	catalogKeyword: z.string().nullable(),
	directResponse: z.string().nullable(),
	intentRawResponse: z.string().nullable(),
	providerUsed: z.enum(["albert", "scaleway", "none"]),
	promptNameUsed: z.string(),
});

export const queryProcessorStateSchema = z.object({
	query: z.string().optional(),
	conversationHistory: z.array(conversationMessageSchema).optional(),
	config: z.object({}).passthrough().optional(),
	queryProcessor: queryProcessorStepOutputSchema.optional(),
	branchPath: z.enum(["rag", "non_rag"]).optional(),
});

type QueryProcessorResult = z.infer<typeof queryProcessorStepOutputSchema>;
type ConversationMessage = z.infer<typeof conversationMessageSchema>;

interface QueryProcessorExecutionOptions {
	forcedIntentRawResponse?: string;
}

function escapeRegex(value: string): string {
	return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function detectAcronyms(query: string, acronyms: Record<string, string>): Record<string, string> {
	const detected: Record<string, string> = {};

	for (const [acronym, expansion] of Object.entries(acronyms)) {
		if (new RegExp(`\\b${escapeRegex(acronym)}\\b`).test(query)) {
			detected[acronym] = expansion;
		}
	}

	return detected;
}

function expandAcronymsInline(query: string, detected: Record<string, string>): string {
	let expanded = query;

	for (const [acronym, expansion] of Object.entries(detected)) {
		expanded = expanded.replace(
			new RegExp(`\\b${escapeRegex(acronym)}\\b`, "g"),
			`${acronym} (${expansion})`,
		);
	}

	return expanded;
}

function formatConversationHistory(conversationHistory: ConversationMessage[] | undefined): string {
	if (!conversationHistory || conversationHistory.length < 2) {
		return "(Pas d'historique de conversation)";
	}

	return conversationHistory
		.slice(-8)
		.map((message) => {
			const role = message.role === "user" ? "Utilisateur" : "Assistant";
			const content =
				message.content.length > 300 ? `${message.content.slice(0, 300)}...` : message.content;
			return `${role}: ${content}`;
		})
		.join("\n");
}

function formatAcronymSection(detected: Record<string, string>): string {
	const entries = Object.entries(detected);
	if (entries.length === 0) {
		return "(Aucun acronyme detecte)";
	}

	const lines = entries.map(([acronym, expansion]) => `- **${acronym}** = ${expansion}`);
	return `Les acronymes suivants ont ete detectes (en MAJUSCULES) :\n${lines.join("\n")}`;
}

function renderIntentPrompt(
	template: string,
	params: {
		history: string;
		query: string;
		acronymsSection: string;
	},
): string {
	return template
		.replace("{history}", params.history)
		.replace("{query}", params.query)
		.replace("{acronyms_section}", params.acronymsSection);
}

function asNullableString(value: unknown): string | null {
	return typeof value === "string" && value.trim().length > 0 ? value.trim() : null;
}

function parseJsonObjectFromModel(rawResponse: string): Record<string, unknown> {
	const fenced = rawResponse.match(/```(?:json)?\s*([\s\S]*?)\s*```/i)?.[1];
	let text = (fenced ?? rawResponse).trim();

	if (text.startsWith("```") && text.endsWith("```")) {
		text = text.slice(3, -3).trim();
	}

	if (text.toLowerCase().startsWith("json")) {
		text = text.slice(4).trim();
	}

	const parsed = JSON.parse(text) as unknown;
	if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
		throw new Error("Intent classifier response is not a JSON object");
	}

	return parsed as Record<string, unknown>;
}

async function requestIntentClassification(args: {
	baseUrl: string;
	apiKey: string;
	model: string;
	prompt: string;
}): Promise<string> {
	const response = await fetch(`${args.baseUrl.replace(/\/$/, "")}/chat/completions`, {
		method: "POST",
		headers: {
			"Content-Type": "application/json",
			Authorization: `Bearer ${args.apiKey}`,
		},
		body: JSON.stringify({
			model: args.model,
			messages: [{ role: "user", content: args.prompt }],
			temperature: 0,
			stream: false,
		}),
		signal: AbortSignal.timeout(30_000),
	});

	if (!response.ok) {
		throw new Error(`Intent classification failed with HTTP ${response.status}`);
	}

	const body = (await response.json()) as {
		choices?: Array<{ message?: { content?: unknown } }>;
	};

	const content = body.choices?.[0]?.message?.content;
	if (typeof content !== "string" || content.trim().length === 0) {
		throw new Error("Intent classification returned empty content");
	}

	return content;
}

function normalizeTheme(value: unknown): (typeof THEMES)[number] | null {
	if (typeof value !== "string" || value.trim().length === 0) {
		return null;
	}

	const candidate = value.trim();
	if ((THEMES as readonly string[]).includes(candidate)) {
		return candidate as (typeof THEMES)[number];
	}

	return "autre";
}

function normalizeIntent(value: unknown): (typeof INTENTS)[number] {
	if (typeof value === "string" && (INTENTS as readonly string[]).includes(value)) {
		return value as (typeof INTENTS)[number];
	}

	return "rag_query";
}

function parseConfidence(value: unknown, fallback: number): number {
	if (typeof value === "number" && Number.isFinite(value)) {
		return value;
	}

	if (typeof value === "string") {
		const parsed = Number.parseFloat(value);
		if (Number.isFinite(parsed)) {
			return parsed;
		}
	}

	return fallback;
}

function buildFallbackResult(args: {
	query: string;
	detectedAcronyms: Record<string, string>;
	reason: string;
	promptNameUsed: string;
}): QueryProcessorResult {
	return {
		originalQuery: args.query,
		processedQuery: args.query,
		reformulatedQuery: null,
		queryForRetrieval: args.query,
		expandedAcronyms: [],
		detectedAcronyms: args.detectedAcronyms,
		wasExpanded: false,
		isInScope: true,
		shouldProceed: true,
		intent: "rag_query",
		intentConfidence: 0.5,
		intentReason: args.reason,
		needsLegalSearch: false,
		theme: null,
		isBetaExcludedTheme: false,
		requestedSource: null,
		isCatalogQuery: false,
		catalogKeyword: null,
		directResponse: null,
		intentRawResponse: null,
		providerUsed: "none",
		promptNameUsed: args.promptNameUsed,
	};
}

async function loadRuntimeConfigSafe() {
	try {
		return await getRuntimeRagConfig();
	} catch {
		return DEFAULT_RUNTIME_RAG_CONFIG;
	}
}

export async function runQueryProcessor(
	input: z.infer<typeof queryProcessorStepInputSchema>,
	runtimeConfigOverride?: Awaited<ReturnType<typeof getRuntimeRagConfig>>,
	executionOptions?: QueryProcessorExecutionOptions,
): Promise<QueryProcessorResult> {
	const query = input.query;
	const conversationHistory = input.conversationHistory ?? [];

	const runtimeConfig = runtimeConfigOverride ?? (await loadRuntimeConfigSafe());
	const queryProcessorConfig = runtimeConfig.query_processor;

	const acronymMap = queryProcessorConfig.enable_acronym_expansion
		? await getAcronymDictionary().catch(() => ({}))
		: {};

	const detectedAcronyms = detectAcronyms(query, acronymMap);
	const promptName = queryProcessorConfig.intent_prompt_name || "intent_unified.md";

	if (!queryProcessorConfig.enable_intent_gating) {
		const expandedQuery = expandAcronymsInline(query, detectedAcronyms);
		const wasExpanded = expandedQuery !== query;

		return {
			originalQuery: query,
			processedQuery: expandedQuery,
			reformulatedQuery: null,
			queryForRetrieval: expandedQuery,
			expandedAcronyms: wasExpanded ? Object.keys(detectedAcronyms) : [],
			detectedAcronyms,
			wasExpanded,
			isInScope: true,
			shouldProceed: true,
			intent: "rag_query",
			intentConfidence: 1,
			intentReason: null,
			needsLegalSearch: false,
			theme: null,
			isBetaExcludedTheme: false,
			requestedSource: null,
			isCatalogQuery: false,
			catalogKeyword: null,
			directResponse: null,
			intentRawResponse: null,
			providerUsed: "none",
			promptNameUsed: promptName,
		};
	}

	const promptTemplate =
		(await resolvePrompt(promptName, { fallbackContent: FALLBACK_INTENT_PROMPT }).catch(
			() => FALLBACK_INTENT_PROMPT,
		)) ?? FALLBACK_INTENT_PROMPT;

	const prompt = renderIntentPrompt(promptTemplate, {
		history: formatConversationHistory(conversationHistory),
		query,
		acronymsSection: formatAcronymSection(detectedAcronyms),
	});

	try {
		const fallbackModel = runtimeConfig.generation.fallback_model ?? "llama-3.1-70b-instruct";
		const classifierModel = queryProcessorConfig.intent_model ?? "openweight-medium";

		const completion: { value: string; provider: "albert" | "scaleway" | "none" } =
			executionOptions?.forcedIntentRawResponse
				? {
						value: executionOptions.forcedIntentRawResponse,
						provider: "none",
					}
				: await withAlbertFallback({
						runAlbert: () =>
							requestIntentClassification({
								baseUrl: getAlbertBaseUrl(),
								apiKey: process.env.ALBERT_API_KEY ?? "",
								model: classifierModel,
								prompt,
							}),
						runScaleway: () =>
							requestIntentClassification({
								baseUrl: getScalewayBaseUrl(),
								apiKey: process.env.SCALEWAY_API_KEY ?? "",
								model: fallbackModel,
								prompt,
							}),
					});

		const parsed = parseJsonObjectFromModel(completion.value);

		const intent = normalizeIntent(parsed.intent);
		const theme = normalizeTheme(parsed.theme);
		const reformulatedQuery = asNullableString(parsed.reformulated_query);
		const llmQueryForRetrieval = asNullableString(parsed.query_for_retrieval);
		const processedQuery = reformulatedQuery ?? llmQueryForRetrieval ?? query;
		const finalQueryForRetrieval = llmQueryForRetrieval ?? reformulatedQuery ?? query;

		const expandedAcronyms = Object.keys(detectedAcronyms);

		const isInScope = intent === "rag_query" || intent === "follow_up";

		return {
			originalQuery: query,
			processedQuery,
			reformulatedQuery,
			queryForRetrieval: finalQueryForRetrieval,
			expandedAcronyms,
			detectedAcronyms,
			wasExpanded: expandedAcronyms.length > 0,
			isInScope,
			shouldProceed: isInScope,
			intent,
			intentConfidence: parseConfidence(parsed.confidence, 0.8),
			intentReason: asNullableString(parsed.reasoning),
			needsLegalSearch: Boolean(parsed.needs_legal_search),
			theme,
			isBetaExcludedTheme: theme !== null && BETA_EXCLUDED_THEMES.has(theme),
			requestedSource:
				parsed.requested_source === "MATTE" || parsed.requested_source === "Service-Public"
					? parsed.requested_source
					: null,
			isCatalogQuery: Boolean(parsed.is_catalog_query),
			catalogKeyword: asNullableString(parsed.catalog_keyword),
			directResponse: isInScope ? null : (DIRECT_RESPONSES[intent] ?? null),
			intentRawResponse: completion.value,
			providerUsed: completion.provider,
			promptNameUsed: promptName,
		};
	} catch (error) {
		const reason = error instanceof Error ? error.message : String(error);
		return buildFallbackResult({
			query,
			detectedAcronyms,
			reason,
			promptNameUsed: promptName,
		});
	}
}

export const queryProcessorStep = createStep({
	id: "query-processor",
	description: "Classify intent and prepare retrieval query",
	inputSchema: queryProcessorStepInputSchema,
	outputSchema: queryProcessorStepOutputSchema,
	stateSchema: queryProcessorStateSchema,
	execute: async ({ inputData, state, setState }) => {
		const runtimeConfig = await loadRuntimeConfigSafe();
		const result = await runQueryProcessor(inputData, runtimeConfig);

		await setState({
			...state,
			query: inputData.query,
			conversationHistory: inputData.conversationHistory ?? [],
			config: runtimeConfig,
			queryProcessor: result,
		});

		return result;
	},
});
