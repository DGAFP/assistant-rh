# Scaleway Serverless Jobs for Service-Public

## Chosen Service

Use `Scaleway Serverless Jobs`.

Why:
- the pipeline is a monthly batch
- it does not need an HTTP endpoint
- it can run on a cron schedule
- it fits the medallion pattern better than a permanent container


## Image Build

Build the dedicated image:

```bash
docker build -f Dockerfile.service_public_pipeline -t assistant-rh/service-public-pipeline:latest .
```

Recommended Scaleway Container Registry target:
- namespace: `assistant-rh`
- image: `rg.fr-par.scw.cloud/assistant-rh/service-public-pipeline:latest`

Local login, tag and push:

```bash
echo "$SCW_SECRET_KEY" | docker login rg.fr-par.scw.cloud/assistant-rh -u nologin --password-stdin
docker build -f Dockerfile.service_public_pipeline -t service-public-pipeline:latest .
docker tag service-public-pipeline:latest rg.fr-par.scw.cloud/assistant-rh/service-public-pipeline:latest
docker push rg.fr-par.scw.cloud/assistant-rh/service-public-pipeline:latest
```

GitHub Actions workflow available:
- [build-service-public-pipeline-image.yml](/Users/omar.gueddari/work/assistant-rh/.github/workflows/build-service-public-pipeline-image.yml)


## Runtime Command

Recommended monthly production command:

```bash
python scripts/service_public_medallion_pipeline.py \
  --target-env prod \
  --sync-object-storage \
  --no-embed
```

This does:
- read the `FXXX` list from the JSON config
- fetch the latest XML source
- generate local bronze, silver and gold artifacts
- sync them to the three Scaleway buckets under `prod/...`
- avoid DB writes

Default config file:
- [service_public_fiches.json](/Users/omar.gueddari/work/assistant-rh/config/service_public_fiches.json)

Migration-only fallback:
- `--batch-from-db` can still be used to bootstrap the JSON config from the current DB


## Recommended Job Definition

Reference manifest:
- [scaleway_serverless_job_service_public.json](/Users/omar.gueddari/work/assistant-rh/config/scaleway_serverless_job_service_public.json)
- UI checklist:
  [SCALEWAY_SERVERLESS_JOBS_UI_CHECKLIST.md](/Users/omar.gueddari/work/assistant-rh/docs/SCALEWAY_SERVERLESS_JOBS_UI_CHECKLIST.md)

Operational rule:
- create the job directly
- do not add a pre-creation listing step
- prefer CLI over UI
- if the job already exists, update it instead of building detection logic first

Recommended values:
- job name: `service-public-monthly-prod`
- region: `fr-par`
- image: `rg.fr-par.scw.cloud/assistant-rh/service-public-pipeline:latest`
- CPU: `2000 mvCPU`
- memory: `4096 MiB`
- local storage: `2048 MiB`
- timeout: `7200` seconds
- cron: `0 3 1 * *`
- timezone: `Europe/Paris`

Reasoning:
- enough headroom for the monthly XML fetch, parse and Object Storage sync
- no embedding workload in this job
- keeps a safe timeout margin without turning the job into a long-running worker


## Exact Creation Command

Template script:
- [create_scaleway_service_public_job.sh](/Users/omar.gueddari/work/assistant-rh/scripts/create_scaleway_service_public_job.sh)

Equivalent command:

```bash
scw jobs definition create \
  name=service-public-monthly-prod \
  cpu-limit=2000 \
  memory-limit=4096 \
  local-storage-capacity=2048 \
  image-uri=rg.fr-par.scw.cloud/assistant-rh/service-public-pipeline:latest \
  startup-command.0=python \
  args.0=scripts/service_public_medallion_pipeline.py \
  args.1=--target-env \
  args.2=prod \
  args.3=--sync-object-storage \
  args.4=--no-embed \
  environment-variables.SCW_ACCESS_KEY="$SCW_ACCESS_KEY" \
  environment-variables.SCW_SECRET_KEY="$SCW_SECRET_KEY" \
  environment-variables.SCW_DEFAULT_REGION=fr-par \
  environment-variables.SCW_BUCKET_BRONZE=assistant-rh-bronze \
  environment-variables.SCW_BUCKET_SILVER=assistant-rh-silver \
  environment-variables.SCW_BUCKET_GOLD=assistant-rh-gold \
  environment-variables.SCW_PREFIX_PROD=prod \
  environment-variables.SCW_PREFIX_STAGING=staging \
  job-timeout=7200s \
  cron-schedule.schedule="0 3 1 * *" \
  cron-schedule.timezone="Europe/Paris" \
  project-id="$SCW_DEFAULT_PROJECT_ID" \
  region=fr-par
```

This command is intentionally a direct `create`.
It does not list existing jobs beforehand.

Current observed blocker with the active API key:
- `insufficient permissions: write definition`

So the CLI path is correct, but the key currently used in `.env` cannot create the Serverless Job definition.


## Required Environment Variables

At minimum:

```env
SCW_ACCESS_KEY=...
SCW_SECRET_KEY=...
SCW_DEFAULT_REGION=fr-par

SCW_BUCKET_BRONZE=assistant-rh-bronze
SCW_BUCKET_SILVER=assistant-rh-silver
SCW_BUCKET_GOLD=assistant-rh-gold
SCW_PREFIX_PROD=prod
SCW_PREFIX_STAGING=staging
```

DB access is not required for the monthly production job.

DB variables are only needed if you later run:
- migration mode with `--batch-from-db`
- comparison scripts against the current database


## Schedule

Recommended cron:

```text
0 3 1 * *
```

Meaning:
- at `03:00 UTC`
- on day `1` of each month


## Recommended GitHub Secrets

For the image build workflow:

- `SCALEWAY_API_KEY`
- `SCW_CONTAINER_REGISTRY_NAMESPACE`

Recommended value:
- `SCW_CONTAINER_REGISTRY_NAMESPACE=assistant-rh`


## Sources

- Scaleway Jobs quickstart: https://www.scaleway.com/en/docs/serverless-jobs/quickstart/
- Scaleway scheduling: https://www.scaleway.com/en/docs/serverless-jobs/how-to/manage-job-schedule
- Scaleway CLI jobs docs: https://cli.scaleway.com/jobs/
- Scaleway Jobs v1alpha2 command model: https://www.scaleway.com/en/docs/serverless-jobs/reference-content/v1alpha1-to-v1alpha2/


## Object Storage Layout

The monthly run writes to:

- `assistant-rh-bronze/prod/bronze/service_public/...`
- `assistant-rh-silver/prod/silver/service_public/...`
- `assistant-rh-gold/prod/gold/service_public/...`


## Operational Guidance

- Keep DB load separate from the monthly generation run.
- Use the comparison scripts before replacing any existing production content.
- Keep manifests in bronze, silver and gold for auditability.
- If the monthly runtime grows too much, split the flow later into:
  - generation job
  - comparison job
  - explicit DB load job
