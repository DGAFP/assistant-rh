import { z } from "zod";
import { loadAcronymMap, loadRuntimeConfig, loadSystemPrompt } from "./db";

const retrievalSchema = z
	.object({
		search_mode: z.enum(["semantic", "lexical", "hybrid"]),
		embedding_model: z.enum(["albert", "bge_scaleway"]),
		initial_top_k: z.number().int().positive(),
		alpha: z.number(),
		tables: z.array(z.string()),
		enable_chunks_test: z.boolean(),
	})
	.partial();

const aggregationSchema = z
	.object({
		weight_max_score: z.number(),
		weight_mean_score: z.number(),
		weight_chunk_count: z.number(),
		enable_section_reranker: z.boolean(),
		section_rerank_top_k: z.number().int().positive(),
	})
	.partial();

const contextSchema = z
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

const selectorSchema = z
	.object({
		enabled: z.boolean(),
		provider: z.enum(["albert", "scaleway", "mistral"]),
		model: z.string(),
		temperature: z.number(),
		prompt_name: z.string(),
	})
	.partial();

const generationSchema = z
	.object({
		provider: z.enum(["albert", "scaleway", "mistral"]),
		model: z.string(),
		temperature: z.number(),
		system_prompt_name: z.string(),
		fallback_provider: z.enum(["albert", "scaleway", "mistral"]),
		fallback_model: z.string(),
	})
	.partial();

const queryProcessorSchema = z
	.object({
		enable_acronym_expansion: z.boolean(),
		enable_intent_gating: z.boolean(),
		intent_model: z.string(),
		intent_prompt_name: z.string(),
	})
	.partial();

const runtimeConfigSchema = z
	.object({
		retrieval: retrievalSchema,
		aggregation: aggregationSchema,
		context: contextSchema,
		selector: selectorSchema,
		generation: generationSchema,
		query_processor: queryProcessorSchema,
		verbose: z.boolean(),
	})
	.partial();

export type RuntimeRagConfig = z.infer<typeof runtimeConfigSchema>;

export const DEFAULT_RUNTIME_RAG_CONFIG: Required<RuntimeRagConfig> = {
	retrieval: {
		search_mode: "semantic",
		embedding_model: "albert",
		initial_top_k: 15,
		alpha: 0.5,
		tables: ["matte", "service_public", "dgafp", "rgrh"],
		enable_chunks_test: false,
	},
	aggregation: {
		weight_max_score: 0.5,
		weight_mean_score: 0.3,
		weight_chunk_count: 0.2,
		enable_section_reranker: true,
		section_rerank_top_k: 10,
	},
	context: {
		context_mode: "standard",
		token_budget: 8000,
		max_full_docs: 1,
		doc_entire_threshold: 3500,
		max_sections: 12,
		triangulation_sections: 2,
		legal_refs_budget: 1000,
		token_budget_wide: 12000,
		max_full_docs_wide: 2,
		doc_entire_threshold_wide: 5000,
		max_sections_wide: 20,
		legal_refs_budget_wide: 2000,
	},
	selector: {
		enabled: false,
		provider: "albert",
		model: "openweight-large",
		temperature: 0,
		prompt_name: "v3_selector_business.md",
	},
	generation: {
		provider: "albert",
		model: "openweight-large",
		temperature: 0,
		system_prompt_name: "system_prompt_V6_optimized.md",
		fallback_provider: "scaleway",
		fallback_model: "llama-3.1-70b-instruct",
	},
	query_processor: {
		enable_acronym_expansion: true,
		enable_intent_gating: true,
		intent_model: "openweight-medium",
		intent_prompt_name: "intent_unified.md",
	},
	verbose: false,
};

export const promptSourceModeSchema = z.enum(["db_first", "mastra_only"]);

export type PromptSourceMode = z.infer<typeof promptSourceModeSchema>;

const DEFAULT_PROMPT_SOURCE_MODE: PromptSourceMode = "db_first";

function deepMergeRuntimeConfig(
	base: Required<RuntimeRagConfig>,
	patch: RuntimeRagConfig,
): Required<RuntimeRagConfig> {
	return {
		retrieval: { ...base.retrieval, ...(patch.retrieval ?? {}) },
		aggregation: { ...base.aggregation, ...(patch.aggregation ?? {}) },
		context: { ...base.context, ...(patch.context ?? {}) },
		selector: { ...base.selector, ...(patch.selector ?? {}) },
		generation: { ...base.generation, ...(patch.generation ?? {}) },
		query_processor: {
			...base.query_processor,
			...(patch.query_processor ?? {}),
		},
		verbose: patch.verbose ?? base.verbose,
	};
}

export function getPromptSourceMode(env: NodeJS.ProcessEnv = process.env): PromptSourceMode {
	const parsed = promptSourceModeSchema.safeParse(env.PROMPT_SOURCE_MODE);
	return parsed.success ? parsed.data : DEFAULT_PROMPT_SOURCE_MODE;
}

export async function getRuntimeRagConfig(): Promise<Required<RuntimeRagConfig>> {
	const rawConfig = await loadRuntimeConfig(1);
	const parsed = runtimeConfigSchema.safeParse(rawConfig);

	if (!parsed.success) {
		return DEFAULT_RUNTIME_RAG_CONFIG;
	}

	return deepMergeRuntimeConfig(DEFAULT_RUNTIME_RAG_CONFIG, parsed.data);
}

export interface PromptLoadOptions {
	fallbackContent?: string | null;
	sourceMode?: PromptSourceMode;
}

/**
 * DB-first prompt resolver for side-by-side compatibility with Python pipeline.
 *
 * Future migration path:
 * - switch `PROMPT_SOURCE_MODE=mastra_only` once prompt management moves to
 *   Mastra Studio or agent-level configuration.
 */
export async function resolvePrompt(
	name: string,
	options: PromptLoadOptions = {},
): Promise<string | null> {
	const sourceMode = options.sourceMode ?? getPromptSourceMode();

	if (sourceMode === "mastra_only") {
		return options.fallbackContent ?? null;
	}

	const dbPrompt = await loadSystemPrompt(name);
	if (dbPrompt) {
		return dbPrompt;
	}

	return options.fallbackContent ?? null;
}

export async function getAcronymDictionary(): Promise<Record<string, string>> {
	return loadAcronymMap();
}
