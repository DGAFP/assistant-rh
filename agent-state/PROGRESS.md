# Assistant RH Agent Loop Progress

**Purpose:** canonical state ledger for the Assistant RH agent loop. Every scheduled or dispatched agent run must read this file first and update it before stopping.

**Last updated:** 2026-06-17T14:15Z
**Loop owner:** Assistant RH — Orchestrator (`agent-d48dfe12`)
**Triage agent:** Assistant RH — Triage (`agent-905cabae`)
**CTO agent:** Assistant RH — CTO (`agent-956b109e`)
**Repo:** `/Users/dasco/dev/clients/dinuum/assistant-rh`

---

## Loop status

| Loop | Trigger | Status | Last result | Next action |
|---|---:|---|---|---|
| Daily triage | Weekdays 08:00 Europe/Paris | Active | 2026-06-17 triage completed | Next run should update `triage-findings.md` and this ledger |
| Daily planning | Weekdays 08:30 Europe/Paris | Active | 2026-06-17 plan drafted | Paul reprioritized: Mastra paused, conformance not P0; docs/audit-plan PR next |
| Dev/review implementation | Manual after validation | Active | Task #103 selected after #102 PR | CTO design + MiniMax dev for MATTE ingestion audit |

---

## Last run

### 2026-06-17 triage

Triage found:
- P0: `goldset_questions_v2` has 0 rows; Conformance Nightly failed twice.
- P0: `rag_chunks_dgafp` has 3,992 chunks with 0% embedding coverage.
- P1: Streamlit staging deploy failed due to Scaleway registry timeout.
- P1/P2: service_public Scalingo → Scaleway chunk discrepancy.
- P2: `rag_chunks_rgrh` only 54.9% embedded.

Artifacts:
- `agent-state/triage-findings.md`
- `agent-state/daily-plan.md`

---



### 2026-06-17 reprioritization — Mastra paused

Paul clarified that Mastra implementation is paused, so Conformance Nightly / Mastra conformance should **not** be treated as P0 operational work.

Decision:
- Stop Task 1 implementation dispatch before dev work.
- Create a docs/audit-plan PR clarifying that conformance failures are informational while Mastra is paused.
- Reprioritize true P0 to RAG/data quality, especially missing DGAFP embeddings and source ingestion audits.

No code-writing dev agent was dispatched for Task 1.

---

## Current plan

Recommended sequence:
1. Restore conformance nightly data / gold set recovery (#106).
2. Generate missing DGAFP embeddings / audit ingestion path (#102).
3. Re-run or investigate Streamlit staging deploy.

Current human checkpoint:
- **Status:** Paul validated Task 1 via “let’s run our loop” at 2026-06-17T12:39Z.
- **Rule:** Task 1 may enter code-writing dispatch after CTO design pass; production writes/migrations still require explicit approval.

---

## In progress

### Task 1 — Restore conformance nightly data / gold set recovery (#106)

| Field | Value |
|---|---|
| Status | STOPPED — reprioritized after Paul clarified Mastra is paused |
| Worktree | `/Users/dasco/dev/clients/dinuum/assistant-rh/.letta/worktrees/issue-102-dgafp-ingestion-audit` |
| Branch | `fix/issue-102-dgafp-ingestion-audit` |
| Iteration | 1/10 |
| Expected iteration budget | 3 |
| Max iteration budget | 10 |
| Dev model | `lc-minimax/MiniMax-M3` |
| Review model | `chatgpt-plus-pro/gpt-5.5` |
| Last dev agent | `agent-b1634832` / MiniMax dev, commit `d12f2a1` |
| Last review agent | `agent-ec45c29c` / ChatGPT reviewer, APPROVED |
| Test gate | PASS — 16 focused tests, 47 targeted tests, 215 broad tests passed |
| Lint/type gate | PASS — ruff + git diff --check clean |
| Requirement gate | PASS — reviewer approved #102 scope |
| Regression gate | PASS — reviewer found Service-Public and non-Légifrance paths scoped safely |
| Verdict | STOPPED |

### Subtask 1a — Docs/audit-plan PR: clarify paused Mastra conformance priority

| Field | Value |
|---|---|
| Status | REVIEWED — APPROVED |
| Worktree | `/Users/dasco/dev/clients/dinuum/assistant-rh/.letta/worktrees/docs-mastra-paused-priority` |
| Branch | `docs/mastra-paused-priority` |
| Iteration | 1/10 |
| Dev model | `lc-minimax/MiniMax-M3` |
| Scope | Docs-only: add paused-Mastra status note, classify Mastra conformance failures as informational/backlog, restate P0 = RAG/data quality (missing DGAFP embeddings, source ingestion audits) |
| Files targeted | `docs/MASTRA_PR_MILESTONES_PLAN.md`, `docs/MASTRA_CONFORMANCE_TESTING.md`, `tests/conformance/README.md` |
| Commit | `6d5d1a82bcb67ff2278b148f64aaa1d799cf67b9` (`docs: clarify paused Mastra conformance priority`) |
| Review commands run | `git status --short`; `git diff --stat main...HEAD && git diff main...HEAD`; `git diff --check main...HEAD`; read changed docs |
| Checks run | `git diff --check main...HEAD` (clean); pre-commit hooks from dev report (no files to check — docs-only) |
| Review gates | Docs-only PASS; requirement match PASS; priority correction PASS; factual/reversible/history PASS; whitespace PASS; regression risk PASS |
| Diffstat | 3 files changed, 78 insertions(+), 0 deletions(-) |
| Pushed | NO (per task instructions) |
| PR opened | NO (per task instructions) |
| Reviewer verdict | APPROVED — wording clearly marks Mastra paused, classifies paused-path conformance failures as informational/backlog, and points current P0 to RAG/data quality |
| Next action | Open/merge docs PR if desired, then return to true P0 RAG/data-quality tasks (DGAFP embeddings, source ingestion audit) |

Acceptance criteria:
- Nightly selection has >=100 eligible rows or its expected minimum is intentionally changed.
- Conformance Nightly can run without the `No eligible rows` failure.
- Migration/recovery script is documented and safe to re-run.

Safety boundary:
- Dev may implement code/scripts/tests.
- Dev must not write to production/staging databases unless Paul explicitly approves.
- Any actual data backfill/migration execution beyond local/test DB is blocked pending Paul approval.

---

## Blocked

| Item | Blocker | Required unblock |
|---|---|---|
| Task 1 production/staging DB writes | Explicit approval required | Paul approves concrete write/backfill command after code review |

---

## Review loop status

No active review loop.

When active, each task must record:

| Field | Value |
|---|---|
| Task | `<task title>` |
| Worktree | `<path>` |
| Branch | `<branch>` |
| Iteration | `0/10` |
| Last dev agent | `<agent/conversation id>` |
| Last review agent | `<agent/conversation id>` |
| Test gate | `PENDING / PASS / FAIL` |
| Lint/type gate | `PENDING / PASS / FAIL` |
| Requirement gate | `PENDING / PASS / FAIL` |
| Regression gate | `PENDING / PASS / FAIL` |
| Verdict | `PENDING / APPROVED / BLOCKED` |

Hard rule: **do not mark a task complete because an agent says it is done.** Completion requires objective gates to pass and the reviewer to write evidence.

---

## Stop conditions

Primary stop condition for implementation tasks:
- Required tests pass.
- Required lint/type checks pass.
- Reviewer confirms requirements are met with evidence.
- No blocking regression risk remains.

Backstop stop condition:
- Maximum 10 dev/review iterations per task.
- If the same failure repeats twice without meaningful progress, halt early and ask Paul or CTO.
- If credentials, production data access, or destructive operations are required, halt and ask Paul.

---

## Cost and safety budget

| Budget item | Limit |
|---|---:|
| Dev/review iterations per task | 10 max |
| Preferred dev model | `lc-minimax/MiniMax-M3` |
| Preferred review model | `openai-codex/gpt-5.5` if verified; otherwise `auto-fast` fallback |
| Human approval before code dispatch | Required |
| Human approval before merge/publish | Required |

Worktree/base rule:
- Implementation worktrees and task branches must be created from `origin/main` after a fresh fetch unless Paul or the validated plan explicitly specifies another base ref. Never assume local `main` is current.

Allowed command families for unattended or semi-attended loop work:
- read/search: `ls`, `cat`, `sed`, `grep`, `find`, `rg`, `head`, `tail`
- git inspection/worktree: `git status`, `git diff`, `git log`, `git worktree`, `git branch`
- project checks: `uv`, `pytest`, `ruff`, `pnpm`
- GitHub inspection/PR creation when explicitly approved: `gh`

Risky operations require explicit human approval:
- production writes or migrations
- deleting files outside the task scope
- force push / reset / rebase
- secret changes
- deploys, merges, or publishing

---

## Next action

PR #127 opened for #103 MATTE read-only ingestion audit. Next source candidate: RGRH (#104) unless PR review feedback arrives first.



---

## Independent review — Issue #102 DGAFP/Légifrance ingestion audit

| Field | Value |
|---|---|
| Review timestamp | 2026-06-17T13:38Z |
| Reviewer | Independent reviewer subagent (`agent-ec45c29c-6515-46ab-a6e2-3361a8d6d2bd`) |
| Worktree | `/Users/dasco/dev/clients/dinuum/assistant-rh/.letta/worktrees/issue-102-dgafp-ingestion-audit` |
| Branch | `fix/issue-102-dgafp-ingestion-audit` |
| Commit reviewed | `d12f2a11694c0cd2e8eb8ba9b93eb341ff0825db` |
| Commands run | `git status --short`; `git diff --stat origin/main...HEAD`; `git diff --check origin/main...HEAD`; full diff/read changed implementation/docs; `uv run pytest tests/test_issue_102_dgafp_ingestion.py`; `uv run pytest tests/test_issue_102_dgafp_ingestion.py tests/test_embeddings_backfill.py tests/test_data_engineering_ci_scripts.py tests/test_legifrance_local_pipeline.py`; `uv run ruff check packages/data-engineering/src/assistant_rh_data_engineering/service_public/db.py packages/data-engineering/src/assistant_rh_data_engineering/legifrance/db.py packages/data-engineering/src/assistant_rh_data_engineering/jobs/embeddings_backfill.py packages/data-engineering/src/assistant_rh_data_engineering/jobs/legifrance_bulk_dump.py tests/test_issue_102_dgafp_ingestion.py` |
| Test gate | PASS — 16/16 targeted issue tests passed; broader set 47 passed, 1 skipped |
| Lint/format gate | PASS — `git diff --check origin/main...HEAD` clean; ruff changed Python files clean |
| Requirement gate | PASS — acquisition audit docs, DGAFP embedding coverage audit, idempotent no-embed upserts, fail-fast article extraction, and targeted backfill controls implemented |
| Safety gate | PASS — review found no staging/prod writes, migrations, job starts, or unapproved real backfill execution; docs keep real backfill pending Paul approval |
| Regression gate | PASS — Service-Public behavior remains default `_upsert`; Légifrance-only preserve-on-null is targeted; embedding check-only path is read-only; tested adjacent ingestion/backfill suites |
| Verdict | APPROVED |

---

## Completed PRs

### PR #118 — docs: clarify paused Mastra conformance priority

| Field | Value |
|---|---|
| Status | OPEN |
| URL | https://github.com/DGAFP/assistant-rh/pull/118 |
| Branch | `docs/mastra-paused-priority` |
| Worktree | `/Users/dasco/dev/clients/dinuum/assistant-rh/.letta/worktrees/docs-mastra-paused-priority` |
| Commit | `6d5d1a82bcb67ff2278b148f64aaa1d799cf67b9` |
| Dev model | `lc-minimax/MiniMax-M3` |
| Review model | `chatgpt-plus-pro/gpt-5.5` |
| Test/check gate | PASS — `git diff --check main...HEAD` |
| Requirement gate | PASS — reviewer approved docs-only priority correction |
| Regression gate | PASS — docs-only, no CI/workflow behavior changes |
| Verdict | APPROVED / PR OPEN |

Outcome: Mastra implementation pause is documented. Conformance failures on paused Mastra path should be informational/backlog, not P0. Next true P0 is RAG/data quality, especially DGAFP embeddings/source ingestion audits.


Follow-up update:
- Added `docs/MASTRA_PORT_EXECUTIVE_SUMMARY.md` status note so the audit/executive summary also records the Mastra pause and priority implications.
- Follow-up commit pushed to PR #118: `6e30521` (`docs: mention Mastra pause in audit summary`).
- Check: `git diff --check`; pre-commit markdown/code hooks skipped non-applicable files.


Correction after stale-base check:
- Paul flagged that the docs audit had recently been merged.
- Verification showed local `main`/previous PR base was stale at `83b6879` while `origin/main` was `128c778` with `docs/audit/` merged.
- Fetched origin, merged latest `origin/main` into PR #118 branch, and added the Mastra pause note to the actual audit synthesis: `docs/audit/00_SYNTHESE_ET_PRIORISATION.md`.
- Repair commit pushed: `2e92405` (`docs: add Mastra pause to audit synthesis`).
- Future worktree creation should refresh base from `origin/main` unless explicitly told not to.


---

## Active task — #102 DGAFP/Légifrance ingestion audit and embeddings

| Field | Value |
|---|---|
| Status | Dev implementation complete; awaiting review and Paul approval for staging backfill |
| Issue | https://github.com/DGAFP/assistant-rh/issues/102 |
| Worktree | `/Users/dasco/dev/clients/dinuum/assistant-rh/.letta/worktrees/issue-102-dgafp-ingestion-audit` |
| Branch | `fix/issue-102-dgafp-ingestion-audit` |
| Iteration | 1/10 |
| Expected iteration budget | 3 |
| Max iteration budget | 10 |
| Dev model | `lc-minimax/MiniMax-M3` |
| Review model | `chatgpt-plus-pro/gpt-5.5` |
| Last dev agent | `lc-minimax/MiniMax-M3` (`d12f2a1`) |
| Last review agent | `agent-ec45c29c` / ChatGPT reviewer, APPROVED |
| Test gate | PASS — 47/47 + 215/215 (broad), 1 skipped (snapshot gate) |
| Lint/type gate | PASS — `ruff check --select E,F,I` clean on all changed files |
| Requirement gate | PENDING (reviewer evidence) |
| Regression gate | PASS — 215/215 in `tests/` (no archive/conformance) |
| Verdict | PENDING (reviewer + Paul approval) |
| Commit | `d12f2a1 fix(ingestion): preserve Legifrance embeddings and add DGAFP audit tooling` |

Diagnosis to prove/fix (validated by code inspection on `fix/issue-102-dgafp-ingestion-audit` @ 128c778):
1. `legifrance_medallion --no-embed` is the documented Scaleway config (`data-engineering-jobs.json` → `legifrance-medallion.args` ends with `--no-embed`); medallion writes chunks without embeddings.
2. `LegifranceDbWriter._upsert` (inherited from `ServicePublicDbWriter._upsert`) `DO UPDATE SET` every non-conflict column from `EXCLUDED` — there is no per-column guard for `embedding_m3`/`embedding_bge_scw`. A no-embed ingestion rerun therefore overwrites existing non-NULL embeddings with NULL.
3. `embeddings_backfill.py` only targets rows where the embedding column is NULL (`fetch_missing_rows` filter). If no-embed ingestion just wiped existing embeddings, the backfill CAN re-create them, but only if called separately. `data-ingestion embeddings legifrance` and `config/legifrance_embedding_tables.json` are correctly targeted to `rag_chunks_dgafp` and `rag_chunks_legifrance` only.
4. `LegiBulkDumpClient.extract_articles` writes `missing_ids` to JSON but returns the partial dict and does not fail — `legifrance_bulk_dump` does not enforce completeness, so a stale `--article-ids-json` can produce silently-incomplete extraction and propagate downstream.
5. `embeddings_backfill.py` has no read-only coverage / dry-run / check-only mode; the only way to know embedding coverage today is an ad-hoc SQL query.

Implementation changes (commit `d12f2a1`):
- (a) `ServicePublicDbWriter._upsert(preserve_on_null_cols=…)` now emits `COALESCE(EXCLUDED.col, <table>.col)` for the listed columns. `LegifranceDbWriter.upsert_legacy_chunks` protects `embedding_m3`, `embedding_bge_scw`, `embedding_qwen3` (filtered by introspection via `information_schema.columns`); `upsert_modern_chunks` protects `embedding_m3` / `embedding_bge_scw` (pas de qwen3 sur la table moderne). Comportement SQL inchangé pour les colonnes non embedding et pour `rag_chunks_service_public` (n'a pas de colonne d'embedding dans le manifest).
- (b) `embeddings_backfill.py` accepts `--check-only` (alias `--dry-run`) and `--coverage-min-pct <float>`. `audit_embedding_coverage` émet par `(table, embedding_column)` : `total`, `non_null`, `missing_with_text`, `empty_text`, `coverage_pct`. Aucun import de `sentence_transformers`, aucun appel à l'API Scaleway, aucun `UPDATE`. Code retour `1` si sous le seuil.
- (c) `legifrance_bulk_dump` accepte `--strict-articles` (implicite avec `--article-ids-json`) qui produit `SystemExit` + payload JSON `status=error reason=incomplete_article_extraction` listant `requested_count / extracted_xml_count / missing_count / missing_ids_sample`. `--allow-partial` est l'opt-out.
- (d) `tests/test_issue_102_dgafp_ingestion.py` : 16 nouveaux tests couvrent l'émission SQL COALESCE, le câblage du writer Légifrance, le mode `--check-only` (asserts explicites qu'aucun modèle/API/écriture ne se produit), et le comportement strict/allow-partial du bulk-dump. Tests n'ouvrent aucune connexion PostgreSQL réelle ni aucun appel réseau.
- (e) Docs mis à jour :
  - `docs/LEGIFRANCE_DGAFP_AUTOMATION_PLAN.md` : addendum dédié à #102 couvrant les deux chemins d'acquisition (automatique Scaleway / manuel article CID/number), l'idempotence embeddings, le mode audit read-only, le fail-fast extraction incomplète, le backfill ciblé, et le lien avec la sélection Scaleway.
  - `docs/SCALEWAY_SERVERLESS_JOBS_LEGIFRANCE.md` : sections "Audit read-only", "Idempotence embeddings", "Fail-fast extraction incomplète".
  - `docs/audit/00_SYNTHESE_ET_PRIORISATION.md` : renvoi vers l'addendum #102 dans les sources.

Commands run / evidence:
- `uv run pytest tests/test_issue_102_dgafp_ingestion.py tests/test_embeddings_backfill.py tests/test_data_engineering_ci_scripts.py tests/test_legifrance_local_pipeline.py` → 47 passed, 1 skipped (snapshot gate).
- `uv run pytest tests/ --ignore=tests/archive --ignore=tests/conformance` → 215 passed, 2 skipped (no regression).
- `uv run ruff check packages/data-engineering/src/assistant_rh_data_engineering/jobs/embeddings_backfill.py jobs/legifrance_bulk_dump.py service_public/db.py legifrance/db.py tests/test_issue_102_dgafp_ingestion.py --select E,F,I` → All checks passed.
- `git diff --check` → clean.
- `python3 .github/scripts/scaleway_data_jobs.py --target-env staging --image-tag staging-test --embeddings true --run-embeddings true --embedding-source legifrance --embedding-only-column embedding_m3 --service-public false --legifrance false --dry-run` (avec `SCW_DEFAULT_PROJECT_ID`, `SCW_POSTGRES_DSN`, `SCALEWAY_API_KEY` factices) → confirme que la sélection `--embedding-source legifrance` cible bien la clé `embeddings-legifrance` et que `--only-column embedding_m3` est correctement ajouté. Aucune commande Scaleway réelle n'est envoyée.
- pre-commit hooks (ruff, ruff-format) → Passed.

Files changed (8 files, +1346 / −43) :
- `docs/LEGIFRANCE_DGAFP_AUTOMATION_PLAN.md`
- `docs/SCALEWAY_SERVERLESS_JOBS_LEGIFRANCE.md`
- `docs/audit/00_SYNTHESE_ET_PRIORISATION.md`
- `packages/data-engineering/src/assistant_rh_data_engineering/jobs/embeddings_backfill.py`
- `packages/data-engineering/src/assistant_rh_data_engineering/jobs/legifrance_bulk_dump.py`
- `packages/data-engineering/src/assistant_rh_data_engineering/legifrance/db.py`
- `packages/data-engineering/src/assistant_rh_data_engineering/service_public/db.py`
- `tests/test_issue_102_dgafp_ingestion.py` (new, 858 lines)

Safety / blocked operations:
- Aucun staging/prod write, aucune migration, aucun démarrage de job Scaleway, aucune mutation Object Storage n'a été exécuté. Les seules commandes Scaleway émises étaient en mode `--dry-run`.
- Le backfill réel `data-ingestion embeddings legifrance ...` reste **bloqué en attente d'approbation Paul** — voir "Proposed backfill commands for Paul approval" ci-dessous.

Proposed backfill commands for Paul approval (read-only audit first, then writes):
```bash
# 1) Audit read-only (AUCUNE écriture, AUCUN appel API)
uv run data-ingestion embeddings legifrance \
  --check-only \
  --coverage-min-pct 95

# 2) Audit ciblé DGAFP/m3 pour confirmer la racine du trou 0/3992
uv run data-ingestion embeddings legifrance \
  --check-only \
  --only-table rag_chunks_dgafp \
  --only-column embedding_m3 \
  --coverage-min-pct 100

# 3) SQL read-only d'investigation équivalente (lecture seule)
psql "$SCW_POSTGRES_DSN" -c "
  SELECT
    COUNT(*) AS total,
    COUNT(*) FILTER (WHERE embedding_m3 IS NULL) AS m3_null,
    COUNT(*) FILTER (WHERE embedding_bge_scw IS NULL) AS bge_null
  FROM rag_chunks_dgafp;
"

# 4) Backfill réel ciblé DGAFP/m3 (BLOQUÉ - approval requise)
# uv run data-ingestion embeddings legifrance \
#   --dsn-env SCW_POSTGRES_DSN \
#   --only-table rag_chunks_dgafp \
#   --only-column embedding_m3

# 5) Backfill bge_scw ciblé DGAFP (BLOQUÉ - approval requise)
# uv run data-ingestion embeddings legifrance \
#   --dsn-env SCW_POSTGRES_DSN \
#   --only-table rag_chunks_dgafp \
#   --only-column embedding_bge_scw
```

Reviewer next action:
- Vérifier la logique COALESCE et le filtrage par introspection schéma (lignes `preserve_on_null_cols` dans `legifrance/db.py:333-342` et `:354-362`).
- Vérifier l'audit `--check-only` (asserts explicites "no model / no API / no DB write" dans `tests/test_issue_102_dgafp_ingestion.py::test_check_only_main_does_not_import_models_or_call_apis`).
- Vérifier le payload JSON du fail-fast `--strict-articles` dans `legifrance_bulk_dump.py:153-178` et le test `test_bulk_dump_strict_by_default_fails_on_missing_ids`.
- Décider d'approuver (ou pas) l'exécution des commandes 4) et 5) sur staging après avoir vu le résultat de 2) et 3).
- Aucun push / aucune PR ouverte par le dev (per instructions).

Acceptance criteria:
- Document automatic vs manual acquisition path for Légifrance/DGAFP (`legifrance_bulk_dump`, medallion, ingestion, CID curation).
- Determine why `rag_chunks_dgafp` has chunks but 0 embeddings, and whether targeted backfill path covers `embedding_source=legifrance`.
- Add or update repeatable audit/backfill/check tooling or docs so embedding coverage can be verified safely.
- No staging/production writes without Paul approving an exact command.
- Reviewer can validate with code inspection, tests/lint, and read-only checks.

Safety boundary:
- Dev may inspect code, write docs/tests/scripts, and run read-only DB queries.
- Dev must not run write backfills/migrations against staging or production.
- If the fix requires executing a real backfill, stop with the exact proposed command and await Paul approval.


### PR #119 — fix(ingestion): preserve Legifrance embeddings and add DGAFP audit tooling

| Field | Value |
|---|---|
| Status | OPEN |
| URL | https://github.com/DGAFP/assistant-rh/pull/119 |
| Branch | `fix/issue-102-dgafp-ingestion-audit` |
| Worktree | `/Users/dasco/dev/clients/dinuum/assistant-rh/.letta/worktrees/issue-102-dgafp-ingestion-audit` |
| Commit | `d12f2a11694c0cd2e8eb8ba9b93eb341ff0825db` |
| Dev model | `lc-minimax/MiniMax-M3` |
| Review model | `chatgpt-plus-pro/gpt-5.5` |
| Tests | PASS — focused, targeted, and broad non-conformance suites passed |
| Lint/check | PASS — ruff and `git diff --check` clean |
| Verdict | APPROVED / PR OPEN |

Outcome:
- Fixes no-embed Légifrance ingestion wiping existing embeddings via preserve-on-null upsert behavior.
- Adds read-only embedding coverage audit mode.
- Makes configured article extraction fail fast on missing CIDs.
- Documents acquisition/backfill/safety workflow.

Next approval gate:
- Run read-only coverage audit first.
- Real `rag_chunks_dgafp.embedding_m3` backfill remains blocked until Paul approves the exact write command.


## PR body convention

All Assistant RH loop PRs should use this body structure:
1. `## Problem` — what is broken/risky/missing and why it matters.
2. `## Solution` — what changed and why the approach is safe.
3. `## Diagram` — optional Mermaid diagram when it clarifies data flow, workflow, or review context.
4. `## Review loop`, `## Checks`, `## Safety`.

Applied retroactively to PR #118 and PR #119 on 2026-06-17.


---

## Active task — #103 MATTE ingestion audit and embedding coverage

| Field | Value |
|---|---|
| Status | Dev implementation complete; awaiting review and Paul approval |
| Issue | https://github.com/DGAFP/assistant-rh/issues/103 |
| Worktree | `/Users/dasco/dev/clients/dinuum/assistant-rh/.letta/worktrees/issue-103-matte-ingestion-audit` |
| Branch | `fix/issue-103-matte-ingestion-audit` |
| Iteration | 1/10 |
| Expected iteration budget | 3 |
| Max iteration budget | 10 |
| Dev model | `lc-minimax/MiniMax-M3` |
| Review model | `chatgpt-plus-pro/gpt-5.5` |
| Last dev agent | `lc-minimax/MiniMax-M3` (`f48f7e3`) |
| Last review agent | Independent reviewer subagent (`agent-8de462ff-11b7-4fab-9269-1077ce21cfde`) — APPROVED |
| Test gate | PASS — reviewer reran 43/43 issue tests and 242 passed, 2 skipped broad non-archive/non-conformance suite |
| Lint/type gate | PASS — reviewer reran `ruff check --select E,F,I` on changed Python files and `git diff --check origin/main...HEAD` clean |
| Requirement gate | PASS — reviewer confirmed #103 MATTE manual acquisition/notebook audit, completeness, embeddings, idempotence, reproducibility, and absent-index status are documented with read-only tooling |
| Regression gate | PASS — docs/script/tests only; no runtime package/app changes; broad tests passed |
| Verdict | APPROVED |
| Commit | `f48f7e3d1f65f35d605195b54bb82de803bf6415 chore(ingestion): add MATTE read-only audit tooling` |

Selection rationale:
- MATTE is the largest remaining non-Service-Public source issue after #102.
- Issue #103 reports manual PDF/OCR/notebook ingestion, no planifiable job, unclear link between notebook README `rag_chunks_3` and production `rag_chunks_matte`, missing vector index, and ~762 NULL embeddings.
- RGRH (#104) and MSO (#105) remain next candidates after MATTE.

Diagnosis to prove/document (validated by code inspection on `fix/issue-103-matte-ingestion-audit` @ 128c778):
1. `scripts/README.md` advertises three notebooks `extract_matte.ipynb` / `amelioration_matte.ipynb` / `ingestion_matte.ipynb`. On a fresh `origin/main`, **only `amelioration_matte.ipynb` is present** (cf. `ls scripts/ | grep -i matte`). The other two are referenced in `.env.example` (`MATTE_INPUT_PATTERNS`, `MATTE_IN_JSONL_WITH_EMB`, `MATTE_TABLE`) but absent. ➜ documented as audit finding, not invented.
2. `amelioration_matte.ipynb` declares 3 PDF inputs (`./data/in/temps_du_travail/Cadrage national DIR_2009.pdf`, `…/instruction_ministerielle_du_6_janvier_2011.pdf`, `…/Reglement_interieur_ARTT_AC_01012013-10.pdf`) hardcoded in the cell-level Python list. No acquisition script, no job — pure manual.
3. The notebook produces JSONL chunks + Parquet/NPY/JSONL embeddings with `BAAI/bge-m3` (1024-dim), but does **not** contain SQL ingestion. The actual SQL upsert lives in `scripts/ingestion_pdf.ipynb` (generic, reused) and writes to `rag_chunks_3` (legacy name) — the current production table is `rag_chunks_matte`. Link is not versioned; this is documented as a discovery.
4. The ingestion SQL in `ingestion_pdf.ipynb` uses `ON CONFLICT (hash_id) DO UPDATE SET … "embedding_m3" = EXCLUDED.embedding_m3, …` — same antipattern as the one fixed in #102 for Légifrance: a future run with `embedding_m3 IS NULL` would **wipe** existing non-NULL embeddings. Documented as a known idempotence risk; **no** fix is applied (out of scope for the audit PR).
5. `make_hash_id` integrates the raw `text` into the SHA-1 — any future normalization change to `normalize_text` would regenerate every `hash_id`. Documented as a stability risk.
6. `packages/rag-pipeline/src/assistant_rh_rag_pipeline/embedder.py` (EMBEDDING_COLUMN_MAP line 39) and `config.py` (CHUNK_TABLES["matte"].embed_col_albert = "embedding_m3", line 60) confirm the canonical runtime column is `embedding_m3` (Albert/BGE-M3 path). The retriever reads **only** that column when Albert is primary, falling back to `embedding_bge_scw` (3584-dim) via `FallbackEmbedder`.
7. Audit notes 06 §1.4 and 07 §P0.3 confirm: 5 embedding columns coexist on `rag_chunks_matte` (`embedding_m3`, `embedding_bge_scw`, `embedding_qwen3`, `embedding_ctx`, `embedding_bge`) with mixed coverage (959/959 on `m3`, 197/959 on `bge_scw`, partial elsewhere); `rag_chunks_matte` has **no vector index** (the only real sequential-scan case among the 4 tables queried by the retriever).
8. `rag_chunks_matte` coverage: 959/959 chunks have `embedding_m3`; **17/44 documents** have no chunks (audit 01 add.1). Neither problem is solved by this PR — both are out of scope.

Implementation changes (commit `f48f7e3`):
- (a) `docs/MATTE_SOURCE_INGESTION_AUDIT.md` (new, 379 lines) : the canonical runbook for the MATTE source audit. Documents the 3-PDF manual acquisition, the missing notebooks, the 3-PDF list parsed from the notebook, the schema ambiguities (5 embedding columns), the canonical runtime columns, the idempotence risks, the absent index, the duplicate-text side effect of `role="TABLE"`, and a reproducible audit checklist with all SQL queries (read-only) listed inline. No remediation SQL is auto-executed; the HNSW index creation is documented for a future PR after Paul approval.
- (b) `scripts/audit_matte_ingestion.py` (new, 781 lines) : a read-only/offline audit tool. Default mode (`--sql-only`) inspects the repo (notebook presence, declared PDF paths, env vars, local generated artifacts) and emits 8 SQL statements (read-only) targeting `rag_chunks_matte` and `pg_indexes`. Optional mode (`--db-readonly` with `MATTE_AUDIT_DSN`) runs the queries in a `psycopg.connect()` that is hard-guarded against any write keyword (`insert|update|delete|create|drop|alter|truncate|grant|revoke|set|reset|…`); any forbidden keyword is refused with `{"error": "refused: forbidden write keyword"}`. Import psycopg is **lazy** to keep the module loadable without the DB driver. Output is JSON or Markdown. Diagnostic: `STALE_NOTEBOOKS: scripts/extract_matte.ipynb, scripts/ingestion_matte.ipynb` confirmed on `origin/main`.
- (c) `tests/test_issue_103_matte_ingestion_audit.py` (new, 764 lines) : 43 tests covering notebook parsing (single/double quotes, dedup, ignore non-PDF, ignore markdown cells, invalid JSON, real-notebook round-trip), env-var detection, artifact inspection (missing/valid/duplicate/empty/chunk_text alias), report assembly (notebook presence, PDF extraction, env-var diagnostics, DB DSN guard, artifacts present/absent), DB read-only guard (refuses INSERT/UPDATE/CREATE INDEX/DROP/ALTER/DELETE/SET, handles missing psycopg, isolates per-statement connection errors), CLI (--help, --sql-only, --format markdown, --db-readonly without DSN, missing repo root), and script invariants (canonical columns match runtime, psycopg import is lazy, audit doc exists with required content).
- (d) No code change in `packages/`, `apps/`, `data/`, `scripts/amelioration_matte.ipynb`, or any notebook ingestion path. The audit PR is strictly read-only/docs/tooling/tests per CTO guidance.

Commands run / evidence:
- `uv run python scripts/audit_matte_ingestion.py --help` → usage doc OK.
- `uv run python scripts/audit_matte_ingestion.py --repo-root . --sql-only` → JSON output with: `canonical_table=rag_chunks_matte`, `canonical_embed_col_albert=embedding_m3`, 3 PDF paths declared, 1 notebook present + 2 missing, 8 SQL statements, `STALE_NOTEBOOKS` diagnostic, 0 errors.
- `uv run pytest tests/test_issue_103_matte_ingestion_audit.py` → 43 passed in 0.26s.
- `uv run pytest tests/test_issue_103_matte_ingestion_audit.py tests/test_embeddings_backfill.py tests/test_data_engineering_ci_scripts.py tests/test_legifrance_local_pipeline.py tests/test_retriever_determinism.py tests/test_db_helpers_dsn_resolution.py` → 97 passed, 1 skipped (no regression).
- `uv run pytest tests/ --ignore=tests/archive --ignore=tests/conformance` → 242 passed, 2 skipped (broad regression clean).
- `uv run ruff check scripts/audit_matte_ingestion.py tests/test_issue_103_matte_ingestion_audit.py --select E,F,I` → All checks passed.
- `git diff --check origin/main...HEAD` → clean (only 3 new untracked files, no whitespace issues).
- pre-commit hooks (ruff, ruff-format) : not run by the dev agent in the worktree (the .pre-commit-config.yaml would otherwise auto-strip notebook outputs / run biome on the Mastra app — not applicable here).

Files changed (3 files, +1827 / −0) :
- `docs/MATTE_SOURCE_INGESTION_AUDIT.md` (new, 379 lines)
- `scripts/audit_matte_ingestion.py` (new, 725 lines after ruff-format)
- `tests/test_issue_103_matte_ingestion_audit.py` (new, 723 lines after ruff-format)

Safety / blocked operations:
- Aucun staging/prod write, aucune migration, aucun démarrage de job Scaleway, aucune mutation Object Storage, aucune création d'index n'a été exécutée.
- L'outil `audit_matte_ingestion.py --db-readonly` est explicitement gardé par construction contre toute écriture (filtre regex sur mots-clés + import psycopg lazy + `conn.rollback()` systématique).
- Les commandes de remédiation (HNSW `CREATE INDEX CONCURRENTLY idx_rag_chunks_matte_embedding_m3_hnsw`, backfill `embedding_bge_scw`) restent **bloquées en attente d'approbation Paul**.

Reviewer next action:
- Vérifier le parsing statique de la liste `PDF_PATHS` (3 PDF parsés sur le notebook livré, test `test_real_amelioration_matte_notebook_parses_three_pdfs`).
- Vérifier le filtre de mots-clés d'écriture dans `audit_matte_ingestion.py:run_db_readonly` (ligne ~469, regex sur `insert|update|delete|create|drop|alter|truncate|…`) + 7 tests dédiés `test_refuses_*_keyword`.
- Vérifier l'émission de SQL read-only (8 requêtes, dont `coverage_embeddings` avec les 5 colonnes connues, `indexes` via `pg_indexes`).
- Vérifier le diagnostic `STALE_NOTEBOOKS` (cf. `test_detects_missing_notebooks`).
- Décider d'approuver (ou pas) la création d'index HNSW sur `embedding_m3` après avoir vu le résultat des requêtes read-only émises par l'outil.
- Aucun push / aucune PR ouverte par le dev (per instructions).

Acceptance criteria:
- Document MATTE acquisition path: where PDFs come from, manual export assumptions, notebook chain, table target, and automation recommendation.
- Audit code/notebooks/scripts/docs for completeness, idempotence, fail-fast, embeddings, and reproducibility risks.
- Add safe code/docs/tests/tooling where useful, without staging/prod writes.
- If a real backfill/index creation/reingestion is needed, stop with exact proposed command/SQL and await Paul approval.
- PR body must use `Problem` / `Solution`; include Mermaid if data flow or notebook-to-job flow is changed.

Safety boundary:
- Dev may inspect notebooks/scripts, write docs/tests/static tooling, and run local tests.
- Dev must not write to staging/prod DB, start jobs, mutate object storage, or create vector indexes outside code/docs without Paul approval.

### Independent review — Issue #103 MATTE ingestion audit

| Field | Value |
|---|---|
| Review timestamp | 2026-06-17T14:13Z |
| Reviewer | Independent reviewer subagent (`agent-8de462ff-11b7-4fab-9269-1077ce21cfde`) |
| Worktree | `/Users/dasco/dev/clients/dinuum/assistant-rh/.letta/worktrees/issue-103-matte-ingestion-audit` |
| Branch | `fix/issue-103-matte-ingestion-audit` |
| Commit reviewed | `f48f7e3d1f65f35d605195b54bb82de803bf6415` |
| Base verification | PASS — `merge-base HEAD origin/main` = `origin/main` = `128c7784bcf213f3b204779bca39604199f8c511`; no #119 dependency detected; diff contains only 3 new files |
| Commands run | `git status --short`; `git diff --stat origin/main...HEAD`; full `git diff origin/main...HEAD`; `git diff --check origin/main...HEAD`; `uv run --directory <worktree> pytest tests/test_issue_103_matte_ingestion_audit.py`; `uv run --directory <worktree> python scripts/audit_matte_ingestion.py --repo-root <worktree> --sql-only`; `uv run --directory <worktree> ruff check scripts/audit_matte_ingestion.py tests/test_issue_103_matte_ingestion_audit.py --select E,F,I`; `uv run --directory <worktree> pytest tests/ --ignore=tests/archive --ignore=tests/conformance` |
| Test gate | PASS — 43/43 issue tests passed; broad suite 242 passed, 2 skipped |
| Lint/format gate | PASS — diff check clean; ruff changed Python files clean |
| Requirement gate | PASS — docs/tool cover MATTE manual PDF acquisition, missing notebooks as findings, completeness gaps, canonical embeddings, idempotence risks, reproducibility, and absent vector index status |
| Safety/read-only gate | PASS — default `--sql-only` is offline/read-only; optional `--db-readonly` requires `MATTE_AUDIT_DSN`, lazy-imports psycopg, refuses write keywords including `CREATE`, `UPDATE`, `DELETE`, `SET`, and rolls back; no notebook execution or DB/object-store/job writes performed |
| Factuality gate | PASS — SQL-only output parsed exactly 3 PDFs from `amelioration_matte.ipynb`, reported missing `extract_matte.ipynb` and `ingestion_matte.ipynb`, emitted 8 read-only SQL statements, and aligned canonical columns to `rag_chunks_matte.embedding_m3` / `embedding_bge_scw` |
| Regression gate | PASS — no app/runtime package changes and broad non-archive/non-conformance tests passed |
| PR body readiness | PASS — should include a Mermaid diagram because manual notebook-to-table flow is central; suggested diagram: `flowchart LR; PDF[3 manual MATTE PDFs] --> NB[amelioration_matte.ipynb]; NB --> ART[JSONL/Parquet/NPY + BGE-M3 embeddings]; ART --> ING[generic ingestion_pdf.ipynb / missing ingestion_matte.ipynb]; ING --> DB[(rag_chunks_matte)]; DB --> RET[RAG retriever: embedding_m3]; AUD[audit_matte_ingestion.py --sql-only] -.read-only checks.-> DB` |
| Verdict | APPROVED |



### PR #127 — chore(ingestion): add MATTE read-only audit tooling

| Field | Value |
|---|---|
| Status | OPEN |
| URL | https://github.com/DGAFP/assistant-rh/pull/127 |
| Issue | https://github.com/DGAFP/assistant-rh/issues/103 |
| Branch | `fix/issue-103-matte-ingestion-audit` |
| Worktree | `/Users/dasco/dev/clients/dinuum/assistant-rh/.letta/worktrees/issue-103-matte-ingestion-audit` |
| Commit | `f48f7e3d1f65f35d605195b54bb82de803bf6415` |
| Dev model | `lc-minimax/MiniMax-M3` |
| Review model | `chatgpt-plus-pro/gpt-5.5` |
| Tests | PASS — 43 focused tests; 242 broad tests passed, 2 skipped |
| Lint/check | PASS — ruff and `git diff --check` clean |
| PR body | Problem / Solution format with Mermaid diagram |
| Verdict | APPROVED / PR OPEN |

Outcome:
- Adds MATTE read-only/offline audit runbook and script.
- Documents missing referenced notebooks, manual PDF acquisition, canonical embedding columns, idempotence risks, and vector-index remediation gates.
- No real data writes, notebook execution, DB backfill, or index creation.

Next candidates:
- RGRH ingestion audit (#104) or review feedback on open PRs.
