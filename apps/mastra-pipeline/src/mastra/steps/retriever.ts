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
const HEADING_SOURCE_PREFIX = "heading:";
const HEADING_CANDIDATE_MULTIPLIER = 5;
const MAX_HEADING_CANDIDATES = 100;

const HEADING_SEARCH_EXCLUDED_PUBLISHERS = new Set(["dgafp"]);
const HEADING_STOPWORDS = new Set([
	"a",
	"au",
	"aux",
	"avec",
	"beneficier",
	"ce",
	"ces",
	"condition",
	"conditions",
	"dans",
	"de",
	"des",
	"du",
	"en",
	"est",
	"et",
	"la",
	"le",
	"les",
	"l",
	"peut",
	"pour",
	"qu",
	"que",
	"quel",
	"quelle",
	"quelles",
	"quels",
	"qui",
	"recevoir",
	"sont",
	"sur",
	"un",
	"une",
]);

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

interface RawHeadingRow extends RawChunkRow {
	section_id: string | null;
	heading: string | null;
	heading_path: string | null;
	document_title: string | null;
	doc_url: string | null;
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
	sourcePath: "chunk" | "heading";
	sourceName: string;
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

function normalizeTextForHeadingMatch(value: string): string {
	return (
		value
			.toLowerCase()
			.normalize("NFKD")
			.replace(/[\u0300-\u036f]/g, "")
			.match(/[a-z0-9]+/g)
			?.join(" ") ?? ""
	);
}

function tokenizeForHeadingMatch(value: string): Set<string> {
	return new Set(
		normalizeTextForHeadingMatch(value)
			.split(" ")
			.map((token) => token.trim())
			.filter((token) => token.length > 1 && !HEADING_STOPWORDS.has(token)),
	);
}

function bigramSimilarity(left: string, right: string): number {
	if (!left || !right) {
		return 0;
	}

	if (left === right) {
		return 1;
	}

	const toBigrams = (value: string): Set<string> => {
		if (value.length < 2) {
			return new Set([value]);
		}

		const grams = new Set<string>();
		for (let index = 0; index < value.length - 1; index += 1) {
			grams.add(value.slice(index, index + 2));
		}

		return grams;
	};

	const leftBigrams = toBigrams(left);
	const rightBigrams = toBigrams(right);
	let overlap = 0;
	for (const gram of leftBigrams) {
		if (rightBigrams.has(gram)) {
			overlap += 1;
		}
	}

	return (2 * overlap) / (leftBigrams.size + rightBigrams.size);
}

function headingMatchScore(heading: string, headingPath: string, query: string): number {
	const queryNorm = normalizeTextForHeadingMatch(query);
	const headingNorm = normalizeTextForHeadingMatch(heading);
	const pathNorm = normalizeTextForHeadingMatch(headingPath);

	if (!queryNorm || (!headingNorm && !pathNorm)) {
		return 0;
	}

	const candidates = [headingNorm, pathNorm].filter((candidate) => candidate.length > 0);
	if (
		candidates.some(
			(candidate) =>
				candidate === queryNorm || candidate.includes(queryNorm) || queryNorm.includes(candidate),
		)
	) {
		return 1;
	}

	const queryTokens = tokenizeForHeadingMatch(query);
	const headingTokens = tokenizeForHeadingMatch(`${heading} ${headingPath}`);
	if (queryTokens.size === 0 || headingTokens.size === 0) {
		return 0;
	}

	let overlap = 0;
	for (const token of queryTokens) {
		if (headingTokens.has(token)) {
			overlap += 1;
		}
	}

	if (overlap === 0) {
		return 0;
	}

	const coverageQuery = overlap / queryTokens.size;
	const coverageHeading = overlap / headingTokens.size;
	if (coverageQuery >= 0.75 && coverageHeading >= 0.45) {
		return 1;
	}

	const tokenScore = 0.7 * coverageQuery + 0.3 * coverageHeading;
	const fuzzyScore = Math.max(
		...candidates.map((candidate) => bigramSimilarity(queryNorm, candidate)),
	);
	const score = Math.max(tokenScore, fuzzyScore >= 0.72 ? fuzzyScore : 0);

	return Math.min(score, 0.99);
}

function withChunkRetrievalMetadata(row: RawChunkRow): RankedChunkRow {
	const metadata = normalizeMetadata(row.metadata);
	metadata.retrieval_path ??= "chunk";
	metadata.heading_search ??= false;

	return {
		chunkId: row.chunk_id,
		text: row.chunk_text || "",
		score: Number(row.score),
		metadata,
	};
}

function shouldSearchHeadings(publisherKey: string): boolean {
	return !HEADING_SEARCH_EXCLUDED_PUBLISHERS.has(publisherKey);
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

	return result.rows.map(withChunkRetrievalMetadata);
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

	return result.rows.map(withChunkRetrievalMetadata);
}

async function queryHeadings(args: {
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
	const candidateLimit = Math.min(
		Math.max(args.topK * HEADING_CANDIDATE_MULTIPLIER, args.topK),
		MAX_HEADING_CANDIDATES,
	);

	const result = await db.query<RawHeadingRow>(
		`
      WITH parsed_query AS (
        SELECT websearch_to_tsquery('french', $1) AS q
      ),
      candidate_chunks AS (
        SELECT
          vector_id AS chunk_id,
          COALESCE(metadata->>'text', '') AS chunk_text,
          metadata,
          NULLIF(metadata->>'section_id', '') AS section_id
        FROM ${args.indexName}
        WHERE lower(COALESCE(metadata->>'publisher', '')) = ANY($2::text[])
          AND NULLIF(metadata->>'section_id', '') IS NOT NULL
      )
      SELECT
        c.chunk_id,
        c.chunk_text,
        0::double precision AS score,
        c.metadata,
        c.section_id,
        s.heading,
        s.heading_path,
        d.title AS document_title,
        d.source_url AS doc_url,
        ts_rank_cd(
          to_tsvector('french', concat_ws(' ', d.title, s.heading, s.heading_path)),
          pq.q
        ) AS lexical_score
      FROM candidate_chunks c
      JOIN rag_sections s ON s.section_id::text = c.section_id
      LEFT JOIN rag_documents d ON d.doc_id = s.doc_id
      CROSS JOIN parsed_query pq
      WHERE to_tsvector('french', concat_ws(' ', d.title, s.heading, s.heading_path)) @@ pq.q
      ORDER BY lexical_score DESC, c.chunk_id
      LIMIT $3
    `,
		[lexicalSearchQuery, args.publisherAliases, candidateLimit],
	);

	const rows = result.rows
		.map((row) => {
			const heading = row.heading?.trim() ?? "";
			const headingPath = row.heading_path?.trim() ?? "";
			const documentTitle = row.document_title?.trim() ?? "";
			const matchScore = headingMatchScore(
				[documentTitle, heading].filter((value) => value.length > 0).join(" "),
				headingPath,
				args.query,
			);

			if (matchScore <= 0) {
				return null;
			}

			const metadata = normalizeMetadata(row.metadata);
			metadata.retrieval_path = "heading";
			metadata.source_score_mode = "heading";
			metadata.heading_search = true;
			metadata.heading_match_score = matchScore;
			metadata.matched_heading = heading || documentTitle;
			metadata.matched_heading_path = headingPath;
			metadata.section_id = row.section_id;

			if (documentTitle.length > 0) {
				metadata.source_name = documentTitle;
			}

			if (row.doc_url?.trim()) {
				metadata.doc_url = row.doc_url.trim();
			}

			return {
				chunkId: row.chunk_id,
				text: row.chunk_text || "",
				score: matchScore,
				metadata,
			} satisfies RankedChunkRow;
		})
		.filter((row): row is RankedChunkRow => row !== null);

	rows.sort((left, right) => {
		const scoreDelta = right.score - left.score;
		if (scoreDelta !== 0) {
			return scoreDelta;
		}

		return left.chunkId < right.chunkId ? -1 : left.chunkId > right.chunkId ? 1 : 0;
	});

	return rows.slice(0, args.topK);
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

function stableCompare(left: string, right: string): number {
	if (left < right) {
		return -1;
	}

	if (left > right) {
		return 1;
	}

	return 0;
}

function getMetadataString(metadata: Record<string, unknown>, key: string): string | null {
	const value = metadata[key];
	return typeof value === "string" && value.trim().length > 0 ? value.trim() : null;
}

function mergeHeadingMetadata(
	target: Record<string, unknown>,
	source: Record<string, unknown>,
): void {
	const incomingScore = Number(source.heading_match_score ?? 0);
	const currentScore = Number(target.heading_match_score ?? 0);
	if (
		Number.isFinite(incomingScore) &&
		(!Number.isFinite(currentScore) || incomingScore > currentScore)
	) {
		target.heading_match_score = incomingScore;
	}

	target.matched_heading ??= source.matched_heading;
	target.matched_heading_path ??= source.matched_heading_path;
}

function mergeCrossPublisherRanks(searchResults: PublisherSearchResult[]): Array<
	PublisherSearchResult["rows"][number] & {
		publisherKey: string;
		mode: SearchMode;
		sourcePath: "chunk" | "heading";
		sourceName: string;
		fusedScore: number;
	}
> {
	const headingBySection = new Map<
		string,
		{
			matchScore: number;
			matchedHeading: unknown;
			matchedHeadingPath: unknown;
		}
	>();
	const chunkSections = new Set<string>();

	for (const result of searchResults) {
		for (const row of result.rows) {
			const sectionId = getMetadataString(row.metadata, "section_id");
			if (!sectionId) {
				continue;
			}

			const sectionKey = `${result.publisherKey}:${sectionId}`;
			if (result.sourcePath === "heading") {
				const matchScore = Number(row.metadata.heading_match_score ?? 0);
				const existing = headingBySection.get(sectionKey);
				if (!existing || matchScore > existing.matchScore) {
					headingBySection.set(sectionKey, {
						matchScore,
						matchedHeading: row.metadata.matched_heading,
						matchedHeadingPath: row.metadata.matched_heading_path,
					});
				}
			} else {
				chunkSections.add(sectionKey);
			}
		}
	}

	const chunkAndHeadingSections = new Set(
		Array.from(chunkSections).filter((sectionKey) => headingBySection.has(sectionKey)),
	);

	const fused = new Map<
		string,
		PublisherSearchResult["rows"][number] & {
			publisherKey: string;
			mode: SearchMode;
			sourcePath: "chunk" | "heading";
			sourceName: string;
			fusedScore: number;
		}
	>();

	for (const result of searchResults) {
		result.rows.forEach((row, index) => {
			const rank = index + 1;
			const contribution = 1 / (RRF_K + rank);
			const existing = fused.get(row.chunkId);
			const sourceIsHeading = result.sourcePath === "heading";

			if (existing) {
				existing.fusedScore += contribution;
				if (sourceIsHeading) {
					existing.metadata.heading_search = true;
					existing.metadata.retrieval_path = "chunk+heading";
					mergeHeadingMetadata(existing.metadata, row.metadata);
				} else if (existing.metadata.heading_search === true) {
					existing.metadata.retrieval_path = "chunk+heading";
				}

				const previousRaw = Number(existing.metadata.source_score ?? Number.NEGATIVE_INFINITY);
				if (!Number.isFinite(previousRaw) || row.score > previousRaw) {
					existing.metadata.source_score = row.score;
					existing.metadata.source_score_mode = row.metadata.source_score_mode ?? result.mode;
					existing.metadata.score_source = result.sourceName;
				}
				return;
			}

			const metadata = { ...row.metadata };
			metadata.source_score ??= row.score;
			metadata.source_score_mode ??= sourceIsHeading ? "heading" : result.mode;
			metadata.score_source ??= result.sourceName;
			if (sourceIsHeading) {
				metadata.heading_search = true;
				metadata.retrieval_path = "heading";
			} else {
				metadata.retrieval_path ??= "chunk";
				metadata.heading_search ??= false;
			}

			fused.set(row.chunkId, {
				...row,
				metadata,
				publisherKey: result.publisherKey,
				mode: result.mode,
				sourcePath: result.sourcePath,
				sourceName: result.sourceName,
				fusedScore: contribution,
			});
		});
	}

	for (const row of fused.values()) {
		const sectionId = getMetadataString(row.metadata, "section_id");
		const sectionKey = sectionId ? `${row.publisherKey}:${sectionId}` : null;
		if (!sectionKey || !chunkAndHeadingSections.has(sectionKey)) {
			continue;
		}

		row.metadata.heading_search = true;
		row.metadata.retrieval_path = "chunk+heading";
		const headingMetadata = headingBySection.get(sectionKey);
		if (headingMetadata) {
			const currentScore = Number(row.metadata.heading_match_score ?? 0);
			if (!Number.isFinite(currentScore) || headingMetadata.matchScore > currentScore) {
				row.metadata.heading_match_score = headingMetadata.matchScore;
			}
			row.metadata.matched_heading ??= headingMetadata.matchedHeading;
			row.metadata.matched_heading_path ??= headingMetadata.matchedHeadingPath;
		}
	}

	return Array.from(fused.values()).sort((left, right) => {
		const scoreDelta = right.fusedScore - left.fusedScore;
		if (scoreDelta !== 0) {
			return scoreDelta;
		}

		const headingDelta =
			Number(right.metadata.heading_match_score ?? 0) -
			Number(left.metadata.heading_match_score ?? 0);
		if (headingDelta !== 0) {
			return headingDelta;
		}

		const sourceDelta = stableCompare(left.sourceName, right.sourceName);
		if (sourceDelta !== 0) {
			return sourceDelta;
		}

		return stableCompare(left.chunkId, right.chunkId);
	});
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

	const searchResultsByPublisher = await Promise.all(
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
			const headingRowsPromise = shouldSearchHeadings(publisherKey)
				? queryHeadings({
						indexName: embeddingResult.indexName,
						query: input.queryForRetrieval,
						publisherAliases,
						topK,
					}).catch((error: unknown) => {
						const message = error instanceof Error ? error.message : String(error);
						warnings.push(`Heading search unavailable on ${publisherKey}: ${message}`);
						return [];
					})
				: Promise.resolve([]);

			let chunkRowsPromise: Promise<RankedChunkRow[]>;

			if (mode === "lexical") {
				chunkRowsPromise = queryLexical({
					indexName: embeddingResult.indexName,
					query: input.queryForRetrieval,
					publisherAliases,
					topK,
				});
			} else if (mode === "hybrid") {
				chunkRowsPromise = Promise.all([
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
				]).then(([semanticRows, lexicalRows]) =>
					mergeHybridRrf({
						semanticRows,
						lexicalRows,
						alpha,
						topK,
					}),
				);
			} else {
				chunkRowsPromise = querySemantic({
					indexName: embeddingResult.indexName,
					vectorLiteral,
					publisherAliases,
					topK,
				});
			}

			const [chunkRows, headingRows] = await Promise.all([chunkRowsPromise, headingRowsPromise]);
			const results: PublisherSearchResult[] = [
				{
					publisherKey,
					mode,
					sourcePath: "chunk",
					sourceName: `chunk:${publisherKey}`,
					rows: chunkRows,
				},
			];

			if (headingRows.length > 0) {
				results.push({
					publisherKey,
					mode: "lexical",
					sourcePath: "heading",
					sourceName: `${HEADING_SOURCE_PREFIX}${publisherKey}`,
					rows: headingRows,
				});
			}

			return results;
		}),
	);
	const searchResults = searchResultsByPublisher.flat();

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

export const __retrieverTestHooks = {
	headingMatchScore,
	mergeCrossPublisherRanks,
};

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
