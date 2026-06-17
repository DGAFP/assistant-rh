# Audit de l'ingestion source MATTE — Issue #103

> **Statut :** audit read-only, scope limité à la documentation + un outil
> d'audit hors-ligne. **Aucune** ré-ingestion, **aucun** backfill d'embeddings,
> **aucune** migration, **aucun** index créé, **aucune** écriture staging/prod
> ne doit être déclenchée par cette PR. Le présent document consigne l'état de
> l'art de l'ingestion MATTE tel qu'il est observable dans le repo et dans les
> audits staging déjà publiés ; il propose un plan d'action mais ne l'exécute
> pas.

- **Issue :** [DGAFP/assistant-rh#103](https://github.com/DGAFP/assistant-rh/issues/103)
- **PR d'audit :** `fix/issue-103-matte-ingestion-audit` (sur `origin/main`)
- **Base :** `origin/main` @ `128c7784bcf213f3b204779bca39604199f8c511`
- **Travaille à partir de :** `main` frais, indépendamment de la PR #119 (#102)
- **Périmètre :** documentation, tests, outillage read-only/offline (`scripts/audit_matte_ingestion.py`)
- **Hors-périmètre strict :** ingestion réelle, backfill embeddings, création d'index, écriture staging/prod

---

## 1. Résumé exécutif

| Élément | Constat (audit) | Action |
|---|---|---|
| Acquisition source | **Manuelle** : 3 PDF « temps du travail » hardcodés dans `amelioration_matte.ipynb`, aucun job versionné, pas d'équivalent `medallion` Service-Public / Légifrance | Documenter la procédure et proposer un plan d'automatisation |
| Complétude | **959/959 chunks** sur `rag_chunks_matte.embedding_m3` ; **17/44 documents** n'ont pas de chunks (cf. note audit 06) ; **5 colonnes d'embedding** coexistent dont 4 alternatives au BGE-M3 | Audit `--check-only` (tooling) ; correction = nouveau chantier de ré-ingestion (hors-périmètre) |
| Idempotence | Le notebook d'ingestion historique (`scripts/ingestion_pdf.ipynb`) fait un `ON CONFLICT (hash_id) DO UPDATE SET` qui **écrase toutes les colonnes, y compris l'embedding** | Recommandation : `DO UPDATE SET ... embedding = COALESCE(EXCLUDED.embedding, …)` (aligné avec #102) |
| Reproductibilité | `data/in/temps_du_travail/` n'est pas versionné ; dépendances (Poppler, Tesseract, `sentence-transformers`) non déclarées dans `pyproject.toml` | Documenter les pré-requis ; pas de portage du notebook dans cette PR |
| Couverture embeddings | Colonnes `embedding_m3` pleine ; `embedding_bge_scw` à **762 NULL**, `embedding_qwen3` partielle ; colonnes `embedding_ctx` et `embedding_bge` présentes (semble être des artefacts de modèles testés) | Tooling : `audit_matte_ingestion.py --sql-only` émet les requêtes ; exécution manuelle après approbation |
| Index vectoriel | **Absent** sur `rag_chunks_matte.embedding_m3` → scan séquentiel à chaque requête (note 06 §2.2) | **Ne pas créer d'index dans cette PR** ; lister le constat + la commande à valider |

> **Source de vérité secondaire :** `docs/audit/06_AUDIT_CODE_ET_DB.md` (côté DB) et
> `docs/audit/01_RAG_QUALITY_AUDIT_2026-06.md` (côté RAG) — chiffres validés sur
> staging au 2026-06-15.

---

## 2. État du pipeline MATTE dans le repo

### 2.1 Ce que dit `scripts/README.md`

Le README des scripts annonce une chaîne à trois notebooks :

| Notebook annoncé | Présent sur `origin/main` ? | Constat |
|---|---|---|
| `scripts/extract_matte.ipynb` | **Non** | Référencé par `scripts/README.md` et par `.env.example` (`MATTE_INPUT_PATTERNS`, `MATTE_CHUNKS_JSONL`, etc.) mais **absent du worktree** |
| `scripts/amelioration_matte.ipynb` | **Oui** | Nettoyage + sectioning pour `temps_du_travail` ; produit le JSONL `matte_temps_travail_3pdf_clean.jsonl` et l'artefact Parquet/NPY/JSONL embeddings ; **ne contient pas de code SQL d'ingestion** |
| `scripts/ingestion_matte.ipynb` | **Non** | Référencé par `scripts/README.md` et par `.env.example` (`MATTE_TABLE`, `MATTE_IN_JSONL_WITH_EMB`, etc.) mais **absent du worktree** |

> **Constat d'audit :** sur un `origin/main` frais, **deux notebooks sur trois sont absents**.
> La chaîne complète d'ingestion MATTE n'est donc pas reproductible à partir du seul
> code versionné. `amelioration_matte.ipynb` produit les artefacts JSONL+embeddings,
> mais aucun script SQL versionné ne les pousse vers `rag_chunks_matte`.

### 2.2 Ce que fait `amelioration_matte.ipynb` (et ce qu'il ne fait pas)

Le notebook `amelioration_matte.ipynb` est le **seul** livrable MATTE présent
sur `origin/main`. Sa lecture exhaustive (cf. son contenu dans le worktree) montre
qu'il :

1. **Déclare explicitement 3 PDF d'entrée** (chemins relatifs) :
   ```python
   PDF_PATHS: List[Path] = [
       Path("./data/in/temps_du_travail/Cadrage national DIR_2009.pdf"),
       Path("./data/in/temps_du_travail/instruction_ministerielle_du_6_janvier_2011.pdf"),
       Path("./data/in/temps_du_travail/Reglement_interieur_ARTT_AC_01012013-10.pdf"),
   ]
   ```
2. **Lit les PDF** avec `pypdf.PdfReader` (sans OCR — les PDF doivent être
   textuels ; pas de fallback `pdf2image` + `pytesseract` pour les scans).
3. **Sectionne** avec un détecteur hiérarchique (numérotation `2.1 - …`,
   `Annexe N`, Préambule, etc.), avec profondeur max 4.
4. **Chunk** en mode « paragraph-aware » (`MAX_CHARS=1800`, `OVERLAP=220`,
   `MIN_CHUNK_CHARS=200`) et **duplique** les chunks qui ressemblent à des
   tableaux (`role = "TABLE"`) — pratique, mais génère mécaniquement des
   doublons de texte sur des changements futurs du classifieur.
5. **Calcule un `hash_id`** :
   ```python
   def make_hash_id(source_name, section_path, role, chunk_index, text):
       key = f"{source_name}|{section_path}|{role}|{chunk_index}|{text}"
       return sha1_hex(key)  # sha1
   ```
   ⚠ Le `text` est intégré **en clair** dans le hash ; tout futur changement
   de normalisation (par ex. ajout d'un collapse d'espaces) régénère **tous**
   les `hash_id` et fait diverger la base de l'identifiant stable historique.
6. **Encode les embeddings** avec `BAAI/bge-m3` (Sentence-Transformers, CUDA
   si dispo, sinon CPU) ; **défaut `BATCH_SIZE=64`**.
7. **Produit trois artefacts** sous `./data/out/` :
   - `matte_temps_travail_3pdf_clean.jsonl` — chunks + métadonnées
   - `matte_temps_du_travail_amelioration_chunks_*.{parquet,npy}` — vecteurs
   - `matte_temps_du_travail_amelioration_chunks_*_with_emb.jsonl` — JSONL
     inline avec la colonne d'embedding (1024-dim)
8. **Ne contient pas de code SQL d'ingestion**. L'écriture en base est faite
   par un autre notebook (`scripts/ingestion_pdf.ipynb`, voir §2.3).

### 2.3 Le notebook SQL d'ingestion réellement présent

Le code SQL d'ingestion MATTE n'est **pas** dans `amelioration_matte.ipynb`,
mais dans `scripts/ingestion_pdf.ipynb` (notebook générique réutilisé). Lecture
exhaustive :

- Cible : `MATTE_TABLE` (défaut `rag_chunks_3` ⚠) ; `MATTE_IN_JSONL_WITH_EMB`
  (défaut `matte_temps_du_travail_amelioration_chunks_baai_bge_m3_with_emb.jsonl`)
- Connexion : `psycopg` direct avec `PGHOST/PGPORT/PGUSER/PGPASSWORD/PGDATABASE`
  ; **port hardcodé à `10001`** (le tunnel `db-tunnel`, pas `SCW_POSTGRES_DSN`).
- **Schéma upsert risqué** :
  ```sql
  INSERT INTO "schema"."table" (...)
  VALUES (...)
  ON CONFLICT (hash_id) DO UPDATE SET
    text = EXCLUDED.text,
    "embedding_m3" = EXCLUDED.embedding_m3,   -- ⚠ écrase l'embedding existant
    ...
  ```
  ➜ Si le JSONL ré-encodé n'a pas la colonne d'embedding (ou l'a à NULL),
  **toutes** les colonnes — y compris l'embedding existant non-NULL — sont
  écrasées avec NULL. C'est le même antipattern que celui corrigé dans #102
  pour Légifrance/Service-Public.
- **Étend le schéma dynamiquement** : il vérifie l'existence de la colonne
  d'embedding et **la crée** (`vector(1024)`) si absente. C'est commode pour
  un labo mais c'est une migration implicite qui n'est pas versionnée.
- **Paramètres HNSW/IVFFLAT** (`SET hnsw.ef_search = 80`, `SET
  ivfflat.probes = 10`) sont positionnés **après** l'upsert et n'ont aucun
  effet : `SET` sans `LOCAL` ne s'applique qu'à la session courante, et
  aucune session de retrieval ne réutilise ce notebook. Ces lignes ne sont
  pas dangereuses mais sont trompeuses.

### 2.4 Couverture de la source — état connu

- **`rag_chunks_matte` (cible canonique de retrieval) :** 959/959 chunks ont un
  `embedding_m3` non NULL — confirmé staging 2026-06-15 (audit 06 §2.2).
- **Couverture documentaire :** **17/44 documents** n'ont **pas** de chunks
  dans la table. C'est l'autre moitié du trou MATTE (cf. audit 01 add.1 et
  audit 06 §2.1). Cette PR n'est pas en charge de le corriger.
- **Colonnes d'embedding coexistantes** (audit 06 §1) :

  | Colonne | Type / dim | Couverture staging (15/06) | Source |
  |---|---|---|---|
  | `embedding_m3` | `vector(1024)` | 959/959 ✅ | Albert / `BAAI/bge-m3` (colonne canonique de retrieval) |
  | `embedding_bge_scw` | `vector(3584)` | 197/959 (762 NULL) | BGE Multilingual Gemma2 Scaleway (fallback embeddings) |
  | `embedding_qwen3` | `vector(… )` | partielle | probablement Qwen3-Embedding — usage interne/expérimental |
  | `embedding_ctx` | `vector(… )` | inconnue | nom générique — usage inconnu, à clarifier |
  | `embedding_bge` | `vector(… )` | inconnue | nom ambigu (≠ `embedding_bge_scw`) — usage inconnu |

  ➜ Le pipeline de retrieval **n'utilise que `embedding_m3`** (cf. `EMBEDDING_COLUMN_MAP` dans
  `packages/rag-pipeline/src/assistant_rh_rag_pipeline/embedder.py` ligne 39 et
  `CHUNK_TABLES["matte"].embed_col_albert = "embedding_m3"` dans `config.py` ligne 60).
  Aucune colonne alternative n'est lue par le retriever. **Aucune colonne ne doit
  être supprimée ou renommée sans approbation explicite** (cf. §5.4).

### 2.5 Index vectoriel

`rag_chunks_matte.embedding_m3` **n'a pas d'index vectoriel** (audit 06 §2.2 et
07 §P0.3) — c'est l'**unique** table parmi les 4 interrogées par le retriever
qui scanne séquentiellement à chaque requête.

> **Cette PR ne crée aucun index.** La commande de création d'un index HNSW
> (pgvector ≥ 0.5) ou IVFFLAT est documentée en §5.5 à titre informatif mais
> doit faire l'objet d'une approbation Paul avant exécution, conformément à
> la consigne du CTO.

### 2.6 Origine des PDF

- `data/in/temps_du_travail/` n'est **pas versionné** (cf. `data/README.md`).
- Les 3 PDF déclarés dans `amelioration_matte.ipynb` semblent être des
  documents administratifs MATTE :
  - `Cadrage national DIR_2009.pdf` — cadrage national 2009
  - `instruction_ministerielle_du_6_janvier_2011.pdf` — instruction ministérielle 2011
  - `Reglement_interieur_ARTT_AC_01012013-10.pdf` — règlement intérieur ARTT 2013
- **L'audit ne peut pas** prouver la complétude de l'inventaire MATTE sans un
  *référentiel de sources* validé par Paul (cf. action `4d730cc docs(audit):
  idée — référentiel de sources (Grist)`).

---

## 3. Schéma canonique runtime

`packages/rag-pipeline/src/assistant_rh_rag_pipeline/embedder.py` (lignes 39-44)
et `config.py` (lignes 57-96) centralisent le mapping **embedding → colonne
DB** :

| Table | Colonne Albert / BGE-M3 | Colonne BGE-Scaleway | Modèle canonique |
|---|---|---|---|
| `rag_chunks_matte` | **`embedding_m3`** | `embedding_bge_scw` | Albert `BAAI/bge-m3` (1024-d) |
| `rag_chunks_service_public` | `embedding_m3` | `embedding_bge_scw` | idem |
| `rag_chunks_dgafp` | `embedding_m3` | `embedding_bge_scw` | idem |
| `rag_chunks_rgrh` | `embedding_m3` | `embedding_bge_scw` | idem |

Conséquences pour MATTE :

- Le retriever lit **uniquement** `embedding_m3` quand Albert est primaire.
- Le `FallbackEmbedder` retombe sur BGE-Scaleway et lit alors
  `embedding_bge_scw` — qui a 762 NULL → silence fonctionnel : la colonne
  est inutilisable tant qu'elle n'est pas backfillée.
- Les colonnes `embedding_qwen3`, `embedding_ctx`, `embedding_bge` (si
  existantes) ne sont lues par **aucun** chemin de retrieval connu. Elles
  sont probablement des artefacts de modèles testés (audit 06 §1.4 « schéma
  ambigu »).

---

## 4. Risques d'idempotence

| Risque | Source | Constat | Recommandation |
|---|---|---|---|
| Écrasement d'embeddings existants | `ON CONFLICT (hash_id) DO UPDATE SET` dans `ingestion_pdf.ipynb` (ligne 235) qui fait `text = EXCLUDED.text, embedding_m3 = EXCLUDED.embedding_m3, …` | Si le JSONL ré-encodé a `embedding_m3 = NULL` (modèle non chargé, OCR non passé, etc.), tous les embeddings existants sont perdus | Aligner sur le pattern #102 : `COALESCE(EXCLUDED.embedding_m3, table.embedding_m3)` pour les colonnes d'embedding. Ne pas écraser `embedding_bge_scw` avec NULL non plus. |
| Stabilité du `hash_id` | `make_hash_id` intègre `text` en clair | Tout futur changement de normalisation du texte (collapse d'espaces, unicode NFKC, etc.) régénère **tous** les `hash_id` et invalide la base existante | Documenter la dépendance : `text` dans le `hash_id` doit être **normalisé de manière stable** et toute évolution de la fonction `normalize_text` doit être annoncée en breaking change |
| Sectioning non déterministe | `HEAD_PAT` matche des heuristiques (« Préambule », « Table des matières », chiffres, `Annexe N`, romains) sur des regex « best-effort » | Des PDF reflowés (changement de police, coupure de mots) peuvent déplacer un heading et changer la `section_path` de **tous** les chunks en aval | Pour la repro : geler la version de `pypdf` (≥ 4.0) et éviter les mises à jour mineures silencieuses de regex |
| Doublons de chunks | `looks_like_table` duplique les chunks type tableau en `role = "TABLE"` | Les chunks `TABLE` partagent `text` avec leur chunk `CHUNK` parent | Acceptable mais à savoir : `COUNT(*) FROM rag_chunks_matte` peut surcompter la « matière utile » ; à coupler avec `COUNT(DISTINCT text)` dans l'audit |
| `source` codée en dur | `df['source'] = "SERVICE PUBLIC"` dans `amelioration_matte.ipynb` (cellule d'embeddings) | Toutes les lignes MATTE sont marquées `source = "SERVICE PUBLIC"` dans la base | Nettoyage de données à prévoir (UPDATE `source` WHERE `source_name` LIKE '%.pdf' AND source LIKE 'SERVICE PUBLIC') ; **bloqué en attente d'approbation Paul** |

---

## 5. Checklist reproductible d'audit (read-only)

### 5.1 Audit local du repo (zéro DB, zéro réseau)

Exécutable localement et en CI avec :

```bash
uv run python scripts/audit_matte_ingestion.py --repo-root . --sql-only
```

Le mode `--sql-only` (par défaut) imprime un rapport JSON-encadré :

- Présence/absence des 3 notebooks `extract_matte.ipynb` / `amelioration_matte.ipynb` / `ingestion_matte.ipynb` dans le repo
- Liste des **PDF déclarés** dans la `PDF_PATHS` du notebook `amelioration_matte.ipynb` (parsing statique de la liste Python)
- Requêtes SQL `SELECT` à exécuter manuellement avec une connexion read-only approuvée
- Diagnostic : `STALE_NOTEBOOKS` si `extract_matte.ipynb` / `ingestion_matte.ipynb` manquent sur `origin/main`

### 5.2 Audit local des artefacts générés (optionnel — follow-up)

L'audit des artefacts locaux `data/out/*.jsonl`, `*.parquet` et `*.npy` est
utile mais **n'est pas inclus dans la première tranche d'outillage** afin de
rester sous la limite de taille PR (objectif ~300 additions, plafond souple
400). Si nécessaire, il doit faire l'objet d'une PR dédiée et vérifier sans
jamais écrire :

- nombre de lignes,
- unicité de `hash_id`,
- non-vacuité de `text` / `chunk_text`,
- cohérence entre la dimension de `embedding_m3` et l'attendu (1024),
- ratio embedding-array-len / row-count = 1 (1 vecteur par ligne).

### 5.3 Audit DB en lecture seule (manuel, hors outillage initial)

La première tranche de `scripts/audit_matte_ingestion.py` **n'ouvre pas de
connexion DB**. Elle émet seulement les requêtes `SELECT` ci-dessous.

Toute exécution DB doit rester manuelle, avec une connexion read-only
explicitement approuvée. Un éventuel mode `--db-readonly` automatisé devra être
livré plus tard dans une PR séparée, avec garde-fous dédiés et tests ciblés.

### 5.4 Audit DB read-only — requêtes SQL à exécuter manuellement

Émises par `audit_matte_ingestion.py --sql-only` ou listées ci-dessous.

#### 5.4.1 Couverture embeddings (par colonne)

```sql
-- Couverture embeddings sur rag_chunks_matte
SELECT
  COUNT(*)                                                    AS total_rows,
  COUNT(*) FILTER (WHERE embedding_m3       IS NULL)          AS embedding_m3_null,
  COUNT(*) FILTER (WHERE embedding_bge_scw  IS NULL)          AS embedding_bge_scw_null,
  COUNT(*) FILTER (WHERE embedding_qwen3    IS NULL)          AS embedding_qwen3_null,
  COUNT(*) FILTER (WHERE embedding_ctx      IS NULL)          AS embedding_ctx_null,
  COUNT(*) FILTER (WHERE embedding_bge      IS NULL)          AS embedding_bge_null
FROM rag_chunks_matte;
```

#### 5.4.2 Colonne canonique de retrieval

```sql
-- Colonne effectivement utilisée par le retriever Albert/BGE-M3
SELECT column_name, data_type, udt_name, character_maximum_length
FROM information_schema.columns
WHERE table_schema = 'public'
  AND table_name   = 'rag_chunks_matte'
  AND column_name IN ('embedding_m3', 'embedding_bge_scw', 'embedding_qwen3', 'embedding_ctx', 'embedding_bge')
ORDER BY column_name;
```

#### 5.4.3 Doublons et intégrité

```sql
-- Doublons de hash_id (devrait toujours être 0)
SELECT hash_id, COUNT(*) AS n
FROM rag_chunks_matte
GROUP BY hash_id
HAVING COUNT(*) > 1
ORDER BY n DESC
LIMIT 20;

-- Sections / documents référencés par les chunks
SELECT
  COUNT(*)                                                        AS total,
  COUNT(*) FILTER (WHERE section_id        IS NULL)               AS section_id_null,
  COUNT(*) FILTER (WHERE source_document_id IS NULL)              AS source_document_id_null,
  COUNT(*) FILTER (WHERE short_id          IS NULL)               AS short_id_null
FROM rag_chunks_matte;

-- Texte vide / NULL
SELECT
  COUNT(*) FILTER (WHERE chunk_text IS NULL OR chunk_text = '')   AS empty_chunk_text,
  COUNT(*) FILTER (WHERE text       IS NULL OR text       = '')   AS empty_text
FROM rag_chunks_matte;
```

#### 5.4.4 Index (recherche vectorielle + texte)

```sql
-- Index existants sur rag_chunks_matte
SELECT indexname, indexdef
FROM pg_indexes
WHERE schemaname = 'public' AND tablename = 'rag_chunks_matte'
ORDER BY indexname;
```

> Si la requête ne retourne **aucun** index contenant `embedding_m3`, cela
> confirme l'absence d'index vectoriel signalée par l'audit 06 §2.2. La
> commande de création d'un index HNSW est documentée en §5.5 mais
> **n'est pas exécutée par cette PR**.

### 5.5 Commandes de remédiation (à valider avec Paul — hors-périmètre)

> **Aucune** de ces commandes n'est exécutée par `audit_matte_ingestion.py`.
> Elles sont listées pour traçabilité et pour la prochaine PR de remédiation.

```sql
-- 1) Créer un index HNSW sur embedding_m3 (pgvector ≥ 0.5)
-- ATTENTION : coûteux, lock table pendant la création (~minutes sur 959 lignes
--   ici c'est rapide, mais l'opération scale linéairement sur de plus gros
--   corpus). À exécuter hors heures de pointe.
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_rag_chunks_matte_embedding_m3_hnsw
  ON rag_chunks_matte
  USING hnsw (embedding_m3 vector_cosine_ops);

-- 2) Backfill embedding_bge_scw (BGE-Scaleway) si nécessaire :
--    via `data-ingestion embeddings backfill --only-table rag_chunks_matte
--    --only-column embedding_bge_scw` après approbation.
```

---

## 6. Conclusion

L'ingestion MATTE est **fonctionnelle** (959 chunks, `embedding_m3` complet,
retrieval opérationnel) mais elle cumule plusieurs fragilités structurelles
qui expliquent les classements P0/P1 dans l'audit global :

1. **Acquisition manuelle non versionnée** — chaîne `extract → amelioration →
   ingestion` non reproductible depuis `origin/main` (deux notebooks manquent).
2. **Couverture documentaire incomplète** — 17/44 documents n'ont aucun
   chunk.
3. **Schéma d'embeddings ambigu** — 5 colonnes coexistent, une seule
   (`embedding_m3`) est canonique de retrieval ; les 4 autres sont
   probablement des artefacts.
4. **Index vectoriel absent** — la seule table canonique MATTE scanne
   séquentiellement.
5. **Idempotence risquée** — le `DO UPDATE SET` du notebook d'ingestion
   historique peut écraser des embeddings existants avec NULL.

Cette PR apporte **uniquement** :

- ce document d'audit (référentiel pour les chantiers de remédiation) ;
- un outil offline/read-only (`scripts/audit_matte_ingestion.py`) qui peut tourner
  en CI sans DB ni réseau ;
- une suite de tests qui couvre le parsing du notebook et la génération de SQL.

Elle ne fait **pas** : de ré-ingestion, de backfill, de migration, d'index,
ni d'écriture staging/prod. Toute commande de remédiation (§5.5) reste
**bloquée en attente d'approbation explicite de Paul**, conformément à la
consigne du CTO.
