# Admin Config: Python vs Mastra Studio

The current Python admin page (`04_Admin_Config.py`) exposes three tabs of runtime configuration. This document maps every capability to what Mastra Studio provides natively, and defines the strategy for each gap.

---

## What Mastra Studio Provides

Mastra Studio (`localhost:4111`) is a development and operations UI with these built-in capabilities:

| Capability | Description |
|------------|-------------|
| **Agent chat** | Interactive chat with agents; dynamically switch models, adjust temperature and top-p at runtime |
| **Workflow visualization** | DAG graph view of workflow steps, real-time execution tracing, step-by-step JSON I/O |
| **Tool testing** | Run tools in isolation, inspect inputs/outputs |
| **Observability** | OpenTelemetry traces for model calls, tool executions, workflow steps, latency breakdown |
| **Scorers** | Attach quality scorers (relevancy, faithfulness, hallucination, etc.) to agents; view results per interaction |
| **Datasets & Experiments** | Upload test cases (CSV/JSON), run experiments against agents/workflows, compare results across runs |
| **Processors & Guardrails** | View input/output processors, token limiters, and guardrails attached to agents |
| **MCP** | List connected MCP servers and their tools |
| **REST API + Swagger** | Full OpenAPI spec at `/api/openapi.json`, interactive Swagger UI at `/swagger-ui` |
| **Auth & RBAC** | SSO (Okta), email/password, role-based access control for production deployments |

---

## Tab 1: RAG Parameters — Config Mapping

### Exact matches (Mastra Studio handles natively)

| Python Config | Mastra Studio | Notes |
|---------------|---------------|-------|
| `v3_generator_model` (model selector) | **Agent model switcher** | Studio lets you dynamically switch models on the agent detail page. The model list comes from registered providers. |
| `v3_temperature` (slider 0.0–1.5) | **Agent temperature/top-p** | Studio exposes temperature and top-p sliders when chatting with an agent. |
| `verbose_mode` | **Observability tab** | Studio's tracing replaces verbose logging entirely — every model call, tool execution, and step I/O is traced automatically. |

### No native equivalent — requires `rag_config` table (existing pattern)

These are domain-specific RAG pipeline parameters. Mastra Studio has no concept of "search mode" or "token budget" — these are internal to our workflow logic. The strategy is the same as Python: **read from `rag_config` at runtime**.

| Python Config | Type | Strategy |
|---------------|------|----------|
| `v3_context_mode` | enum: narrow/standard/wide | Read from `rag_config` in `contextBuilder` step. Determines token budget, doc-entire threshold, max sections. |
| `v3_search_mode` | enum: semantic/hybrid/lexical | Read from `rag_config` in `retriever` step. Controls which search paths execute. |
| `v3_token_budget` | int: 4000–16000 | Read from `rag_config`. Overrides the mode-derived default. |
| `v3_doc_entire_threshold` | int: 1000–6000 | Read from `rag_config`. Controls when full documents are included. |
| `v3_enable_selector` | bool | Read from `rag_config`. Toggles the `contextSelector` step (pass-through when false). |
| `v3_selector_model` | model ID | Read from `rag_config`. Determines which model the selector step uses. |
| `v3_triangulation_sections` | int: 0–5 | Read from `rag_config`. Controls publisher diversity enforcement. |
| `v3_tables` | list of publisher keys | Read from `rag_config`. Translates to metadata filters in unified index queries. |
| `v3_initial_top_k` | int: 5–30 | Read from `rag_config`. Controls chunks per query in the retriever. |
| `v3_alpha` | float: 0.0–1.0 | Read from `rag_config`. RRF weight for hybrid search (only visible when search_mode=hybrid). |
| `v3_enable_reranker` | bool | Read from `rag_config`. Toggles section-level reranking. |
| `v3_rerank_top_k` | int: 3–15 | Read from `rag_config`. Sections kept after reranking. |
| `v3_enable_escalation` | bool | Read from `rag_config`. Controls escalation behavior. |

**Implementation approach**: The existing `rag_config` table stays as the single source of truth. Each workflow step reads its relevant parameters via `lib/config.ts → getConfig()` at the start of execution. This preserves the existing runtime-configurable behavior without needing any UI in Mastra Studio.

**Admin UI replacement**: Three options, in order of pragmatism:

1. **Direct DB edit** (simplest). Admins update `rag_config` via a SQL client or a simple script. Low ceremony, appropriate if config changes are infrequent.

2. **Mastra custom tool** (recommended). Create a `updateConfig` tool registered in Mastra. Expose it via the REST API (`POST /api/tools/updateConfig/execute`). Admins call it via Swagger UI, curl, or a thin admin page. The tool validates inputs against the same rules as the Python `validate_config()`.

3. **Lightweight admin page**. Build a standalone HTML form (no framework) that calls the Mastra REST API. Serves the same role as the Streamlit page but decoupled from the pipeline codebase.

### Workflow input overrides (new capability)

Mastra Studio lets you run workflows with **custom JSON input**. This means any config parameter can be passed per-invocation without touching `rag_config`:

```json
{
  "query": "Quelle est la durée maximale d'un CDD ?",
  "configOverrides": {
    "searchMode": "hybrid",
    "tokenBudget": 12000,
    "enableSelector": false
  }
}
```

The workflow's `inputSchema` accepts optional `configOverrides`, and each step merges them with the DB config. This is strictly more powerful than the Python admin page, which can only set global state.

---

## Tab 2: System Prompts — Config Mapping

### Python capabilities

The Streamlit admin exposes a full prompt management system:
- List prompts by type (generator, llm_selector, intent_gating)
- Create, edit, duplicate, delete prompts
- View active prompt per pipeline stage
- Markdown preview
- Select which prompt is active for each stage (`v3_system_prompt_name`, `v3_selector_prompt_name`, `v3_intent_prompt_name`)

### Mastra Studio native

Studio shows the **system prompt** of each registered agent in the agent detail panel. You can view it, but **cannot edit it at runtime** — it's defined in code. Studio has no concept of a prompt library or prompt versioning.

### Strategy

**Keep the `system_prompts` table and the same DB-backed prompt loading pattern.** Each workflow step loads its prompt by name via `getPrompt(name)` from the database, with file fallback.

For the admin UI, the same three options apply:

1. **Direct DB edit**. Update `system_prompts` rows via SQL. The `content` column is plain text/markdown.

2. **Mastra custom tools** (recommended). Create three tools:
   - `listPrompts({ type })` → returns available prompts for a given type
   - `getPrompt({ name })` → returns prompt content
   - `updatePrompt({ name, content, description })` → validates and saves

   These are accessible via Swagger UI at `/swagger-ui` or programmatically via the REST API. This gives you prompt management without building any frontend.

3. **Lightweight admin page**. Same as above — a standalone form calling the Mastra API.

### Active prompt selection

The Python admin lets you select which prompt is "active" per stage (e.g., change the generator prompt from `system_prompt_V6_optimized.md` to `system_prompt_V7.md`). This maps to `rag_config` fields (`v3_system_prompt_name`, `v3_selector_prompt_name`, `v3_intent_prompt_name`), which the workflow steps already read. No new mechanism needed.

---

## Tab 3: Acronyms — Config Mapping

### Python capabilities

- List all acronyms with search/filter
- Inline edit (expansion, category) via data editor
- Add new acronym (acronym, expansion, category)
- Delete acronym
- View missing acronyms detected in user queries (with occurrence count, sample query, last seen date)
- Mark missing acronyms as treated
- Statistics (total count, categories, most recent addition)

### Mastra Studio native

No equivalent. Studio has no concept of dictionaries, lookup tables, or user-detected data gaps.

### Strategy

**Keep the `acronyms` and `acronyms_missing` tables as-is.** The `queryProcessor` step reads the acronym dict via `getAcronyms()` on every invocation.

For the admin UI:

1. **Mastra custom tools** (recommended). Create tools for CRUD operations:
   - `listAcronyms({ search?, category? })` → filtered list
   - `addAcronym({ acronym, expansion, category })` → insert with duplicate check
   - `updateAcronym({ acronym, expansion, category })` → update existing
   - `deleteAcronym({ acronym })` → remove
   - `listMissingAcronyms({ minOccurrences?, limit? })` → pending missing acronyms
   - `markAcronymTreated({ acronym })` → mark as handled

   All accessible via Swagger UI or REST API.

2. **Lightweight admin page**. A simple table editor UI calling the above API endpoints.

The "missing acronyms" feature requires that the `queryProcessor` step **writes** newly detected unknown acronyms to `acronyms_missing`. This is a side effect inside the step — straightforward to implement.

---

## Health Check

The Python admin page includes an inline health check (DB, Albert API, Scaleway API connectivity). Mastra Studio doesn't have a built-in health check panel, but the same functionality is available through:

- **Observability traces**: Failed model calls or DB queries are visible in the tracing tab
- **Custom health tool**: Create a `healthCheck` tool that pings DB, Albert, and Scaleway; run it from Studio's tool testing panel
- **Swagger endpoint**: Expose a `/api/health` endpoint on the Mastra server

---

## Summary

| Admin Feature | Mastra Studio | Gap Strategy |
|---------------|---------------|-------------|
| Model selection | ✅ Native (agent model switcher) | — |
| Temperature | ✅ Native (agent settings) | — |
| Verbose/debug logs | ✅ Native (observability tracing) | Better than Python — automatic per-step tracing |
| Workflow execution + inspection | ✅ Native (workflow graph + traces) | Better than Python — visual DAG, step-by-step I/O |
| Per-invocation config overrides | ✅ Native (custom workflow JSON input) | Better than Python — per-request overrides without global state change |
| Quality evaluation | ✅ Native (datasets, experiments, scorers) | Better than Python — structured experiments with comparison |
| REST API | ✅ Native (OpenAPI + Swagger) | — |
| Auth/RBAC | ✅ Native (SSO, roles, permissions) | — |
| RAG pipeline parameters | ❌ Not native | Read from `rag_config` table; expose via Mastra tools + Swagger |
| System prompt management | ❌ Not native | Keep `system_prompts` table; expose via Mastra tools + Swagger |
| Acronym dictionary | ❌ Not native | Keep `acronyms` table; expose via Mastra tools + Swagger |
| Missing acronyms tracking | ❌ Not native | Keep `acronyms_missing` table; queryProcessor writes detections |
| Health check | ❌ Not native | Custom tool or `/api/health` endpoint |

**Bottom line**: Mastra Studio covers the development/testing/observability surface well — better than Streamlit in most cases. The gap is domain-specific runtime config (RAG parameters, prompts, acronyms), which is best handled by keeping the existing DB tables and exposing CRUD via Mastra tools + REST API. A lightweight admin page can be added later if the Swagger UI proves insufficient for non-technical admins.
