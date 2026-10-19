#!/usr/bin/env bash
set -euo pipefail
: "${IMAGE_DIGEST:?digest issu du job de publication requis}"
[[ "$IMAGE_DIGEST" =~ ^sha256:[a-f0-9]{64}$ ]] || exit 2
namespace=assistant-rh-api
expected=https://fce3ddcb-b9ff-49fb-8b7d-aca605272b0a.api.k8s.fr-par.scw.cloud:6443
server=$(kubectl config view --minify -o jsonpath='{.clusters[0].cluster.server}')
[[ "$server" == "$expected" ]] || { echo 'Le kubeconfig ne cible pas le cluster staging attendu.' >&2; exit 2; }
kubectl -n "$namespace" get secret assistant-rh-api-runtime scw-registry scaleway-database-ca -o name
# Le bootstrap du schéma et du corpus est une opération distincte du rollout.
kubectl -n "$namespace" get configmap assistant-rh-schema-ready -o name
helm upgrade --install assistant-rh-api deploy/api --namespace "$namespace"   --set-string "image.digest=$IMAGE_DIGEST" --atomic --wait --timeout 10m --history-max 10
kubectl -n "$namespace" rollout status deployment/assistant-rh-api --timeout=120s
kubectl -n "$namespace" exec deployment/assistant-rh-api --   python -c 'import json,urllib.request; d=json.load(urllib.request.urlopen("http://127.0.0.1:8000/healthz", timeout=10)); assert d["status"]=="ok" and d["db"]=="ok" and d["config_loaded"] is True; print("API health/config/db: OK")'
