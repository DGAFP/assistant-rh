# Environnement local — base Postgres + evals

Environnement d'expérimentation entièrement local : un Postgres/pgvector en
Docker, seedé depuis un dump de staging (corpus + goldset + config runtime),
sur lequel on peut lancer des evals RAG sans toucher aux bases partagées.

## 1. Démarrer la base locale

```bash
docker compose -f docker-compose.local.yml up -d
```

- Image `pgvector/pgvector:pg17` (staging est en PG 17), port hôte **55432**
  (évite les ports Supabase CLI 54321/54322), volume persistant
  `assistant_rh_local_pgdata`.
- DSN local (aussi dans `.env` sous `SCW_POSTGRES_DSN_LOCAL`) :

```
postgresql://assistant_rh:assistant_rh@localhost:55432/assistant_rh?sslmode=disable
```

⚠️ Le `?sslmode=disable` est **obligatoire** : `create_engine_from_env()`
(shared-config) ajoute `sslmode=require` si la clause est absente, et le
conteneur local n'a pas de TLS.

## 2. Seed depuis staging

Le schéma complet ne vit pas dans `supabase/migrations/` (les tables corpus
historiques prédatent les migrations). Le seed utilise donc une **liste
blanche** de tables nécessaires aux evals, jamais un dump général de staging.
Les conversations, feedbacks, identifiants de session et données
d'authentification ne doivent pas quitter l'environnement partagé.

```bash
DSN_STAGING=$(grep '^SCW_POSTGRES_DSN_STAGING' .env | cut -d= -f2- | tr -d '"')
LOCAL_EVAL_DSN="${SCW_POSTGRES_DSN_LOCAL:-postgresql://assistant_rh:assistant_rh@localhost:55432/assistant_rh?sslmode=disable}"
EVAL_DUMP_DATE=$(date +%Y%m%d)
mkdir -p data/dumps

SAFE_EVAL_TABLES=(
  rag_chunks_matte rag_chunks_service_public rag_chunks_dgafp rag_chunks_rgrh
  rag_chunks_mso rag_chunks_mi rag_chunks_masa
  rag_documents rag_sections goldset_questions_v2
  rag_config system_prompts acronyms rag_quality_eval_runs
)
PG_DUMP_TABLE_ARGS=()
for table in "${SAFE_EVAL_TABLES[@]}"; do
  PG_DUMP_TABLE_ARGS+=(--table="public.${table}")
done

# Le schéma du goldset est inclus dans le dump, mais ses lignes sont exportées
# séparément afin d'écarter les questions auto-enrichies depuis des sessions.
pg_dump "$DSN_STAGING" -Fc --strict-names --no-owner --no-privileges \
  "${PG_DUMP_TABLE_ARGS[@]}" \
  --exclude-table-data='public.goldset_questions_v2' \
  -f "data/dumps/staging_eval_${EVAL_DUMP_DATE}.dump"
psql "$DSN_STAGING" -v ON_ERROR_STOP=1 -c \
  "\copy (SELECT * FROM public.goldset_questions_v2 WHERE source IS DISTINCT FROM 'user' AND original_turn_id IS NULL) TO 'data/dumps/goldset_${EVAL_DUMP_DATE}.csv' WITH (FORMAT csv, HEADER true)"

psql "$LOCAL_EVAL_DSN" -v ON_ERROR_STOP=1 -c 'CREATE EXTENSION IF NOT EXISTS vector'
pg_restore -d "$LOCAL_EVAL_DSN" \
  --no-owner --no-privileges --clean --if-exists \
  "data/dumps/staging_eval_${EVAL_DUMP_DATE}.dump"
psql "$LOCAL_EVAL_DSN" -v ON_ERROR_STOP=1 -c \
  "\copy public.goldset_questions_v2 FROM 'data/dumps/goldset_${EVAL_DUMP_DATE}.csv' WITH (FORMAT csv, HEADER true)"
```

Ce qui est copié : les corpus `rag_chunks_*` actifs (matte, mso, mi, masa,
service_public, dgafp, rgrh), `rag_documents`, `rag_sections`, le goldset hors
lignes issues des utilisateurs ou liées à leurs sessions, `rag_config`, `system_prompts`,
`acronyms` et `rag_quality_eval_runs` pour les baselines. Aucune table
`chat_*`, trace, feedback ou authentification (`user_groups`) n'est incluse.
Toute extension de `SAFE_EVAL_TABLES` doit faire l'objet d'une revue des
données potentiellement personnelles avant ajout.

Réseau : si les ports DB Scaleway sont bloqués (réseau pro), passer par le
pont `ssh dev@assistant-rh` (dump sur la VM puis `scp`).

⚠️ **Piège : dump pendant une ingestion.** Le snapshot `pg_dump` capture l'état
instantané — si un backfill d'embeddings tourne sur staging au même moment, la
table arrive localement avec des `embedding_m3` NULL (vécu le 20/08 : SP dumpée
en plein re-embedding → 256/4991 embeddings, hit_rate local effondré et eval
faussée). **Toujours valider le seed** avant de lancer une eval :

```bash
for t in rag_chunks_matte rag_chunks_service_public rag_chunks_dgafp \
         rag_chunks_mso rag_chunks_mi rag_chunks_masa rag_chunks_rgrh; do
  echo "$t local=$(psql "$SCW_POSTGRES_DSN_LOCAL" -tAc "SELECT count(*)||'/'||count(embedding_m3) FROM $t") \
   staging=$(psql "$DSN_STAGING" -tAc "SELECT count(*)||'/'||count(embedding_m3) FROM $t")"
done
```

Resynchroniser une seule table sans tout redumper :

```bash
pg_dump "$DSN_STAGING" -Fc --data-only -t public.<table> -f data/dumps/resync.dump
psql "$SCW_POSTGRES_DSN_LOCAL" -c "TRUNCATE <table>"
pg_restore -d "$SCW_POSTGRES_DSN_LOCAL" --data-only --no-owner data/dumps/resync.dump
```

## 3. Modèle générateur local (ex. deepseek-v4-flash)

La `rag_config` locale est une copie de staging (`v3_generator_model =
openweight-large`). En local, la base n'est pas partagée : on peut la muter
librement pour expérimenter.

```bash
# Modèles disponibles côté Albert :
curl -s https://albert.api.etalab.gouv.fr/v1/models -H "Authorization: Bearer $ALBERT_API_KEY"

psql "$SCW_POSTGRES_DSN_LOCAL" -c \
  "UPDATE rag_config SET config = jsonb_set(config, '{v3_generator_model}', '\"deepseek-v4-flash\"')"
```

Le générateur passe par le provider **albert** (`ALBERT_API_KEY` +
`ALBERT_BASE_URL` du `.env`), donc tout modèle listé par l'API Albert est
utilisable tel quel. Fallback : Scaleway (`SCALEWAY_API_KEY`).

## 4. Lancer une eval locale

Même script que staging (`scripts/run_rag_quality_eval.py`), mais pointé sur
la base locale via `--dsn` (propagé au pipeline via `SCW_POSTGRES_DSN`) :

```bash
set -a; source .env 2>/dev/null; set +a
uv run --no-sync python scripts/run_rag_quality_eval.py \
  --goldset-name baseline_v1 --any-goldset --tag baseline_v1 \
  --dsn "$SCW_POSTGRES_DSN_LOCAL" \
  --record-db --dedupe-scope config \
  --ministry-scope per-question \
  --run-label "local_<experience>_<date>" \
  --limit 5 --skip-ragas \
  --output-dir data/eval-local/<label>
```

- Smoke rapide : `--limit 2 --skip-ragas --skip-judge`.
- Les runs `--record-db` s'écrivent dans `rag_quality_eval_runs` **locale** —
  aucune pollution du suivi staging, mais du coup pas de baseline croisée :
  comparer localement des runs locaux entre eux.
- Le juge et RAGAS restent des appels Scaleway (réseau requis).
- La règle du journal (`docs/evals/journal-experimentations-rag.md`) vaut pour
  les runs qui informent une décision d'équipe ; les tâtonnements locaux
  peuvent rester hors journal, à condition de re-valider sur staging avant
  toute conclusion.

## Rafraîchir le seed

Rejouer le § 2 (le `--clean --if-exists` remplace les objets existants), ou
`docker compose -f docker-compose.local.yml down -v` pour repartir de zéro.
