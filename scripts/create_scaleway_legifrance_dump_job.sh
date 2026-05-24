#!/usr/bin/env bash
set -euo pipefail

: "${SCW_DEFAULT_PROJECT_ID:?SCW_DEFAULT_PROJECT_ID is required}"
: "${SCW_ACCESS_KEY:?SCW_ACCESS_KEY is required}"
: "${SCW_SECRET_KEY:?SCW_SECRET_KEY is required}"

REGION="${SCW_DEFAULT_REGION:-fr-par}"
REGISTRY_NAMESPACE="${SCW_CONTAINER_REGISTRY_NAMESPACE:-assistant-rh}"
TARGET_ENV="${TARGET_ENV:-prod}"
IMAGE_TAG="${IMAGE_TAG:-${TARGET_ENV}-latest}"
JOB_NAME="${JOB_NAME:-legifrance-bulk-dump-monthly-${TARGET_ENV}}"
LOCAL_STORAGE_CAPACITY="${LOCAL_STORAGE_CAPACITY:-8192}"

scw jobs definition create \
  name="${JOB_NAME}" \
  cpu-limit=2000 \
  memory-limit=4096 \
  local-storage-capacity="${LOCAL_STORAGE_CAPACITY}" \
  image-uri="rg.${REGION}.scw.cloud/${REGISTRY_NAMESPACE}/legifrance-bulk-dump:${IMAGE_TAG}" \
  startup-command.0=assistant-rh-data \
  args.0=legifrance \
  args.1=bulk-dump \
  args.2=--target-env \
  args.3="${TARGET_ENV}" \
  args.4=--article-ids-json \
  args.5=config/legifrance_article_cids.json \
  args.6=--sync-object-storage \
  args.7=--delete-remote \
  args.8=--delete-local-archive \
  environment-variables.SCW_ACCESS_KEY="${SCW_ACCESS_KEY}" \
  environment-variables.SCW_SECRET_KEY="${SCW_SECRET_KEY}" \
  environment-variables.SCW_DEFAULT_REGION="${REGION}" \
  environment-variables.SCW_BUCKET_BRONZE="${SCW_BUCKET_BRONZE:-assistant-rh-bronze}" \
  environment-variables.SCW_BUCKET_SILVER="${SCW_BUCKET_SILVER:-assistant-rh-silver}" \
  environment-variables.SCW_BUCKET_GOLD="${SCW_BUCKET_GOLD:-assistant-rh-gold}" \
  environment-variables.SCW_PREFIX_PROD="${SCW_PREFIX_PROD:-prod}" \
  environment-variables.SCW_PREFIX_STAGING="${SCW_PREFIX_STAGING:-staging}" \
  job-timeout=14400s \
  cron-schedule.schedule="0 2 1 * *" \
  cron-schedule.timezone="Europe/Paris" \
  project-id="${SCW_DEFAULT_PROJECT_ID}" \
  region="${REGION}"
