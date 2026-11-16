#!/usr/bin/env bash
# Vérifie que tous les secrets et variables requis pour un déploiement
# Streamlit sont présents, et liste l'ensemble des manquants en une fois.
#
# Appelé par streamlit-deploy-staging.yml et streamlit-deploy-production.yml.
# La liste des exigences est partagée par les deux environnements : la garder
# ici évite qu'une variable ajoutée à l'un des deux déploiements soit oubliée
# dans l'autre.
#
# La liste est lue depuis l'environnement : chaque nom doit y être défini.
set -euo pipefail
missing=()
required=(
  SCW_ACCESS_KEY
  SCW_SECRET_KEY
  SCW_DEFAULT_PROJECT_ID
  SCW_DEFAULT_ORGANIZATION_ID
  SCW_CONTAINER_REGISTRY_NAMESPACE
  SCW_POSTGRES_DSN
  ALBERT_API_KEY
  SCALEWAY_API_KEY
  COOKIES_PASSWORD
  ADMIN_PASSWORD
  GRIST_API_BASE_URL
  GRIST_API_KEY
  GRIST_DOC_ID
  GRIST_TABLE_ID
)
for name in "${required[@]}"; do
  if [[ -z "${!name:-}" ]]; then
    missing+=("$name")
  fi
done

if (( ${#missing[@]} > 0 )); then
  echo "Missing required secret/variable(s): ${missing[*]}" >&2
  exit 1
fi
