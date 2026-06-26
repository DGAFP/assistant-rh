# Git Flow

Assistant RH uses three long-lived branches before production:

```text
feature branch -> dev -> staging -> main -> release-please PR -> GitHub Release -> production deploy
```

## Branch Roles

- `dev` is the integration branch for feature work. Feature PRs target `dev`.
- `staging` is the deployable test branch. Merging `dev` into `staging` deploys the staging app and runs staging data preview jobs.
- `main` is the release branch. Merging `staging` into `main` lets release-please prepare the next release.

Do not use `main` as the default target for feature work. Production deployment is gated by a published GitHub Release, not by an ordinary merge to `main`.

## Promotion Sequence

1. Create feature branches from up-to-date `dev`.
2. Open feature PRs against `dev`.
3. Squash feature PRs into `dev` with a conventional PR title, for example `feat: add trace filters` or `fix: harden staging deploy`.
4. After local validation on `dev`, open a promotion PR from `dev` to `staging`.
5. Merge `dev -> staging` with a merge commit, not squash.
6. Verify the staging deployment, migrations, and staging data preview jobs.
7. Open a promotion PR from `staging` to `main`.
8. Merge `staging -> main` with a merge commit, not squash.
9. Wait for release-please to open or update its release PR against `main`.
10. Merge the release-please PR to create the version bump, changelog update, tag, and GitHub Release.
11. The published GitHub Release triggers production migrations and Streamlit production deployment.

Useful PR commands:

```bash
gh pr create -R DGAFP/assistant-rh --base dev --head <feature-branch>
gh pr create -R DGAFP/assistant-rh --base staging --head dev --title "chore: promote dev to staging"
gh pr create -R DGAFP/assistant-rh --base main --head staging --title "chore: promote staging to main"
```

## Merge Policy

Use squash merges for feature PRs into `dev`, and make the squash commit title conventional:

- `feat:` creates a minor release candidate.
- `fix:` creates a patch release candidate.
- `feat!:` or `BREAKING CHANGE:` creates a major release candidate.

Use merge commits for branch promotions:

- `dev -> staging`: preserve the conventional feature/fix commits that were tested locally.
- `staging -> main`: preserve those commits so release-please can compute the next version and release notes.

Do not squash promotion PRs. If a promotion PR is squashed into `main`, release-please only sees the promotion title and may compute the wrong release version.

## Deployment Behavior

On push to `staging`:

- `.github/workflows/streamlit-deploy-staging.yml` deploys Streamlit to `scaleway-staging`.
- `.github/workflows/db-migrations-scaleway.yml` pushes Supabase migrations to the staging database when migration or seed paths changed.
- `.github/workflows/data-engineering-preview-staging.yml` builds selected staging job images and runs staging data preview jobs for changed data-engineering sources.

The automatic staging data preview runs selected medallion, ingestion, and embeddings jobs for changed Service-Public or Legifrance sources. It keeps `wipe_existing_chunks=false`. MATTE embeddings remain manual.

On push to `main`:

- `.github/workflows/release-please.yml` opens or updates the release-please PR.
- Production does not deploy from the ordinary promotion merge itself.

On release publication:

- `.github/workflows/db-migrations-scaleway.yml` pushes production migrations.
- `.github/workflows/streamlit-deploy-production.yml` deploys Streamlit production after the production migration workflow succeeds.

Production data ingestion/promotion remains manual through workflow dispatch.

## Release-Please

Release-please reads conventional commits on `main`, updates `CHANGELOG.md`, bumps version fields, and publishes tags such as `v0.8.1`.

Current release configuration:

- Workflow: `.github/workflows/release-please.yml`
- Config: `release-please-config.json`
- Manifest: `.release-please-manifest.json`
- Version files: root `pyproject.toml`, package `pyproject.toml` files, and `apps/mastra-pipeline/package.json`

Examples:

- A preserved `feat: add admin export` commit after the previous release creates a minor bump.
- A preserved `fix: repair staging health check` commit after the previous release creates a patch bump.
- A `chore:`-only promotion creates no release unless there are releasable commits since the previous release.

## Rollback and Manual Runs

For Streamlit production rollback, run **Streamlit Deploy Production** manually with `existing_image_tag` set to a known-good image tag, for example `release-v0.8.0`.

For staging or production data jobs, prefer workflow dispatch with explicit source and embedding options. Avoid destructive staging or production data operations unless the selected workflow inputs clearly request them and the target database has been confirmed.

## Repository Settings

Protect `dev`, `staging`, and `main` in GitHub:

- Require PRs before merging.
- Require status checks appropriate to each branch.
- Keep merge commits available for `staging` and `main` promotion PRs.
- Keep the `scaleway-production` environment protected for production secrets and deployment approval.
