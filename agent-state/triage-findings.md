# Triage Findings

**Last updated:** 2026-06-18T06:00Z
**Triage agent:** Triage (agent-905cabae)
**Repo:** DGAFP/assistant-rh

---

## Executive Summary

7 new issues opened since last triage (2026-06-17), including 2 P0 and 1 P0/P1. CI has recovered from yesterday's failures. The DGAFP embedding gap (0% coverage, 3,992 chunks) remains the most critical confirmed data defect — the ghost `rag_chunks_dgafp_scalingo` table has 100% coverage, making keyed reconciliation viable. Missing vector indexes on matte, legifrance, and rgrh tables are a newly confirmed gap. PR #140 (MATTE embeddings backfill) is open but unmerged. No user feedback in 7 days. Grafana/Cockpit API unreachable from this machine.

---

## Open Issues

**45 open issues** total. Key changes since last triage:

### New Issues (2026-06-17)

| # | Title | Severity | Labels | Key point |
|---|-------|----------|--------|-----------|
| 120 | [P0] Rendre rag_chunks_test fail-fast ou explicitement optionnelle | P0 | bug, qualité | Table absent in staging but config enables it; fail-open behavior |
| 121 | [P0] Restaurer la recherche sémantique DGAFP depuis les embeddings du fantôme rag_chunks_dgafp_scalingo | P0 | bug, qualité | 3,992 chunks with 0% embedding coverage; ghost table has 100% |
| 122 | [P0/P1] Ajouter l'alerting opérationnel sur les échecs du reranker section-level | P0/P1 | qualité, observability | Reranker failures invisible without structured alerting |
| 123 | [P1] Normaliser les diagnostics de retrieval par étape | P1 | enhancement, qualité, observability | Cannot explain why relevant chunks are lost between stages |
| 124 | [P1] Versionner les index vectoriels manquants des tables RAG vivantes | P1 | enhancement, qualité | **Confirmed**: matte, legifrance, rgrh have no vector indexes |
| 125 | [P1] Clarifier le dashboard RAG health avec une taxonomie corpus/source/table | P1 | enhancement, qualité, observability | Dashboard shows physical tables, not logical corpus structure |
| 126 | [P1] Ajouter des alertes de qualité de chunking au dashboard RAG health | P1 | enhancement, qualité, observability | No chunking quality alerts on RAG health dashboard |

### Previously Tracked Issues (unchanged status)

| # | Title | Severity | Status |
|---|-------|----------|--------|
| 111 | CVE Lite / minimumReleaseAge skill | security | Open |
| 106 | Golden set recovery from old Scalingo DB | qualité | Open — related to P0 goldset gap |
| 105 | MSO ingestion audit | qualité | Open |
| 104 | RGRH ingestion audit | qualité | Open — was next candidate |
| 103 | MATTE ingestion audit | qualité | Open — PRs #129, #134 open |
| 102 | Légifrance/DGAFP ingestion audit | qualité | Open — PRs #131, #132, #133 open |
| 101 | Extend Service-Public guarantees to other corpora | qualité | Open |
| 100 | Service-Public reconciliation job | — | Open |
| 99 | json.loads protection in CI | — | Open |
| 82 | Embedding sync audit | qualité | Open |

---

## CI Status

### Current State: ALL GREEN

| Workflow | Status | Change since last triage |
|----------|--------|--------------------------|
| CI Tests | ✅ success | No change (was green) |
| Data Engineering CI | ✅ success | No change (was green) |
| Conformance | ✅ success | No change (was green) |
| Streamlit Deploy Staging | ✅ success | **RECOVERED** — was failing (Docker registry timeout) |
| Security Audit | ✅ success | No change |

### Previously Failed (now resolved)

1. **Streamlit Deploy Staging** — was failing 2026-06-17T09:26Z with Docker registry timeout. Now passing since 2026-06-17T18:59Z.
2. **Conformance Nightly** — was failing due to empty `goldset_questions_v2`. No recent nightly runs visible in the last 50 CI runs (may be scheduled outside recent window). **Goldset table still empty** — this will fail again on next nightly run.

---

## Database Anomalies

### PostgreSQL Staging (Scaleway)

- **Version:** PostgreSQL 17.10
- **Connection:** ✅ healthy (1 active / 13 idle connections)
- **Access method:** Python/psycopg via `.env` DSN (no psql/scalingo CLI available)

### Critical Findings

#### 1. Empty goldset_questions_v2 table — P0 (UNCHANGED)

- **0 rows** in `goldset_questions_v2` — Conformance Nightly CI will fail on next scheduled run
- `intent_eval_goldset` has 73 rows but no `gold_sources` column
- **Action needed:** Populate goldset_questions_v2 or fix the nightly job to use intent_eval_goldset
- **Confidence:** High (confirmed by direct DB query)
- **Source:** `SELECT COUNT(*) FROM goldset_questions_v2`
- **Suspected cause:** Migration gap — goldset data exists in old format but not in the v2 table the nightly job expects
- **Recommended next action:** Issue #106 (golden set recovery) or direct backfill from intent_eval_goldset

#### 2. Missing embeddings in rag_chunks_dgafp — P0 (UNCHANGED, now tracked as #121)

- **3,992 chunks with 0% embedding coverage** (embedding_m3 IS NULL for all rows)
- Ghost table `rag_chunks_dgafp_scalingo` has **3,992 rows with 100% embedding coverage**
- Keyed reconciliation is viable: same chunk count, ghost table fully populated
- **Action needed:** Copy embeddings from ghost table to live table by chunk_id key
- **Confidence:** High (confirmed by direct DB query)
- **Source:** `SELECT COUNT(*) FROM rag_chunks_dgafp WHERE embedding_m3 IS NOT NULL` → 0
- **Suspected cause:** Migration from Scalingo to Scaleway did not copy embeddings to the live table
- **Recommended next action:** Issue #121 — implement keyed copy with text-hash guard

#### 3. Missing vector indexes — P1 (NEW, tracked as #124)

| Table | Has vector index? | Embedding coverage |
|-------|-------------------|--------------------|
| rag_chunks_service_public | ✅ idx_rag_chunks_service_public_embedding_m3 | 100% |
| rag_chunks_mso | ✅ idx_rag_chunks_mso_embedding_m3 | 100% |
| rag_chunks_dgafp_scalingo | ✅ idx_dgafp_embedding | 100% (ghost) |
| rag_chunks_matte | ❌ None | 100% |
| rag_chunks_legifrance | ❌ None | 100% |
| rag_chunks_rgrh | ❌ None | 54.9% |
| rag_chunks_dgafp (live) | ❌ None | 0% (no embeddings) |

- **Impact:** Vector similarity search on matte, legifrance, rgrh uses sequential scans — slower retrieval and no ANN optimization
- **Confidence:** High (confirmed by `pg_indexes` query)
- **Source:** `SELECT indexname, tablename FROM pg_indexes WHERE indexdef LIKE '%vector%'`
- **Suspected cause:** Vector indexes were never created for these tables after migration
- **Recommended next action:** Issue #124 — create vector indexes; prioritize matte (959 chunks, 100% coverage)

#### 4. rag_chunks_test absent — P0 (NEW, tracked as #120)

- Table `rag_chunks_test` does **not exist** in staging DB
- Config enables `v3_enable_chunks_test` by default
- Runtime silently swallows `UndefinedTable` errors and returns empty results
- **Impact:** Source announced as "searched" but never contributes; false `tables_searched` entries
- **Confidence:** High (confirmed by `information_schema.tables` query)
- **Source:** `SELECT EXISTS(SELECT 1 FROM information_schema.tables WHERE table_name='rag_chunks_test')` → false
- **Suspected cause:** Table was never created in staging; test-only artifact
- **Recommended next action:** Issue #120 — make fail-fast or explicitly optional per environment

#### 5. Partial embeddings in rag_chunks_rgrh — P2 (UNCHANGED)

- **178/324 chunks embedded (54.9%)** — 146 missing
- No vector index even on the 178 embedded rows
- **Recommended next action:** Backfill remaining 146 chunks, then add vector index

#### 6. Scalingo → Scaleway chunk sync discrepancy — P2 (UNCHANGED)

| Table | Live | _scalingo |
|-------|------|-----------|
| rag_chunks_service_public | 2,782 | 1,140 |
| rag_chunks_dgafp | 3,992 | 3,992 |
| rag_chunks_legifrance | 429 | 429 |

- **service_public:** live has 2,782 chunks but _scalingo only 1,140 — incomplete migration
- **Recommended next action:** Issue #100 (reconciliation job)

### Performance & Activity (last 7 days)

| Metric | Value | Change |
|--------|-------|--------|
| Total chat runs | 3,096 | +3 since last triage |
| Runs last 7d | 41 | +2 |
| Runs last 24h | 3 | Low activity |
| Latest run | 2026-06-17T14:02Z | Yesterday |
| Reranker errors last 7d | 0 | Was 1 last week |
| Feedback last 7d | 0 | Unchanged — 0 for 2nd consecutive triage |

### Feedback Breakdown (cumulative, 761 total)

| Category | Count |
|----------|-------|
| retrieval_issue | 210 |
| missing_document | 69 |
| other | 22 |
| generator_incomplete | 15 |
| selector_wrong_priority | 10 |
| generator_hallucination | 9 |
| generator_wrong_interpretation | 7 |
| chunk_quality | 2 |
| selector_misunderstanding | 1 |

- **Helpful=true: 564, Helpful=false: 197** (74% positive)
- Stars: 0★(116), 1★(81), 2★(143), 3★(170), 4★(251)

---

## Grafana Alerts

**Status: ACCESS FAILED**

- Cockpit API endpoint `https://cockpit.fr-par.scw.cloud/api/search` returned HTTP 000 (connection refused/timeout)
- Auth method: `X-Auth-Token: $SCW_SECRET_KEY` — previously worked on 2026-06-17
- **Error:** Connection timeout — likely network/firewall issue on this machine, not auth failure
- **Impact:** Cannot verify Grafana alert rules or dashboard data from this environment
- **Recommended next action:** Verify network access to cockpit.fr-par.scw.cloud; check if VPN or specific network required

---

## Open PRs

| # | Branch | Title | Status | Notes |
|---|--------|-------|--------|-------|
| 140 | chore/backfill-matte-embeddings | chore(ingestion): wire MATTE embeddings backfill | Open | **NEW** — addresses MATTE embedding backfill |
| 138 | letta/backend-proconnect-synthesis | docs: recommend backend and ProConnect path | Open | Architecture docs |
| 134 | chore/issue-103-matte-offline-audit-tooling | chore(ingestion): add MATTE offline audit tooling | Open | Part of #103 split |
| 133 | feat/issue-102-legifrance-embedding-check-only | feat(ingestion): add read-only embedding coverage audit | Open | Part of #102 work |
| 132 | fix/issue-102-strict-legifrance-articles | fix(ingestion): fail fast on missing Legifrance articles | Open | Part of #102 work |
| 131 | fix/issue-102-preserve-legifrance-embeddings | fix(ingestion): preserve Legifrance embeddings on upsert | Open | Part of #102 work |
| 129 | docs/issue-103-matte-audit-runbook | docs(ingestion): add MATTE source audit runbook | Open | Part of #103 split |
| 128 | chore/aidev-harness | chore: add aidev harness (rtk + agentloop state) | Open | Infrastructure |
| 115 | codex-rag-health-monitoring | feat: add RAG data health monitoring | Open | RAG health monitoring |
| 114 | feat/issue-36-quality-gates | feat: add post-ingestion quality gates | Open | Quality gates |

### Recently Merged (since last triage)

| # | Title | Merged |
|---|-------|--------|
| 139 | docs(audit): renumber concurrent notes as 08/09/10 | 2026-06-17T19:06Z |
| 137 | docs(audit): add ingestion architecture audit vs. 2025/2026 SOTA | 2026-06-17T18:59Z |
| 136 | docs(audit): add note 08 — RAG architecture review | 2026-06-17T18:58Z |
| 135 | docs(audit): Streamlit UI architecture review (note 08) | 2026-06-17T18:56Z |
| 130 | docs(ingestion): document Legifrance DGAFP audit workflow | 2026-06-17T15:58Z |
| 118 | docs: clarify paused Mastra conformance priority | 2026-06-17T14:36Z |

---

## Priority Assessment

### P0 — Immediate

| # | Issue | Impact | Confidence |
|---|-------|--------|------------|
| 1 | Empty `goldset_questions_v2` (0 rows) | Conformance Nightly will fail again; no quality regression testing | High |
| 2 | Missing embeddings in `rag_chunks_dgafp` (3,992 chunks, 0% coverage) | DGAFP circulaires invisible to vector search; keyed reconciliation from ghost table viable | High |
| 3 | `rag_chunks_test` absent but enabled in config (#120) | Fail-open: source announced as searched but never contributes; false `tables_searched` | High |

### P1 — This Sprint

| # | Issue | Impact | Confidence |
|---|-------|--------|------------|
| 4 | Missing vector indexes on matte, legifrance, rgrh (#124) | Sequential scan on vector search; slower retrieval | High |
| 5 | Reranker failure alerting (#122) | Reranker errors invisible without structured alerting | High |
| 6 | Scalingo→Scaleway migration gap for service_public (2,782 vs 1,140) | Incomplete migration; _scw table missing | High |
| 7 | Issue #106: Golden set recovery | Directly related to P0 #1; needs turn_id join | Medium |
| 8 | Issue #100: Reconciliation job for Service-Public | Would address the sync discrepancy | Medium |

### P2 — Next Sprint

| # | Issue | Impact | Confidence |
|---|-------|--------|------------|
| 9 | Partial embeddings in `rag_chunks_rgrh` (54.9%) | 146 chunks unsearchable | High |
| 10 | No user feedback in 7+ days (2 consecutive triages) | Possible broken collection or low engagement | Medium |
| 11 | Retrieval diagnostics normalization (#123) | Cannot explain chunk loss between stages | Medium |
| 12 | RAG health dashboard taxonomy (#125) | Dashboard shows physical tables, not logical corpus | Medium |
| 13 | Chunking quality alerts (#126) | No chunking quality monitoring | Medium |
| 14 | Issue #99: json.loads protection in CI | Fragile CI parsing | High |

### P3 — Backlog

| # | Issue | Impact |
|---|-------|--------|
| 15 | Issues #30, #29: Migration workflow & schema reconciliation | Infrastructure debt |
| 16 | Issue #31: Plan Omar stabilisation | Still open past target date |
| 17 | Issue #36: Quality gates post-ingestion | Good first issue, aligned with P1 priorities |
| 18 | Mastra conformance (#78, #79) | Paused per PROGRESS.md |

---

## Recommended Daily Plan Seed

1. **[P0] DGAFP embedding reconciliation** (#121) — Implement keyed copy from `rag_chunks_dgafp_scalingo` to live table with text-hash guard. Ghost table has 100% coverage, same row count. Highest-impact data fix.
2. **[P0] rag_chunks_test fail-fast** (#120) — Make source explicitly optional or fail-fast when table absent. Small, well-scoped code change.
3. **[P0] Goldset population** — Backfill `goldset_questions_v2` from `intent_eval_goldset` or recover from old Scalingo DB (#106). Unblocks Conformance Nightly.
4. **[P1] Create vector indexes** (#124) — Add `hnsw` indexes on matte, legifrance, rgrh tables. Quick win for retrieval performance.
5. **[P1] Review/merge open ingestion PRs** — #129, #131, #132, #133, #134 are all open and unmerged. Consider merging the #102 stack (Legifrance) first since it's most complete.
