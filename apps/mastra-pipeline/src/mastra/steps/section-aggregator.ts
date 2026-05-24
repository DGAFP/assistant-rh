import { createStep } from "@mastra/core/workflows";
import { z } from "zod";
import { rerankWithAlbert } from "../lib/albert";
import {
	DEFAULT_RUNTIME_RAG_CONFIG,
	getRuntimeRagConfig,
	type RuntimeRagConfig,
} from "../lib/config";
import { getDbPool } from "../lib/db";
import { retrievedChunkSchema, retrieverStateSchema, retrieverStepInputSchema } from "./retriever";

const MAX_RERANK_INPUT = 20;
const MAX_RERANK_TEXT_LENGTH = 1500;
const UUID_V4_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

const aggregationConfigSchema = z
	.object({
		weight_max_score: z.number(),
		weight_mean_score: z.number(),
		weight_chunk_count: z.number(),
		enable_section_reranker: z.boolean(),
		section_rerank_top_k: z.number().int().positive(),
	})
	.partial();

interface NormalizedAggregationConfig {
	weight_max_score: number;
	weight_mean_score: number;
	weight_chunk_count: number;
	enable_section_reranker: boolean;
	section_rerank_top_k: number;
}

interface SectionMetadataRow {
	section_id: string;
	heading: string | null;
	section_markdown: string | null;
	heading_path: string | null;
	references_juridiques: unknown;
	doc_id: string | null;
	doc_short_id: string | null;
	doc_title: string | null;
	doc_url: string | null;
	doc_token_count: number | null;
	doc_publisher: string | null;
	doc_date: string | null;
}

const aggregationMetaSchema = z.object({
	weights: z.object({
		max: z.number(),
		mean: z.number(),
		count: z.number(),
	}),
	sectionCountBeforeRerank: z.number().int().nonnegative(),
	sectionCountAfterRerank: z.number().int().nonnegative(),
	rerankerEnabled: z.boolean(),
	rerankerApplied: z.boolean(),
	rerankerTopK: z.number().int().positive(),
	rerankerCandidateCount: z.number().int().nonnegative(),
	warnings: z.array(z.string()),
});

export const aggregatedSectionSchema = z.object({
	sectionId: z.string().nullable(),
	heading: z.string(),
	markdown: z.string(),
	chunks: z.array(retrievedChunkSchema),
	score: z.number(),
	documentId: z.string().nullable(),
	publisher: z.string().nullable(),
	referencesJuridiques: z.unknown().nullable(),
	headingPath: z.string().nullable(),
	metadata: z.record(z.string(), z.unknown()),
});

export const sectionAggregatorStepInputSchema = z.object({
	chunks: z.array(retrievedChunkSchema),
	queryForRetrieval: z.string().optional(),
	reformulatedQuery: z.string().optional(),
	config: aggregationConfigSchema.optional(),
});

export const sectionAggregatorStepOutputSchema = z.object({
	sections: z.array(aggregatedSectionSchema),
	aggregationMeta: aggregationMetaSchema,
});

export const sectionAggregatorStateSchema = retrieverStateSchema.extend({
	config: z
		.object({
			retrieval: retrieverStepInputSchema.shape.config.optional(),
			aggregation: aggregationConfigSchema.optional(),
		})
		.passthrough()
		.optional(),
	sectionAggregator: sectionAggregatorStepOutputSchema.optional(),
});

type AggregationConfigInput = z.infer<typeof aggregationConfigSchema>;
type SectionAggregatorInput = z.infer<typeof sectionAggregatorStepInputSchema>;
type SectionAggregatorOutput = z.infer<typeof sectionAggregatorStepOutputSchema>;
type AggregatedSection = z.infer<typeof aggregatedSectionSchema>;
type RetrievedChunk = z.infer<typeof retrievedChunkSchema>;

function asNullableString(value: unknown): string | null {
	if (typeof value !== "string") {
		return null;
	}

	const normalized = value.trim();
	return normalized.length > 0 ? normalized : null;
}

function asNumber(value: unknown, fallback = 0): number {
	if (typeof value === "number" && Number.isFinite(value)) {
		return value;
	}

	const parsed = Number.parseFloat(String(value));
	return Number.isFinite(parsed) ? parsed : fallback;
}

function normalizeReferences(value: unknown): unknown | null {
	if (value === null || value === undefined) {
		return null;
	}

	if (typeof value === "string") {
		const normalized = value.trim();
		if (normalized.length === 0) {
			return null;
		}

		try {
			return JSON.parse(normalized) as unknown;
		} catch {
			return normalized;
		}
	}

	if (typeof value === "object") {
		return value;
	}

	return null;
}

function getChunkMetadataString(chunk: RetrievedChunk, key: string): string | null {
	return asNullableString(chunk.metadata[key]);
}

function normalizeAggregationConfig(
	runtimeConfig: RuntimeRagConfig["aggregation"],
	override?: AggregationConfigInput,
): NormalizedAggregationConfig {
	const base: NormalizedAggregationConfig = {
		weight_max_score: 0.5,
		weight_mean_score: 0.3,
		weight_chunk_count: 0.2,
		enable_section_reranker: true,
		section_rerank_top_k: 10,
	};

	return {
		weight_max_score:
			override?.weight_max_score ?? runtimeConfig?.weight_max_score ?? base.weight_max_score,
		weight_mean_score:
			override?.weight_mean_score ?? runtimeConfig?.weight_mean_score ?? base.weight_mean_score,
		weight_chunk_count:
			override?.weight_chunk_count ?? runtimeConfig?.weight_chunk_count ?? base.weight_chunk_count,
		enable_section_reranker:
			override?.enable_section_reranker ??
			runtimeConfig?.enable_section_reranker ??
			base.enable_section_reranker,
		section_rerank_top_k:
			override?.section_rerank_top_k ??
			runtimeConfig?.section_rerank_top_k ??
			base.section_rerank_top_k,
	};
}

async function loadRuntimeAggregationConfigSafe(): Promise<RuntimeRagConfig["aggregation"]> {
	try {
		const runtimeConfig = await getRuntimeRagConfig();
		return runtimeConfig.aggregation;
	} catch {
		return DEFAULT_RUNTIME_RAG_CONFIG.aggregation;
	}
}

async function loadSectionMetadata(sectionIds: string[]): Promise<Map<string, SectionMetadataRow>> {
	const validSectionIds = Array.from(
		new Set(
			sectionIds
				.map((sectionId) => sectionId.trim())
				.filter((sectionId) => UUID_V4_RE.test(sectionId)),
		),
	);

	if (validSectionIds.length === 0) {
		return new Map();
	}

	const db = getDbPool();
	const result = await db.query<SectionMetadataRow>(
		`
      SELECT
        s.section_id::text AS section_id,
        s.heading,
        s.section_markdown,
        s.heading_path,
        s.references_juridiques,
        s.doc_id::text AS doc_id,
        d.short_id AS doc_short_id,
        d.title AS doc_title,
        d.source_url AS doc_url,
        d.token_count AS doc_token_count,
        d.publisher AS doc_publisher,
        d.last_updated_date::text AS doc_date
      FROM rag_sections s
      LEFT JOIN rag_documents d ON d.doc_id = s.doc_id
      WHERE s.section_id = ANY($1::uuid[])
    `,
		[validSectionIds],
	);

	return new Map(result.rows.map((row) => [row.section_id, row]));
}

async function rerankSections(
	query: string,
	sections: AggregatedSection[],
	topK: number,
): Promise<{ sections: AggregatedSection[]; applied: boolean }> {
	if (sections.length === 0) {
		return { sections, applied: false };
	}

	const candidates = sections.slice(0, MAX_RERANK_INPUT);
	const documents = candidates.map(
		(section) => `# ${section.heading}\n\n${section.markdown.slice(0, MAX_RERANK_TEXT_LENGTH)}`,
	);

	const ranked = await rerankWithAlbert({
		query,
		documents,
		topN: topK,
	});

	if (ranked.length === 0) {
		return {
			sections: sections.slice(0, topK),
			applied: false,
		};
	}

	const reranked: AggregatedSection[] = [];
	for (const row of ranked) {
		if (row.index < 0 || row.index >= candidates.length) {
			continue;
		}

		const section = candidates[row.index];
		reranked.push({
			...section,
			score: row.score,
		});
	}

	if (reranked.length === 0) {
		return {
			sections: sections.slice(0, topK),
			applied: false,
		};
	}

	return {
		sections: reranked.slice(0, topK),
		applied: true,
	};
}

export async function runSectionAggregator(
	input: SectionAggregatorInput,
	runtimeAggregationConfigOverride?: RuntimeRagConfig["aggregation"],
): Promise<SectionAggregatorOutput> {
	const runtimeAggregationConfig =
		runtimeAggregationConfigOverride ?? (await loadRuntimeAggregationConfigSafe());

	const config = normalizeAggregationConfig(runtimeAggregationConfig, input.config);

	if (input.chunks.length === 0) {
		return {
			sections: [],
			aggregationMeta: {
				weights: {
					max: config.weight_max_score,
					mean: config.weight_mean_score,
					count: config.weight_chunk_count,
				},
				sectionCountBeforeRerank: 0,
				sectionCountAfterRerank: 0,
				rerankerEnabled: config.enable_section_reranker,
				rerankerApplied: false,
				rerankerTopK: config.section_rerank_top_k,
				rerankerCandidateCount: 0,
				warnings: [],
			},
		};
	}

	const groups = new Map<string, RetrievedChunk[]>();
	for (const chunk of input.chunks) {
		const groupKey = chunk.sectionId ? chunk.sectionId : `_standalone_${chunk.chunkId}`;

		const existing = groups.get(groupKey);
		if (existing) {
			existing.push(chunk);
		} else {
			groups.set(groupKey, [chunk]);
		}
	}

	const maxChunkCount = Array.from(groups.values()).reduce(
		(max, group) => Math.max(max, group.length),
		0,
	);

	const sectionIds = Array.from(groups.keys()).filter(
		(groupKey) => !groupKey.startsWith("_standalone_"),
	);

	const warnings: string[] = [];
	let sectionMetadata = new Map<string, SectionMetadataRow>();
	try {
		sectionMetadata = await loadSectionMetadata(sectionIds);
	} catch (error) {
		const message =
			error instanceof Error ? error.message : "Unknown section metadata lookup error";
		warnings.push(`Section metadata lookup failed: ${message}`);
	}

	let sections: AggregatedSection[] = Array.from(groups.entries()).map(([groupKey, chunks]) => {
		const scores = chunks.map((chunk) => chunk.score);
		const maxScore = Math.max(...scores);
		const meanScore = scores.reduce((acc, value) => acc + value, 0) / scores.length;
		const normalizedCount = chunks.length / maxChunkCount;

		const score =
			config.weight_max_score * maxScore +
			config.weight_mean_score * meanScore +
			config.weight_chunk_count * normalizedCount;

		const firstChunk = chunks[0];
		const isStandalone = groupKey.startsWith("_standalone_");
		const meta = sectionMetadata.get(groupKey);

		const docId =
			asNullableString(meta?.doc_id) ?? getChunkMetadataString(firstChunk, "source_document_id");

		const docShortId =
			asNullableString(meta?.doc_short_id) ??
			getChunkMetadataString(firstChunk, "source_document_id");

		const metadata: Record<string, unknown> = {
			doc_id: docId ?? "",
			doc_short_id: docShortId ?? "",
			doc_title:
				asNullableString(meta?.doc_title) ??
				getChunkMetadataString(firstChunk, "source_name") ??
				"",
			doc_url: asNullableString(meta?.doc_url) ?? getChunkMetadataString(firstChunk, "url"),
			doc_publisher: asNullableString(meta?.doc_publisher) ?? firstChunk.tableSource,
			doc_date: asNullableString(meta?.doc_date) ?? "",
			doc_token_count: asNumber(meta?.doc_token_count, 0),
			chunk_count: chunks.length,
			max_chunk_score: maxScore,
			mean_chunk_score: meanScore,
		};

		if (isStandalone) {
			for (const key of ["number", "full_title", "title", "category", "cid"]) {
				const value = firstChunk.metadata[key];
				if (value !== undefined && value !== null && String(value).trim().length > 0) {
					metadata[key] = value;
				}
			}
		}

		return {
			sectionId: isStandalone ? null : groupKey,
			heading:
				asNullableString(meta?.heading) ?? getChunkMetadataString(firstChunk, "source_name") ?? "",
			markdown: asNullableString(meta?.section_markdown) ?? firstChunk.text,
			chunks,
			score,
			documentId: docId,
			publisher: asNullableString(meta?.doc_publisher) ?? firstChunk.tableSource,
			referencesJuridiques: normalizeReferences(meta?.references_juridiques),
			headingPath: asNullableString(meta?.heading_path),
			metadata,
		};
	});

	sections = sections.sort((left, right) => right.score - left.score);

	const sectionCountBeforeRerank = sections.length;
	let rerankerApplied = false;

	const rerankQuery = input.queryForRetrieval?.trim() || input.reformulatedQuery?.trim() || "";

	if (config.enable_section_reranker && rerankQuery) {
		try {
			const reranked = await rerankSections(rerankQuery, sections, config.section_rerank_top_k);
			sections = reranked.sections;
			rerankerApplied = reranked.applied;

			if (!reranked.applied) {
				warnings.push("Section reranker returned no usable rows; kept aggregated ordering.");
			}
		} catch (error) {
			const message = error instanceof Error ? error.message : "Unknown section reranker error";
			warnings.push(`Section reranking failed; kept aggregated ordering: ${message}`);
			sections = sections.slice(0, config.section_rerank_top_k);
		}
	} else {
		sections = sections.slice(0, config.section_rerank_top_k);
	}

	return {
		sections,
		aggregationMeta: {
			weights: {
				max: config.weight_max_score,
				mean: config.weight_mean_score,
				count: config.weight_chunk_count,
			},
			sectionCountBeforeRerank,
			sectionCountAfterRerank: sections.length,
			rerankerEnabled: config.enable_section_reranker,
			rerankerApplied,
			rerankerTopK: config.section_rerank_top_k,
			rerankerCandidateCount: Math.min(sectionCountBeforeRerank, MAX_RERANK_INPUT),
			warnings,
		},
	};
}

export const sectionAggregatorStep = createStep({
	id: "section-aggregator",
	description: "Aggregate retrieved chunks into ranked sections",
	inputSchema: sectionAggregatorStepInputSchema,
	outputSchema: sectionAggregatorStepOutputSchema,
	stateSchema: sectionAggregatorStateSchema,
	execute: async ({ inputData, state, setState }) => {
		const runtimeAggregationConfig = await loadRuntimeAggregationConfigSafe();
		const result = await runSectionAggregator(inputData, runtimeAggregationConfig);

		await setState({
			...state,
			sectionAggregator: result,
		});

		return result;
	},
});
