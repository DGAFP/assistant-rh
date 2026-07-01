# Mastra pipeline port — implementation plan (compatibility-first)

> **Status (2026-06-17): Mastra implementation is PAUSED.**
>
> Per Paul's reprioritization, the Mastra pipeline port described in this
> document is currently paused. The compatibility-first PR milestones below
> remain the *plan of record* for when work resumes; no milestone PRs should
> be opened or merged while the port is paused unless Mastra work is
> explicitly resumed.
>
> **Priority implications while paused:**
>
> - Conformance failures (Conformance Nightly, Mastra-vs-Python parity,
>   replay-cache misses on the Mastra path) are classified as
>   **informational / backlog**. They are not P0 operational work and must
>   not block other PRs or daily triage.
> - The production path remains the Python v3 RAG pipeline in
>   `packages/rag-pipeline/`. Conformance tooling (`tests/conformance/`,
>   `scripts/run_mastra_conformance.py`, the replay-cache helpers, and the
>   associated workflows) is retained as documentation/tooling so the
>   pending plan stays reproducible when work resumes, but it is **not** a
>   P0 production gate during the pause.
> - Current P0 work is **RAG / data quality**: missing DGAFP embeddings,
>   source ingestion audits (DGAFP, MATTE, Service-Public, RGRH), and other
>   data-coverage issues tracked separately from the Mastra port.
>
> This note is factual and reversible: it does not delete historical
> implementation details, and it can be removed when Mastra work is
> explicitly resumed.

## Objective

Implement the Mastra pipeline port in a **new worktree** as a sequence of focused, testable PRs, while preserving behavioral parity with the current Python v3 custom pipeline.

## Context checked

- Repository: `assistant-rh` (moonrepo structure)
- Current worktree: `main` (clean)
- Mastra docs reviewed in `docs/`:
  - `MASTRA_IMPLEMENTATION_PLAN.md`
  - `MASTRA_PORT_ANALYSIS.md`
  - `MASTRA_PORT_EXECUTIVE_SUMMARY.md`
  - `MASTRA_PORT_RISK_ANALYSIS.md`
  - `MASTRA_STUDIO_CONFIG_ANALYSIS.md`
  - `MIGRATION_COMPATIBILITY_FIRST.md`
- Current code reference for parity: `packages/rag-pipeline/src/assistant_rh_rag_pipeline/*`

## Guardrails (non-negotiable)

1. **Compatibility first**: no unvalidated functional drift.
2. **Same providers/fallback chain** (Albert primary, Scaleway fallback).
3. **Same retrieval semantics** (hybrid behavior, legal-search gating, source filters).
4. **Same runtime config model** (`rag_config`, `system_prompts`, `acronyms`).
5. **Parallel comparability**: every milestone must be testable against Python baseline.

## Worktree + branch strategy

1. Create a dedicated worktree for porting, e.g.:
   - worktree dir: `../feat-mastra-pipeline`
   - branch: `feat/mastra-pipeline-foundation`
2. Keep one branch per milestone PR (`feat/mastra-<milestone>`), rebased/stacked from previous milestone for clean review.
3. No changes in `main` worktree while porting.

## PR milestones

### PR1 — Conformance harness baseline (first, before feature port)

**Scope**
- Create `tests/conformance/` baseline framework.
- Add runner scripts to execute the same query set against:
  - Python v3 pipeline (baseline)
  - Mastra candidate endpoint (initially can be stub)
- Persist comparable artifacts (JSON) per stage + final answer.

**Why first**
- Avoid subjective parity judgments later.
- Gives immediate regression signal for every subsequent PR.

**Exit criteria**
- One command produces a structured comparison report.
- Baseline fixtures committed (golden queries + expected schema).

---

### PR2 — Mastra app foundation + infra plumbing

**Scope**
- Scaffold `apps/mastra-pipeline/` app.
- Pin Mastra versions (per risk doc) and TS config.
- Add DB/provider/config foundation:
  - `lib/db.ts`, `lib/config.ts`, `lib/albert.ts`, `lib/circuit-breaker.ts`
- Health endpoint/tooling and startup scripts.

**Exit criteria**
- App boots locally.
- DB + provider connectivity validated.
- `rag_config` read path working.

---

### PR3 — QueryProcessor parity (intent/theme/reformulation)

**Scope**
- Implement `steps/query-processor.ts`:
  - intent classes
  - acronym expansion behavior
  - legal-search flag (`needsLegalSearch`)
  - fallback behavior on LLM failure

**Exit criteria**
- Intent gating branch data is produced in workflow state.
- Match-rate thresholds against Python baseline reached (see metrics section).

---

### PR4 — Retriever parity (dual-index + hybrid search)

**Scope**
- Implement dual PgVector indexes:
  - `rag_chunks_albert` (1024d)
  - `rag_chunks_scaleway` (3584d)
- Add tsvector generated column from `metadata->>'text'`.
- Implement hybrid retrieval + RRF and legal-search table/filter behavior.

**Exit criteria**
- Correct index chosen based on embedding provider used.
- Top-k overlap and latency envelopes pass vs Python baseline.

---

### PR5 — Section aggregation + reranking parity

**Scope**
- Implement `steps/section-aggregator.ts`.
- Preserve weighted score formula and grouping logic.
- Integrate Albert reranker endpoint with robust fallback.

**Exit criteria**
- Section ranking correlation meets threshold.
- Fallback behavior matches Python semantics on reranker errors.

---

### PR6 — Context selector + context builder parity

**Scope**
- Implement `steps/context-selector.ts` and `steps/context-builder.ts`.
- Preserve STANDARD/WIDE behavior:
  - token budgets
  - doc-entire inclusion
  - triangulation rules
  - legal refs injection

**Exit criteria**
- Selection overlap + token-budget drift + source-diversity checks pass.

---

### PR7 — Generator + full workflow orchestration

**Scope**
- Implement `steps/generator.ts` and `workflows/rag-pipeline.ts`.
- Wire branching for non-RAG intents.
- Stream output and preserve fallback LLM behavior.

**Exit criteria**
- End-to-end workflow stable in Mastra Studio.
- Answer similarity and operational metrics within target envelopes.

---

### PR8 — OpenAI-compatible endpoint + CI parity gate

**Scope**
- Add `/v1/chat/completions` route (stream + non-stream).
- Add observability minimums replacing Python `chat_runs` needs:
  - per-step timings
  - selected sources/ids
  - fallback triggers
- Integrate conformance checks into CI as merge gate.

**Exit criteria**
- Endpoint contract validated by schema and SDK smoke tests.
- CI blocks regressions above defined threshold.

## Conformance testing strategy

### Datasets

1. Goldset (existing evaluation queries).
2. Edge suite:
   - legal-search required/not-required
   - follow-up with history
   - acronym-heavy queries
   - out-of-scope / chit-chat / clarification
3. Stress suite:
   - concurrency
   - long context / high token load

### Comparison levels

1. **Step-level parity** (primary signal):
   - intent/theme + reformulation
   - retrieved chunks
   - reranked sections
   - selected sections
   - context structure/tokens
2. **End-to-end parity**:
   - semantic similarity of final answer
   - citation/source overlap
   - refusal/short-circuit behavior
3. **Operational parity**:
   - TTFT and total latency
   - fallback trigger rates
   - error rates

### Initial acceptance thresholds

- Intent class match: **>= 95%**
- Retrieval top-k overlap (Jaccard): **>= 0.80**
- Section ranking correlation (Kendall tau): **>= 0.80**
- Context token drift: **<= 10%**
- Final answer semantic similarity: **>= 0.90**
- Latency regression: **<= 30%** (tighten later)

### CI gating policy

- Mark metrics as:
  - P0 (must pass): intent, retrieval overlap, gross answer regressions
  - P1 (warn/fix quickly): ranking/order/context drift
  - P2 (monitor): latency/cost deltas
- PR blocked if any P0 metric fails threshold.

> **Status note (paused Mastra):** while the Mastra implementation is paused,
> any Conformance Nightly / Mastra-vs-Python / replay-cache failures that
> originate from the paused Mastra path are **informational / backlog** by
> default. They must not be treated as P0 production gates, and they must
> not pre-empt the real P0 RAG / data-quality work (missing DGAFP
> embeddings, source ingestion audits). This classification reverts to the
> table above only when Mastra work is explicitly resumed.

## Execution order I will follow once approved

1. Create worktree + first branch.
2. Implement PR1 (conformance harness) first.
3. Open PR1 and validate report quality.
4. Proceed milestone-by-milestone with parity checks at each stage.

## Notes on unresolved choices to lock early

1. Exact observability sink (OTel-only vs minimal DB audit table) for parity with current debugging workflows.
2. Candidate query set size for CI runtime budget (fast smoke subset + full nightly run).

## PR-C follow-up plan (incremental)

After landing PR-C, keep these as explicit next steps instead of mixing scope:

1. **Retriever gate hardening**
   - Remove temporary CI tolerance for missing dual-index prerequisites.
   - Make retriever threshold enforcement strictly fail-closed once
     `rag_chunks_albert` + `rag_chunks_scaleway` are guaranteed in the CI DB.

2. **Conformance config centralization**
   - Move per-stage conformance constants (threshold key mappings, replay behavior,
     and runner metadata) into shared config/helpers to reduce duplication across
     `*-conformance.ts` scripts.

3. **PR-D → PR-E continuity**
   - Keep generator (`rag-pipeline`) informational in PR-D.
   - Extend replay cache coverage to generator and decide promotion criteria in PR-E.
