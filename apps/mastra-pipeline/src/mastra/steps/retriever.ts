import { createStep } from "@mastra/core/workflows";
import { z } from "zod";
import { getAlbertBaseUrl, getScalewayBaseUrl } from "../lib/albert";
import { CircuitBreaker } from "../lib/circuit-breaker";
import {
	DEFAULT_RUNTIME_RAG_CONFIG,
	getRuntimeRagConfig,
	type RuntimeRagConfig,
} from "../lib/config";
import { getDbPool } from "../lib/db";
import { queryProcessorStateSchema } from "./query-processor";

const EMBEDDING_BREAKER = new CircuitBreaker({ cooldownMs: 60_000 });
const RRF_K = 60;

const INDEX_NAME_BY_EMBEDDING_MODEL = {
	albert: "rag_chunks_albert",
	bge_scaleway: "rag_chunks_scaleway",
} as const;

export const searchModeSchema = z.enum(["semantic", "hybrid", "lexical"]);
const embeddingModelSchema = z.enum(["albert", "bge_scaleway"]);

export type SearchMode = z.infer<typeof searchModeSchema>;
type EmbeddingModel = z.infer<typeof embeddingModelSchema>;

type RuntimeRetrievalConfig = RuntimeRagConfig["retrieval"];

interface NormalizedRetrievalConfig {
	search_mode: SearchMode;
	embedding_model: EmbeddingModel;
	initial_top_k: number;
	alpha: number;
	tables: string[];
	enable_chunks_test: boolean;
	enable_selector_retry: boolean;
	selector_retry_search_mode: SearchMode;
	selector_retry_top_k: number;
}

const retrieverConfigSchema = z
	.object({
		search_mode: searchModeSchema,
		embedding_model: embeddingModelSchema,
		initial_top_k: z.number().int().positive(),
		alpha: z.number(),
		tables: z.array(z.string()),
		enable_chunks_test: z.boolean(),
		enable_selector_retry: z.boolean(),
		selector_retry_search_mode: searchModeSchema,
		selector_retry_top_k: z.number().int().positive(),
	})
	.partial();

export const retrievedChunkSchema = z.object({
	chunkId: z.string(),
	text: z.string(),
	score: z.number(),
	tableSource: z.string(),
	publisher: z.string(),
	publisherKey: z.string(),
	sectionId: z.string().nullable(),
	metadata: z.record(z.string(), z.unknown()),
	embeddingModelUsed: embeddingModelSchema,
	retrievalMode: searchModeSchema,
	sourceIndex: z.enum(["rag_chunks_albert", "rag_chunks_scaleway"]),
});

export const retrieverStepInputSchema = z.object({
	queryForRetrieval: z.string().min(1),
	needsLegalSearch: z.boolean(),
	config: retrieverConfigSchema.optional(),
});

export const retrieverStepOutputSchema = z.object({
	chunks: z.array(retrievedChunkSchema),
	retrievalMeta: z.object({
		configuredSearchMode: searchModeSchema,
		embeddingModelConfigured: embeddingModelSchema,
		embeddingModelUsed: embeddingModelSchema,
		embeddingProviderUsed: z.enum(["albert", "scaleway"]),
		indexName: z.enum(["rag_chunks_albert", "rag_chunks_scaleway"]),
		topKPerPublisher: z.number().int().positive(),
		alpha: z.number(),
		publishersSearched: z.array(z.string()),
		modeByPublisher: z.record(z.string(), searchModeSchema),
		warnings: z.array(z.string()),
		chunkCount: z.number().int().nonnegative(),
		rankingStrategy: z.enum(["global_rrf"]),
	}),
});

export const retrieverStateSchema = queryProcessorStateSchema.extend({
	retriever: retrieverStepOutputSchema.optional(),
});

type RetrieverConfigInput = z.infer<typeof retrieverConfigSchema>;
type RetrieverStepInput = z.infer<typeof retrieverStepInputSchema>;
type RetrieverStepOutput = z.infer<typeof retrieverStepOutputSchema>;
type RetrievedChunk = z.infer<typeof retrievedChunkSchema>;

interface EmbeddingResult {
	embedding: number[];
	embeddingModelUsed: EmbeddingModel;
	embeddingProviderUsed: "albert" | "scaleway";
	indexName: "rag_chunks_albert" | "rag_chunks_scaleway";
}

interface RawChunkRow {
	chunk_id: string;
	chunk_text: string;
	score: number;
	metadata: unknown;
}

interface RankedChunkRow {
	chunkId: string;
	text: string;
	score: number;
	metadata: Record<string, unknown>;
}

interface PublisherSearchResult {
	publisherKey: string;
	mode: SearchMode;
	rows: RankedChunkRow[];
}

interface PublisherRoutingEntry {
	tableSource: string;
	aliases: string[];
}

const DEFAULT_PUBLISHER_ROUTING: Record<string, PublisherRoutingEntry> = {
	matte: {
		tableSource: "MATTE",
		aliases: ["matte"],
	},
	service_public: {
		tableSource: "Service-Public",
		aliases: ["service_public"],
	},
	service_public_scw: {
		tableSource: "Service-Public (Scaleway)",
		aliases: ["service_public_scw"],
	},
	dgafp: {
		tableSource: "DGAFP",
		aliases: ["dgafp"],
	},
	rgrh: {
		tableSource: "RGRH",
		aliases: ["rgrh"],
	},
	chunkstest: {
		tableSource: "ChunksTest",
		aliases: ["chunkstest", "chunks_test", "chunk_test"],
	},
};

const SAFE_TABLE_KEY_RE = /^[a-z0-9_]+$/;

let dualIndexTablesChecked = false;
const contentTsvCache = new Map<string, boolean>();

function normalizeMetadata(value: unknown): Record<string, unknown> {
	if (value && typeof value === "object" && !Array.isArray(value)) {
		return value as Record<string, unknown>;
	}

	if (typeof value === "string") {
		try {
			const parsed = JSON.parse(value) as unknown;
			if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
				return parsed as Record<string, unknown>;
			}
		} catch {
			// Ignore and fallback to empty object.
		}
	}

	return {};
}

function normalizePublisherRoutingConfig(value: unknown): Record<string, PublisherRoutingEntry> {
	if (!value || typeof value !== "object" || Array.isArray(value)) {
		return DEFAULT_PUBLISHER_ROUTING;
	}

	const normalized = { ...DEFAULT_PUBLISHER_ROUTING };

	for (const [rawKey, rawEntry] of Object.entries(value)) {
		const key = rawKey.trim().toLowerCase();
		if (!SAFE_TABLE_KEY_RE.test(key)) {
			continue;
		}

		if (!rawEntry || typeof rawEntry !== "object" || Array.isArray(rawEntry)) {
			continue;
		}

		const tableSource =
			typeof (rawEntry as { tableSource?: unknown }).tableSource === "string"
				? (rawEntry as { tableSource: string }).tableSource.trim()
				: "";

		const aliasesRaw = (rawEntry as { aliases?: unknown }).aliases;
		const aliases = Array.isArray(aliasesRaw)
			? aliasesRaw
					.map((alias) => String(alias).trim().toLowerCase())
					.filter((alias) => alias.length > 0 && SAFE_TABLE_KEY_RE.test(alias))
			: [];

		normalized[key] = {
			tableSource: tableSource || normalized[key]?.tableSource || key,
			aliases:
				aliases.length > 0 ? Array.from(new Set(aliases)) : normalized[key]?.aliases || [key],
		};
	}

	return normalized;
}

function getPublisherRoutingConfig(): Record<string, PublisherRoutingEntry> {
	const raw = process.env.RETRIEVER_PUBLISHER_ROUTING_JSON;
	if (!raw) {
		return DEFAULT_PUBLISHER_ROUTING;
	}

	try {
		const parsed = JSON.parse(raw) as unknown;
		return normalizePublisherRoutingConfig(parsed);
	} catch {
		return DEFAULT_PUBLISHER_ROUTING;
	}
}

function normalizeRetrievalConfig(
	runtimeConfig: RuntimeRetrievalConfig,
	override?: RetrieverConfigInput,
): NormalizedRetrievalConfig {
	const base: NormalizedRetrievalConfig = {
		search_mode: "semantic",
		embedding_model: "albert",
		initial_top_k: 15,
		alpha: 0.5,
		tables: ["matte", "service_public", "dgafp", "rgrh"],
		enable_chunks_test: false,
		enable_selector_retry: true,
		selector_retry_search_mode: "hybrid",
		selector_retry_top_k: 30,
	};

	return {
		search_mode: override?.search_mode ?? runtimeConfig?.search_mode ?? base.search_mode,
		embedding_model:
			override?.embedding_model ?? runtimeConfig?.embedding_model ?? base.embedding_model,
		initial_top_k: override?.initial_top_k ?? runtimeConfig?.initial_top_k ?? base.initial_top_k,
		alpha: override?.alpha ?? runtimeConfig?.alpha ?? base.alpha,
		tables: override?.tables ?? runtimeConfig?.tables ?? base.tables,
		enable_chunks_test:
			override?.enable_chunks_test ?? runtimeConfig?.enable_chunks_test ?? base.enable_chunks_test,
		enable_selector_retry:
			override?.enable_selector_retry ??
			runtimeConfig?.enable_selector_retry ??
			base.enable_selector_retry,
		selector_retry_search_mode:
			override?.selector_retry_search_mode ??
			runtimeConfig?.selector_retry_search_mode ??
			base.selector_retry_search_mode,
		selector_retry_top_k:
			override?.selector_retry_top_k ??
			runtimeConfig?.selector_retry_top_k ??
			base.selector_retry_top_k,
	};
}

function getPublisherKeys(config: NormalizedRetrievalConfig): string[] {
	const keys = (config.tables || [])
		.map((key) => key.trim().toLowerCase())
		.filter((key) => key.length > 0 && SAFE_TABLE_KEY_RE.test(key));

	const deduped = Array.from(new Set(keys));
	if (config.enable_chunks_test && !deduped.includes("chunkstest")) {
		deduped.push("chunkstest");
	}

	return deduped;
}

function getPublisherAliases(
	publisherKey: string,
	routingConfig: Record<string, PublisherRoutingEntry>,
): string[] {
	const aliases = routingConfig[publisherKey]?.aliases ?? [publisherKey];
	return aliases.map((value) => value.toLowerCase());
}

function getTableSourceLabel(
	publisherKey: string,
	routingConfig: Record<string, PublisherRoutingEntry>,
): string {
	return routingConfig[publisherKey]?.tableSource ?? publisherKey;
}

function toVectorLiteral(embedding: number[]): string {
	const serialized = embedding.map((value) => (Number.isFinite(value) ? `${value}` : "0"));
	return `[${serialized.join(",")}]`;
}

function buildLexicalSearchQuery(query: string): string | null {
	const matches = query.toLowerCase().match(/[\p{L}\p{N}_]+/gu);

	if (!matches || matches.length === 0) {
		return null;
	}

	const tokens = Array.from(
		new Set(matches.map((token) => token.trim()).filter((token) => token.length > 0)),
	);

	if (tokens.length === 0) {
		return null;
	}

	return tokens.join(" OR ");
}

async function ensureDualIndexTables(): Promise<void> {
	if (dualIndexTablesChecked) {
		return;
	}

	const db = getDbPool();
	const requiredTables = ["rag_chunks_albert", "rag_chunks_scaleway"];
	const result = await db.query<{ table_name: string }>(
		`
      SELECT table_name
      FROM information_schema.tables
      WHERE table_schema = 'public'
      AND table_name = ANY($1::text[])
    `,
		[requiredTables],
	);

	const existing = new Set(result.rows.map((row) => row.table_name));
	const missing = requiredTables.filter((name) => !existing.has(name));
	if (missing.length > 0) {
		throw new Error(
			`Missing required dual-index table(s): ${missing.join(", ")}. PR4 is configured for dual-index retrieval only.`,
		);
	}

	dualIndexTablesChecked = true;
}

async function hasContentTsv(
	indexName: "rag_chunks_albert" | "rag_chunks_scaleway",
): Promise<boolean> {
	if (contentTsvCache.has(indexName)) {
		return Boolean(contentTsvCache.get(indexName));
	}

	const db = getDbPool();
	const result = await db.query<{ has_content_tsv: boolean }>(
		`
      SELECT EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = $1
          AND column_name = 'content_tsv'
      ) AS has_content_tsv
    `,
		[indexName],
	);

	const hasColumn = Boolean(result.rows[0]?.has_content_tsv);
	contentTsvCache.set(indexName, hasColumn);
	return hasColumn;
}

async function requestEmbeddings(args: {
	baseUrl: string;
	apiKey: string;
	model: string;
	input: string;
}): Promise<number[]> {
	const response = await fetch(`${args.baseUrl.replace(/\/$/, "")}/embeddings`, {
		method: "POST",
		headers: {
			"Content-Type": "application/json",
			Authorization: `Bearer ${args.apiKey}`,
		},
		body: JSON.stringify({ model: args.model, input: args.input }),
		signal: AbortSignal.timeout(30_000),
	});

	if (!response.ok) {
		throw new Error(`Embedding request failed with HTTP ${response.status}`);
	}

	const body = (await response.json()) as {
		data?: Array<{ embedding?: unknown }>;
	};

	const embedding = body.data?.[0]?.embedding;
	if (!Array.isArray(embedding) || embedding.length === 0) {
		throw new Error("Embedding response is missing vector data");
	}

	const numericEmbedding = embedding.map((value) => {
		const parsed = typeof value === "number" ? value : Number.parseFloat(String(value));
		return Number.isFinite(parsed) ? parsed : 0;
	});

	return numericEmbedding;
}

async function embedWithPreferredModel(
	query: string,
	embeddingModelConfigured: EmbeddingModel,
): Promise<EmbeddingResult> {
	if (embeddingModelConfigured === "bge_scaleway") {
		const embedding = await requestEmbeddings({
			baseUrl: getScalewayBaseUrl(),
			apiKey: process.env.SCALEWAY_API_KEY ?? "",
			model: "bge-multilingual-gemma2",
			input: query,
		});

		return {
			embedding,
			embeddingModelUsed: "bge_scaleway",
			embeddingProviderUsed: "scaleway",
			indexName: INDEX_NAME_BY_EMBEDDING_MODEL.bge_scaleway,
		};
	}

	if (EMBEDDING_BREAKER.shouldSkip()) {
		const embedding = await requestEmbeddings({
			baseUrl: getScalewayBaseUrl(),
			apiKey: process.env.SCALEWAY_API_KEY ?? "",
			model: "bge-multilingual-gemma2",
			input: query,
		});

		return {
			embedding,
			embeddingModelUsed: "bge_scaleway",
			embeddingProviderUsed: "scaleway",
			indexName: INDEX_NAME_BY_EMBEDDING_MODEL.bge_scaleway,
		};
	}

	try {
		const embedding = await requestEmbeddings({
			baseUrl: getAlbertBaseUrl(),
			apiKey: process.env.ALBERT_API_KEY ?? "",
			model: "openweight-embeddings",
			input: query,
		});

		EMBEDDING_BREAKER.recordSuccess();
		return {
			embedding,
			embeddingModelUsed: "albert",
			embeddingProviderUsed: "albert",
			indexName: INDEX_NAME_BY_EMBEDDING_MODEL.albert,
		};
	} catch {
		EMBEDDING_BREAKER.recordFailure();

		const embedding = await requestEmbeddings({
			baseUrl: getScalewayBaseUrl(),
			apiKey: process.env.SCALEWAY_API_KEY ?? "",
			model: "bge-multilingual-gemma2",
			input: query,
		});

		return {
			embedding,
			embeddingModelUsed: "bge_scaleway",
			embeddingProviderUsed: "scaleway",
			indexName: INDEX_NAME_BY_EMBEDDING_MODEL.bge_scaleway,
		};
	}
}

async function querySemantic(args: {
	indexName: "rag_chunks_albert" | "rag_chunks_scaleway";
	vectorLiteral: string;
	publisherAliases: string[];
	topK: number;
}): Promise<RankedChunkRow[]> {
	const db = getDbPool();

	const result = await db.query<RawChunkRow>(
		`
      SELECT
        vector_id AS chunk_id,
        COALESCE(metadata->>'text', '') AS chunk_text,
        1 - (embedding <=> $1::vector) AS score,
        metadata
      FROM ${args.indexName}
      WHERE embedding IS NOT NULL
        AND lower(COALESCE(metadata->>'publisher', '')) = ANY($2::text[])
      ORDER BY embedding <=> $1::vector
      LIMIT $3
    `,
		[args.vectorLiteral, args.publisherAliases, args.topK],
	);

	return result.rows.map((row) => ({
		chunkId: row.chunk_id,
		text: row.chunk_text || "",
		score: Number(row.score),
		metadata: normalizeMetadata(row.metadata),
	}));
}

async function queryLexical(args: {
	indexName: "rag_chunks_albert" | "rag_chunks_scaleway";
	query: string;
	publisherAliases: string[];
	topK: number;
}): Promise<RankedChunkRow[]> {
	const lexicalSearchQuery = buildLexicalSearchQuery(args.query);
	if (!lexicalSearchQuery) {
		return [];
	}

	const db = getDbPool();

	const result = await db.query<RawChunkRow>(
		`
      WITH parsed_query AS (
        SELECT websearch_to_tsquery('french', $1) AS q
      )
      SELECT
        vector_id AS chunk_id,
        COALESCE(metadata->>'text', '') AS chunk_text,
        ts_rank_cd(content_tsv, pq.q) AS score,
        metadata
      FROM ${args.indexName}
      CROSS JOIN parsed_query pq
      WHERE content_tsv @@ pq.q
        AND lower(COALESCE(metadata->>'publisher', '')) = ANY($2::text[])
      ORDER BY ts_rank_cd(content_tsv, pq.q) DESC
      LIMIT $3
    `,
		[lexicalSearchQuery, args.publisherAliases, args.topK],
	);

	return result.rows.map((row) => ({
		chunkId: row.chunk_id,
		text: row.chunk_text || "",
		score: Number(row.score),
		metadata: normalizeMetadata(row.metadata),
	}));
}

function mergeHybridRrf(args: {
	semanticRows: RankedChunkRow[];
	lexicalRows: RankedChunkRow[];
	alpha: number;
	topK: number;
}): RankedChunkRow[] {
	const semanticRanks = new Map<string, number>();
	const lexicalRanks = new Map<string, number>();
	const merged = new Map<string, RankedChunkRow>();

	args.semanticRows.forEach((row, index) => {
		semanticRanks.set(row.chunkId, index + 1);
		merged.set(row.chunkId, row);
	});

	args.lexicalRows.forEach((row, index) => {
		lexicalRanks.set(row.chunkId, index + 1);
		if (!merged.has(row.chunkId)) {
			merged.set(row.chunkId, row);
		}
	});

	const scored = Array.from(merged.values()).map((row) => {
		const semanticRank = semanticRanks.get(row.chunkId) ?? args.topK;
		const lexicalRank = lexicalRanks.get(row.chunkId) ?? args.topK;
		const rrfScore =
			args.alpha * (1 / (RRF_K + semanticRank)) + (1 - args.alpha) * (1 / (RRF_K + lexicalRank));

		return {
			...row,
			score: rrfScore,
		};
	});

	scored.sort((a, b) => b.score - a.score);
	return scored.slice(0, args.topK);
}

function mergeCrossPublisherRanks(searchResults: PublisherSearchResult[]): Array<
	PublisherSearchResult["rows"][number] & {
		publisherKey: string;
		mode: SearchMode;
		fusedScore: number;
	}
> {
	const fused = new Map<
		string,
		PublisherSearchResult["rows"][number] & {
			publisherKey: string;
			mode: SearchMode;
			fusedScore: number;
		}
	>();

	for (const result of searchResults) {
		result.rows.forEach((row, index) => {
			const rank = index + 1;
			const contribution = 1 / (RRF_K + rank);
			const existing = fused.get(row.chunkId);

			if (existing) {
				existing.fusedScore += contribution;
				return;
			}

			fused.set(row.chunkId, {
				...row,
				publisherKey: result.publisherKey,
				mode: result.mode,
				fusedScore: contribution,
			});
		});
	}

	return Array.from(fused.values()).sort((left, right) => right.fusedScore - left.fusedScore);
}

function resolveModeForPublisher(args: {
	configuredMode: SearchMode;
	publisherKey: string;
	needsLegalSearch: boolean;
	lexicalAvailable: boolean;
	warnings: string[];
}): SearchMode {
	let mode: SearchMode = args.configuredMode;

	if (
		args.publisherKey === "dgafp" &&
		args.needsLegalSearch &&
		args.configuredMode === "semantic"
	) {
		mode = "hybrid";
	}

	if ((mode === "hybrid" || mode === "lexical") && !args.lexicalAvailable) {
		args.warnings.push(
			`Lexical mode unavailable on ${args.publisherKey} because content_tsv is missing on the selected index. Falling back to semantic.`,
		);
		mode = "semantic";
	}

	return mode;
}

async function loadRuntimeRetrievalConfigSafe(): Promise<RuntimeRetrievalConfig> {
	try {
		const runtimeConfig = await getRuntimeRagConfig();
		return runtimeConfig.retrieval;
	} catch {
		return DEFAULT_RUNTIME_RAG_CONFIG.retrieval;
	}
}

export async function runRetriever(
	input: RetrieverStepInput,
	runtimeRetrievalConfigOverride?: RuntimeRetrievalConfig,
): Promise<RetrieverStepOutput> {
	await ensureDualIndexTables();

	const runtimeRetrievalConfig =
		runtimeRetrievalConfigOverride ?? (await loadRuntimeRetrievalConfigSafe());

	const config = normalizeRetrievalConfig(runtimeRetrievalConfig, input.config);
	const configuredMode: SearchMode = config.search_mode;
	const embeddingModelConfigured: EmbeddingModel = config.embedding_model;
	const topK = config.initial_top_k;
	const alpha = config.alpha;

	const embeddingResult = await embedWithPreferredModel(
		input.queryForRetrieval,
		embeddingModelConfigured,
	);
	const vectorLiteral = toVectorLiteral(embeddingResult.embedding);
	const lexicalAvailable = await hasContentTsv(embeddingResult.indexName);

	const publisherRouting = getPublisherRoutingConfig();

	const publisherKeys = getPublisherKeys(config).filter(
		(publisherKey) => input.needsLegalSearch || publisherKey !== "dgafp",
	);

	const warnings: string[] = [];
	const modeByPublisher: Record<string, SearchMode> = {};

	const searchResults: PublisherSearchResult[] = await Promise.all(
		publisherKeys.map(async (publisherKey) => {
			const mode = resolveModeForPublisher({
				configuredMode,
				publisherKey,
				needsLegalSearch: input.needsLegalSearch,
				lexicalAvailable,
				warnings,
			});

			modeByPublisher[publisherKey] = mode;
			const publisherAliases = getPublisherAliases(publisherKey, publisherRouting);

			if (mode === "lexical") {
				const lexicalRows = await queryLexical({
					indexName: embeddingResult.indexName,
					query: input.queryForRetrieval,
					publisherAliases,
					topK,
				});

				return { publisherKey, mode, rows: lexicalRows };
			}

			if (mode === "hybrid") {
				const [semanticRows, lexicalRows] = await Promise.all([
					querySemantic({
						indexName: embeddingResult.indexName,
						vectorLiteral,
						publisherAliases,
						topK,
					}),
					queryLexical({
						indexName: embeddingResult.indexName,
						query: input.queryForRetrieval,
						publisherAliases,
						topK,
					}),
				]);

				const mergedRows = mergeHybridRrf({
					semanticRows,
					lexicalRows,
					alpha,
					topK,
				});

				return { publisherKey, mode, rows: mergedRows };
			}

			const semanticRows = await querySemantic({
				indexName: embeddingResult.indexName,
				vectorLiteral,
				publisherAliases,
				topK,
			});

			return { publisherKey, mode, rows: semanticRows };
		}),
	);

	const globallyRankedRows = mergeCrossPublisherRanks(searchResults);

	const chunks: RetrievedChunk[] = globallyRankedRows.map((row) => {
		const sectionIdRaw = row.metadata.section_id;
		const sectionId =
			typeof sectionIdRaw === "string" && sectionIdRaw.trim().length > 0 ? sectionIdRaw : null;

		const metadataPublisher = row.metadata.publisher;
		const publisher =
			typeof metadataPublisher === "string" && metadataPublisher.trim().length > 0
				? metadataPublisher
				: row.publisherKey;

		return {
			chunkId: row.chunkId,
			text: row.text,
			score: row.fusedScore,
			tableSource: getTableSourceLabel(row.publisherKey, publisherRouting),
			publisher,
			publisherKey: row.publisherKey,
			sectionId,
			metadata: row.metadata,
			embeddingModelUsed: embeddingResult.embeddingModelUsed,
			retrievalMode: row.mode,
			sourceIndex: embeddingResult.indexName,
		};
	});

	return {
		chunks,
		retrievalMeta: {
			configuredSearchMode: configuredMode,
			embeddingModelConfigured,
			embeddingModelUsed: embeddingResult.embeddingModelUsed,
			embeddingProviderUsed: embeddingResult.embeddingProviderUsed,
			indexName: embeddingResult.indexName,
			topKPerPublisher: topK,
			alpha,
			publishersSearched: publisherKeys,
			modeByPublisher,
			warnings,
			chunkCount: chunks.length,
			rankingStrategy: "global_rrf",
		},
	};
}

export const retrieverStep = createStep({
	id: "retriever",
	description: "Retrieve and rank chunks from dual pgvector indexes",
	inputSchema: retrieverStepInputSchema,
	outputSchema: retrieverStepOutputSchema,
	stateSchema: retrieverStateSchema,
	execute: async ({ inputData, state, setState }) => {
		const runtimeRetrievalConfig = await loadRuntimeRetrievalConfigSafe();
		const result = await runRetriever(inputData, runtimeRetrievalConfig);

		await setState({
			...state,
			retriever: result,
		});

		return result;
	},
});
