# Scaleway Serverless Job UI Checklist

Use this checklist only as a fallback when CLI creation is blocked by IAM permissions.

## Policy

Operational choice:
- create the job directly
- prefer CLI over UI
- do not add a preliminary `list jobs` step in automation
- if the job already exists, update it in the UI instead of adding detection logic first


## Target Job

- name: `service-public-monthly-prod`
- region: `fr-par`
- image: `rg.fr-par.scw.cloud/assistant-rh/service-public-pipeline:latest`


## Resources

- CPU: `2000 mvCPU`
- memory: `4096 MiB`
- local storage: `2048 MiB`
- timeout: `7200 s`


## Command

Startup command:

```text
python
```

Arguments:

```text
scripts/service_public_medallion_pipeline.py
--target-env
prod
--sync-object-storage
--no-embed
```


## Schedule

- cron: `0 3 1 * *`
- timezone: `Europe/Paris`


## Environment Variables

- `SCW_ACCESS_KEY`
- `SCW_SECRET_KEY`
- `SCW_DEFAULT_REGION=fr-par`
- `SCW_BUCKET_BRONZE=assistant-rh-bronze`
- `SCW_BUCKET_SILVER=assistant-rh-silver`
- `SCW_BUCKET_GOLD=assistant-rh-gold`
- `SCW_PREFIX_PROD=prod`
- `SCW_PREFIX_STAGING=staging`


## Config Source

The job reads fiche IDs from:
- [service_public_fiches.json](../../config/service_public_fiches.json)

The job does not need DB access for the monthly production run.


## After Creation

- run the job once manually
- verify bronze, silver and gold objects are written under:
  - `assistant-rh-bronze/prod/bronze/service_public/...`
  - `assistant-rh-silver/prod/silver/service_public/...`
  - `assistant-rh-gold/prod/gold/service_public/...`
- check the job logs
- keep DB comparison or DB load as separate steps
