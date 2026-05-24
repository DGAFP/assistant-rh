#!/usr/bin/env bash
set -euo pipefail

: "${SCW_DEFAULT_PROJECT_ID:?SCW_DEFAULT_PROJECT_ID is required}"
: "${SCW_ACCESS_KEY:?SCW_ACCESS_KEY is required}"
: "${SCW_SECRET_KEY:?SCW_SECRET_KEY is required}"
: "${SCW_POSTGRES_DSN:?SCW_POSTGRES_DSN is required}"

REGION="${SCW_DEFAULT_REGION:-fr-par}"
REGISTRY_NAMESPACE="${SCW_CONTAINER_REGISTRY_NAMESPACE:-assistant-rh}"
TARGET_ENV="${TARGET_ENV:-prod}"
IMAGE_TAG="${IMAGE_TAG:-${TARGET_ENV}-latest}"
JOB_NAME="${JOB_NAME:-service-public-ingestion-monthly-${TARGET_ENV}}"

scw jobs definition create \
  name="${JOB_NAME}" \
  cpu-limit=1000 \
  memory-limit=2048 \
  local-storage-capacity=2048 \
  image-uri="rg.${REGION}.scw.cloud/${REGISTRY_NAMESPACE}/service-public-ingestion:${IMAGE_TAG}" \
  startup-command.0=assistant-rh-data \
  args.0=service-public \
  args.1=ingest \
  args.2=--dsn-env \
  args.3=SCW_POSTGRES_DSN \
  args.4=--from-object-storage \
  args.5=--target-env \
  args.6="${TARGET_ENV}" \
  environment-variables.SCW_ACCESS_KEY="${SCW_ACCESS_KEY}" \
  environment-variables.SCW_SECRET_KEY="${SCW_SECRET_KEY}" \
  environment-variables.SCW_DEFAULT_REGION="${REGION}" \
  environment-variables.SCW_POSTGRES_DSN="${SCW_POSTGRES_DSN}" \
  environment-variables.SCW_BUCKET_GOLD="${SCW_BUCKET_GOLD:-assistant-rh-gold}" \
  environment-variables.SCW_PREFIX_PROD="${SCW_PREFIX_PROD:-prod}" \
  environment-variables.SCW_PREFIX_STAGING="${SCW_PREFIX_STAGING:-staging}" \
  job-timeout=7200s \
  cron-schedule.schedule="30 3 1 * *" \
  cron-schedule.timezone="Europe/Paris" \
  project-id="${SCW_DEFAULT_PROJECT_ID}" \
  region="${REGION}"
