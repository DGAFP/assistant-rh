# Scaleway Pipeline Runbook

## Target Operating Mode

For the Service-Public pipeline, use a monthly batch job, not a permanent service.

Current migration principle:
- compatibility first
- industrial improvements later
- migration reference:
  [MIGRATION_COMPATIBILITY_FIRST.md](/Users/omar.gueddari/work/assistant-rh/docs/MIGRATION_COMPATIBILITY_FIRST.md)
- later improvements reference:
  [SERVICE_PUBLIC_INDUSTRIAL_IMPROVEMENTS_LATER.md](/Users/omar.gueddari/work/assistant-rh/docs/SERVICE_PUBLIC_INDUSTRIAL_IMPROVEMENTS_LATER.md)

Recommended cadence:
- `monthly`

Reasoning:
- the source is snapshot-based
- updates do not justify a continuously running service
- monthly execution keeps operations simple under the current delivery constraints


## Execution Model

Run one containerized batch job that:
1. fetches the official XML snapshot
2. writes local medallion outputs
3. syncs bronze/silver/gold to Scaleway Object Storage
4. optionally runs comparison against DB
5. optionally loads DB only in a separate controlled step


## Recommended Monthly Flow

Monthly production run:
- generate bronze/silver/gold
- sync to `prod/...`
- run comparison and validation
- keep DB load as a separate explicit step

Example:

```bash
python3 scripts/service_public_medallion_pipeline.py \
  --target-env prod \
  --sync-object-storage \
  --no-embed
```

By default, the pipeline reads the fiche list from:
- [service_public_fiches.json](/Users/omar.gueddari/work/assistant-rh/config/service_public_fiches.json)

Migration-only mode:
- `--batch-from-db` remains available only to bootstrap or compare against the current DB content


## Storage Layout

In the three buckets created:

- `assistant-rh-bronze/prod/bronze/service_public/...`
- `assistant-rh-silver/prod/silver/service_public/...`
- `assistant-rh-gold/prod/gold/service_public/...`

This keeps:
- environment explicit
- medallion layer explicit
- source explicit


## Separation of Concerns

Keep these concerns separate:

1. Lake generation
- local medallion build + object storage sync

2. Validation
- compare generated output vs current DB or expected outputs

3. DB load
- explicit step, not automatic by default

This reduces the risk of pushing a bad monthly snapshot directly into production tables.


## Minimal Env Vars

```env
SCW_DEFAULT_REGION=fr-par
SCW_BUCKET_BRONZE=assistant-rh-bronze
SCW_BUCKET_SILVER=assistant-rh-silver
SCW_BUCKET_GOLD=assistant-rh-gold
SCW_PREFIX_PROD=prod
SCW_PREFIX_STAGING=staging
```


## Next Infrastructure Step

When you are ready to productionize further:
- package the pipeline as a Docker image
- schedule one monthly batch job on Scaleway
- store logs and manifests per run
- keep DB load as a separate manual or scheduled downstream job
- add staging later only if operational pressure justifies it

Current operational choice:
- create or update the monthly Serverless Job directly via CLI
- do not add job-listing logic before creation
- use the creation script:
  [create_scaleway_service_public_job.sh](/Users/omar.gueddari/work/assistant-rh/scripts/create_scaleway_service_public_job.sh)
- keep the UI checklist only as fallback:
  [SCALEWAY_SERVERLESS_JOBS_UI_CHECKLIST.md](/Users/omar.gueddari/work/assistant-rh/docs/SCALEWAY_SERVERLESS_JOBS_UI_CHECKLIST.md)
