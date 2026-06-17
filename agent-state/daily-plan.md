# Daily Plan

Last updated: 2026-06-17T12:22Z

## Proposed Tasks

### 1. P0 — Restore conformance nightly data

**Problem:** `goldset_questions_v2` has 0 rows, causing two consecutive Conformance Nightly failures.

**Recommended action:** Design and implement a recovery path for the gold set. Start from GitHub issue #106 and decide whether to populate `goldset_questions_v2` from the old Scalingo DB or update nightly selection to use `intent_eval_goldset`.

**Done when:**
- Nightly selection has >=100 eligible rows or its expected minimum is intentionally changed.
- Conformance Nightly can run without the `No eligible rows` failure.
- Migration/recovery script is documented and safe to re-run.

**Agent path:** Ask CTO for issue design first, then dispatch dev.

---

### 2. P0 — Generate missing DGAFP embeddings

**Problem:** `rag_chunks_dgafp` has 3,992 rows and 0 embeddings, making the DGAFP circulaire corpus invisible to vector search.

**Recommended action:** Audit ingestion/embedding path for DGAFP (#102), then run or fix the embedding job for `rag_chunks_dgafp.embedding_m3`.

**Done when:**
- `rag_chunks_dgafp` embedding coverage is >99%.
- A targeted retrieval smoke test can retrieve DGAFP content semantically.
- The embedding job is repeatable and documented.

**Agent path:** CTO reviews approach because this touches RAG data quality; then dev implements.

---

### 3. P1 — Re-run or investigate Streamlit staging deploy

**Problem:** Latest staging deploy failed on Scaleway registry timeout.

**Recommended action:** Re-run the failed workflow first. If it fails again, inspect registry/network/auth configuration.

**Done when:**
- Streamlit staging deploy succeeds from latest `main`.
- If transient, note it and close. If persistent, create a focused issue.

**Agent path:** Orchestrator can handle manually; no dev agent unless failure repeats.

---

## My Recommended Sequence

Start with **Task 1: Restore conformance nightly data**. It is the cleanest first loop test: high impact, objectively verifiable, and tied to an existing issue (#106).


---

## Loop Hardening Rules

Canonical state ledger: `agent-state/PROGRESS.md`. Every triage, planning, dev, and review run must read it first and update it before stopping.

No code-writing dev agent may be dispatched until Paul validates this plan. Read-only investigation is allowed.

Implementation tasks must use the dev/review loop:
- Dev works in an isolated worktree.
- Reviewer is independent from the dev agent.
- Completion requires hard evidence, not an agent claim.
- Required gates: tests, lint/type checks, acceptance criteria, regression risk.
- Maximum 10 dev/review iterations; halt earlier if the same failure repeats twice.
- Track iteration count, gate status, blocker status, and next action in `PROGRESS.md`.

## Dispatch Status

Waiting for Paul validation. No dev agents dispatched yet.


---

## Priority Correction — Mastra Paused

Paul clarified on 2026-06-17 that Mastra implementation is paused. Therefore Conformance Nightly / Mastra conformance failures should not be treated as P0 operational work while the port is paused.

PR opened to document this: https://github.com/DGAFP/assistant-rh/pull/118

Updated sequence:
1. **P0 — Generate missing DGAFP embeddings / audit DGAFP ingestion path** (#102)
2. **P1 — Source ingestion audits and coverage checks** (#101–105)
3. **Backlog / informational while paused — Mastra conformance and goldset recovery** (#106 unless Mastra work resumes)
