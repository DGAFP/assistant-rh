---
name: promote-dev-staging
description: Promote the integration branch to staging by opening a dev -> staging promotion PR with a merge-commit (never squash). Runs a preflight that lists the commits being promoted and predicts which staging workflows will fire (Streamlit deploy, DB migrations, data-engineering preview full vs scoped). Use when the user wants to promote dev to staging, cut a staging release, or ship the current dev to the staging environment.
triggers:
  - promote dev to staging
  - promote to staging
  - dev to staging
  - staging promotion
  - ship to staging
category: release
---

# Promote dev → staging

Open a **promotion PR** from `dev` into `staging` for Assistant RH, after a
preflight that shows exactly what is being promoted and what will run when the
PR is merged. This skill **stops at PR creation** — a human reviews and merges.

Repo: `DGAFP/assistant-rh`. Canonical flow: `docs/git_flow.md`.

## Hard rule: merge commit, never squash

`dev → staging` (and later `staging → main`) **must be merged with a merge
commit**, not squashed. release-please computes the next version from the
individual `feat:` / `fix:` conventional commits once they reach `main`; a
squash collapses them into the promotion title and breaks version/changelog
computation. The PR this skill opens is titled `chore:` precisely so a squash
would produce *no* release at all — that is the failure mode to avoid.

## Procedure

### 1. Sync refs
```bash
git fetch origin dev staging
```
Operate on `origin/dev` and `origin/staging`, not whatever is checked out
locally.

### 2. Preflight — what is being promoted
```bash
git log --oneline origin/staging..origin/dev
```
- If this is **empty**, `staging` is already up to date with `dev` — report
  "nothing to promote" and stop.
- Otherwise list the commits. Flag any that are **not** conventional
  (`feat:`/`fix:`/`chore:`/…) since they still need to survive into `main` for
  release-please.

Compute the promoted file set once and reuse it:
```bash
git diff --name-only origin/staging..origin/dev
```

### 3. Preflight — what will fire when staging is pushed

Every merge to `staging` is a push to `staging`. Predict the triggered
workflows from the promoted file set:

- **Streamlit Deploy Staging** (`streamlit-deploy-staging.yml`) — **always
  runs** (no path filter). The staging app is redeployed on every promotion.
- **Database Migrations (Scaleway)** (`db-migrations-scaleway.yml`) — runs only
  if the promoted files touch any of:
  - `supabase/migrations/**`
  - `tests/conformance/**/*.jsonl`
  - `scripts/load_goldset_seed.py`
  - `.github/workflows/db-migrations-scaleway.yml`
- **Data Engineering Preview Staging** (`data-engineering-preview-staging.yml`)
  — runs only if the promoted files touch its path filter:
  - `.github/workflows/data-engineering-*.yml`
  - `.github/scripts/data_engineering_plan.py`,
    `.github/scripts/scaleway_data_jobs.py`
  - `.github/data-engineering-jobs.json`
  - `apps/data-ingestion-cli/**`, `packages/data-engineering/**`,
    `packages/shared-config/**`, `config/**`
  - any `Dockerfile.{service_public,legifrance,embeddings}_*`

  If it runs, predict **full vs scoped** using the same logic as
  `classify_from_files` in `.github/scripts/data_engineering_plan.py`:
  - A change touching a **specific source** (Service-Public or Légifrance
    paths/Dockerfiles/config) scopes the preview to that source.
  - A change touching **only common/CI/shared paths** (workflows, the plan/jobs
    scripts, `uv.lock`, `packages/shared-config/**`, `…/utils/**`, …) with no
    source-specific file triggers a **full all-sources** preview: medallion +
    ingestion + re-embed of **both** sources. This is a long (up to 180 min),
    Scaleway-compute-heavy run. It is **non-destructive** (`wipe_existing_chunks`
    stays `false`).

  Dry-run the prediction instead of guessing:
  ```bash
  GITHUB_EVENT_NAME=push python3 - <<'PY'
  import subprocess
  import importlib.util
  spec = importlib.util.spec_from_file_location("plan", ".github/scripts/data_engineering_plan.py")
  plan = importlib.util.module_from_spec(spec); spec.loader.exec_module(plan)
  files = subprocess.check_output(
      ["git", "diff", "--name-only", "origin/staging..origin/dev"], text=True
  ).split()
  selected = plan.classify_from_files(files)
  print("data preview selection:", selected)
  print("embedding_source:", plan.infer_embedding_source(selected))
  PY
  ```

Surface this prediction to the user **before** creating the PR. If a full
all-sources data preview is predicted and it looks unintended, say so.

### 4. Don't duplicate an open promotion PR
```bash
gh pr list -R DGAFP/assistant-rh --base staging --head dev --state open
```
If one already exists, link it and stop (it auto-updates as `dev` advances).

### 5. Open the promotion PR
```bash
gh pr create -R DGAFP/assistant-rh \
  --base staging --head dev \
  --title "chore: promote dev to staging" \
  --body "<preflight summary: commits promoted + predicted staging workflows>"
```
Put the preflight summary (promoted commits and predicted Streamlit,
migrations, and data-preview behavior) in the PR body.

### 6. Hand off — the human merges
Do **not** merge. Tell the user to merge with a **merge commit**, e.g.:
```bash
gh pr merge <number> -R DGAFP/assistant-rh --merge
```
Reiterate: do **not** use `--squash`.

## After the user merges (for reference)

The staging push runs the workflows predicted in step 3. Once staging is
validated, the next step is a `staging → main` promotion PR (also a merge
commit), which lets release-please open its release PR. See `docs/git_flow.md`.
