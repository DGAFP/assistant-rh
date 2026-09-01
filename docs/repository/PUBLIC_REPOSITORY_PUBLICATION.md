# Public repository publication runbook

## Status

Draft runbook for publishing a clean public repository from the private `DGAFP/assistant-rh` repository.

## Decision

Use a staged public repository:

- Keep the current private repository: `DGAFP/assistant-rh`.
- Create a new public repository: `DGAFP/assistant-rh-public`.
- Seed the public repository from a clean snapshot of `main`, not from the private Git history.
- Validate the public repository and its workflows before making the private repository read-only.

Do **not** make the current private repository public. Its Git history, pull request diffs, Actions logs, and review history may contain restricted artifacts or historical secrets that are no longer present in the cleaned tree.

## Why not rename or flip visibility directly?

The private repository has accumulated private operational history. The current tree has been cleaned, but GitHub visibility changes expose more than the current file tree: historical commits, PR discussions, review diffs, Actions metadata, and some logs become part of the public repository context.

The safe publication boundary is therefore a new Git repository initialized from the cleaned tree.

## Current private repository settings snapshot

Observed from `DGAFP/assistant-rh` on 2026-05-24.

| Setting | Value |
| --- | --- |
| Visibility | private |
| Default branch | `main` |
| Issues | enabled |
| Projects | enabled |
| Wiki | disabled |
| Discussions | disabled |
| Delete branch on merge | enabled |
| Merge commits | enabled |
| Squash merge | enabled |
| Rebase merge | enabled |
| Auto-merge | disabled |
| Update branch button | disabled |
| Actions | enabled |
| Allowed Actions | all |
| Default workflow permissions | read |
| Actions can approve PR reviews | true |

Open milestone:

```text
Répondre de manière aux questions RH interministérielles (MATTE, MASA, MSO, MI)
```

Current environments:

```text
scaleway-staging
scaleway-production
```

Current environment protection rules are empty. For the public repository, consider adding a required reviewer on `scaleway-production` before loading production deployment secrets.

The private repository has an active ruleset, but GitHub currently returns `403` for branch protection/ruleset details while the repository is private on the current plan. Recreate the desired public-repository rules explicitly after `assistant-rh-public` exists rather than assuming the private ruleset can be copied exactly.

## Workflow secret usage

Several workflows declare a deployment environment and should use environment-scoped secrets:

```text
data-engineering-preview-staging.yml    → scaleway-staging
data-engineering-prod-ingestion.yml     → scaleway-production
data-engineering-promote-prod.yml       → scaleway-production
db-migrations-scaleway.yml              → scaleway-staging / scaleway-production
streamlit-deploy-staging.yml            → scaleway-staging
streamlit-deploy-production.yml         → scaleway-production
```

Some image-build/release workflows do **not** declare an environment and currently rely on repository-level secrets:

```text
build-embeddings-job-image.yml
build-legifrance-bulk-dump-image.yml
build-legifrance-ingestion-image.yml
build-legifrance-pipeline-image.yml
build-service-public-pipeline-image.yml
release-please.yml
```

Do not delete or omit repository-level registry/release secrets until these workflows are either updated to use environments or confirmed unnecessary in the public repository.

## Secret and variable inventory

GitHub does not allow reading secret values back. Treat this table as a name inventory only. Values must come from the source secret manager or be rotated and re-entered.

### Repository variables

| Name | Value |
| --- | --- |
| `ALBERT_BASE_URL` | `https://albert.api.etalab.gouv.fr/v1` |
| `SCALEWAY_BASE_URL` | `https://api.scaleway.ai/v1` |

### Repository secrets currently present

```text
ADMIN_PASSWORD
ALBERT_API_KEY
COOKIES_PASSWORD
RELEASE_PLEASE_TOKEN
SCALEWAY_API_KEY
SCW_ACCESS_KEY
SCW_CONTAINER_REGISTRY_NAMESPACE
SCW_DEFAULT_ORGANIZATION_ID
SCW_DEFAULT_PROJECT_ID
SCW_DEFAULT_REGION
SCW_SECRET_KEY
```

### Environment secrets currently present

```text
scaleway-staging:
- SCW_ACCESS_KEY
- SCW_POSTGRES_DSN
- SCW_SECRET_KEY

scaleway-production:
- SCW_ACCESS_KEY
- SCW_POSTGRES_DSN
- SCW_SECRET_KEY
```

## Secret cleanup before publication

Before loading secrets into the public repository:

1. Rotate any credential that appeared in private notebook output or private PR diffs.
2. Rotate the database user/password embedded in the exposed local Postgres DSN.
3. Update `SCW_POSTGRES_DSN` for `scaleway-staging` and `scaleway-production` if either DSN used the exposed credential.
4. Prefer environment-scoped secrets for deployment/runtime credentials.
5. Keep repository-level secrets only when workflows run without an environment.

Recommended split for the public repository:

### Repository-level secrets

Keep only secrets required by workflows that do not declare an environment:

```text
RELEASE_PLEASE_TOKEN
SCW_CONTAINER_REGISTRY_NAMESPACE
SCW_SECRET_KEY              # only if image-build workflows need repo-level registry auth
SCALEWAY_API_KEY            # only if image-build fallback auth is still needed
```

### Environment-level secrets

Use environment secrets for staging/production deployment and database credentials:

```text
scaleway-staging:
- ADMIN_PASSWORD
- ALBERT_API_KEY
- COOKIES_PASSWORD
- SCALEWAY_API_KEY
- SCW_ACCESS_KEY
- SCW_DEFAULT_ORGANIZATION_ID
- SCW_DEFAULT_PROJECT_ID
- SCW_POSTGRES_DSN
- SCW_SECRET_KEY

scaleway-production:
- ADMIN_PASSWORD
- ALBERT_API_KEY
- COOKIES_PASSWORD
- SCALEWAY_API_KEY
- SCW_ACCESS_KEY
- SCW_DEFAULT_ORGANIZATION_ID
- SCW_DEFAULT_PROJECT_ID
- SCW_POSTGRES_DSN
- SCW_SECRET_KEY
```

### Variables instead of secrets

Use variables for non-secret configuration:

```text
ALBERT_BASE_URL=https://albert.api.etalab.gouv.fr/v1
SCALEWAY_BASE_URL=https://api.scaleway.ai/v1
SCW_DEFAULT_REGION=fr-par
```

`SCW_DEFAULT_PROJECT_ID` and `SCW_DEFAULT_ORGANIZATION_ID` are not passwords, but the current workflows read them from `secrets.*`. Keep them as secrets unless the workflows are updated in the same change to read them from `vars.*`.

## Pre-publication tree audit

Run from the private repository `main` worktree after all public-prep PRs are merged:

```bash
git pull --ff-only

# Notebook hygiene must be idempotent.
python3 scripts/strip_notebook_outputs.py --fix notebooks scripts
python3 scripts/strip_notebook_outputs.py notebooks scripts

# Targeted leak checks for known public-prep risks.
rg -n \
  "golden_beta_judge|src/_archive|postgresql://|hf_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9_-]{20,}|print\\(os\\.environ\\[\\\"DATABASE_URL\\\"\\]\\)" \
  . \
  --glob '!uv.lock' \
  --glob '!package-lock.json' \
  --glob '!pnpm-lock.yaml'

# Secret scanner on the current tree only.
gitleaks detect --no-git --redact

# Minimum regression checks used during public prep.
uv run pytest tests/test_private_datasets.py tests/test_service_public_archive_imports.py
```

The `rg` command is expected to find references in documentation only if the runbook intentionally names these patterns. Review every match before publication.

## Create the public repository

Create the repository empty first:

```bash
gh repo create DGAFP/assistant-rh-public \
  --public \
  --description "Assistant RH - chatbot for French public-sector HR questions" \
  --disable-wiki
```

Apply baseline settings:

```bash
gh repo edit DGAFP/assistant-rh-public \
  --enable-issues \
  --enable-projects \
  --enable-wiki=false \
  --delete-branch-on-merge \
  --enable-merge-commit \
  --enable-squash-merge \
  --enable-rebase-merge \
  --enable-auto-merge=false \
  --allow-update-branch=false \
  --squash-merge-commit-message pr-title-commits
```

`gh repo edit` uses lower-case values for `--squash-merge-commit-message`; `pr-title-commits` maps to the REST API value `COMMIT_MESSAGES`, which matches the current private repository setting.

If available for the organization/public repository, enable secret scanning and push protection:

```bash
gh repo edit DGAFP/assistant-rh-public \
  --enable-secret-scanning \
  --enable-secret-scanning-push-protection
```

Configure Actions permissions:

```bash
gh api \
  --method PUT \
  repos/DGAFP/assistant-rh-public/actions/permissions/workflow \
  -f default_workflow_permissions=read \
  -F can_approve_pull_request_reviews=true
```

Create environments:

```bash
gh api --method PUT repos/DGAFP/assistant-rh-public/environments/scaleway-staging

gh api --method PUT repos/DGAFP/assistant-rh-public/environments/scaleway-production
```

Before adding production secrets, consider adding environment protection in the GitHub UI:

```text
Settings → Environments → scaleway-production → Required reviewers
```

## Seed the public repository with clean history

Use `git archive` so only tracked files from the cleaned `main` tree are copied. This avoids accidentally copying `.env`, `.venv`, local caches, or the private `.git` directory.

```bash
SOURCE=/path/to/private/assistant-rh/main
TARGET=/tmp/assistant-rh-public-seed

rm -rf "$TARGET"
mkdir -p "$TARGET"

git -C "$SOURCE" archive --format=tar HEAD | tar -x -C "$TARGET"

cd "$TARGET"

gitleaks detect --no-git --redact
python3 scripts/strip_notebook_outputs.py notebooks scripts
uv run pytest tests/test_private_datasets.py tests/test_service_public_archive_imports.py

git init
git add .
git commit -m 'Initial public release'
git branch -M main
git remote add origin git@github.com:DGAFP/assistant-rh-public.git
git push -u origin main
```

## Configure variables and secrets

Set variables:

```bash
printf '%s' 'https://albert.api.etalab.gouv.fr/v1' | gh variable set ALBERT_BASE_URL --repo DGAFP/assistant-rh-public
printf '%s' 'https://api.scaleway.ai/v1' | gh variable set SCALEWAY_BASE_URL --repo DGAFP/assistant-rh-public
printf '%s' 'fr-par' | gh variable set SCW_DEFAULT_REGION --repo DGAFP/assistant-rh-public
```

Set repository secrets only when needed by workflows without an environment:

```bash
gh secret set RELEASE_PLEASE_TOKEN --repo DGAFP/assistant-rh-public
# Optional, only if image-build workflows still need repo-level registry auth:
gh secret set SCW_CONTAINER_REGISTRY_NAMESPACE --repo DGAFP/assistant-rh-public
gh secret set SCW_SECRET_KEY --repo DGAFP/assistant-rh-public
gh secret set SCALEWAY_API_KEY --repo DGAFP/assistant-rh-public
```

Set environment secrets:

```bash
# Staging
gh secret set ADMIN_PASSWORD --repo DGAFP/assistant-rh-public --env scaleway-staging
gh secret set ALBERT_API_KEY --repo DGAFP/assistant-rh-public --env scaleway-staging
gh secret set COOKIES_PASSWORD --repo DGAFP/assistant-rh-public --env scaleway-staging
gh secret set SCALEWAY_API_KEY --repo DGAFP/assistant-rh-public --env scaleway-staging
gh secret set SCW_ACCESS_KEY --repo DGAFP/assistant-rh-public --env scaleway-staging
gh secret set SCW_DEFAULT_ORGANIZATION_ID --repo DGAFP/assistant-rh-public --env scaleway-staging
gh secret set SCW_DEFAULT_PROJECT_ID --repo DGAFP/assistant-rh-public --env scaleway-staging
gh secret set SCW_POSTGRES_DSN --repo DGAFP/assistant-rh-public --env scaleway-staging
gh secret set SCW_SECRET_KEY --repo DGAFP/assistant-rh-public --env scaleway-staging

# Production
gh secret set ADMIN_PASSWORD --repo DGAFP/assistant-rh-public --env scaleway-production
gh secret set ALBERT_API_KEY --repo DGAFP/assistant-rh-public --env scaleway-production
gh secret set COOKIES_PASSWORD --repo DGAFP/assistant-rh-public --env scaleway-production
gh secret set SCALEWAY_API_KEY --repo DGAFP/assistant-rh-public --env scaleway-production
gh secret set SCW_ACCESS_KEY --repo DGAFP/assistant-rh-public --env scaleway-production
gh secret set SCW_DEFAULT_ORGANIZATION_ID --repo DGAFP/assistant-rh-public --env scaleway-production
gh secret set SCW_DEFAULT_PROJECT_ID --repo DGAFP/assistant-rh-public --env scaleway-production
gh secret set SCW_POSTGRES_DSN --repo DGAFP/assistant-rh-public --env scaleway-production
gh secret set SCW_SECRET_KEY --repo DGAFP/assistant-rh-public --env scaleway-production
```

## Recreate labels and milestones

Labels can be cloned from the private repository:

```bash
gh label clone DGAFP/assistant-rh --repo DGAFP/assistant-rh-public --force
```

Milestones must be recreated manually or by API. Current open milestone:

```bash
gh api --method POST repos/DGAFP/assistant-rh-public/milestones \
  -f title='Répondre de manière aux questions RH interministérielles (MATTE, MASA, MSO, MI)' \
  -f due_on='2026-04-25T00:00:00Z'
```

The due date is already in the past in the private repository. Decide whether to preserve it exactly or update it before creating the public milestone.

## Post-public validation

After the initial push:

1. Confirm the repository is public and has a single clean initial commit.
2. Confirm Actions are visible and default permissions are read-only.
3. Confirm no secret values appear in Actions logs.
4. Run the CI workflow on `main`.
5. Test that private dataset access fails clearly without `HF_TOKEN`.
6. Test that private dataset access succeeds when `HF_TOKEN` is provided locally or in a trusted environment.
7. Confirm `data/golden_beta` restricted CSVs are absent.
8. Confirm `src/_archive` is absent.
9. Confirm notebooks have no committed outputs.

Useful checks:

```bash
gh repo view DGAFP/assistant-rh-public --json visibility,defaultBranchRef,hasIssuesEnabled,hasProjectsEnabled,hasWikiEnabled

gh workflow list --repo DGAFP/assistant-rh-public --all

gh secret list --repo DGAFP/assistant-rh-public

gh secret list --repo DGAFP/assistant-rh-public --env scaleway-staging
gh secret list --repo DGAFP/assistant-rh-public --env scaleway-production
```

## Make the private repository read-only

Do this only after the public repository has been validated and any required issues/projects have been moved or intentionally left in the private archive.

On the current GitHub plan, private-repository branch protection APIs returned `403`, so a branch-rule-only freeze may not be available or reliable while the repository remains private. GitHub archive mode is the reliable hard read-only switch.

There are two modes:

### Soft freeze

Use this if the team still needs to reference or migrate issues/PRs:

1. Announce that `DGAFP/assistant-rh` is frozen.
2. Disable or ignore deployment workflows from the private repository.
3. Close or migrate open PRs.
4. Update the private repository description to point to `DGAFP/assistant-rh-public`.
5. Avoid further pushes except emergency corrections.

Suggested description:

```bash
gh repo edit DGAFP/assistant-rh \
  --description 'Private historical archive. Public development continues at DGAFP/assistant-rh-public.'
```

### Hard read-only

Use GitHub archive mode for true read-only behavior:

```bash
gh repo archive DGAFP/assistant-rh --yes
```

Archiving is intentionally heavy-weight: it makes the repository read-only and stops normal development in that repository. Do not run it until public repository validation is complete.

## Rollback

If the public repository validation fails before the private repository is archived:

1. Keep `DGAFP/assistant-rh` as the source of truth.
2. Make `DGAFP/assistant-rh-public` private or delete it if publication exposed a problem.
3. Fix the issue in the private repository.
4. Recreate the public repository from a new clean snapshot.

If the private repository has already been archived, unarchive it before resuming private development:

```bash
gh repo unarchive DGAFP/assistant-rh
```
