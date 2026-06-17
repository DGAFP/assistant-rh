# Scaleway Serverless Jobs for Legifrance

## Chosen Service

Use `Scaleway Serverless Jobs`.

Why:
- the Legifrance flow is a batch
- it does not need an HTTP endpoint
- it fits the bronze/silver/gold pattern
- generation and DB ingestion should stay separate


## Image Naming

Recommended registry target:
- namespace: `assistant-rh`
- pipeline image: `rg.fr-par.scw.cloud/assistant-rh/legifrance-pipeline:latest`
- ingestion image: `rg.fr-par.scw.cloud/assistant-rh/legifrance-ingestion:latest`

Build locally:

```bash
docker build -f Dockerfile.legifrance_pipeline -t legifrance-pipeline:latest .
docker build -f Dockerfile.legifrance_ingestion -t legifrance-ingestion:latest .
```

Login, tag and push:

```bash
echo "$SCW_SECRET_KEY" | docker login rg.fr-par.scw.cloud/assistant-rh -u nologin --password-stdin
docker tag legifrance-pipeline:latest rg.fr-par.scw.cloud/assistant-rh/legifrance-pipeline:latest
docker tag legifrance-ingestion:latest rg.fr-par.scw.cloud/assistant-rh/legifrance-ingestion:latest
docker push rg.fr-par.scw.cloud/assistant-rh/legifrance-pipeline:latest
docker push rg.fr-par.scw.cloud/assistant-rh/legifrance-ingestion:latest
```

GitHub Actions workflows available:
- [build-legifrance-pipeline-image.yml](/Users/omar.gueddari/work/assistant-rh/.github/workflows/build-legifrance-pipeline-image.yml)
- [build-legifrance-ingestion-image.yml](/Users/omar.gueddari/work/assistant-rh/.github/workflows/build-legifrance-ingestion-image.yml)
- Orchestrator script:
  [deploy_legifrance_jobs.sh](/Users/omar.gueddari/work/assistant-rh/scripts/deploy_legifrance_jobs.sh)

Single command once Docker is running:

```bash
./scripts/deploy_legifrance_jobs.sh
```

Useful variants:

```bash
./scripts/deploy_legifrance_jobs.sh --skip-create
./scripts/deploy_legifrance_jobs.sh --skip-build
```


## Runtime Commands

Generation job:

```bash
python scripts/legifrance_medallion_pipeline.py \
  --article-config config/legifrance_articles.json \
  --target-env prod \
  --sync-object-storage \
  --no-embed
```

This does:
- read the article list from JSON
- fetch official DILA/PISTE article payloads
- build local bronze, silver and gold artifacts
- sync them to Scaleway Object Storage under `prod/...`
- avoid DB writes

Ingestion job:

```bash
python scripts/legifrance_ingestion_job.py \
  --article-config config/legifrance_articles.json \
  --dsn-env SCW_POSTGRES_DSN \
  --from-object-storage \
  --target-env prod
```

This does:
- download silver and gold from Object Storage
- upsert `rag_documents`
- upsert `rag_sections`
- upsert `rag_chunks_dgafp`
- upsert `rag_chunks_legifrance`


## Reference Config

- [legifrance_articles.json](/Users/omar.gueddari/work/assistant-rh/config/legifrance_articles.json)
- [legifrance_articles_smoke.json](/Users/omar.gueddari/work/assistant-rh/config/legifrance_articles_smoke.json)
- [scaleway_serverless_job_legifrance.json](/Users/omar.gueddari/work/assistant-rh/config/scaleway_serverless_job_legifrance.json)
- [scaleway_serverless_job_legifrance_ingestion.json](/Users/omar.gueddari/work/assistant-rh/config/scaleway_serverless_job_legifrance_ingestion.json)
- UI checklist:
  [SCALEWAY_SERVERLESS_JOBS_UI_CHECKLIST.md](/Users/omar.gueddari/work/assistant-rh/docs/SCALEWAY_SERVERLESS_JOBS_UI_CHECKLIST.md)


## Exact Creation Commands

Pipeline job helper:
- [create_scaleway_legifrance_job.sh](/Users/omar.gueddari/work/assistant-rh/scripts/create_scaleway_legifrance_job.sh)

Equivalent command:

```bash
scw jobs definition create \
  name=legifrance-monthly-prod \
  cpu-limit=2000 \
  memory-limit=4096 \
  local-storage-capacity=2048 \
  image-uri=rg.fr-par.scw.cloud/assistant-rh/legifrance-pipeline:latest \
  startup-command.0=python \
  args.0=scripts/legifrance_medallion_pipeline.py \
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
  cron-schedule.schedule="15 3 1 * *" \
  cron-schedule.timezone="Europe/Paris" \
  project-id="$SCW_DEFAULT_PROJECT_ID" \
  region=fr-par
```

Ingestion job helper:
- [create_scaleway_legifrance_ingestion_job.sh](/Users/omar.gueddari/work/assistant-rh/scripts/create_scaleway_legifrance_ingestion_job.sh)

Equivalent command:

```bash
scw jobs definition create \
  name=legifrance-ingestion-monthly-prod \
  cpu-limit=1000 \
  memory-limit=2048 \
  local-storage-capacity=2048 \
  image-uri=rg.fr-par.scw.cloud/assistant-rh/legifrance-ingestion:latest \
  startup-command.0=python \
  args.0=scripts/legifrance_ingestion_job.py \
  args.1=--dsn-env \
  args.2=SCW_POSTGRES_DSN \
  args.3=--from-object-storage \
  args.4=--target-env \
  args.5=prod \
  environment-variables.SCW_ACCESS_KEY="$SCW_ACCESS_KEY" \
  environment-variables.SCW_SECRET_KEY="$SCW_SECRET_KEY" \
  environment-variables.SCW_DEFAULT_REGION=fr-par \
  environment-variables.SCW_POSTGRES_DSN="$SCW_POSTGRES_DSN" \
  environment-variables.SCW_BUCKET_SILVER=assistant-rh-silver \
  environment-variables.SCW_BUCKET_GOLD=assistant-rh-gold \
  environment-variables.SCW_PREFIX_PROD=prod \
  environment-variables.SCW_PREFIX_STAGING=staging \
  job-timeout=7200s \
  cron-schedule.schedule="45 3 1 * *" \
  cron-schedule.timezone="Europe/Paris" \
  project-id="$SCW_DEFAULT_PROJECT_ID" \
  region=fr-par
```


## Required Environment Variables

At minimum for generation:

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

Additional variable for ingestion:

```env
SCW_POSTGRES_DSN=postgresql://...
```


## Schedule

Recommended cron:

```text
15 3 1 * *
45 3 1 * *
```

Meaning:
- generation at `03:15`
- ingestion at `03:45`
- on day `1` of each month


## Object Storage Layout

The monthly generation run writes to:

- `assistant-rh-bronze/prod/bronze/legifrance/...`
- `assistant-rh-silver/prod/silver/legifrance/...`
- `assistant-rh-gold/prod/gold/legifrance/...`


## Operational Guidance

- Keep generation and DB load separated.
- Keep `dgafp_compatible` as the default chunk strategy for migration parity.
- Use the smoke JSON before running the full monthly job.
- If the workload grows later, split comparison and validation into a separate job.

## Audit read-only : couverture d'embeddings Légifrance

Pour vérifier la couverture d'embeddings sans appeler d'API ni écrire en
base, utiliser le mode `--check-only` du job `embeddings-backfill` (issu
de `data-ingestion embeddings legifrance`) :

```bash
# Audit global Légifrance (read-only, exit 1 si sous le seuil)
uv run data-ingestion embeddings legifrance \
  --check-only \
  --coverage-min-pct 95

# Audit ciblé DGAFP / m3
uv run data-ingestion embeddings legifrance \
  --check-only \
  --only-table rag_chunks_dgafp \
  --only-column embedding_m3 \
  --coverage-min-pct 100
```

Ce mode n'importe pas `sentence_transformers`, ne crée pas de
`ScalewayBgeClient`, et n'exécute aucun `UPDATE`. Il peut être utilisé
depuis un workflow CI, un Cockpit check, ou un Scaleway
`workflow_dispatch` ad hoc (voir aussi
`docs/SCALEWAY_SERVERLESS_JOBS_SERVICE_PUBLIC.md` pour la version
Service-Public).

## Idempotence embeddings sur rerun `--no-embed`

`legifrance-ingestion` (et indirectement `legifrance-medallion --no-embed`)
utilise désormais un upsert qui préserve les embeddings existants :

- les colonnes `embedding_m3`, `embedding_bge_scw` et `embedding_qwen3`
  (legacy) ou `embedding_m3` / `embedding_bge_scw` (moderne) sont
  émises en `COALESCE(EXCLUDED.col, <table>.col)` ;
- un rerun `--no-embed` n'écrase donc plus un vecteur persisté avec
  `NULL`.

Le backfill Légifrance reste le job dédié
`embeddings-legifrance` (cf. `data-engineering-jobs.json`).

## Fail-fast extraction incomplète

`legifrance-bulk-dump` est désormais strict par défaut avec
`--article-ids-json` : si le manifest référence N LEGIARTI et que
l'extraction en trouve strictement moins, le job sort en erreur avec
un payload JSON `status=error reason=incomplete_article_extraction`.
Pour tolérer un manifest connu partiellement absent (migration
depuis un export de référence), passer `--allow-partial`.
