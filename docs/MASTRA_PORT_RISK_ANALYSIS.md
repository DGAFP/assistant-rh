# Mastra Port: Risk & Opportunity Analysis

Critical review of [MASTRA_PORT_ANALYSIS.md](./MASTRA_PORT_ANALYSIS.md) and [MASTRA_IMPLEMENTATION_PLAN.md](./MASTRA_IMPLEMENTATION_PLAN.md), cross-referenced against the Python source code.

---

## P0 — Foundational Issues

### 1. Embedding fallback breaks with a unified index

**The most serious issue in both documents.**

The Python pipeline handles Albert→Scaleway embedding fallback by storing **two separate vector columns per chunk** (`embedding_m3` at 1024d, `embedding_bge_scw` at 3584d) and dynamically selecting which column to query based on which embedder succeeded:

```python
# retriever.py:184
embed_col = table.embed_col_albert if model_used == "albert" else table.embed_col_bge
```

Mastra PgVector has **one `embedding` column per index**. If we create a 1024d index and Albert fails, we cannot query it with a 3584d BGE vector — pgvector will reject the dimension mismatch. The entire embedding fallback chain is silently lost.

**Options:**

| Option | Tradeoff |
|--------|----------|
| **Two indexes** (1024d + 3584d), both embeddings per chunk | Preserves full fallback. Doubles storage, doubles migration work. Query the right index based on which embedder succeeds. |
| **Albert-only embeddings** | Simplest. Drop BGE query fallback entirely. Circuit breaker now means "no results" instead of "degraded results." |
| **Same-dimension fallback model** | If Scaleway offers a 1024d model, use it as embedding fallback. Needs investigation — BGE Gemma2 is 3584d only. |

The analysis document correctly identifies the two-column pattern but didn't flag that it can't be preserved in a single Mastra index.

> **Status: RESOLVED.** The implementation plan now uses dual indexes (`rag_chunks_albert` 1024d + `rag_chunks_scaleway` 3584d).

### 2. No `content` column in Mastra PgVector — tsvector migration is wrong

The implementation plan proposes:

```sql
ALTER TABLE rag_chunks
ADD COLUMN IF NOT EXISTS content_tsv tsvector
GENERATED ALWAYS AS (to_tsvector('french', content)) STORED;
```

But Mastra's `createIndex()` creates only four columns:

| Column | Type |
|--------|------|
| `id` | `SERIAL PRIMARY KEY` |
| `vector_id` | `TEXT UNIQUE NOT NULL` |
| `embedding` | `vector(N)` |
| `metadata` | `JSONB DEFAULT '{}'` |

There is **no `content` column**. All text is stored inside `metadata` as JSON.

**Fix:** Generate the tsvector from JSONB instead:

```sql
ALTER TABLE rag_chunks
ADD COLUMN IF NOT EXISTS content_tsv tsvector
GENERATED ALWAYS AS (to_tsvector('french', metadata->>'text')) STORED;

CREATE INDEX IF NOT EXISTS idx_rag_chunks_content_tsv
ON rag_chunks USING GIN (content_tsv);
```

This also means `pgVector.query()` results come back as `{ id, score, metadata }`, not `{ text, score }`. Every downstream step must extract text from `metadata.text`.

> **Status: RESOLVED.** The implementation plan now uses `metadata->>'text'` in the tsvector migration and notes the JSONB extraction requirement.

---

## P1 — Design Gaps

### 3. Cross-step data flow not designed

The workflow chains steps with `.then()`, which requires each step's `outputSchema` to match the next step's `inputSchema`. But several steps need data from *earlier* steps, not just the immediately preceding one:

- `contextSelector` needs `query` (from step 1 output), but receives `sections` (from step 3 output)
- `generator` needs `query`, `conversationHistory`, and `systemPromptName` — none come from `contextBuilder`
- `contextBuilder` needs runtime config (STANDARD vs WIDE) from `rag_config`, not from the selector step

The plan mentions `.map()` and `getStepResult()` in passing but doesn't design the actual data flow. Mastra's `stateSchema` (shared workflow state) is the right tool — carry `query`, `config`, and `conversationHistory` in state and read them from any step via `state`.

> **Status: RESOLVED.** The implementation plan now includes a `stateSchema` with `query`, `conversationHistory`, and `config`.

### 4. `rag_chunks_test` table missing from migration plan

The analysis documents it (with `enable_chunks_test = true` in prod), but the implementation plan's unified index and metadata schema don't mention test chunks. If prod uses this table, the migration must include it. Its schema differs (embeddings in a separate `rag_chunk_embeddings` table, `chunk_tsv` for tsvector).

---

## P2 — Infrastructure Risks

### 5. Two database clients need coordinated pooling

The plan requires both Mastra PgVector (for vector operations) and a raw `pg.Pool` (for relational tables: sections, documents, acronyms, config, prompts). These share the same connection string but are independent clients. Scalingo has connection limits, and two pools competing could exhaust them.

**Mitigation:** Share a single `pg.Pool` and pass it to PgVector's constructor (if supported), or use Mastra's `PostgresStore` for both concerns.

### 6. Mastra version stability

Mastra is at `@mastra/core@1.13.0` (March 2025) and iterating rapidly. The plan relies on specific APIs (`createStep`, `createWorkflow`, `.branch()`, `.map()`, `stateSchema`) that have shifted between versions. The `@mastra/pg` schema support PR (#3463) and custom columns PR (#12809) suggest the PgVector internals are still evolving.

**Mitigation:** Pin exact versions in `package.json`. Verify all APIs against embedded docs (`node_modules/@mastra/*/dist/docs/`) before writing code, not the website.

### 7. No observability replacement for `chat_runs`

The Python pipeline logs ~120 columns per turn into `chat_runs`. The plan says "replaced by Mastra observability/tracing" but doesn't design what that looks like. Mastra has built-in OpenTelemetry tracing per workflow step, which is good, but the team currently queries `chat_runs` for debugging and evaluation. There needs to be a story for how the Mastra pipeline feeds the same debugging loop.

### 8. Reranker API is undocumented/fragile

Albert's `/rerank` endpoint isn't a standard OpenAI API. The Python code constructs a raw HTTP POST. The plan correctly identifies this (custom fetch), but the request/response format must be verified against the actual Albert API. If Albert changes this endpoint, there's no SDK-level protection.

---

## P3 — Correctness Issues in the Analysis

### 9. DGAFP exclusion behavior is understated

The analysis says "Conditional DGAFP: excluded when `needs_legal_search=false`; forced hybrid when `true`."

The code actually **removes DGAFP from the tables list entirely** when `needs_legal_search=false`:

```python
# pipeline.py:209-213
if not qr.needs_legal_search and "dgafp" in self._retriever.config.tables:
    self._retriever.config.tables = [t for t in original_tables if t != "dgafp"]
elif qr.needs_legal_search:
    force_hybrid_tables.add("dgafp")
```

This is stronger than "excluded from results" — the table is never queried. In the unified index model, this becomes a metadata filter (`publisher != 'dgafp'` when `!needsLegalSearch`), which is semantically correct but worth being precise about.

### 10. `initial_top_k` = 20 in prod is a documentation claim, not a code default

The analysis says "15 (config default, 20 in prod)." The code default is `15`:

```python
# config.py:83
initial_top_k: int = 15
```

The "20" appears only in evaluation scripts and `docs/PIPELINE.md`. The runtime config from `rag_config` could override it, but there's no code-level prod/dev distinction. The implementation should use 15 as default and allow runtime override via `rag_config`.

---

## Opportunities

### 11. Free per-step observability

Every `createStep()` is automatically traced with OpenTelemetry. The Python pipeline manually logs timing breakdowns (`v3_query_processing_ms`, `v3_retrieval_ms`, etc.) into `chat_runs`. With Mastra, per-step latency, input/output, and status come free in Mastra Studio and any OTel-compatible backend.

### 12. `.branch()` is cleaner than the Python intent gating

The Python pipeline has `if not qr.should_proceed: return direct_response` scattered through `pipeline.py`. Mastra's `.branch()` makes this explicit and visible in the workflow graph. Studio's graph view shows which branch was taken for each run.

### 13. Unified index simplifies retrieval

Going from 4-5 tables to 2 indexes (with metadata filtering) eliminates the `ThreadPoolExecutor` + per-table SQL + column-name mapping complexity. The Python pipeline has `CHUNK_TABLES` config with per-table column mappings, ID column names, tsvector column names — all of which collapse into a single query per index with `filter: { publisher: { $ne: 'dgafp' } }`.

### 14. Type safety across the pipeline

Zod schemas at every step boundary catch shape mismatches at definition time, not at runtime. The Python pipeline passes untyped dicts between stages — the Mastra port catches a missing `section_id` field before any code runs.

### 15. Streaming workflow events

Mastra workflows support `.stream()` which emits events as steps complete. A frontend could show "classifying intent..." → "searching..." → "building context..." → streaming answer, all from a single workflow invocation. The Python pipeline only streams the generator output; earlier stages are opaque to the client.

### 16. Mastra Studio for interactive testing

Running `npm run dev` gives a local UI at `:4111` where you can paste a query, run the workflow, inspect every step's input/output, and replay individual steps. This replaces the Python evaluation scripts for quick debugging and is available from Phase 0.

---

## Action Items Before Implementation

| Priority | Issue | Status |
|----------|-------|--------|
| **P0** | Embedding fallback breaks with unified index | ✅ Resolved — dual indexes (`rag_chunks_albert` + `rag_chunks_scaleway`) |
| **P0** | No `content` column — tsvector must reference `metadata->>'text'` | ✅ Resolved — SQL migration updated in implementation plan |
| **P1** | Cross-step data flow not designed | ✅ Resolved — `stateSchema` with `query`, `config`, `history` |
| **P1** | `rag_chunks_test` missing from plan | Open — include in data migration scope |
| **P2** | Two database clients need coordinated pooling | Open — share pool or use Mastra's PostgresStore |
| **P2** | No observability replacement for `chat_runs` | Open — define minimum logging for debugging |
| **P3** | Mastra version pinning | ✅ Resolved — pinned to `@mastra/*@1.13.0` |
| **P3** | `initial_top_k` default is 15, not 20 | ✅ Resolved — analysis doc corrected |
