# Environnement local — base Postgres + evals

Environnement d'expérimentation entièrement local : un Postgres/pgvector en
Docker, seedé depuis un dump de staging (corpus + goldset + config runtime),
sur lequel on peut lancer des evals RAG sans toucher aux bases partagées.

## Conversations et API — `make dev`

Cette stack de développement est distincte des bases d’eval et des tests API
synthétiques décrites plus bas. Prérequis : Linux/macOS, Docker local avec
Compose **2.30+**, Python **3.10+** et Git. Les dépendances Python et Node des
applications sont installées dans leurs images Docker ; la clé Ed25519 est générée
avec l’OpenSSL de l’image PostgreSQL (le LibreSSL de macOS ne la gère pas).

Renseigner dans le `.env` du worktree, sans écraser un fichier existant :

```dotenv
ALBERT_API_KEY=...
SCALEWAY_API_KEY=...
SCW_POSTGRES_DSN_STAGING=postgresql://lecteur:mot-de-passe@hote-staging:port/base?sslmode=require
# Optionnel : utiliser une copie de Conversations déjà présente.
CONVERSATIONS_DIR=/chemin/vers/conversations
```

`make dev` clone sinon `DGAFP/conversations` dans `.dev/conversations`, à la
révision `b3d3018a93759515b0c828f58fb8d9c1d9182e78` de la
[PR Conversations #1](https://github.com/DGAFP/conversations/pull/1). Cette révision
contient la connexion locale et la délégation individuelle. Une copie fournie
doit contenir ce commit ; la commande ne change pas sa branche et n’écrase pas
ses modifications. Le commit n’étant porté que par la branche de la PR, la copie
gérée dans `.dev/conversations` le récupère explicitement par son SHA (GitHub le
conserve via `refs/pull/1/head`), même après suppression de la branche. La mise à
jour de cette épingle est volontaire et revue ; après fusion de la PR #1, épingler
le commit de fusion pour que les copies fournies à jour de `main` soient acceptées.

```bash
make dump     # importer les corpus/config ; possible avant le premier make dev
make dev      # construire si nécessaire, migrer et démarrer les quatre services
make dev-logs # suivre les logs ; Ctrl-C quitte seulement le suivi
make dev-down # arrêter les services, sans supprimer le volume
make dev-rebuild # reconstruire les images après un changement de dépendances
```

| Service | Adresse par défaut | Variable du port |
|---|---|---|
| Interface Conversations | http://localhost:3000 | `DEV_FRONTEND_PORT` |
| Administration Conversations | http://localhost:3000/admin/ | `DEV_FRONTEND_PORT` |
| Backend direct (diagnostic) | http://localhost:8071/api/v1.0/config/ | `DEV_BACKEND_PORT` |
| API RH | http://localhost:8000/healthz | `DEV_API_PORT` |
| PostgreSQL | 127.0.0.1:55435 | `DEV_DB_PORT` |

Les quatre ports sont liés uniquement à la boucle locale. Chaque worktree a son
projet Compose et son volume dédiés ; choisir des ports distincts pour lancer
plusieurs worktrees simultanément. Vite relaie `/api/`, `/admin/` et `/static/`
vers Django : la connexion, les cookies et les messages utilisent la même adresse
que l’interface. Il suffit de transférer le port de l’interface pour l’ouvrir dans
un navigateur distant ; les redirections conservent son adresse et son port.
Le rechargement des sources API, Django et Vite est actif ; les changements de dépendances nécessitent
`make dev-rebuild`, qui renouvelle aussi les volumes anonymes de dépendances.
Les démarrages ordinaires réutilisent les images et les dépendances présentes.

Deux comptes sont créés au premier démarrage :

- `dev@example.invalid`, utilisateur admis avec accès MATTE/MSO/MI/MASA ;
- `admin@example.invalid`, administrateur local pour gérer les comptes et droits.

Leur mot de passe initial est généré dans `.dev/password` (`cat .dev/password`).
Les démarrages suivants préservent les mots de passe et droits modifiés ; la
création d’un compte et de ses droits est atomique. Le mode
OIDC est désactivé, la session Django est persistée en base. Les clés de signature,
d’audit et Django sont générées une seule fois dans `.dev/`, hors Git et hors
contexte de build ; aucune clé IA ne va au navigateur. Seules les variables IA
explicitement autorisées sont transmises à l’API. Sa base est toujours le service
Docker local, même si `.env` contient un `SCW_POSTGRES_DSN` distant.

Sans dump, `make dev` démarre avec un schéma vide et une configuration par défaut,
sans les identifiants de démonstration de l’API synthétique (`local_auth.sql`).
Le volume neuf et `make dump` appliquent le même bootstrap
(`docker/dev/bootstrap-api.sh`), en une seule transaction.
Pour obtenir des réponses RAG, importer les corpus et renseigner les clés IA :
**les appels Albert/Scaleway restent réels**. Les pièces jointes et le feedback
de l’interface restent soumis aux limitations du fork (#594/#595).

### Rafraîchir les corpus avec `make dump`

La commande accepte uniquement `SCW_POSTGRES_DSN_STAGING`, jamais un fallback
vers le DSN runtime. Le client PostgreSQL 17 tourne dans Docker, en lecture seule.
L’archive contient uniquement douze tables : `rag_documents`, `rag_sections`,
`rag_config`, `system_prompts`, `acronyms`, et les sept `rag_chunks_*` connus
(matte, mso, mi, masa, service_public, dgafp, rgrh). Elle ne contient ni historiques,
feedbacks, traces, groupes, sessions, goldset ni comptes de staging.

La restauration se fait d’abord dans `assistant_rh_import` dans **le conteneur
de ce worktree**, par socket local : aucun DSN de destination n’est accepté.
Les extensions et le schéma runtime API sont installés, avec un compte runtime
non propriétaire rattaché à `arh_api`. La seule contrainte omise est
`rag_documents_legacy_doc_id_fkey`, lien historique vers `documents`, une table
hors périmètre qui n’est pas utilisée par l’API. Les contraintes entre documents
et sections ainsi que la hiérarchie des sections restent présentes.
Après validation, l’API est brièvement
arrêtée si elle tournait, les nouvelles connexions à `assistant_rh` sont refusées
(un client psql/IDE ouvert ne peut pas bloquer la bascule), la nouvelle base prend
le nom `assistant_rh` et l’API redémarre. L’ancienne base reste disponible sous
`assistant_rh_previous` jusqu’au prochain import réussi : la copie antérieure n’est
supprimée qu’une fois la bascule validée (trois copies coexistent brièvement sur
le disque). Si l’export, la restauration ou la bascule échoue, la base courante
et les services existants sont conservés.

Les **comptes et conversations Django sont conservés** dans la base séparée
`conversations`. Le runtime API est neuf après import : ses anciens runs et
feedbacks locaux restent seulement dans `assistant_rh_previous`. L’archive
`.dev/dumps/staging.dump` et le rapport de comptages `.dev/dump-counts.txt` restent
locaux et ignorés par Git. Aucun rôle, mot de passe ou propriétaire de staging
n’est restauré. Ne pas effectuer l’export au milieu d’un backfill d’embeddings.

Les commandes utilisent le démon Docker courant (`DOCKER_HOST`, `DOCKER_CONTEXT` ou
contexte actif : Docker Desktop, Colima, OrbStack…) à condition qu’il soit local
(socket `unix://`) ; un démon distant (`tcp://`, `ssh://`) est refusé. Le point
d’accès est résolu une fois puis passé explicitement à chaque appel. Le DSN de
production (`SCW_POSTGRES_DSN_PROD`, URI ou `host=… dbname=…`) est comparé à la
source : même serveur et même base, ou valeur illisible, et l’import est refusé.
`connect_timeout` et `application_name` de l’URI staging sont respectés.
Le port staging doit être joignable depuis Docker ;
en cas de tunnel, fournir une URI staging explicitement accessible au conteneur.
Une erreur de réseau ou un schéma staging incompatible fait échouer l’import,
sans basculer vers une autre source ou un autre serveur.

Validation locale du 12 octobre 2026 : import réel des douze tables staging
(4 882 documents, 10 412 sections et 25 940 chunks), démarrage des quatre services,
connexion navigateur avec le compte ordinaire, question MATTE traitée par Albert
et run `completed` enregistré en local, réponse retrouvée après reconnexion sans
erreur JavaScript. Un second `make dev` réutilise les images et données. Les douze
tests de garde de `tests/test_dev_stack.py`, Ruff et la validation Compose/YAML
passent. Ce contrôle valide le lancement local, pas un déploiement partagé.

Le parcours navigateur a aussi été vérifié avec un unique port transféré
(`53578` vers `3000`), en bloquant les accès directs du navigateur aux ports
`3000`, `8071` et `8000` : connexion, historique, nouvelle réponse MATTE,
rechargement de la réponse et déconnexion réussis, sans erreur JavaScript.
Une requête de connexion d’une origine étrangère reste refusée par CSRF (403),
même munie d’un jeton valide.

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

## Base runtime synthétique de l'API

La stack dédiée `docker/api/compose.yml` lance l'API et un service pgvector avec
un volume séparé du banc d'eval. Cette séparation est volontaire : les tests API
ne doivent jamais lire le dump staging ni aucune table contenant des
conversations, feedbacks ou identifiants d'utilisateurs.

```bash
proto install
moon run api:local
curl --fail "http://127.0.0.1:${API_PORT:-8000}/healthz"
```

La commande attend les healthchecks PostgreSQL et API. Le probe doit répondre
`200` avec `{"status":"ok","db":"ok","config_loaded":true}`.

Pour suivre les logs puis arrêter la stack en conservant sa base :

```bash
moon run api:local-logs
moon run api:local-down
```

Le schéma et les valeurs minimales viennent uniquement de
`apps/api/tests/fixtures/runtime.sql`. Pour réinitialiser cette base sans
toucher au volume d'eval :

```bash
moon run api:local-reset
moon run api:local
```

Compose scope le conteneur et le volume au worktree courant. Si le port par
défaut est déjà pris par un autre worktree, définir `API_PORT` et
`API_POSTGRES_PORT` pour cette invocation :

```bash
API_PORT=8010 API_POSTGRES_PORT=55443 moon run api:local
```
