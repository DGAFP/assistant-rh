# Environnements du chantier

> Référence : [00-overview.md](00-overview.md) (décision D10). Aucune infra cloud persistante pendant le chantier.

## 1. Dev quotidien — full local

Extension de l'environnement local existant (`docs/LOCAL_DEV.md`) :

- **DB** : Postgres pgvector docker `:55432`, seedé depuis staging (existant).
- **API** : service `api` ajouté au compose (uvicorn, hot-reload), branché sur la DB locale.
- **LLM** : Albert par clé API quand la fidélité compte ; `deepseek-v4-flash` local pour les boucles rapides (existant).
- Usage : TDD des handlers, tests d'adaptateurs, développement des PRs des phases A–D.

```bash
docker compose up api
```

## 2. Parité & intégration — VM homelab

- `ssh dev@assistant-rh` (repo + `.env` en place ; la VM atteint les ports DB Scaleway que le réseau pro bloque — c'est déjà le runner des runs longs).
- **API en docker compose sur la VM, branchée directement sur la DB staging** : mêmes données que les baselines d'éval.
- Toutes les écritures `chat_runs` issues de la VM portent **`source=api-vm`** (même mécanique de tags que le flux Suivi-Tests) pour ne pas polluer les stats staging.
- Usage : jalon **M2** (fidélité adaptateur), jalon **M3** (parité finale), branchement du fork `conversations` en conditions réelles (temps 2, plus tard).

## 3. Validation containerisation — smoke Scaleway (unique, pré-merge)

- Workflow `workflow_dispatch` (déclenchement manuel uniquement), phase **E1** : build `Dockerfile.api`, déploiement d'un container serverless éphémère, tests de smoke, extinction.
- Ce qu'on y vérifie précisément : cold start, empreinte mémoire, et surtout **le comportement SSE sur serverless** (timeouts d'idle pendant la phase de retrieval, buffering éventuel du proxy, durée max de connexion).
- Pas de container permanent avant le merge.

## 4. Cible post-merge (pour mémoire)

- **2 containers serverless Scaleway** : `api` (public, min-scale=1 — pas de cold start pour les consommateurs) et `streamlit` (interne, scale-to-zero).
- Workflows de déploiement branchés sur le flux habituel `dev` → promotion `staging` → prod.
- Secrets : DSN par environnement, clés Albert, `ADMIN_TOKEN` (env), tokens de groupe distribués via `/admin/user-groups/{slug}/rotate-token`.

## Matrice récapitulative

| Environnement | Quand | DB | Écritures taguées | Coût |
|---|---|---|---|---|
| Local compose | Tous les jours | pgvector local seedé | — | 0 |
| VM homelab | Jalons M2/M3, intégration front | **staging** (directe) | `source=api-vm` | 0 |
| Smoke Scaleway | Une fois, phase E1 | staging | `source=api-smoke` | ~0 (éphémère) |
| Prod Scaleway | Post-merge | prod | — | min-scale=1 |
