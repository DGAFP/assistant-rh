# Assistant RH

**Projet** : Assistant-RH
**Incubateur** : Alliance
**Directions** : DINUM × DGAFP

---

## Chatbot IA – RAG pour la Fonction Publique

Chatbot RAG (Retrieval-Augmented Generation) spécialisé sur les données RH du
Ministère de la Transition Écologique (MATTE). Il aide les gestionnaires RH à
trouver rapidement les informations réglementaires et pratiques relatives aux
contractuels de la fonction publique d'État.

### Stack technique

| Composant | Technologie |
|-----------|-------------|
| UI | **Streamlit** |
| Base de données | **PostgreSQL** + pgvector (Scalingo) |
| Embeddings | Albert (DINUM), Scaleway BGE (fallback) |
| Reranking | Albert /rerank (BGE-m3) |
| LLMs | Albert (DINUM), Scaleway (fallback) |
| Hébergement | **Scalingo** (SecNumCloud `osc-secnum-fr1`) |

### Architecture du pipeline RAG

```
Query
  → QueryProcessor   (intent + acronymes + reformulation)
  → Retriever         (recherche parallèle sur 4-5 tables)
  → SectionAggregator (chunks → sections, reranking)
  → ContextSelector   (filtre LLM, short-circuit si rejet)
  → ContextBuilder    (token budget, triangulation, refs juridiques)
  → Generator         (streaming LLM avec fallback)
→ PipelineResult
```

Le module principal est **`src/rag_v3_clean/`**, entièrement autonome (zéro
dépendance vers les anciens modules RAG). Voir `docs/PIPELINE.md`
pour la documentation détaillée.

### Structure du projet

```
assistant-rh/
├── Home.py                     # Point d'entrée Streamlit
├── pages/                      # 13 pages Streamlit de production
│   ├── 01_Chatbot.py           # Interface principale
│   ├── 02-11_*.py              # Logs, feedback, admin, éval... (admin)
│   └── _PDF_Viewer.py          # Viewer PDF
├── packages/
│   ├── rag-pipeline/           # Pipeline RAG V3 (production)
│   ├── data-engineering/       # Jobs et transformations d'ingestion
│   └── shared-config/          # Configuration partagée
├── src/
│   ├── ui/                     # Composants UI Streamlit
│   └── goldset/                # Outils d'évaluation
├── tests/                      # Tests unitaires
├── docs/
│   ├── PIPELINE.md             # Architecture détaillée du pipeline RAG
│   ├── DATABASE.md             # Schéma complet de la base de données
│   └── rapport_fin_de_mission.md
├── apps/
│   └── data-ingestion-cli/     # CLI canonique d'ingestion de données
├── scripts/                    # Scripts historiques et outillage ponctuel
├── notebooks/                  # Notebooks d'évaluation (RAGAS)
└── data/                       # Données locales gitignored
```

### Sources de données

Le pipeline interroge **4-5 tables** (DGAFP conditionnel via intent gater) :

| Table | Contenu | Publisher |
|-------|---------|-----------|
| `rag_chunks_matte` | Guides pratiques MATTE | MATTE |
| `rag_chunks_service_public` | Fiches Service Public | Service-Public |
| `rag_chunks_dgafp` | Textes réglementaires Legifrance + CGFP | DGAFP |
| `rag_chunks_rgrh` | Base RGRH | RGRH |
| `rag_chunks_test` | Chunks Matte + Service public (activé par défaut, pour meilleur recall) | ChunksTest |

Les chunks sont liés à des **`rag_sections`** (contexte markdown plus large)
elles-mêmes liées à des **`rag_documents`** (titre, URL, publisher).
Voir `docs/DATABASE.md` pour le schéma complet.

### Utilisation rapide

```python
from src.rag_v3_clean import create_pipeline

pipe = create_pipeline()
result = pipe.run("Qu'est-ce que le RIFSEEP ?")
print(result.answer)
print(result.sources)
```

Pour le streaming (Streamlit) :

```python
pipe = create_pipeline()
qr = pipe.process_query("Qu'est-ce que le RIFSEEP ?")
if qr.should_proceed:
    for token in pipe.run_stream(qr):
        print(token, end="")
```

---

## Data Engineering

This repository also contains a data-ingestion CLI to ingest legal documents,
extract text, preprocess into chunks, and compute embeddings for vector search.

The canonical command is `data-ingestion`:

```bash
uv run data-ingestion --help
uv run data-ingestion service-public medallion --help
uv run data-ingestion service-public ingest --help
uv run data-ingestion legifrance bulk-dump --help
uv run data-ingestion legifrance medallion --help
uv run data-ingestion legifrance ingest --help
uv run data-ingestion embeddings service-public --help
uv run data-ingestion embeddings legifrance --help
```

`assistant-rh-data` remains available as a backward-compatible alias during the
migration.

Historical ingestion notebooks and compatibility scripts remain in `scripts/`:

| Script | Source |
|--------|--------|
| `ingestion_matte.ipynb` | Guides pratiques MATTE (PDF) |
| `ingestion_SP.ipynb` | Fiches Service Public (XML/HTML) |
| `ingestion_legifrance.ipynb` | Articles Legifrance (API PISTE) |
| `ingestion_rgrh.ipynb` | Base RGRH |

For more details, see `data/README.md` and `scripts/README.md`.

> **Note** : Make sure Poppler and Tesseract are installed for PDF extraction.

---

## Variables d'environnement

```bash
# Obligatoires
SCALINGO_POSTGRESQL_URL   # Connexion PostgreSQL (ou PG_DSN)
ALBERT_API_KEY            # Clé API Albert (DINUM)
ALBERT_BASE_URL           # https://albert.api.etalab.gouv.fr/v1

# Optionnelles
SCW_SECRET_KEY            # Fallback LLM + embeddings (Scaleway)
SCW_DEFAULT_PROJECT_ID    # Scaleway project
OPENAI_API_KEY            # Métriques RAGAS uniquement
ADMIN_PASSWORD            # Mot de passe pages admin
COOKIES_PASSWORD          # Clé chiffrement cookies navigateur (obligatoire en staging/prod)
ALLOW_INSECURE_COOKIES_PASSWORD  # Local uniquement: fallback explicite, jamais en staging/prod
```


> **Sécurité cookies** : en staging/prod, l'application échoue au démarrage si `COOKIES_PASSWORD` est absent.
> En local, aucun fallback silencieux: définir `COOKIES_PASSWORD` ou activer explicitement `ALLOW_INSECURE_COOKIES_PASSWORD=true` (déconseillé hors dev).

## Lancer en local

```bash
# Installer les dépendances
uv sync --group dev

# Ouvrir le tunnel DB (dans un terminal séparé)
scalingo --region osc-secnum-fr1 --app assistant-rh-staging \
  db-tunnel "$SCALINGO_POSTGRESQL_URL" --port 10001

# Lancer Streamlit
uv run streamlit run Home.py

# Lancer les tests
uv run python -m pytest tests/ --ignore=tests/archive -v
```

## Déploiement

Push sur `main` déclenche le déploiement automatique sur Scalingo.
Seules les dépendances `[project].dependencies` sont installées en prod
(`default-groups = []` dans `[tool.uv]`).

```bash
git push origin main
```

---

## Linting & Formatting

### Python (ruff)

```bash
# Lint + format check
uv run ruff check src apps/streamlit-ui/pages tests --select E,F,I

# Auto-fix
uv run ruff check --fix src apps/streamlit-ui/pages tests
```

### TypeScript (Biome)

```bash
# Lint + format check
pnpm lint:ts

# Auto-fix (safe + unsafe)
pnpm exec biome check --write --unsafe apps/mastra-pipeline/src apps/mastra-pipeline/scripts
```

Biome config is in `biome.json` at the workspace root. It covers all `.ts`/`.tsx` files under `apps/mastra-pipeline/src/` and `apps/mastra-pipeline/scripts/`.

### Pre-commit hooks

This repo uses [pre-commit](https://pre-commit.com) for both ruff (Python) and Biome (TypeScript).

**Installing hooks in a bare-repo workspace:**

Because this repo uses the bare-repo + worktree pattern, `git rev-parse --git-common-dir` returns `.bare/`, which has no real hooks. To install pre-commit hooks so they run on every commit:

```bash
# From inside any worktree (e.g. main/ or feat-*/):
pre-commit install --config $(git rev-parse --show-toplevel)/.pre-commit-config.yaml
```

This sets `core.hooksPath` to the pre-commit managed directory, bypassing the bare repo's empty hooks.

**Running hooks manually (targeted):**

```bash
# Run only on specific files (avoids reformatting unrelated code)
pre-commit run --files path/to/file.ts path/to/other.py

# Run only the Biome hook
pre-commit run biome-check --files apps/mastra-pipeline/src/mastra/index.ts

# Run only the ruff hook
pre-commit run ruff --files packages/rag-pipeline/src/assistant_rh_rag_pipeline/pipeline.py
```

> **Warning**: Do not run `pre-commit run --all-files` — it may reformat large areas of the codebase that are outside the scope of your change.
