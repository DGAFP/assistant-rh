import { z } from "zod";
import type { ragPipelineWorkflowOutputSchema } from "../workflows/rag-pipeline";
import { getDbPool } from "./db";

/**
 * Observability schema for Mastra pipeline runs.
 *
 * This is a leaner alternative to the Python `chat_runs` table (40+ columns).
 * We capture the essential fields for parity checking and regression detection,
 * plus the full timing/metadata as JSONB for detailed analysis.
 */
export const CHAT_RUNS_MASTRA_TABLE = "chat_runs_mastra";

export const chatRunMastraSchema = z.object({
	turn_id: z.string(),
	ts: z.string().datetime(),
	query: z.string(),
	answer: z.string(),
	branch_path: z.enum(["rag", "non_rag"]),
	intent: z.string().nullable(),
	intent_confidence: z.number().nullable(),
	theme: z.string().nullable(),
	model_used: z.string().nullable(),
	provider_used: z.enum(["albert", "scaleway", "mistral", "none"]).nullable(),
	fallback_triggered: z.boolean(),
	short_circuit: z.boolean(),
	tables_searched: z.array(z.string()),
	chunk_count: z.number().int().nonnegative(),
	section_count: z.number().int().nonnegative(),
	context_section_count: z.number().int().nonnegative(),
	context_tokens: z.number().int().nonnegative(),
	timing: z.record(z.string(), z.unknown()),
	metadata: z.record(z.string(), z.unknown()),
});

export type ChatRunMastra = z.infer<typeof chatRunMastraSchema>;

let observabilityTableChecked = false;

/**
 * Create the observability table if it doesn't exist.
 * Uses a singleton flag to avoid re-checking on every request.
 */
export async function ensureObservabilityTable(): Promise<void> {
	if (observabilityTableChecked) {
		return;
	}

	const db = getDbPool();
	await db.query(`
    CREATE TABLE IF NOT EXISTS ${CHAT_RUNS_MASTRA_TABLE} (
      turn_id        TEXT PRIMARY KEY,
      ts             TIMESTAMPTZ DEFAULT now(),
      query          TEXT NOT NULL,
      answer         TEXT,
      branch_path    TEXT,
      intent         TEXT,
      intent_confidence DOUBLE PRECISION,
      theme          TEXT,
      model_used     TEXT,
      provider_used  TEXT,
      fallback_triggered BOOLEAN DEFAULT false,
      short_circuit  BOOLEAN DEFAULT false,
      tables_searched TEXT[],
      chunk_count    INT DEFAULT 0,
      section_count  INT DEFAULT 0,
      context_section_count INT DEFAULT 0,
      context_tokens INT DEFAULT 0,
      timing         JSONB DEFAULT '{}',
      metadata       JSONB DEFAULT '{}'
    )
  `);

	observabilityTableChecked = true;
}

/**
 * Log a Mastra pipeline run to the observability table.
 */
export async function logMastraRun(
	turnId: string,
	query: string,
	result: z.infer<typeof ragPipelineWorkflowOutputSchema>,
): Promise<void> {
	const db = getDbPool();

	// Extract key fields from result (metadata is a record, so we need to safely access)
	const meta = result.metadata as Record<string, unknown> | null;
	const timing = result.timing as Record<string, unknown>;

	// Safely extract metadata fields with proper type coercion
	const intent = typeof meta?.intent === "string" ? meta.intent : null;
	const intentConfidence =
		typeof meta?.intent_confidence === "number" ? meta.intent_confidence : null;
	const theme = typeof meta?.theme === "string" ? meta.theme : null;
	const modelUsed = typeof meta?.generator_model === "string" ? meta.generator_model : null;
	const providerUsed =
		typeof meta?.generator_provider === "string"
			? (meta.generator_provider as "albert" | "scaleway" | "mistral" | "none")
			: null;
	const tablesSearched = Array.isArray(meta?.tables_searched)
		? (meta.tables_searched as string[])
		: [];

	const row: ChatRunMastra = {
		turn_id: turnId,
		ts: new Date().toISOString(),
		query,
		answer: result.answer,
		branch_path: result.branchPath,
		intent,
		intent_confidence: intentConfidence,
		theme,
		model_used: modelUsed,
		provider_used: providerUsed,
		fallback_triggered: result.generationMeta?.fallbackTriggered ?? false,
		short_circuit: result.shortCircuit,
		tables_searched: tablesSearched,
		chunk_count: result.chunks.length,
		section_count: result.sections.length,
		context_section_count: result.contextItems.length,
		context_tokens: result.contextMeta?.tokenCount ?? 0,
		timing,
		metadata: meta ?? {},
	};

	await db.query(
		`INSERT INTO ${CHAT_RUNS_MASTRA_TABLE} (
      turn_id, ts, query, answer, branch_path, intent, intent_confidence,
      theme, model_used, provider_used, fallback_triggered, short_circuit,
      tables_searched, chunk_count, section_count, context_section_count,
      context_tokens, timing, metadata
    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18, $19)
    ON CONFLICT (turn_id) DO UPDATE SET
      ts = EXCLUDED.ts,
      answer = EXCLUDED.answer,
      branch_path = EXCLUDED.branch_path,
      intent = EXCLUDED.intent,
      intent_confidence = EXCLUDED.intent_confidence,
      theme = EXCLUDED.theme,
      model_used = EXCLUDED.model_used,
      provider_used = EXCLUDED.provider_used,
      fallback_triggered = EXCLUDED.fallback_triggered,
      short_circuit = EXCLUDED.short_circuit,
      tables_searched = EXCLUDED.tables_searched,
      chunk_count = EXCLUDED.chunk_count,
      section_count = EXCLUDED.section_count,
      context_section_count = EXCLUDED.context_section_count,
      context_tokens = EXCLUDED.context_tokens,
      timing = EXCLUDED.timing,
      metadata = EXCLUDED.metadata`,
		[
			row.turn_id,
			row.ts,
			row.query,
			row.answer,
			row.branch_path,
			row.intent,
			row.intent_confidence,
			row.theme,
			row.model_used,
			row.provider_used,
			row.fallback_triggered,
			row.short_circuit,
			row.tables_searched,
			row.chunk_count,
			row.section_count,
			row.context_section_count,
			row.context_tokens,
			row.timing,
			row.metadata,
		],
	);
}
