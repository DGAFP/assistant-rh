# Triage Findings

**Last updated:** 2026-06-17T12:20Z
**Triage agent:** Triage (agent-905cabae)
**Repo:** DGAFP/assistant-rh

---

## Open Issues

**45 open issues** total. Breakdown by label:

| Label | Count | Key Issues |
|-------|-------|------------|
| qualité | 12 | #111, #106, #105, #104, #103, #102, #101, #82, #79, #78, #36, #23, #22, #21, #14, #6 |
| Interministériel | 8 | #27, #26, #25, #24, #20, #19, #18, #17, #12 |
| enhancement | 2 | #79, #78 |
| security | 1 | #111 |
| dependencies | 1 | #111 |
| good first issue | 2 | #36, #33 |
| documentation | 1 | #33 |

### Recently Created (last 7 days)

| # | Title | Labels | Created |
|---|-------|--------|---------|
| 111 | Créer une skill d'analyse pour arbitrer CVE Lite et minimumReleaseAge | dependencies, security | 2026-06-15 |
| 106 | Golden set : récupérer les réponses du beta test depuis l'ancienne DB Scalingo | qualité | 2026-06-12 |
| 105 | Source MSO : auditer l'ingestion notebook (documents internes pdf/pptx/docx) | qualité | 2026-06-12 |
| 104 | Source RGRH : auditer l'ingestion notebook (exports Excel manuels) | qualité | 2026-06-12 |
| 103 | Source MATTE : auditer l'ingestion notebook (exports PDF manuels) | qualité | 2026-06-12 |
| 102 | Source Légifrance/DGAFP : auditer l'ingestion et le mode d'acquisition | qualité | 2026-06-12 |
| 101 | Audit des sources d'ingestion : étendre les garanties Service-Public aux autres corpus | qualité | 2026-06-12 |
| 100 | feat(ingestion): job de réconciliation Service-Public (config vs lake vs RAG) | — | 2026-06-12 |
| 99 | chore(ci): protéger les json.loads restants de scaleway_data_jobs.py | — | 2026-06-12 |

### Stale / Backlog Issues (pre-June)

- #31: [Plan Omar] Stabilisation ingestion + déploiement Scaleway (mai 2026) — still open
- #30: [Durcissement migrations] Définir un workflow de migrations robuste
- #29: [Réconciliation schéma] Cadrer la fusion à 3 voies
- #36: [Backlog] Ajouter des quality gates post-ingestion — good first issue

---

## CI Failures

### 1. Streamlit Deploy Staging — FAILED (2026-06-17T09:26Z)

- **Workflow:** Streamlit Deploy Staging (push to main)
- **Job:** `deploy`
- **Root cause:** Docker registry timeout — `Error response from daemon: Get "https://rg.fr-par.scw.cloud/v2/": context deadline exceeded (Client.Timeout exceeded while awaiting headers)`
- **Impact:** Staging UI not deployed from latest main push (commit `128c778`)
- **Severity:** P1 — blocks staging validation; likely transient Scaleway Container Registry networking issue

### 2. Conformance Nightly — FAILED (2026-06-17T07:33Z)

- **Workflow:** Conformance Nightly (scheduled)
- **Job:** `nightly-conformance`
- **Root cause:** `No eligible rows for nightly selection (gold_sources is NULL or empty for all rows). Eligible row count (0) is below nightly limit (100).`
- **Impact:** Nightly conformance tests cannot run — no gold-set data available
- **Severity:** P1 — **directly confirmed by DB**: `goldset_questions_v2` table has **0 rows**. This is a data gap, not a CI bug.

### 3. Conformance Nightly — FAILED (2026-06-16T08:26Z)

- Same root cause as above (goldset empty). **Two consecutive nightly failures.**

### Other CI (healthy)

| Workflow | Status | Notes |
|----------|--------|-------|
| CI Tests (push main) | ✅ success | |
| Security Audit (push main) | ✅ success | |
| Release Please (push main) | ✅ success | |
| Data Engineering CI (push main) | ✅ success | |
| Conformance (PR) | ✅ success | On release-please branch |
| CI Tests (PR) | ✅ success | On release-please branch |

---

## Database Anomalies

### PostgreSQL Staging (Scaleway)

- **Version:** PostgreSQL 17.10
- **Connection:** ✅ healthy (1 active / 13 total connections)
- **Dead tuples:** None > 1000 (healthy)
- **Long-running queries:** None > 5min (healthy)

### Critical Findings

#### 1. Empty goldset_questions_v2 table — P0

- **0 rows** in `goldset_questions_v2` — this is why Conformance Nightly fails
- `intent_eval_goldset` has 73 rows but no `gold_sources` column
- **Action needed:** Populate goldset_questions_v2 or fix the nightly job to use intent_eval_goldset

#### 2. Missing embeddings in rag_chunks_dgafp — P1

- **3,992 chunks with 0% embedding coverage** (embedding_m3 column is NULL for all rows)
- This means DGAFP circulaires are completely unsearchable by vector similarity
- Other tables: service_public (100%), matte (100%), legifrance (100%), mso (100%), rgrh (54.9%)

#### 3. Partial embeddings in rag_chunks_rgrh — P2

- **178/324 chunks embedded (54.9%)** — 146 missing
- RGRH source partially searchable

#### 4. Scalingo → Scaleway chunk sync discrepancy — P2

| Table | Base | _scw | _scalingo |
|-------|------|------|-----------|
| rag_chunks_dgafp | 3,992 | 3,992 | 3,992 |
| rag_chunks_legifrance | 429 | 429 | 429 |
| rag_chunks_service_public | 2,782 | N/A | 1,140 |

- **service_public:** base has 2,782 chunks but _scalingo only has 1,140; _scw table doesn't exist
- This suggests incomplete migration from Scalingo to Scaleway for service_public source

#### 5. No recent user feedback — P2

- **0 feedback entries in last 7 days** (out of 761 total)
- May indicate low usage or broken feedback collection

#### 6. Chat activity

- **3,093 total chat runs**, 39 in last 7 days, 10 in last 24h
- Latest run: 2026-06-16T15:27Z (yesterday)

### Performance (last 7 days)

| Metric | Value |
|--------|-------|
| avg total time | 10,064ms |
| p50 total time | 8,699ms |
| p95 total time | 14,084ms |
| max total time | 58,406ms |
| v3 avg generation | 3,463ms |
| v3 avg retrieval | 1,298ms |
| v3 avg selector | 2,239ms |

### Reranker Issues

- 1 reranker failure in last 7 days: `502 Server Error: Bad Gateway for url: https://albert.api.etalab.gouv.fr/v1/rerank`
- 34 runs with `v3_reranker_status=completed` (healthy)

### Feedback Breakdown

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

**Status: ACCESS OK**

- Auth works with `SCW_SECRET_KEY` from `.env` as `X-Auth-Token` against the direct Grafana API.
- Custom dashboard visible: **Assistant RH - RAG Data Health** (`assistant-rh-rag-data-health`).
- Datasources visible:
  - Assistant RH RAG Health - fr-par (`dfp8eneiuegaob`, Prometheus)
  - Scaleway Metrics - fr-par (`bfj2tut7o4pvka`, Prometheus)
  - Scaleway Logs - fr-par (`afj2tuu81eqrkf`, Loki)
- Grafana unified alert rules endpoint returned **0 alert rules**. No firing Grafana alerts detected from configured alert rules.
- Note: Triage initially attempted the wrong Cockpit endpoint; runbook has been corrected.

---

## Priority Assessment

### P0 — Immediate

| # | Issue | Impact |
|---|-------|--------|
| 1 | Empty `goldset_questions_v2` table | Conformance Nightly CI has failed 2 consecutive nights; no quality regression testing is running |
| 2 | Missing embeddings in `rag_chunks_dgafp` (3,992 chunks, 0% coverage) | DGAFP circulaires corpus is completely invisible to vector search; users cannot retrieve this content via semantic query |

### P1 — This Sprint

| # | Issue | Impact |
|---|-------|--------|
| 3 | Streamlit Deploy Staging CI failure | Staging UI not deployed; likely transient registry timeout but needs re-trigger or investigation |
| 4 | Scalingo→Scaleway migration gap for service_public chunks | 2,782 vs 1,140 row discrepancy; _scw table missing |
| 5 | Issue #106: Golden set recovery from old Scalingo DB | Directly related to P0 #1; needs turn_id join with eval LLM judge |
| 6 | Issue #100: Reconciliation job for Service-Public | Would address the sync discrepancy |

### P2 — Next Sprint

| # | Issue | Impact |
|---|-------|--------|
| 7 | Partial embeddings in `rag_chunks_rgrh` (54.9%) | 146 chunks unsearchable |
| 8 | No user feedback in last 7 days | Possible broken collection or low engagement |
| 9 | Issues #101–105: Ingestion audit across all sources | Quality gates for all corpora |
| 10 | Issue #99: json.loads protection in CI | Fragile CI parsing |
| 11 | Cockpit/Grafana access | Need monitoring visibility; grant API key permissions |

### P3 — Backlog

| # | Issue | Impact |
|---|-------|--------|
| 12 | Issues #30, #29: Migration workflow & schema reconciliation | Infrastructure debt |
| 13 | Issue #31: Plan Omar stabilisation | Still open past target date |
| 14 | Issue #36: Quality gates post-ingestion | Good first issue, aligned with P1 priorities |
