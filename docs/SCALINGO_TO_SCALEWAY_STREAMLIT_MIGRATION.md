# Migration Scalingo -> Scaleway pour la Streamlit

Date d'inventaire utilisee pour ce plan: `27 avril 2026`.

## Objectif

Rendre la base Scaleway exploitable par l'application Streamlit sans recopier aveuglement toute la base Scalingo.

Le principe retenu est:

- **ne pas migrer les tables `_scw`**
- **ne pas ecraser par defaut les tables Scaleway deja plus riches que Scalingo**
- **si une table homonyme existe deja sur Scaleway, ecrire la copie dans `<table>_scalingo`**
- **migrer d'abord le socle vraiment necessaire a la Streamlit**

Exception actuelle:

- `rag_documents` reste migree en place vers `rag_documents` avec upsert

## Constat reel au `27 avril 2026`

Inventaire compare depuis les deux Postgres:

- Scalingo: `68` tables publiques
- Scaleway: `17` tables publiques

Tables deja presentes et peuplees sur Scaleway:

- `rag_chunks_service_public`: `1526` lignes
- `rag_chunks_dgafp`: `3992` lignes
- `rag_chunks_legifrance`: `429` lignes
- `rag_documents`: `4014` lignes
- `rag_sections`: `4029` lignes
- `rag_config`: `1` ligne

Tables presentes mais vides/incompletes sur Scaleway, utiles a la Streamlit:

- `acronyms`
- `acronyms_missing`
- `system_prompts`
- `chat_runs`
- `chat_feedbacks`
- `chat_reviews`
- `documents`
- `rag_chunks_matte`
- `rag_chunks_rgrh`

Tables absentes sur Scaleway mais utiles aux pages d'evaluation:

- `goldset_questions_v2`
- `goldset_runs`
- `intent_eval_experiments`
- `intent_eval_goldset`
- `pipeline_eval_experiments`
- `rag_chunk_embeddings`
- `rag_chunks_test`
- `retrieval_eval_runs`

## Profils de migration

Le script ajoute des profils cibles:

- `streamlit-core`
  - pour faire tourner le chatbot, l'admin, les logs, le PDF viewer, MATTE et RGRH
- `streamlit-eval`
  - pour les pages goldset / evaluation / ablation
- `streamlit-full`
  - union de `streamlit-core` + `streamlit-eval`
- `all-non-scw`
  - toutes les tables non `_scw` que l'on a retenues comme migrables sans dump global

## Script

Fichier:

- [copy_scalingo_tables_to_scaleway.py](/Users/omar.gueddari/work/assistant-rh/scripts/copy_scalingo_tables_to_scaleway.py)

Ce que fait le script:

- lit la source Scalingo via le tunnel local `PGHOST/PGPORT/PGUSER/PGPASSWORD/PGDATABASE`
- lit la cible via `SCW_POSTGRES_DSN`
- cree les tables manquantes
- cree une table shadow suffixee `_scalingo` quand le meme nom existe deja sur Scaleway
- ajoute les colonnes absentes sur la table cible quand le schema est incomplet
- recree les index non-PK par defaut
- upsert les lignes par cle primaire
- resynchronise les sequences `SERIAL`

Exception:

- `rag_documents` n'utilise pas le suffixe `_scalingo` et est upsertee directement dans `rag_documents`

## Execution locale

Plan a blanc:

```bash
python3 scripts/copy_scalingo_tables_to_scaleway.py --profile streamlit-core --plan-only
```

Migration du socle Streamlit:

```bash
python3 scripts/copy_scalingo_tables_to_scaleway.py --profile streamlit-core
```

Migration complete Streamlit + eval:

```bash
python3 scripts/copy_scalingo_tables_to_scaleway.py --profile streamlit-full
```

Si tu veux un miroir strict des tables selectionnees:

```bash
python3 scripts/copy_scalingo_tables_to_scaleway.py --profile streamlit-full --truncate-first
```

## Execution Docker

Dockerfile dedie:

- [Dockerfile.scalingo_to_scaleway_migration](/Users/omar.gueddari/work/assistant-rh/Dockerfile.scalingo_to_scaleway_migration)

Build:

```bash
docker build -f Dockerfile.scalingo_to_scaleway_migration -t assistant-rh/scalingo-to-scaleway .
```

Run local:

```bash
docker run --rm --env-file .env assistant-rh/scalingo-to-scaleway --profile streamlit-core
```

## Bascule Streamlit vers Scaleway

La resolution de DSN accepte maintenant en priorite:

1. `APP_POSTGRES_DSN`
2. `STREAMLIT_POSTGRES_DSN`
3. `SCALINGO_POSTGRESQL_URL`
4. `PG_DSN`
5. `DATABASE_URL`
6. `SCW_POSTGRES_DSN`

La bascule la plus propre est donc:

```bash
export APP_POSTGRES_DSN="$SCW_POSTGRES_DSN"
streamlit run apps/streamlit-ui/Home.py
```

Attention:

- les tables migrees dans des suffixes `_scalingo` sont des **snapshots de comparaison**
- la Streamlit actuelle ne les lira pas automatiquement tant qu'on ne remappe pas explicitement les requetes ou qu'on ne cree pas de vues SQL

## Recommandation pragmatique

Pour limiter le risque:

1. lancer `streamlit-core`
2. verifier le chatbot, l'admin, DB Explorer et PDF Viewer sur Scaleway
3. seulement ensuite lancer `streamlit-full` si tu veux aussi les pages d'evaluation historiques

Je ne recommande pas d'ecraser par defaut:

- `rag_chunks_service_public`
- `rag_chunks_dgafp`
- `rag_chunks_legifrance`
- `rag_documents`
- `rag_sections`

Sur l'inventaire du `27 avril 2026`, ces tables sont deja alimentees sur Scaleway et certaines sont plus riches que la version Scalingo.
