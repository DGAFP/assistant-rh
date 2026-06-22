# Daily Plan

**Last updated:** 2026-06-18T06:07Z  
**Prepared by:** Assistant RH — Orchestrator  
**Source:** `agent-state/triage-findings.md` from 2026-06-18T06:00Z

---

## Decision summary

Today’s plan prioritizes **RAG/data quality**. Mastra remains paused; therefore the empty `goldset_questions_v2` table is tracked as a P0 risk for Conformance Nightly, but it should not displace live RAG retrieval fixes unless Paul explicitly reactivates Mastra/conformance work.

**Recommended human validation target:** **Issue #121 — DGAFP embedding reconciliation**.

Reason: live `rag_chunks_dgafp` has 3,992 chunks with 0% embedding coverage, while ghost `rag_chunks_dgafp_scalingo` has the same row count with 100% coverage. A guarded keyed reconciliation is the highest-impact fix for live semantic retrieval.

---

## Proposed sequence

### 1. P0 — #121 Restore DGAFP semantic retrieval from ghost embeddings

**Problem:** `rag_chunks_dgafp` has 3,992 live chunks but 0 usable `embedding_m3`; semantic DGAFP retrieval is effectively invisible. `rag_chunks_dgafp_scalingo` has 3,992 rows and 100% embeddings.

**Recommended action:** Build a safe, idempotent reconciliation job:
- dry-run first: counts, duplicate keys, live-only/ghost-only keys, text/hash parity, candidate row count;
- copy embeddings only where stable key and text/hash guard pass;
- emit JSON/Markdown summary;
- add/readiness plan for vector index on live DGAFP after coverage is restored;
- update runbook and monitoring/gate expectations.

**Done when:**
- Dry-run report proves whether keyed copy is safe.
- Reviewed code has explicit guards preventing unqualified bulk copy.
- If Paul approves DB write separately, `rag_chunks_dgafp.embedding_m3` reaches expected coverage or exceptions are listed by key.
- Live DGAFP semantic smoke test returns DGAFP results without relying only on lexical forced path.

**Human approval required:**
1. Approve this as next code-writing task.
2. Confirm base ref (`origin/main` by default).
3. Confirm no DB write during dev/review; any staging/prod update command requires a separate exact-command approval.

---

### 2. P0 — #120 Make `rag_chunks_test` fail-fast or explicitly optional

**Problem:** staging has no `rag_chunks_test`, but config enables it by default; runtime swallows errors and may report false `tables_searched`.

**Recommended action:** After #121 is validated or if Paul prefers a small scoped code fix first, dispatch a dev/review loop to clarify strict vs optional source behavior and structured diagnostics.

**Done when:**
- Enabled-but-absent `rag_chunks_test` no longer fails silently in staging/prod strict mode.
- Optional mode is explicit and does not count the table as successfully searched.
- Unit tests cover absent-table strict and optional behavior.

---

### 3. P0 risk / backlog while Mastra paused — Goldset population (#106)

**Problem:** `goldset_questions_v2` is empty; Conformance Nightly can fail again.

**Current priority note:** Mastra/conformance work is paused per Paul’s 2026-06-17 clarification, so keep this visible but do not select it ahead of live RAG retrieval unless Paul re-prioritizes.

**Recommended action when reactivated:** recover beta-test answers from old Scalingo DB by `turn_id` or adapt nightly to the available `intent_eval_goldset` source with an explicit migration path.

---

### 4. P1 — #124 Version vector indexes, starting with MATTE

**Problem:** MATTE, Légifrance, and RGRH lack vector indexes. MATTE has complete embeddings and is the safest first index migration.

**Recommended action:** Plan a versioned, replayable pgvector index migration. Start with `rag_chunks_matte.embedding_m3`; defer DGAFP/RGRH indexes until embeddings coverage is sufficient unless explicitly justified.

---

### 5. P1 — Review/merge open ingestion PRs

Open ingestion PRs remain candidates for human review/merge sequencing:
- #129 MATTE audit runbook
- #131–#133 Légifrance/DGAFP ingestion audit stack
- #134 MATTE offline audit tooling
- #140 MATTE embeddings backfill

Consider merging the #102 stack (#131–#133) before broadening additional ingestion work.

---

## Selected issue for validation

**Selected:** #121 — DGAFP embedding reconciliation  
**Status:** pending Paul validation; no code-writing dev agent dispatched yet.

Default dispatch plan after validation:
1. Create implementation worktree from fresh `origin/main`.
2. Dispatch MiniMax dev agent with issue #121 acceptance criteria.
3. Run independent review agent after implementation.
4. Require tests/lint/check evidence and reviewer acceptance before asking for any DB write approval.

