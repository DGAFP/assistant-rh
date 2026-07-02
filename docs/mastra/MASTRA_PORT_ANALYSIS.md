# RAG Pipeline Analysis: assistant-rh → Mastra Port

## Overview

The `DGAFP/assistant-rh` project is a French government HR chatbot for the Ministry of Ecological Transition (MATTE), answering questions about contractual public employees (FPE). The active RAG pipeline (`src/rag_v3_clean/`) is a 6-stage orchestration backed by PostgreSQL + pgvector, using Albert (DINUM) and Scaleway as LLM/embedding providers.

**What to port**: The complete RAG pipeline logic (minus Streamlit UI) into Mastra TypeScript, connected to the same PostgreSQL database (via DSN) and using the Albert API for LLM/embeddings (configured in .env as `ALBERT_BASE_URL`).

---

## Pipeline Architecture (6 stages)

```
Query → QueryProcessor → Retriever → SectionAggregator → ContextSelector → ContextBuilder → Generator
         (intent gate)    (pgvector)   (chunk→section)    (LLM filter)     (token budget)   (streaming)
```

---

## Stage 1: QueryProcessor (`query_processor.py`)

**Purpose**: Single LLM call for intent classification + theme detection + query reformulation + legal-search flag.

### Parameters
| Parameter | Default | Description |
|-----------|---------|-------------|
| `enable_intent_gating` | `true` | Master toggle for intent classification |
| `intent_model` | `"openweight-medium"` | Albert model for intent (lighter, faster) |
| `intent_prompt_name` | `"intent_unified.md"` | Prompt template name (DB or file fallback) |
| `enable_acronym_expansion` | `true` | Regex-based acronym detection from DB `acronyms` table |
| `enable_hyde` | `false` | Hypothetical Document Embeddings (not used) |

### Intent Classes
- `rag_query` → proceed to RAG pipeline
- `follow_up` → proceed (reformulated with conversation context)
- `chit_chat` → short-circuit with greeting
- `out_of_scope` → short-circuit with scope message
- `clarification` → short-circuit asking for precision
- `document_request` → short-circuit explaining no document access

### HR Themes (15)
`recrutement`, `typologie_contrats`, `remuneration`, `renouvellement_mobilite`, `fin_contrat_licenciement`, `temps_de_travail`, `conges`, `formation`, `action_sociale`*, `psc`*, `sante_securite`, `retraite`*, `apprentis`*, `deontologie`, `autre`
(*starred = excluded from beta scope*)

### Behavior Details
- Acronym detection: case-sensitive regex against DB `acronyms` table (priority-ordered)
- Conversation history: last 8 messages, truncated to 300 chars each
- LLM output: JSON with `intent`, `theme`, `confidence`, `needs_legal_search`, `reformulated_query`, `query_for_retrieval`
- Fallback on LLM failure: defaults to `rag_query` with confidence 0.5
- `needs_legal_search`: enables DGAFP table for legal/regulatory questions

---

## Stage 2: Retriever (`retriever.py`)

**Purpose**: Parallel semantic/hybrid search across PostgreSQL chunk tables via pgvector.

### Parameters
| Parameter | Default | Description |
|-----------|---------|-------------|
| `search_mode` | `SearchMode.SEMANTIC` | `semantic`, `hybrid`, or `lexical` |
| `embedding_model` | `EmbeddingModel.ALBERT` | Primary: `albert` (1024d), alt: `bge_scaleway` (3584d) |
| `initial_top_k` | `15` | Chunks per table (overridable via `rag_config`) |
| `alpha` | `0.5` | RRF weight for hybrid (semantic vs lexical) |
| `tables` | `["matte", "service_public", "dgafp", "rgrh"]` | Active chunk tables |
| `enable_chunk_reranker` | `false` | Chunk-level reranking (unused) |
| `chunk_rerank_top_k` | `30` | Top-K for chunk reranking |

### Chunk Tables (PostgreSQL)
| Table | Publisher | ID Col | Text Col | Embed Albert Col | has_sections | tsvector |
|-------|-----------|--------|----------|-------------------|--------------|----------|
| `rag_chunks_matte` | MATTE | `hash_id` | `chunk_text` | `embedding_m3` | yes | `text_tsv` |
| `rag_chunks_service_public` | Service-Public | `hash_id` | `chunk_text` | `embedding_m3` | yes | `text_tsv` |
| `rag_chunks_dgafp` | DGAFP | `chunk_id` | `chunk_text` | `embedding_m3` | no | `chunk_text_tsv` |
| `rag_chunks_rgrh` | RGRH | `hash_id` | `chunk_text` | `embedding_m3` | no | `text_tsv` |

### Embedding Providers
| Provider | Model | Dimensions | Base URL |
|----------|-------|------------|----------|
| Albert (DINUM) | `openweight-embeddings` | 1024 | `https://albert.api.etalab.gouv.fr/v1` |
| Scaleway BGE | `bge-multilingual-gemma2` | 3584 | `<SCALEWAY_BASE_URL>` |

### Search Modes
- **Semantic**: Cosine distance via pgvector `<=>` operator. Score = `1 - distance`
- **Hybrid (RRF)**: Reciprocal Rank Fusion combining semantic + lexical (tsvector `ts_rank_cd`). RRF constant `k=60`
- **Lexical**: Pure `ts_rank_cd` on French tsvector columns. Uses OR-linked tsquery (any word matches)
- **Conditional DGAFP**: excluded when `needs_legal_search=false`; forced hybrid when `true`

### Embedding Fallback Chain
Albert → Scaleway BGE → None (triggers empty results). Circuit breaker: 60s cooldown on Albert failure.

### Parallelism
`ThreadPoolExecutor` with `max_workers = len(tables)`. All tables searched concurrently.

---

## Stage 3: SectionAggregator (`section_aggregator.py`)

**Purpose**: Group chunks by their parent `rag_sections` row, compute weighted score, rerank sections.

### Parameters
| Parameter | Default | Description |
|-----------|---------|-------------|
| `weight_max_score` | `0.5` | Weight for max chunk score in section |
| `weight_mean_score` | `0.3` | Weight for mean chunk score |
| `weight_chunk_count` | `0.2` | Weight for normalized chunk count |
| `enable_section_reranker` | `true` | Albert reranker for sections |
| `section_rerank_top_k` | `10` | Sections kept after reranking |

### Aggregation Formula
```
score = 0.5 × max(chunk_scores) + 0.3 × mean(chunk_scores) + 0.2 × (chunk_count / max_chunk_count)
```

### Section Metadata (SQL join)
```sql
SELECT s.section_id, s.heading, s.section_markdown, s.heading_path, s.references_juridiques,
       d.title, d.source_url, d.token_count, d.publisher, d.last_updated_date
FROM rag_sections s LEFT JOIN rag_documents d ON d.doc_id = s.doc_id
WHERE s.section_id = ANY(...)
```

### Reranking
- Model: `openweight-rerank` (Albert API `/rerank` endpoint)
- Input: `"# {heading}\n\n{markdown[:1500]}"` per section
- Max input: 20 sections (overflow dropped before reranking)
- Fallback on API failure: keep aggregation order, truncated to `section_rerank_top_k`

---

## Stage 4: ContextSelector (`context_selector.py`) — Optional

**Purpose**: LLM-based filter that reviews sections and drops irrelevant ones.

### Parameters
| Parameter | Default | Description |
|-----------|---------|-------------|
| `enabled` | `false` (code), `true` (prod) | Master toggle |
| `provider` | `LLMProvider.ALBERT` | LLM provider |
| `model` | `"openweight-large"` | Model for selection |
| `temperature` | `0.0` | Deterministic |
| `prompt_name` | `"v3_selector_business.md"` | Prompt template |

### Behavior
- Sends all sections numbered `[0]...[N]` with heading + markdown to LLM
- LLM returns JSON `{"selected_ids": [0, 2, 5], "reason": "..."}`
- **Explicit empty** (`selected_ids: []`): pipeline short-circuits with "no relevant info" message
- **Parse failure**: fallback to top 5 sections by reranker score
- Source priority in prompt: MATTE > Service-Public > DGAFP

---

## Stage 5: ContextBuilder (`context_builder.py`)

**Purpose**: Select sections for the LLM prompt under a token budget, with doc-entire inclusion, source triangulation, and legal reference injection.

### Parameters (Two modes: STANDARD / WIDE)
| Parameter | STANDARD | WIDE | Description |
|-----------|----------|------|-------------|
| `token_budget` | 8,000 | 12,000 | Max tokens for context |
| `max_full_docs` | 1 | 2 | Docs included entirely |
| `doc_entire_threshold` | 3,500 | 5,000 | Max tokens for doc-entire |
| `max_sections` | 12 | 20 | Max sections in context |
| `triangulation_sections` | 2 | 2 | Min sections from secondary publishers |
| `legal_refs_budget` | 1,000 | 2,000 | Token budget for legal references |

### 4-Step Strategy
1. **Doc-entire**: If top document's `token_count` ≤ threshold, load full `doc_markdown` from `rag_documents`
2. **Top sections fill**: Add sections by descending score until budget exhausted
3. **Triangulation**: Always add 2 sections from publishers other than the primary (ignores budget!)
4. **Legal references**: Collect `references_juridiques` from sections, resolve CIDs from `rag_chunks_dgafp`, inject as formatted text

### Token Estimation
`len(text) // 4` (rough estimate for French text)

### Context Formatting for Prompt
````markdown
### [Source 1] Document Title (Publisher)

```markdown
{content}
```

---
````

---

## Stage 6: Generator (`generator.py`)

**Purpose**: Stream LLM answer using context + system prompt.

### Parameters
| Parameter | Default | Description |
|-----------|---------|-------------|
| `provider` | `LLMProvider.ALBERT` | Primary LLM |
| `model` | `"openweight-large"` | Primary model |
| `temperature` | `0.0` | Deterministic |
| `system_prompt_name` | `"system_prompt_V6_optimized.md"` | System prompt |
| `fallback_provider` | `LLMProvider.SCALEWAY` | Fallback LLM |
| `fallback_model` | `"llama-3.1-70b-instruct"` | Fallback model |

### User Prompt Template
```
Voici le contexte documentaire pour repondre a la question :

{context}

---

**Question de l'utilisateur :** {question}

---

En vous appuyant uniquement sur les sources ci-dessus, repondez de maniere claire et operationnelle.
Si les sources ne permettent pas de repondre, dites-le explicitement et n'inventez pas.
```

### System Prompt (V6 Optimized) — Key Points
- Role: HR assistant for MATTE ministry, contractual FPE employees
- Source priority cascade: MATTE (ministry-specific) > Service-Public (interministerial) > Regulatory texts (raw law)
- Citation rules: no numbered refs `[1][2]`, no source list at end, reformulate rather than quote
- Temporal awareness: uses `{today}` placeholder
- Contradiction handling: signal legal vs practical differences
- Anti-hallucination: explicit instruction to say when sources insufficient

### Fallback
- If primary (Albert) fails **before first token**: retry on Scaleway Llama
- If fails **mid-stream**: yield partial + error marker
- Conversation history passed to LLM for multi-turn context

---

## Data Ingestion Pipeline (Service Public example)

### Medallion Architecture
```
Bronze → Silver → Gold → DB
(raw XML)  (docs + sections)  (chunks + embeddings)
```

### Chunking Strategy (QnA-based)
- Parse markdown into Q&A blocks using regex patterns
- Chunk roles: `Q_ONLY` (question), `QA_COMPOSITE` (Q+A combined, max 1500 chars), `A_ATOMIC` (answer paragraphs), `TABLE` (tabular data)
- `max_chars`: 1200 per chunk
- `overlap`: 200 chars
- Paragraph-based splitting with hard-wrap fallback
- Hash-based deduplication (`hash_id = sha1(source_name|qa_id|role|chunk_index|text[:256])`)

### Embedding at Ingestion
- Primary: `BAAI/bge-m3` via sentence-transformers (local), stored as `embedding_m3` (1024d)
- Optional: `bge-multilingual-gemma2` via Scaleway API, stored as `embedding_bge_scw` (3584d)
- Batch size: 32, L2 normalized

---

## Database Schema Summary

### Core Tables
| Table | Purpose |
|-------|---------|
| `rag_documents` | Document metadata + full markdown (`doc_id` UUID PK) |
| `rag_sections` | Section-level markdown + hierarchy (`section_id` UUID PK, `doc_id` FK) |
| `rag_chunks_matte` | MATTE chunks with embeddings (`hash_id` PK, `section_id` FK) |
| `rag_chunks_service_public` | Service-Public chunks (same schema as matte) |
| `rag_chunks_dgafp` | DGAFP regulatory chunks (`chunk_id` PK, no sections, has `number/cid/url`) |
| `rag_chunks_rgrh` | RGRH chunks (same schema as matte) |

### pgvector Columns
| Column | Dimensions | Model |
|--------|-----------|-------|
| `embedding_m3` | 1024 | Albert (BAAI/bge-m3) |
| `embedding_bge_scw` | 3584 | BGE Multilingual Gemma2 (Scaleway) |

Search uses cosine distance operator `<=>`. Score = `1 - (a <=> b)`.

### Config/Reference Tables
| Table | Purpose |
|-------|---------|
| `rag_config` | Runtime config (single-row JSONB, id=1) |
| `system_prompts` | Editable prompt templates (name PK, content, prompt_type) |
| `acronyms` | Acronym → expansion dictionary (priority-ordered) |
| `acronyms_missing` | User-detected unknown acronyms |

### Observability Tables
| Table | Purpose |
|-------|---------|
| `chat_runs` | Full interaction logs (~120 columns, per-turn) |
| `chat_feedbacks` | User feedback + LLM analysis |
| `chat_reviews` | Manual review tracking |

### Evaluation Tables
| Table | Purpose |
|-------|---------|
| `goldset_questions_v2` | Evaluation questions with gold answers |
| `goldset_runs` | Pipeline execution results on goldset |
| `intent_eval_goldset` | Intent classification evaluation dataset |
| `pipeline_eval_experiments` | Full pipeline evaluation results |
| `retrieval_eval_runs` | Retrieval configuration comparison |

---

## Environment Variables

| Variable | Required | Purpose |
|----------|----------|---------|
| `SCW_POSTGRES_DSN` | Yes | Canonical PostgreSQL/pgvector DSN for Scaleway runtime environments |
| `ALBERT_API_KEY` | Yes | DINUM Albert API (LLM + embeddings + reranking) |
| `ALBERT_BASE_URL` | Yes | Albert API base URL — no code-level default; if unset, the OpenAI client falls back to `https://api.openai.com/v1` which will fail |
| `SCALEWAY_API_KEY` | No | Fallback LLM + embeddings |
| `SCALEWAY_BASE_URL` | No | Scaleway API base URL — no code-level default; same fallback behavior as `ALBERT_BASE_URL` if unset |

---

## LLM Models Used

| Use | Provider | Model ID | Purpose |
|-----|----------|----------|---------|
| Intent classification | Albert | `openweight-medium` | Fast, lighter model |
| Context selection | Albert | `openweight-large` | Better reasoning |
| Generation (primary) | Albert | `openweight-large` | Best quality |
| Generation (fallback) | Scaleway | `llama-3.1-70b-instruct` | Reliability fallback |
| Embedding (primary) | Albert | `openweight-embeddings` | 1024d vectors |
| Embedding (fallback) | Scaleway | `bge-multilingual-gemma2` | 3584d vectors |
| Reranking | Albert | `openweight-rerank` | BGE-m3 backend |

---

## Key Design Decisions to Preserve in Mastra Port

1. **Single LLM call for query processing** — intent + theme + reformulation + legal flag in one shot
2. **Parallel multi-table retrieval** — ThreadPool equivalent needed (Promise.all in TS)
3. **Section-level aggregation** — chunks are just retrieval units; sections are the context units
4. **Weighted scoring formula** — `0.5*max + 0.3*mean + 0.2*norm_count`
5. **Reranking at section level** — not chunk level
6. **Token budget with doc-entire** — small docs included whole
7. **Triangulation** — guaranteed publisher diversity (ignores budget)
8. **Legal reference resolution** — cross-table CID lookup from `rag_chunks_dgafp`
9. **Conditional DGAFP search** — only when intent signals legal need
10. **Fallback chains everywhere** — embedding, LLM, reranking all have graceful degradation
11. **All prompts DB-backed** — editable at runtime via admin panel, file fallback
12. **Circuit breaker** — for Albert embedding failures (60s cooldown)

---

## What NOT to Port (UI-only)

- Streamlit pages (01-11) and `Home.py`
- `src/ui/` components (chatbot_feedback, chatbot_styles, etc.)
- PDF Viewer
- Chat logging to `chat_runs` (may be replaced with Mastra observability)
- Admin Config page (runtime config can be managed differently)
- Feedback analysis pipeline

---

## Latency Profile (full pipeline)

| Stage | Typical | % of Total |
|-------|---------|------------|
| QueryProcessor (intent) | 200-500ms | 10-15% |
| Retriever (embedding + pgvector) | 300-800ms | 15-25% |
| SectionAggregator (SQL + rerank) | 200-500ms | 10-15% |
| ContextSelector (LLM) | 300-800ms | 10-25% |
| ContextBuilder (SQL + logic) | 50-100ms | 2-5% |
| Generator (streaming LLM) | 1000-5000ms | 40-60% |
| **Total** | **2-8s** | 100% |
