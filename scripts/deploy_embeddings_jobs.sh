#!/usr/bin/env bash
set -euo pipefail

REGION="${SCW_DEFAULT_REGION:-fr-par}"
REGISTRY_NAMESPACE="${SCW_CONTAINER_REGISTRY_NAMESPACE:-assistant-rh}"
TARGET_ENV="${TARGET_ENV:-prod}"
IMAGE_TAG="${IMAGE_TAG:-${TARGET_ENV}-latest}"
REGISTRY_HOST="rg.${REGION}.scw.cloud"
TARGET_PLATFORM="${TARGET_PLATFORM:-linux/amd64}"

BUILD=1
PUSH=1
CREATE=1

while (($#)); do
  case "$1" in
    --skip-build)
      BUILD=0
      ;;
    --skip-push)
      PUSH=0
      ;;
    --skip-create)
      CREATE=0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 1
      ;;
  esac
  shift
done

EMBEDDINGS_REMOTE_TAG="${REGISTRY_HOST}/${REGISTRY_NAMESPACE}/embeddings-job:${IMAGE_TAG}"

if (( BUILD )); then
  docker buildx build --platform "${TARGET_PLATFORM}" --load -f Dockerfile.embeddings_job -t "embeddings-job:${IMAGE_TAG}" .
fi

if (( PUSH )); then
  : "${SCW_SECRET_KEY:?SCW_SECRET_KEY is required}"
  echo "${SCW_SECRET_KEY}" | docker login "${REGISTRY_HOST}/${REGISTRY_NAMESPACE}" -u nologin --password-stdin
  docker tag "embeddings-job:${IMAGE_TAG}" "${EMBEDDINGS_REMOTE_TAG}"
  docker push "${EMBEDDINGS_REMOTE_TAG}"
fi

if (( CREATE )); then
  : "${SCW_DEFAULT_PROJECT_ID:?SCW_DEFAULT_PROJECT_ID is required}"
  : "${SCW_ACCESS_KEY:?SCW_ACCESS_KEY is required}"
  : "${SCW_SECRET_KEY:?SCW_SECRET_KEY is required}"
  : "${SCALEWAY_API_KEY:?SCALEWAY_API_KEY is required}"
  : "${SCW_POSTGRES_DSN:?SCW_POSTGRES_DSN is required}"
  TARGET_ENV="${TARGET_ENV}" IMAGE_TAG="${IMAGE_TAG}" ./scripts/create_scaleway_service_public_embeddings_job.sh
  TARGET_ENV="${TARGET_ENV}" IMAGE_TAG="${IMAGE_TAG}" ./scripts/create_scaleway_legifrance_embeddings_job.sh
  TARGET_ENV="${TARGET_ENV}" IMAGE_TAG="${IMAGE_TAG}" ./scripts/create_scaleway_matte_embeddings_job.sh
fi
