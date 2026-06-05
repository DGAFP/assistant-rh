# Scaleway Streamlit Deploy Runbook

## Scope

This runbook covers deployment and rollback for the Streamlit application on Scaleway Serverless Containers.

- Staging workflow: `.github/workflows/streamlit-deploy-staging.yml`
- Production workflow: `.github/workflows/streamlit-deploy-production.yml`

## Deployment strategy

- **Staging** deploys automatically on every push/merge to `main`.
- **Production** deploys automatically on `release.published`.
- **Rollback** can be performed by redeploying a previous image tag via `workflow_dispatch` on the production workflow.

## Release creation (release-please)

- Workflow: `.github/workflows/release-please.yml`
- Trigger: push to `main` (and manual `workflow_dispatch`)
- Config: `release-please-config.json` + `.release-please-manifest.json`
- Strategy: `release-type=python` + `extra-files` so release PRs also bump version fields in `pyproject.toml` and `package.json` files
- Auth token: `RELEASE_PLEASE_TOKEN` secret (PAT/GitHub App token). This is required so release-please-created tags/releases can trigger downstream workflows (production deploy on `release.published`).
- Required token repository permissions: `contents: write`, `pull-requests: write`, `issues: write`.

Flow:

1. release-please opens/updates a release PR from conventional commits on `main`.
2. Merging that PR creates a tag + GitHub Release (for example `v0.3.1`).
3. The published release event triggers the production deployment workflow.

Versioning rules:

- `feat:` → minor bump
- `fix:` → patch bump
- `!` or `BREAKING CHANGE:` → major bump

## Image naming convention

Registry image repository:

- `rg.<region>.scw.cloud/<namespace>/streamlit-ui`

Tags:

- staging immutable tag: `staging-<commit_sha>`
- staging rolling tag: `staging-latest`
- production immutable tag: `release-<release_tag>`
- production rolling tag: `prod-latest`

## Scaleway resources

The deployment script creates resources if they do not exist yet:

- staging namespace/container: `assistant-rh-streamlit-staging`
- production namespace/container: `assistant-rh-streamlit-production`

The Streamlit container is intentionally deployed with `max-scale=1` by default.
Streamlit keeps UI session state in the running process, and the admin UI is expected
to remain stateful while the end-user chat UI moves to a separate client/container.
During the experimental phase, single-instance deployment is preferred over horizontal
scaling to avoid cross-instance session routing issues.

## Required GitHub configuration

Required secrets (scaleway-staging and/or scaleway-production environment):

> GitHub Environment secrets use the same names in staging and production; the selected `environment:` supplies the right value. Do not use repo-level fallback DSNs for runtime deployment.

- `SCW_ACCESS_KEY`
- `SCW_SECRET_KEY`
- `SCW_DEFAULT_PROJECT_ID`
- `SCW_DEFAULT_ORGANIZATION_ID`
- `SCW_CONTAINER_REGISTRY_NAMESPACE`
- `SCW_POSTGRES_DSN` (staging and production; same secret name, environment-scoped value)
- `ALBERT_API_KEY`
- `SCALEWAY_API_KEY`
- `COOKIES_PASSWORD`
- `ADMIN_PASSWORD`

Runtime environment values set by the workflows:

- `APP_ENV=staging|production`
- `APP_DB_TARGET=scaleway`
- `APP_SCALEWAY_ENV=staging|production`

Optional variables:

- `SCW_DEFAULT_REGION` (defaults to `fr-par`)
- `ALBERT_BASE_URL` (defaults to `https://albert.api.etalab.gouv.fr/v1`)
- `SCALEWAY_BASE_URL` (defaults to `https://api.scaleway.ai/v1`)
- `STREAMLIT_STAGING_BASE_URL` (preferred canonical staging URL)
- `STREAMLIT_PRODUCTION_BASE_URL` (preferred canonical production URL)

## Health check contract

Post-deploy health check URL:

- `/<base_url>/_stcore/health`

`base_url` resolution order:

1. `STREAMLIT_<ENV>_BASE_URL` GitHub variable, if provided
2. Scaleway generated domain name from container metadata

If both are missing, deployment fails.

## Rollback procedure

### Preferred (workflow-based)

1. Open **Actions** → **Streamlit Deploy Production**.
2. Click **Run workflow**.
3. Set `existing_image_tag` to a previously known-good tag (for example `release-v0.9.3`).
4. Run workflow and verify health check is green.

### CLI fallback

If GitHub Actions is unavailable, redeploy directly with Scaleway CLI:

```bash
scw container container update <container-id> \
  registry-image=rg.fr-par.scw.cloud/<registry-namespace>/streamlit-ui:<image-tag> \
  region=fr-par \
  -w
```

Then verify:

```bash
curl -fsS https://<production-domain>/_stcore/health
```

## Verification checklist

After deployment (or rollback):

- Health endpoint returns 200.
- Main chatbot page loads.
- At least one real question returns an answer.
- Admin page auth works.
- Logs show no startup crash loop.

## Incident note template

```text
Date/Time:
Environment: staging|production
Trigger: main push | release published | manual rollback
Image tag:
Observed issue:
Rollback tag (if any):
Health check result:
Follow-up actions:
```
