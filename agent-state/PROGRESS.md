# Assistant RH Agent Loop Progress

**Purpose:** short canonical state ledger. Every scheduled or dispatched loop run must read this first and update it before stopping.

**Last updated:** 2026-06-19T06:31Z
**Repo:** `/Users/dasco/dev/clients/dinuum/assistant-rh`
**Canonical state dir:** `/Users/dasco/dev/clients/dinuum/assistant-rh/agent-state`
**Alias:** `STATE.md -> PROGRESS.md`

---

## Loop topology

| Loop | Trigger | Agent | Output | Status |
|---|---|---|---|---|
| 1. Triage | Weekdays 08:00 Europe/Paris | Assistant RH — Triage (`agent-905cabae`) | `triage-findings.md`, `PROGRESS.md` | Active locally; remote runner not verified |
| 2. Issue selection | Weekdays 08:30 Europe/Paris | Assistant RH — Orchestrator (`agent-d48dfe12`) | `selected-issue.md`, `daily-plan.md`, `PROGRESS.md` | Active |
| 3. Dev cycle | Event/one-shot after Paul validation | Orchestrator + MiniMax dev subagents | worktree branch, run log | Human-gated |
| 4. Review cycle | After each dev iteration | ChatGPT review subagent | review verdict, run log, `PROGRESS.md` gates | Human-gated |

Design rule: scheduled loops discover and select. Code-writing loops start only after Paul validates the selected issue/task.

---

## Current state

| Field | Value |
|---|---|
| Current focus | RAG/data quality, not Mastra conformance |
| Mastra status | Paused; conformance failures are informational/backlog until Paul reactivates Mastra |
| Latest triage artifact | `triage-findings.md` from 2026-06-18T06:00Z |
| Latest triage run | 2026-06-18T06:00Z (daily-triage cron, fire #1) |
| Loop status | Healthy — CI green, DB accessible, Grafana unreachable from this machine |
| Blocked | Grafana/Cockpit API: connection timeout (HTTP 000); no psql/scalingo CLI locally |
| Latest selection | #121 DGAFP embedding reconciliation preserved for Paul validation; no newer triage artifact available |
| Active dev/review loop | None currently active — awaiting Paul validation before code-writing dispatch |
| Human checkpoint | Required before starting #121 Dev Cycle |
| Next recommended action | Ask Paul to validate #121 acceptance criteria/base ref/DB-write boundary; if he prefers small scope, validate #120 instead |

---

## Active PRs / artifacts

| Item | Status | Notes |
|---|---|---|
| PR #118 | Merged | Docs clarify Mastra pause and priority implications |
| PR #127 | Closed (split) | Replaced by #129 and #134 |
| PR #129 | Open | docs(ingestion): MATTE source audit runbook; scope corrected to offline/manual DB checks |
| PR #131 | Open | fix(ingestion): preserve Legifrance embeddings on upsert |
| PR #132 | Open | fix(ingestion): fail fast on missing Legifrance articles |
| PR #133 | Open | feat(ingestion): read-only embedding coverage audit |
| PR #134 | Open | chore(ingestion): MATTE offline audit tooling |
| PR #140 | Open | chore(ingestion): wire MATTE embeddings backfill |
| Issue #102 work | Reviewed/approved | DGAFP/Légifrance ingestion audit — PRs #131-133 open |
| Issue #120 | Open (P0) | rag_chunks_test fail-fast — new since last triage |
| Issue #121 | Open (P0) | DGAFP embedding reconciliation from ghost table — new since last triage |
| Issue #122 | Open (P0/P1) | Reranker failure alerting — new since last triage |
| Issue #124 | Open (P1) | Missing vector indexes on matte/legifrance/rgrh — confirmed by DB query |

Long history archived in `runs/2026-06-17-progress-before-loop-split.md`.

---

## Blockers and approvals

| Item | Blocker | Required unblock |
|---|---|---|
| Any production/staging DB write or backfill | Explicit approval required | Paul approves exact command after code/review |
| Next dev cycle | Human validation required | Paul validates selected issue/task |
| Merge/publish/deploy | Human approval required | Paul approves merge/deploy action |

---

## Dev/review gates

A task is complete only when all applicable gates pass with evidence:

| Gate | Required evidence |
|---|---|
| Test gate | Exact test command + PASS output, or justified N/A |
| Lint/type gate | Exact lint/type/check command + PASS output, or justified N/A |
| Requirement gate | Reviewer maps implementation to acceptance criteria |
| Regression gate | Reviewer identifies no blocking side effects |

Hard rule: never mark complete because an agent says “done.”

Backstops:
- Default expected budget: 3 dev/review iterations.
- Maximum budget: 10 iterations.
- If the same failure repeats twice without meaningful progress, halt and ask Paul or CTO.

---

## Worktree and state access rules

Worktree/base rule:
- Implementation worktrees and task branches must be created from fresh `origin/main` unless Paul or the validated plan explicitly specifies another base ref.
- Never assume local `main` is current.

Shared state access:
- Preferred path from any process: `/Users/dasco/dev/clients/dinuum/assistant-rh/agent-state/PROGRESS.md`.
- Every new worktree should also symlink local `agent-state` to the canonical state dir:
  `ln -sfn /Users/dasco/dev/clients/dinuum/assistant-rh/agent-state agent-state`.
- Long details go in `agent-state/runs/`; keep `PROGRESS.md` short.

---

## Remote runner status

Current status: **NOT VERIFIED**. The local machine has the repo, `.env`, GitHub CLI auth, and current crons, but that does not prove any always-on remote environment has them.

Evidence from local inspection:
- Local runner passes `scripts/agent-loop/remote-triage-readiness.sh` with `REMOTE_TRIAGE_READY=1`.
- Desktop preference `remoteEnvName` is `MacBookPro.lan`, which is not proof of a laptop-independent runner.
- Current verified repo, `.env`, GitHub auth, and cron definitions are local until the readiness script is run inside the remote environment.

Required before relying on unattended triage:
1. Run `scripts/agent-loop/remote-triage-readiness.sh` inside the remote environment.
2. Confirm `REMOTE_TRIAGE_READY=1`.
3. Confirm `daily-triage` and `issue-selection` crons exist in that remote environment.
4. Confirm the remote can access the canonical state path or a deliberate symlink equivalent.

Bootstrap doc: `scripts/agent-loop/bootstrap-remote-triage.md`.

---

## Cost and safety budget

| Budget item | Limit |
|---|---:|
| Dev model | `lc-minimax/MiniMax-M3` |
| Review model | `chatgpt-plus-pro/gpt-5.5` |
| Dev/review iterations | 10 max per task |
| Human approval before code dispatch | Required |
| Human approval before merge/publish/deploy | Required |

Allowed command families for semi-attended loop work:
- read/search: `ls`, `cat`, `sed`, `grep`, `find`, `rg`, `head`, `tail`
- git inspection/worktree: `git status`, `git diff`, `git log`, `git fetch`, `git worktree`, `git branch`
- project checks: `uv`, `pytest`, `ruff`, `pnpm`
- GitHub inspection/PR creation when explicitly approved: `gh`

Risky operations require explicit approval:
- production/staging writes or migrations
- deleting files outside task scope
- force push / reset / rebase
- secret changes
- deploys, merges, or publishing


### PR #127 split completion

Oversized PR #127 was split and closed. Replacement PRs under size limit:
- #129 — docs(ingestion): add MATTE source audit runbook — 373 additions after integrity correction.
- #134 — chore(ingestion): add MATTE offline audit tooling — 248 additions.

The optional DB-readonly/artifact-validation portions from the oversized aggregate were intentionally deferred; if needed, they should be implemented as a separate small follow-up PR, preferably stacked after #134 or after #134 merges.

### Daily plan selected (2026-06-18)

1. **Selected for validation: [P0] #121 DGAFP embedding reconciliation** — Build dry-run diagnostics plus guarded keyed copy from `rag_chunks_dgafp_scalingo` (100% coverage) to live `rag_chunks_dgafp` (0% coverage). No DB write until separate exact-command approval.
2. **Fallback if Paul wants small scope: [P0] #120 rag_chunks_test fail-fast** — Make source explicitly optional or fail-fast when table absent; fix false diagnostics.
3. **Visible P0 risk while Mastra paused: Goldset population/#106** — `goldset_questions_v2` has 0 rows, but conformance/Mastra remains paused unless Paul reactivates.
4. **[P1] #124 Create vector indexes** — Start with MATTE via versioned migration; defer DGAFP/RGRH indexes until coverage is sufficient unless justified.
5. **[P1] Review/merge open ingestion PRs** — #129, #131-134, #140 are open; consider #102 stack first.


### Issue-selection run (2026-06-18T06:31Z)

No material priority change from `triage-findings.md` 2026-06-18T06:00Z. Selection remains **#121 DGAFP embedding reconciliation** for Paul validation because live DGAFP has 3,992 chunks with 0% `embedding_m3` coverage while `rag_chunks_dgafp_scalingo` has matching row count and 100% coverage. Mastra remains paused; conformance/goldset risk stays visible but is not selected as the next P0 implementation task. Fallback remains **#120 rag_chunks_test fail-fast/optional** if Paul wants a smaller first task.

Updated `selected-issue.md` with explicit acceptance criteria, hard gates, expected iteration budget, and required approvals. `daily-plan.md` was not changed because the plan did not materially change.


### Issue-selection run (2026-06-19T06:31Z)

No newer `triage-findings.md` was available for the 2026-06-19 issue-selection cron; latest triage remains 2026-06-18T06:00Z. Selection therefore remains **#121 DGAFP embedding reconciliation** for Paul validation, preserving the prior RAG/data-quality priority and the paused-Mastra rule. No active dev/review loop was recorded. Fallback remains **#120 rag_chunks_test fail-fast/optional** if Paul wants a smaller no-DB-write task. `selected-issue.md` was refreshed with the same acceptance criteria, gates, budget, and approval boundaries; `daily-plan.md` was not changed because the plan did not materially change.
