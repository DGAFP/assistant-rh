#!/usr/bin/env bash
set -euo pipefail

: "${SCW_DEFAULT_PROJECT_ID:?SCW_DEFAULT_PROJECT_ID is required}"
: "${SCW_ACCESS_KEY:?SCW_ACCESS_KEY is required}"
: "${SCW_SECRET_KEY:?SCW_SECRET_KEY is required}"
: "${SCALEWAY_API_KEY:?SCALEWAY_API_KEY is required}"
: "${SCW_POSTGRES_DSN:?SCW_POSTGRES_DSN is required}"

REGION="${SCW_DEFAULT_REGION:-fr-par}"
REGISTRY_NAMESPACE="${SCW_CONTAINER_REGISTRY_NAMESPACE:-assistant-rh}"
TARGET_ENV="${TARGET_ENV:-prod}"
IMAGE_TAG="${IMAGE_TAG:-${TARGET_ENV}-latest}"
JOB_NAME="${JOB_NAME:-matte-embeddings-${TARGET_ENV}}"
CPU_LIMIT="${CPU_LIMIT:-4000}"
MEMORY_LIMIT="${MEMORY_LIMIT:-8192}"
LOCAL_STORAGE_CAPACITY="${LOCAL_STORAGE_CAPACITY:-8192}"
JOB_TIMEOUT="${JOB_TIMEOUT:-14400s}"
M3_BATCH_SIZE="${M3_BATCH_SIZE:-64}"
BGE_BATCH_SIZE="${BGE_BATCH_SIZE:-32}"
BGE_WORKERS="${BGE_WORKERS:-2}"
SCALEWAY_BASE_URL_VALUE="${SCALEWAY_BASE_URL:-https://api.scaleway.ai/11aa88cb-ec5b-4df9-bcb4-e9e82576ae58/v1}"

scw jobs definition create \
  name="${JOB_NAME}" \
  cpu-limit="${CPU_LIMIT}" \
  memory-limit="${MEMORY_LIMIT}" \
  local-storage-capacity="${LOCAL_STORAGE_CAPACITY}" \
  image-uri="rg.${REGION}.scw.cloud/${REGISTRY_NAMESPACE}/embeddings-job:${IMAGE_TAG}" \
  startup-command.0=data-ingestion \
  args.0=embeddings \
  args.1=matte \
  args.2=--dsn-env \
  args.3=SCW_POSTGRES_DSN \
  args.4=--m3-device \
  args.5=cpu \
  args.6=--m3-batch-size \
  args.7="${M3_BATCH_SIZE}" \
  args.8=--bge-batch-size \
  args.9="${BGE_BATCH_SIZE}" \
  args.10=--bge-workers \
  args.11="${BGE_WORKERS}" \
  environment-variables.SCALEWAY_API_KEY="${SCALEWAY_API_KEY}" \
  environment-variables.SCALEWAY_BASE_URL="${SCALEWAY_BASE_URL_VALUE}" \
  environment-variables.SCW_POSTGRES_DSN="${SCW_POSTGRES_DSN}" \
  job-timeout="${JOB_TIMEOUT}" \
  cron-schedule.schedule="40 3 1 * *" \
  cron-schedule.timezone="Europe/Paris" \
  project-id="${SCW_DEFAULT_PROJECT_ID}" \
  region="${REGION}"
