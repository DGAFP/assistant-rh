import { createStep } from "@mastra/core/workflows";
import { z } from "zod";
import { getAlbertBaseUrl, getScalewayBaseUrl } from "../lib/albert";
import {
	DEFAULT_RUNTIME_RAG_CONFIG,
	getRuntimeRagConfig,
	type RuntimeRagConfig,
	resolvePrompt,
} from "../lib/config";
import { retrieverStepInputSchema } from "./retriever";
import {
	aggregatedSectionSchema,
	sectionAggregatorStateSchema,
	sectionAggregatorStepInputSchema,
} from "./section-aggregator";

const SELECTOR_FALLBACK_TOP_K = 5;

const DEFAULT_SELECTOR_PROMPT = `Tu es un expert en selection de contexte pour un assistant RH.

**Question :** {query}

**Sections disponibles :**
{context}

Selectionne les sections pertinentes pour repondre a la question.

Reponds UNIQUEMENT avec un JSON :
\`\`\`json
{{
  "selected_ids": [0, 2, 5],
  "reason": "Explication courte"
}}
\`\`\`
`;

const SELECTOR_ALL_REJECTED_MESSAGE =
	"Je n'ai pas trouvé d'informations suffisamment pertinentes dans ma base de connaissances " +
	"pour répondre à cette question. N'hésitez pas à reformuler votre question ou à contacter " +
	"votre service RH pour obtenir une réponse précise.";

export const contextBuildConfigSchema = z
	.object({
		context_mode: z.enum(["standard", "wide"]),
		token_budget: z.number().int().positive(),
		max_full_docs: z.number().int().nonnegative(),
		doc_entire_threshold: z.number().int().positive(),
		max_sections: z.number().int().positive(),
		triangulation_sections: z.number().int().nonnegative(),
		legal_refs_budget: z.number().int().nonnegative(),
		token_budget_wide: z.number().int().positive(),
		max_full_docs_wide: z.number().int().nonnegative(),
		doc_entire_threshold_wide: z.number().int().positive(),
		max_sections_wide: z.number().int().positive(),
		legal_refs_budget_wide: z.number().int().nonnegative(),
	})
	.partial();

export const selectorConfigSchema = z
	.object({
		enabled: z.boolean(),
		provider: z.enum(["albert", "scaleway", "mistral"]),
		model: z.string(),
		temperature: z.number(),
		prompt_name: z.string(),
	})
	.partial();

interface NormalizedSelectorConfig {
	enabled: boolean;
	provider: "albert" | "scaleway" | "mistral";
	model: string;
	temperature: number;
	prompt_name: string;
}

interface SelectorParseResult {
	ids: number[];
	explicitEmpty: boolean;
	parseFailed: boolean;
}

const selectorDecisionSchema = z.object({
	idx: z.number().int().nonnegative(),
	heading: z.string(),
	publisher: z.string(),
});

const selectorMetaSchema = z.object({
	enabled: z.boolean(),
	providerConfigured: z.enum(["albert", "scaleway", "mistral"]),
	providerUsed: z.enum(["albert", "scaleway", "mistral", "none"]),
	modelUsed: z.string(),
	promptNameUsed: z.string(),
	selectedCount: z.number().int().nonnegative(),
	removedCount: z.number().int().nonnegative(),
	kept: z.array(selectorDecisionSchema),
	removed: z.array(selectorDecisionSchema),
	reason: z.string().nullable(),
	rawResponse: z.string().nullable(),
	allRejected: z.boolean(),
	fallbackMode: z.enum([
		"none",
		"disabled",
		"parse_failure_top_k",
		"exception_passthrough",
		"explicit_all_rejected",
	]),
	warnings: z.array(z.string()),
});

export const contextSelectorStepInputSchema = z.object({
	queryForRetrieval: z.string(),
	sections: z.array(aggregatedSectionSchema),
	config: selectorConfigSchema.optional(),
});

export const contextSelectorStepOutputSchema = z.object({
	sections: z.array(aggregatedSectionSchema),
	selectorMeta: selectorMetaSchema,
	shortCircuit: z.boolean(),
	shortCircuitMessage: z.string().nullable(),
});

export const contextSelectorStateSchema = sectionAggregatorStateSchema.extend({
	config: z
		.object({
			retrieval: retrieverStepInputSchema.shape.config.optional(),
			aggregation: sectionAggregatorStepInputSchema.shape.config.optional(),
			selector: selectorConfigSchema.optional(),
			context: contextBuildConfigSchema.optional(),
		})
		.passthrough()
		.optional(),
	contextSelector: contextSelectorStepOutputSchema.optional(),
});

type SelectorConfigInput = z.infer<typeof selectorConfigSchema>;
type SelectorStepInput = z.infer<typeof contextSelectorStepInputSchema>;
type SelectorStepOutput = z.infer<typeof contextSelectorStepOutputSchema>;
type AggregatedSection = z.infer<typeof aggregatedSectionSchema>;

interface SelectorExecutionOptions {
	forcedRawResponse?: string;
}

function asNullableString(value: unknown): string | null {
	if (typeof value !== "string") {
		return null;
	}

	const normalized = value.trim();
	return normalized.length > 0 ? normalized : null;
}

function normalizeSelectorConfig(
	runtimeConfig: RuntimeRagConfig["selector"],
	override?: SelectorConfigInput,
): NormalizedSelectorConfig {
	const base: NormalizedSelectorConfig = {
		enabled: false,
		provider: "albert",
		model: "openweight-large",
		temperature: 0,
		prompt_name: "v3_selector_business.md",
	};

	return {
		enabled: override?.enabled ?? runtimeConfig?.enabled ?? base.enabled,
		provider: override?.provider ?? runtimeConfig?.provider ?? base.provider,
		model: override?.model ?? runtimeConfig?.model ?? base.model,
		temperature: override?.temperature ?? runtimeConfig?.temperature ?? base.temperature,
		prompt_name: override?.prompt_name ?? runtimeConfig?.prompt_name ?? base.prompt_name,
	};
}

async function loadRuntimeSelectorConfigSafe(): Promise<RuntimeRagConfig["selector"]> {
	try {
		const runtimeConfig = await getRuntimeRagConfig();
		return runtimeConfig.selector;
	} catch {
		return DEFAULT_RUNTIME_RAG_CONFIG.selector;
	}
}

function buildSectionPromptContext(sections: AggregatedSection[]): string {
	return sections
		.map((section, idx) => {
			const heading = section.heading?.trim() || "(Sans titre)";
			const publisher = section.publisher?.trim() || "unknown";
			return `[${idx}] ${heading} (${publisher})\n${section.markdown}`;
		})
		.join("\n\n---\n\n");
}

function parseSelectorResponse(rawResponse: string, sectionCount: number): SelectorParseResult {
	const fenced = rawResponse.match(/```(?:json)?\s*([\s\S]*?)\s*```/i)?.[1];
	const text = (fenced ?? rawResponse).trim();

	let parsed: Record<string, unknown>;
	try {
		const json = JSON.parse(text) as unknown;
		if (!json || typeof json !== "object" || Array.isArray(json)) {
			return { ids: [], explicitEmpty: false, parseFailed: true };
		}
		parsed = json as Record<string, unknown>;
	} catch {
		return { ids: [], explicitEmpty: false, parseFailed: true };
	}

	const rawIds = parsed.selected_ids ?? parsed.selected_indices ?? parsed.selected_ordered;

	if (rawIds === undefined || rawIds === null) {
		return { ids: [], explicitEmpty: true, parseFailed: false };
	}

	if (!Array.isArray(rawIds)) {
		return { ids: [], explicitEmpty: false, parseFailed: true };
	}

	if (rawIds.length === 0) {
		return { ids: [], explicitEmpty: true, parseFailed: false };
	}

	const extracted: number[] = [];
	for (const raw of rawIds) {
		if (Number.isInteger(raw)) {
			extracted.push(raw as number);
			continue;
		}

		if (typeof raw === "string") {
			const digits = raw.replace(/[^0-9]/g, "");
			if (digits.length > 0) {
				extracted.push(Number.parseInt(digits, 10));
			}
		}
	}

	const filtered = extracted.filter((value) => value >= 0 && value < sectionCount);

	return {
		ids: filtered,
		explicitEmpty: false,
		parseFailed: filtered.length === 0,
	};
}

function parseSelectorReason(rawResponse: string): string | null {
	const fenced = rawResponse.match(/```(?:json)?\s*([\s\S]*?)\s*```/i)?.[1];
	const text = (fenced ?? rawResponse).trim();

	try {
		const json = JSON.parse(text) as unknown;
		if (!json || typeof json !== "object" || Array.isArray(json)) {
			return null;
		}

		return asNullableString((json as Record<string, unknown>).reason);
	} catch {
		return null;
	}
}

function toDecision(
	section: AggregatedSection,
	idx: number,
): { idx: number; heading: string; publisher: string } {
	return {
		idx,
		heading: (section.heading ?? "").slice(0, 80),
		publisher: section.publisher ?? "",
	};
}

function requiredEnv(name: string): string {
	const value = process.env[name];
	if (!value) {
		throw new Error(`Missing required environment variable: ${name}`);
	}
	return value;
}

function resolveProviderConfig(provider: NormalizedSelectorConfig["provider"]): {
	baseUrl: string;
	apiKey: string;
} {
	if (provider === "scaleway") {
		return {
			baseUrl: getScalewayBaseUrl(),
			apiKey: requiredEnv("SCALEWAY_API_KEY"),
		};
	}

	if (provider === "mistral") {
		return {
			baseUrl: process.env.OPENAI_BASE_URL ?? "https://api.openai.com/v1",
			apiKey: requiredEnv("OPENAI_API_KEY"),
		};
	}

	return {
		baseUrl: getAlbertBaseUrl(),
		apiKey: requiredEnv("ALBERT_API_KEY"),
	};
}

async function requestSelectorCompletion(args: {
	provider: NormalizedSelectorConfig["provider"];
	model: string;
	temperature: number;
	prompt: string;
}): Promise<string> {
	const providerConfig = resolveProviderConfig(args.provider);
	const response = await fetch(`${providerConfig.baseUrl.replace(/\/$/, "")}/chat/completions`, {
		method: "POST",
		headers: {
			"Content-Type": "application/json",
			Authorization: `Bearer ${providerConfig.apiKey}`,
		},
		body: JSON.stringify({
			model: args.model,
			messages: [{ role: "user", content: args.prompt }],
			temperature: args.temperature,
			stream: false,
		}),
		signal: AbortSignal.timeout(30_000),
	});

	if (!response.ok) {
		throw new Error(`Selector completion failed with HTTP ${response.status}`);
	}

	const body = (await response.json()) as {
		choices?: Array<{ message?: { content?: unknown } }>;
	};

	const content = body.choices?.[0]?.message?.content;
	if (typeof content !== "string" || content.trim().length === 0) {
		throw new Error("Selector completion returned empty content");
	}

	return content;
}

export async function runContextSelector(
	input: SelectorStepInput,
	runtimeSelectorConfigOverride?: RuntimeRagConfig["selector"],
	executionOptions?: SelectorExecutionOptions,
): Promise<SelectorStepOutput> {
	const runtimeSelectorConfig =
		runtimeSelectorConfigOverride ?? (await loadRuntimeSelectorConfigSafe());
	const config = normalizeSelectorConfig(runtimeSelectorConfig, input.config);

	if (!config.enabled || input.sections.length === 0) {
		return {
			sections: input.sections,
			shortCircuit: false,
			shortCircuitMessage: null,
			selectorMeta: {
				enabled: config.enabled,
				providerConfigured: config.provider,
				providerUsed: "none",
				modelUsed: config.model,
				promptNameUsed: config.prompt_name,
				selectedCount: input.sections.length,
				removedCount: 0,
				kept: input.sections.map((section, idx) => toDecision(section, idx)),
				removed: [],
				reason: null,
				rawResponse: null,
				allRejected: false,
				fallbackMode: "disabled",
				warnings: [],
			},
		};
	}

	const promptTemplate =
		(await resolvePrompt(config.prompt_name, {
			fallbackContent: DEFAULT_SELECTOR_PROMPT,
		})) ?? DEFAULT_SELECTOR_PROMPT;

	const prompt = promptTemplate
		.replaceAll("{query}", input.queryForRetrieval)
		.replaceAll("{context}", buildSectionPromptContext(input.sections))
		.replaceAll("{theme}", "");

	try {
		const providerUsed = executionOptions?.forcedRawResponse ? "none" : config.provider;

		const rawResponse =
			executionOptions?.forcedRawResponse ??
			(await requestSelectorCompletion({
				provider: config.provider,
				model: config.model,
				temperature: config.temperature,
				prompt,
			}));

		const parseResult = parseSelectorResponse(rawResponse, input.sections.length);
		const reason = parseSelectorReason(rawResponse);

		if (parseResult.explicitEmpty) {
			return {
				sections: [],
				shortCircuit: true,
				shortCircuitMessage: SELECTOR_ALL_REJECTED_MESSAGE,
				selectorMeta: {
					enabled: config.enabled,
					providerConfigured: config.provider,
					providerUsed,
					modelUsed: config.model,
					promptNameUsed: config.prompt_name,
					selectedCount: 0,
					removedCount: input.sections.length,
					kept: [],
					removed: input.sections.map((section, idx) => toDecision(section, idx)),
					reason,
					rawResponse,
					allRejected: true,
					fallbackMode: "explicit_all_rejected",
					warnings: [],
				},
			};
		}

		if (parseResult.parseFailed) {
			const fallbackSections = input.sections.slice(0, SELECTOR_FALLBACK_TOP_K);
			return {
				sections: fallbackSections,
				shortCircuit: false,
				shortCircuitMessage: null,
				selectorMeta: {
					enabled: config.enabled,
					providerConfigured: config.provider,
					providerUsed,
					modelUsed: config.model,
					promptNameUsed: config.prompt_name,
					selectedCount: fallbackSections.length,
					removedCount: Math.max(input.sections.length - fallbackSections.length, 0),
					kept: fallbackSections.map((section, idx) => toDecision(section, idx)),
					removed: input.sections
						.slice(fallbackSections.length)
						.map((section, idx) => toDecision(section, idx + fallbackSections.length)),
					reason,
					rawResponse,
					allRejected: false,
					fallbackMode: "parse_failure_top_k",
					warnings: [
						`Selector parse failed; fell back to top ${SELECTOR_FALLBACK_TOP_K} sections.`,
					],
				},
			};
		}

		const keptSet = new Set(parseResult.ids);
		const removedIds = Array.from({ length: input.sections.length }, (_, idx) => idx).filter(
			(idx) => !keptSet.has(idx),
		);

		const filtered = parseResult.ids
			.map((idx) => input.sections[idx])
			.filter((section): section is AggregatedSection => Boolean(section));

		const selectedSections = filtered.length > 0 ? filtered : input.sections;

		return {
			sections: selectedSections,
			shortCircuit: false,
			shortCircuitMessage: null,
			selectorMeta: {
				enabled: config.enabled,
				providerConfigured: config.provider,
				providerUsed,
				modelUsed: config.model,
				promptNameUsed: config.prompt_name,
				selectedCount: selectedSections.length,
				removedCount: selectedSections === input.sections ? 0 : removedIds.length,
				kept:
					selectedSections === input.sections
						? input.sections.map((section, idx) => toDecision(section, idx))
						: parseResult.ids.map((idx) => toDecision(input.sections[idx], idx)),
				removed:
					selectedSections === input.sections
						? []
						: removedIds.map((idx) => toDecision(input.sections[idx], idx)),
				reason,
				rawResponse,
				allRejected: false,
				fallbackMode: "none",
				warnings: [],
			},
		};
	} catch (error) {
		const message = error instanceof Error ? error.message : "Unknown selector execution error";

		return {
			sections: input.sections,
			shortCircuit: false,
			shortCircuitMessage: null,
			selectorMeta: {
				enabled: config.enabled,
				providerConfigured: config.provider,
				providerUsed: "none",
				modelUsed: config.model,
				promptNameUsed: config.prompt_name,
				selectedCount: input.sections.length,
				removedCount: 0,
				kept: input.sections.map((section, idx) => toDecision(section, idx)),
				removed: [],
				reason: null,
				rawResponse: null,
				allRejected: false,
				fallbackMode: "exception_passthrough",
				warnings: [`Context selector failed; kept all sections: ${message}`],
			},
		};
	}
}

export const contextSelectorStep = createStep({
	id: "context-selector",
	description: "Filter aggregated sections with optional LLM selector",
	inputSchema: contextSelectorStepInputSchema,
	outputSchema: contextSelectorStepOutputSchema,
	stateSchema: contextSelectorStateSchema,
	execute: async ({ inputData, state, setState }) => {
		const runtimeSelectorConfig = await loadRuntimeSelectorConfigSafe();
		const result = await runContextSelector(inputData, runtimeSelectorConfig);

		await setState({
			...state,
			contextSelector: result,
		});

		return result;
	},
});
