#!/usr/bin/env bash
set -euo pipefail

: "${SCW_DEFAULT_PROJECT_ID:?SCW_DEFAULT_PROJECT_ID is required}"
: "${SCW_ACCESS_KEY:?SCW_ACCESS_KEY is required}"
: "${SCW_SECRET_KEY:?SCW_SECRET_KEY is required}"

REGION="${SCW_DEFAULT_REGION:-fr-par}"
REGISTRY_NAMESPACE="${SCW_CONTAINER_REGISTRY_NAMESPACE:-assistant-rh}"
TARGET_ENV="${TARGET_ENV:-prod}"
IMAGE_TAG="${IMAGE_TAG:-${TARGET_ENV}-latest}"
JOB_NAME="${JOB_NAME:-legifrance-medallion-monthly-${TARGET_ENV}}"
LOCAL_STORAGE_CAPACITY="${LOCAL_STORAGE_CAPACITY:-8192}"
CPU_LIMIT="${CPU_LIMIT:-4000}"
MEMORY_LIMIT="${MEMORY_LIMIT:-8192}"
JOB_TIMEOUT="${JOB_TIMEOUT:-14400s}"
PIPELINE_BATCH_SIZE="${PIPELINE_BATCH_SIZE:-50}"

scw jobs definition create \
  name="${JOB_NAME}" \
  cpu-limit="${CPU_LIMIT}" \
  memory-limit="${MEMORY_LIMIT}" \
  local-storage-capacity="${LOCAL_STORAGE_CAPACITY}" \
  image-uri="rg.${REGION}.scw.cloud/${REGISTRY_NAMESPACE}/legifrance-pipeline:${IMAGE_TAG}" \
  startup-command.0=data-ingestion \
  args.0=legifrance \
  args.1=medallion \
  args.2=--target-env \
  args.3="${TARGET_ENV}" \
  args.4=--from-object-storage \
  args.5=--sync-object-storage \
  args.6=--delete-remote \
  args.7=--no-embed \
  args.8=--batch-size \
  args.9="${PIPELINE_BATCH_SIZE}" \
  environment-variables.SCW_ACCESS_KEY="${SCW_ACCESS_KEY}" \
  environment-variables.SCW_SECRET_KEY="${SCW_SECRET_KEY}" \
  environment-variables.SCW_DEFAULT_REGION="${REGION}" \
  environment-variables.SCW_BUCKET_BRONZE="${SCW_BUCKET_BRONZE:-assistant-rh-bronze}" \
  environment-variables.SCW_BUCKET_SILVER="${SCW_BUCKET_SILVER:-assistant-rh-silver}" \
  environment-variables.SCW_BUCKET_GOLD="${SCW_BUCKET_GOLD:-assistant-rh-gold}" \
  environment-variables.SCW_PREFIX_PROD="${SCW_PREFIX_PROD:-prod}" \
  environment-variables.SCW_PREFIX_STAGING="${SCW_PREFIX_STAGING:-staging}" \
  job-timeout="${JOB_TIMEOUT}" \
  cron-schedule.schedule="15 3 1 * *" \
  cron-schedule.timezone="Europe/Paris" \
  project-id="${SCW_DEFAULT_PROJECT_ID}" \
  region="${REGION}"
