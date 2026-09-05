#!/usr/bin/env bash
set -euo pipefail

if (( $# != 1 )); then
  echo "Usage: $0 <release-sha>" >&2
  exit 2
fi

RELEASE_SHA="$1"
if [[ ! "${RELEASE_SHA}" =~ ^[0-9a-f]{40}$ ]]; then
  echo "Invalid release SHA: ${RELEASE_SHA}" >&2
  exit 1
fi

if [[ "$(git rev-parse HEAD)" != "${RELEASE_SHA}" ]]; then
  echo "Checked-out commit does not match release SHA ${RELEASE_SHA}." >&2
  exit 1
fi

ALL_TAGS="$(git tag --points-at "${RELEASE_SHA}")"
RELEASE_TAGS="$(printf '%s\n' "${ALL_TAGS}" | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' || true)"
if [[ -z "${RELEASE_TAGS}" ]]; then
  echo "No semantic release tag points at ${RELEASE_SHA}." >&2
  exit 1
fi
if [[ "${RELEASE_TAGS}" == *$'\n'* ]]; then
  echo "Multiple semantic release tags point at ${RELEASE_SHA}." >&2
  exit 1
fi

RELEASE_TAG="${RELEASE_TAGS}"
PUBLISHED_TAG="$(
  gh api "repos/${GITHUB_REPOSITORY}/releases/tags/${RELEASE_TAG}" \
    --jq 'select(.draft == false and .prerelease == false) | .tag_name'
)"
if [[ "${PUBLISHED_TAG}" != "${RELEASE_TAG}" ]]; then
  echo "Release ${RELEASE_TAG} is missing, draft, or a prerelease." >&2
  exit 1
fi

printf '%s\n' "${RELEASE_TAG}"
