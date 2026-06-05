# Mastra RAG Pipeline Implementation Plan

## Goal

Port the assistant-rh RAG V3 Clean pipeline (6 stages) from Python to Mastra TypeScript. Keep the same PostgreSQL database, same Albert API, same retrieval quality. No UI — just the core pipeline exposed as a Mastra workflow.

See [MASTRA_PORT_ANALYSIS.md](./MASTRA_PORT_ANALYSIS.md) for the complete analysis of the existing Python pipeline.

---

## Architecture Overview

```
Query
  |
  v
+---------------- Mastra Workflow: ragPipeline -----------------+
|                                                                |
|  1. queryProcessor (step)    -> intent + reformulation (LLM)   |
|       |                                                        |
|       +-- branch: non-RAG intents -> short-circuit response    |
|       |                                                        |
|  2. retriever (step)         -> hybrid search (semantic + lex) |
|       |                                                        |
|  3. sectionAggregator (step) -> chunks -> sections + rerank    |
|       |                                                        |
|  4. contextSelector (step)   -> LLM filter (optional)          |
|       |                                                        |
|  5. contextBuilder (step)    -> token budget + triangulation    |
|       |                                                        |
|  6. generator (step)         -> streaming LLM answer           |
|                                                                |
+----------------------------------------------------------------+
```

**Key decision**: This is a **Workflow**, not an Agent. Every step is deterministic and predefined. The LLM is called inside specific steps (query processing, context selection, generation), but the orchestration is fixed.

---

## Data Model

### Mastra PgVector: Dual Indexes

Two PgVector indexes to preserve the embedding fallback chain. All publishers in each index, differentiated by metadata.

```ts
// Primary: Albert embeddings (1024d)
await pgVector.createIndex({
  indexName: 'rag_chunks_albert',
  dimension: 1024,
})

// Fallback: Scaleway BGE embeddings (3584d)
await pgVector.createIndex({
  indexName: 'rag_chunks_scaleway',
  dimension: 3584,
})
```

The retriever queries whichever index matches the embedding model that succeeded (same pattern as the Python pipeline's dual-column approach). Both indexes contain the same chunks with the same metadata — only the embedding vectors differ.

Each chunk is upserted into both indexes with rich metadata:

```ts
// Upsert into both indexes (same metadata, different embeddings)
for (const indexName of ['rag_chunks_albert', 'rag_chunks_scaleway']) {
  await pgVector.upsert({
    indexName,
    vectors: indexName === 'rag_chunks_albert' ? albertEmbeddings : scalewayEmbeddings,
    metadata: chunks.map(chunk => ({
    text: chunk.text,
    publisher: chunk.publisher,        // 'matte' | 'service_public' | 'dgafp' | 'rgrh'
    section_id: chunk.sectionId,       // UUID, null for dgafp
    source_document_id: chunk.docId,   // UUID
    source_name: chunk.sourceName,
    section_path: chunk.sectionPath,
    role: chunk.role,                  // 'Q_ONLY' | 'QA_COMPOSITE' | 'A_ATOMIC' | 'TABLE'
    thematique: chunk.thematique,
    short_id: chunk.shortId,
    // DGAFP-specific
    number: chunk.number,              // article number for legal ref resolution
    cid: chunk.cid,                    // Legifrance CID
    url: chunk.url,
  })),
  })
}
```

### Hybrid Search: tsvector Extension

After `createIndex()` creates the Mastra table, run a one-time migration to add French full-text search:

```sql
-- Applied to both indexes
ALTER TABLE rag_chunks_albert
ADD COLUMN IF NOT EXISTS content_tsv tsvector
GENERATED ALWAYS AS (to_tsvector('french', metadata->>'text')) STORED;

CREATE INDEX IF NOT EXISTS idx_rag_chunks_albert_content_tsv
ON rag_chunks_albert USING GIN (content_tsv);

ALTER TABLE rag_chunks_scaleway
ADD COLUMN IF NOT EXISTS content_tsv tsvector
GENERATED ALWAYS AS (to_tsvector('french', metadata->>'text')) STORED;

CREATE INDEX IF NOT EXISTS idx_rag_chunks_scaleway_content_tsv
ON rag_chunks_scaleway USING GIN (content_tsv);
```

Note: Mastra's PgVector schema has no `content` column — text is stored in `metadata` JSONB. The generated column extracts it via `metadata->>'text'`.

This gives us:
- **Semantic search**: via Mastra's `pgVector.query()` (cosine similarity on `embedding`)
- **Lexical search**: via raw SQL on `content_tsv` (French tsvector with `ts_rank_cd`)
- **Hybrid (RRF)**: combine both in TypeScript with `k=60` constant

### Relational Tables (Keep As-Is)

These are NOT vector tables — they stay in their current schema:
- `rag_sections` — section markdown, heading_path, references_juridiques
- `rag_documents` — document metadata, full markdown, token_count, publisher
- `rag_config` — runtime JSONB config (single row)
- `system_prompts` — editable prompt templates
- `acronyms` / `acronyms_missing` — acronym dictionary

Access them via a shared `postgres` (node-postgres `pg`) or `postgres.js` client.

---

## Albert API Configuration

Albert is OpenAI-compatible. Use Mastra's inline model config:

```ts
// For agents/LLM calls
const ALBERT_CONFIG = {
  id: 'albert/openweight-large',
  url: 'https://albert.api.etalab.gouv.fr/v1',
}

// For embeddings (via Vercel AI SDK)
import { createOpenAI } from '@ai-sdk/openai'
const albert = createOpenAI({
  baseURL: 'https://albert.api.etalab.gouv.fr/v1',
  apiKey: process.env.ALBERT_API_KEY!,
})
const embeddingModel = albert.embedding('openweight-embeddings')
```

For the **reranking** endpoint (`/rerank`), Albert exposes a non-standard OpenAI-compatible endpoint. This will be a custom fetch call:

```ts
// POST https://albert.api.etalab.gouv.fr/v1/rerank
// Body: { model: 'openweight-rerank', query, documents, top_n }
```

### Scaleway Fallback

Same pattern with `createOpenAI({ baseURL: scalewayUrl })`. Circuit breaker logic (60s cooldown) implemented as a utility class.

---

## Project Structure

```
assistant-rh/
+-- main/                          # Python (existing)
+-- feat-mastra-pipeline/          # New worktree (TS)
|   +-- src/
|   |   +-- mastra/
|   |       +-- index.ts           # Mastra instance
|   |       +-- agents/
|   |       |   +-- rag-assistant.ts  # Agent wrapper for API layer
|   |       +-- workflows/
|   |       |   +-- rag-pipeline.ts
|   |       +-- steps/
|   |       |   +-- query-processor.ts
|   |       |   +-- retriever.ts
|   |       |   +-- section-aggregator.ts
|   |       |   +-- context-selector.ts
|   |       |   +-- context-builder.ts
|   |       |   +-- generator.ts
|   |       +-- tools/
|   |       |   +-- hybrid-search.ts
|   |       |   +-- reranker.ts
|   |       |   +-- db-lookup.ts
|   |       +-- routes/
|   |       |   +-- chat-completions.ts  # OpenAI Chat Completions endpoint
|   |       |   +-- responses.ts         # Optional: /v1/responses if exact path needed
|   |       +-- lib/
|   |       |   +-- albert.ts      # Albert/Scaleway providers
|   |       |   +-- db.ts          # Postgres client (relational)
|   |       |   +-- circuit-breaker.ts
|   |       |   +-- config.ts      # Runtime config from rag_config
|   |       +-- schemas/
|   |           +-- pipeline.ts    # Zod schemas for workflow I/O
|   +-- .env
|   +-- package.json
|   +-- tsconfig.json
```

---

## Implementation: Step by Step

### Phase 0: Project Setup

1. Create worktree `feat/mastra-pipeline` from `main`
2. Initialize Mastra project (manual setup, not CLI — we're in an existing repo)
3. Install (pin exact versions): `@mastra/core@1.13.0`, `@mastra/pg@1.13.0`, `@mastra/rag@1.13.0`, `@ai-sdk/openai`, `pg`, `zod`
4. Configure `tsconfig.json` with `ES2022` module
5. Set up `.env` with `SCW_POSTGRES_DSN`, `APP_DB_TARGET=scaleway`, `ALBERT_API_KEY`, `ALBERT_BASE_URL`, `SCALEWAY_API_KEY`, `SCALEWAY_BASE_URL`
6. Create `src/mastra/index.ts` with Mastra instance + PgVector

### Phase 1: Foundation (`lib/`)

**`lib/albert.ts`** — Provider setup
- Albert OpenAI-compatible provider (LLM + embeddings)
- Scaleway fallback provider
- Reranker function (custom HTTP to `/rerank`)

**`lib/db.ts`** — Relational DB client
- Shared `pg.Pool` for relational queries (sections, documents, acronyms, config, prompts)
- Helper functions: `getSections(sectionIds)`, `getDocument(docId)`, `getAcronyms()`, `getConfig()`, `getPrompt(name)`

**`lib/circuit-breaker.ts`** — Embedding fallback
- Albert -> Scaleway BGE with 60s cooldown on failure
- Same pattern for LLM fallback

**`lib/config.ts`** — Runtime config
- Load from `rag_config` JSONB (single row, id=1)
- Type-safe config interface matching Python's `RuntimeRAGConfig`

### Phase 2: Workflow Steps (`steps/`)

Each step is a `createStep()` with typed `inputSchema` / `outputSchema`.

#### Step 1: `query-processor.ts`

```ts
const queryProcessorStep = createStep({
  id: 'query-processor',
  inputSchema: z.object({
    query: z.string(),
    conversationHistory: z.array(messageSchema).optional(),
  }),
  outputSchema: z.object({
    intent: intentEnum,
    theme: themeEnum,
    confidence: z.number(),
    needsLegalSearch: z.boolean(),
    reformulatedQuery: z.string(),
    queryForRetrieval: z.string(),
    shouldProceed: z.boolean(),
    directResponse: z.string().nullable(),
    acronymsExpanded: z.string().nullable(),
  }),
  execute: async ({ inputData }) => {
    // 1. Load acronym dict from DB
    // 2. Regex acronym detection + expansion
    // 3. Build prompt from DB system_prompts (intent_unified.md)
    // 4. Single LLM call (Albert openweight-medium) -> JSON parse
    // 5. Intent gating: if non-RAG intent, set directResponse + shouldProceed=false
    // 6. Fallback on LLM failure: rag_query, confidence 0.5
  },
})
```

#### Step 2: `retriever.ts`

```ts
const retrieverStep = createStep({
  id: 'retriever',
  inputSchema: z.object({
    queryForRetrieval: z.string(),
    needsLegalSearch: z.boolean(),
    config: retrievalConfigSchema,
  }),
  outputSchema: z.object({
    chunks: z.array(chunkSchema),
  }),
  execute: async ({ inputData }) => {
    // 1. Embed query via Albert (with circuit breaker fallback to Scaleway)
    // 2. Semantic search: pgVector.query({ indexName: 'rag_chunks_albert' or 'rag_chunks_scaleway', queryVector, topK, filter })
    //    - Index selected based on which embedder succeeded (Albert → albert index, BGE → scaleway index)
    //    - Filter: exclude dgafp when !needsLegalSearch
    // 3. Lexical search: raw SQL with ts_rank_cd on content_tsv
    //    - to_tsquery('french', OR-linked words)
    // 4. Hybrid RRF merge (alpha=0.5, k=60)
    // 5. Return merged + scored chunks
  },
})
```

The hybrid search logic:

```ts
// RRF formula
function rrf(semanticRank: number, lexicalRank: number, alpha: number, k = 60): number {
  const semanticScore = 1 / (k + semanticRank)
  const lexicalScore = 1 / (k + lexicalRank)
  return alpha * semanticScore + (1 - alpha) * lexicalScore
}
```

#### Step 3: `section-aggregator.ts`

```ts
const sectionAggregatorStep = createStep({
  id: 'section-aggregator',
  inputSchema: z.object({ chunks: z.array(chunkSchema) }),
  outputSchema: z.object({ sections: z.array(sectionSchema) }),
  execute: async ({ inputData }) => {
    // 1. Group chunks by section_id (from metadata)
    // 2. For each section: weighted score = 0.5*max + 0.3*mean + 0.2*norm_count
    // 3. SQL join with rag_sections + rag_documents for metadata
    // 4. Rerank top 20 sections via Albert /rerank endpoint
    //    - Input: "# {heading}\n\n{markdown[:1500]}"
    //    - Fallback: keep aggregation order
    // 5. Return top section_rerank_top_k (default 10) sections
  },
})
```

#### Step 4: `context-selector.ts` (Optional)

```ts
const contextSelectorStep = createStep({
  id: 'context-selector',
  inputSchema: z.object({
    sections: z.array(sectionSchema),
    query: z.string(),
    enabled: z.boolean(),
  }),
  outputSchema: z.object({
    sections: z.array(sectionSchema),
    shortCircuit: z.boolean(),
    shortCircuitMessage: z.string().nullable(),
  }),
  execute: async ({ inputData }) => {
    // If !enabled, pass through
    // 1. Build prompt from DB (v3_selector_business.md)
    // 2. LLM call (Albert openweight-large) -> JSON { selected_ids, reason }
    // 3. If selected_ids empty -> shortCircuit = true
    // 4. If parse failure -> fallback top 5 by score
    // 5. Filter sections by selected_ids
  },
})
```

#### Step 5: `context-builder.ts`

```ts
const contextBuilderStep = createStep({
  id: 'context-builder',
  inputSchema: z.object({
    sections: z.array(sectionSchema),
    config: contextBuildConfigSchema, // STANDARD vs WIDE
  }),
  outputSchema: z.object({
    context: z.string(),
    contextItems: z.array(contextItemSchema),
    legalRefs: z.array(legalRefSchema),
    tokenCount: z.number(),
  }),
  execute: async ({ inputData }) => {
    // 1. Doc-entire check: if top doc token_count <= threshold, load full doc_markdown
    // 2. Fill sections by descending score until token budget exhausted
    // 3. Triangulation: force 2 sections from secondary publishers (ignores budget)
    // 4. Legal ref resolution: collect references_juridiques -> lookup CIDs from chunks
    //    where publisher='dgafp' via metadata filter
    // 5. Format context: "### [Source N] Title (Publisher)\n```markdown\n{content}\n```"
    // Token estimation: text.length / 4
  },
})
```

#### Step 6: `generator.ts`

```ts
const generatorStep = createStep({
  id: 'generator',
  inputSchema: z.object({
    context: z.string(),
    query: z.string(),
    conversationHistory: z.array(messageSchema).optional(),
    systemPromptName: z.string(),
  }),
  outputSchema: z.object({
    answer: z.string(),
  }),
  execute: async ({ inputData }) => {
    // 1. Load system prompt from DB (system_prompt_V6_optimized.md)
    // 2. Replace {today} placeholder
    // 3. Build user message with context + question template (French)
    // 4. Call Albert openweight-large (streaming)
    //    - Fallback to Scaleway Llama if fails before first token
    // 5. Return full answer text
  },
})
```

### Phase 3: Workflow Composition (`workflows/rag-pipeline.ts`)

```ts
export const ragPipeline = createWorkflow({
  id: 'rag-pipeline',
  inputSchema: z.object({
    query: z.string(),
    conversationHistory: z.array(messageSchema).optional(),
  }),
  // Shared state accessible by all steps via context.state
  // Carries data that multiple downstream steps need (query, history, config)
  stateSchema: z.object({
    query: z.string(),
    conversationHistory: z.array(messageSchema).optional(),
    config: ragConfigSchema, // Runtime config from rag_config table
  }),
  outputSchema: z.object({
    answer: z.string(),
    intent: z.string(),
    theme: z.string(),
    // ...metadata
  }),
})
  .then(queryProcessorStep)
  .branch([
    // Non-RAG intents -> short-circuit
    [async ({ inputData }) => !inputData.shouldProceed, shortCircuitStep],
    // RAG intents -> continue pipeline
    [async ({ inputData }) => inputData.shouldProceed, ragContinuationWorkflow],
  ])
  .map(async ({ inputData }) => {
    // Merge branch outputs
    const result = inputData['short-circuit'] || inputData['rag-continuation']
    return result
  })
  .commit()

// ragContinuationWorkflow is a child workflow:
const ragContinuationWorkflow = createWorkflow({
  id: 'rag-continuation',
  // ... schemas
})
  .then(retrieverStep)
  .then(sectionAggregatorStep)
  .then(contextSelectorStep)
  .then(contextBuilderStep)
  .then(generatorStep)
  .commit()
```

Between steps, `.map()` transforms are used to reshape data as needed (e.g., injecting config from state, mapping outputSchema -> next inputSchema).

### Phase 4: Mastra Instance (`index.ts`)

```ts
import { Mastra } from '@mastra/core'
import { PgVector } from '@mastra/pg'
import { ragPipeline } from './workflows/rag-pipeline'
import { ragAssistant } from './agents/rag-assistant'
import { chatCompletionsRoute } from './routes/chat-completions'

const pgVector = new PgVector({
  id: 'pg-vector',
  connectionString: process.env.SCW_POSTGRES_DSN!,
})

export const mastra = new Mastra({
  agents: { ragAssistant },
  workflows: { ragPipeline },
  vectors: { pgVector },
  server: {
    apiRoutes: [chatCompletionsRoute],
  },
})
```

### Phase 5: API Integration

#### Agent Wrapper (`agents/rag-assistant.ts`)

The workflow is deterministic — it's not an agent. But Mastra's OpenAI-compatible routes (Responses API) are agent-backed. Solution: a thin agent wrapper whose single tool invokes the workflow.

```ts
import { Agent } from '@mastra/core'
import { createTool } from '@mastra/core'
import { z } from 'zod'
import { ragPipeline } from '../workflows/rag-pipeline'

// Tool that runs the RAG pipeline
const ragPipelineTool = createTool({
  id: 'rag-pipeline',
  inputSchema: z.object({
    query: z.string(),
    conversationHistory: z.array(z.object({
      role: z.enum(['user', 'assistant']),
      content: z.string(),
    })).optional(),
  }),
  outputSchema: z.object({
    answer: z.string(),
    intent: z.string(),
    theme: z.string(),
  }),
  execute: async ({ inputData }) => {
    const run = await ragPipeline.createRun()
    const result = await run.start({ inputData })
    return result.result
  },
})

// Thin agent wrapper — bridges API layer to workflow
export const ragAssistant = new Agent({
  id: 'rag-assistant',
  name: 'Assistant RH',
  instructions: 'You are a French HR assistant for contractual public employees. You have access to a RAG pipeline tool that retrieves and synthesizes answers from official documentation. Always use the rag-pipeline tool to answer questions.',
  model: {
    id: 'albert/openweight-large',
    url: process.env.ALBERT_BASE_URL,
  },
  tools: { ragPipelineTool },
})
```

#### Chat Completions Route (`routes/chat-completions.ts`)

Custom route that accepts OpenAI Chat Completions format and returns the same format.

```ts
import { registerApiRoute } from '@mastra/core/server'
import { z } from 'zod'

// Request schema (OpenAI Chat Completions)
const chatCompletionsRequestSchema = z.object({
  model: z.string(),
  messages: z.array(z.object({
    role: z.enum(['system', 'user', 'assistant', 'developer']),
    content: z.string(),
  })),
  stream: z.boolean().optional().default(false),
  temperature: z.number().optional(),
  max_tokens: z.number().optional(),
})

export const chatCompletionsRoute = registerApiRoute('/v1/chat/completions', {
  method: 'POST',
  handler: async (c) => {
    const mastra = c.get('mastra')
    const body = await c.req.json()
    const { messages, stream, temperature, max_tokens } = chatCompletionsRequestSchema.parse(body)

    // Extract query from last user message
    const lastUserMessage = messages.filter(m => m.role === 'user').pop()
    if (!lastUserMessage) {
      return c.json({ error: 'No user message found' }, 400)
    }

    // Build conversation history (exclude last message)
    const conversationHistory = messages
      .slice(0, -1)
      .filter(m => m.role === 'user' || m.role === 'assistant')
      .map(m => ({ role: m.role, content: m.content }))

    // Get agent and run via tool
    const agent = mastra.getAgent('rag-assistant')

    if (!stream) {
      // Non-streaming: return full response
      const result = await agent.generate([
        { role: 'user', content: lastUserMessage.content },
      ], {
        context: { conversationHistory },
        temperature,
        maxTokens: max_tokens,
      })

      // Transform to OpenAI Chat Completions format
      return c.json({
        id: `chatcmpl-${Date.now()}`,
        object: 'chat.completion',
        created: Math.floor(Date.now() / 1000),
        model: body.model,
        choices: [{
          index: 0,
          message: {
            role: 'assistant',
            content: result.text,
          },
          finish_reason: 'stop',
        }],
        usage: {
          prompt_tokens: result.usage?.promptTokens || 0,
          completion_tokens: result.usage?.completionTokens || 0,
          total_tokens: (result.usage?.promptTokens || 0) + (result.usage?.completionTokens || 0),
        },
      })
    } else {
      // Streaming: SSE format
      const streamResult = await agent.stream([
        { role: 'user', content: lastUserMessage.content },
      ], {
        context: { conversationHistory },
        temperature,
        maxTokens: max_tokens,
      })

      // Transform Mastra stream to OpenAI SSE format
      const encoder = new TextEncoder()
      const readable = new ReadableStream({
        async start(controller) {
          const chatId = `chatcmpl-${Date.now()}`

          // Stream text deltas
          for await (const chunk of streamResult.textStream) {
            const data = JSON.stringify({
              id: chatId,
              object: 'chat.completion.chunk',
              created: Math.floor(Date.now() / 1000),
              model: body.model,
              choices: [{
                index: 0,
                delta: { content: chunk },
                finish_reason: null,
              }],
            })
            controller.enqueue(encoder.encode(`data: ${data}\n\n`))
          }

          // Final chunk
          const finalData = JSON.stringify({
            id: chatId,
            object: 'chat.completion.chunk',
            created: Math.floor(Date.now() / 1000),
            model: body.model,
            choices: [{
              index: 0,
              delta: {},
              finish_reason: 'stop',
            }],
          })
          controller.enqueue(encoder.encode(`data: ${finalData}\n\n`))
          controller.enqueue(encoder.encode('data: [DONE]\n\n'))
          controller.close()
        },
      })

      return new Response(readable, {
        headers: {
          'Content-Type': 'text/event-stream',
          'Cache-Control': 'no-cache',
          'Connection': 'keep-alive',
        },
      })
    }
  },
})
```

**Notes on the implementation:**
- Route is mounted at `/v1/chat/completions` (exact OpenAI path, no `/api` prefix)
- Client sets `baseURL` to `http://server:4111` and SDK appends `/v1/chat/completions`
- Non-streaming returns full JSON response with usage stats
- Streaming transforms Mastra's `textStream` to OpenAI's SSE chunk format (`data: {...}\n\n`)
- Stream ends with `data: [DONE]\n\n`

#### Responses API Path Option

Mastra's built-in Responses API is at `/api/v1/responses`. If the exact path `/v1/responses` is needed (without `/api`), a similar custom route can be added. Otherwise, clients point to `http://server:4111/api` and the SDK appends `/v1/responses`.

#### Parallel Operation Constraint

Both Python (Streamlit) and Mastra pipelines run simultaneously during evaluation:

- **Shared DB (reads)**: Both read from `rag_sections`, `rag_documents`, `rag_config`, `system_prompts`, `acronyms`
- **Separate vector stores**: Python uses existing `rag_chunks_*` tables; Mastra uses new PgVector indexes
- **Shared ingestion**: Python ingestion continues; one-time migration populates Mastra indexes
- **Config sync**: Changes to `rag_config` apply to both immediately
- **Evaluation**: Same queries against both endpoints, compare answers

---

## Modules Not Ported

Per the analysis, these are excluded:
- Streamlit UI (pages 01-11, Home.py)
- `src/ui/` components
- Chat logging to `chat_runs` (replaced by Mastra observability/tracing)
- Admin Config Streamlit page (replaced by Mastra custom tools + REST API + Swagger UI; DB tables preserved)
- Feedback analysis pipeline
- Data ingestion pipeline (stays in Python, migration script planned separately)

## What is Added

- **Agent wrapper** (`rag-assistant.ts`) — bridges API layer to workflow, enabling both Chat Completions and Responses API endpoints
- **Chat Completions route** (`chat-completions.ts`) — OpenAI-compatible `/v1/chat/completions` endpoint (primary integration interface)
- **Optional Responses route** — exact `/v1/responses` path if needed (Mastra provides `/api/v1/responses` by default)
- **Parallel operation** — both Python and Mastra pipelines run simultaneously during evaluation, sharing the same PostgreSQL database

---

## Implementation Order

For milestone-by-milestone PR sequencing and conformance gates, see [MASTRA_PR_MILESTONES_PLAN.md](./MASTRA_PR_MILESTONES_PLAN.md).

| Phase | Scope | Deliverable |
|-------|-------|-------------|
| 0 | Project setup | Worktree, deps, Mastra instance, PgVector, env |
| 1 | Foundation | `lib/albert.ts`, `lib/db.ts`, `lib/circuit-breaker.ts`, `lib/config.ts` |
| 2a | Steps 1-2 | `query-processor.ts`, `retriever.ts` (with hybrid search) |
| 2b | Steps 3-4 | `section-aggregator.ts`, `context-selector.ts` |
| 2c | Steps 5-6 | `context-builder.ts`, `generator.ts` |
| 3 | Workflow | `rag-pipeline.ts` — compose all steps |
| 4 | Test | Manual testing via Mastra Studio (`npm run dev`) |
| 5 | API integration | `agents/rag-assistant.ts`, `routes/chat-completions.ts`, optional Responses route |

---

## Key Files to Read Before Implementation

| File | Why |
|------|-----|
| `src/rag_v3_clean/query_processor.py` | Intent prompt template, JSON parsing, acronym regex |
| `src/rag_v3_clean/retriever.py` | Hybrid RRF formula, table-specific SQL, embedding fallback |
| `src/rag_v3_clean/section_aggregator.py` | Weighted score formula, section SQL join, reranker call |
| `src/rag_v3_clean/context_selector.py` | Selector prompt, JSON parsing, fallback logic |
| `src/rag_v3_clean/context_builder.py` | Token budget, doc-entire, triangulation, legal ref resolution |
| `src/rag_v3_clean/generator.py` | User prompt template, streaming, fallback chain |
| `src/rag_v3_clean/config.py` | All config constants, table mappings, enums |
| `src/rag_v3_clean/embedder.py` | FallbackEmbedder, circuit breaker logic |
| `src/rag_v3_clean/reranker.py` | AlbertReranker, /rerank API call format |
| `src/rag_v3_clean/prompts/*.md` | System prompt templates (fallback files) |

---

## Risks and Mitigations

| Risk | Mitigation |
|------|-----------|
| Albert API rate limits | Circuit breaker + Scaleway fallback (same as Python) |
| Embedding dimension mismatch on fallback | Dual indexes (`rag_chunks_albert` 1024d + `rag_chunks_scaleway` 3584d); retriever queries the index matching the embedder that succeeded |
| Mastra PgVector schema has no `content` column | tsvector generated column references `metadata->>'text'` instead |
| DGAFP chunks lack section_id | Handle in aggregator: DGAFP chunks bypass section grouping, scored individually |
| Reranking is non-standard API | Custom fetch wrapper, graceful fallback to aggregation order |
| Cross-step data flow | Workflow `stateSchema` carries `query`, `conversationHistory`, and `config` across all steps |
| Mastra version instability | Pin exact versions (`@mastra/core@1.13.0`, `@mastra/pg@1.13.0`, `@mastra/rag@1.13.0`) |
| Chat Completions format drift | Custom route tested against OpenAI SDK; stream format matches OpenAI SSE spec exactly |
| Responses API experimental | Chat Completions is primary; Responses API is secondary and uses Mastra's built-in route |
