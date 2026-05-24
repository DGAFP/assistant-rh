# Schéma de la base de données

## Infrastructure

- **SGBD** : PostgreSQL (avec extension `pgvector` pour la recherche sémantique)
- **Hébergement** : Scalingo, région SecNumCloud `osc-secnum-fr1`
- **Connexion** : résolution explicite via `APP_DB_TARGET` + DSN canonique
  - `APP_DB_TARGET=scalingo` → `SCALINGO_POSTGRESQL_URL`
  - `APP_DB_TARGET=scaleway` + `APP_SCALEWAY_ENV=prod` → `SCW_POSTGRES_DSN_PROD`
  - `APP_DB_TARGET=scaleway` + `APP_SCALEWAY_ENV=staging` → `SCW_POSTGRES_DSN_STAGING`
  - fallback legacy (hors ciblage explicite) : `APP_POSTGRES_DSN`, `STREAMLIT_POSTGRES_DSN`, `SCALINGO_POSTGRESQL_URL`, `PG_DSN`, `DATABASE_URL`
- **ORM** : pas d'ORM — requêtes SQL directes via `psycopg` (v3) et `sqlalchemy` (engine)

---

## Vue d'ensemble des relations

```
rag_documents (1)
    │
    ├── rag_sections (N)         # sections markdown d'un document
    │       │
    │       ├── rag_chunks_matte (N)            # chunks avec section_id FK
    │       ├── rag_chunks_service_public (N)
    │       ├── rag_chunks_rgrh (N)
    │       └── rag_chunks_test (N)
    │               │
    │               └── rag_chunk_embeddings (1:1)  # embeddings séparés
    │
    └── documents (legacy)       # table historique avec PDF en bytea
            liée via rag_documents.legacy_doc_id

rag_chunks_dgafp                 # standalone — pas de section_id
```

---

## 1. Tables de contenu

### 1.1 Tables de chunks (DE)

Les chunks sont les unités de texte indexées pour la recherche sémantique. Quatre tables de production + une table de test partagent un schéma similaire mais avec des variations de noms de colonnes.

#### `rag_chunks_matte`

Chunks issus des fiches et guides MATTE (Ministère de la Transition Écologique).

| Colonne | Type | Description |
|---------|------|-------------|
| `hash_id` | `VARCHAR(64)` PK | Identifiant unique (hash du contenu) |
| `source_name` | `VARCHAR(255)` | Nom du document source |
| `section_path` | `TEXT` | Chemin hiérarchique dans le document |
| `chunk_index` | `INTEGER` | Ordre du chunk dans le document |
| `text` | `TEXT` | Contenu textuel brut |
| `chunk_text` | `TEXT` | Contenu textuel (alias utilisé par le retriever) |
| `role` | `TEXT` | Rôle/catégorie du chunk |
| `thematique` | `TEXT` | Thématique RH |
| `references_juridiques` | `TEXT` | Références juridiques associées |
| `source_document_id` | `UUID` | FK vers `rag_documents.doc_id` |
| `section_id` | `UUID` | FK vers `rag_sections.section_id` |
| `short_id` | `VARCHAR` | Identifiant court du document |
| `embedding_m3` | `vector(1024)` | Embedding Albert (BAAI/bge-m3) |
| `embedding_bge_scw` | `vector(3584)` | Embedding BGE Scaleway |

#### `rag_chunks_service_public`

Chunks issus des fiches Service-Public.fr. **Schéma identique** à `rag_chunks_matte`.

#### `rag_chunks_rgrh`

Chunks issus du Répertoire de Gestion des Ressources Humaines. **Schéma identique** à `rag_chunks_matte`.

#### `rag_chunks_dgafp`

Chunks issus de la DGAFP (textes réglementaires Legifrance). Schéma distinct — pas de `section_id`.

| Colonne | Type | Description |
|---------|------|-------------|
| `chunk_id` | `VARCHAR(64)` PK | Identifiant unique |
| `title` | `TEXT` | Titre court de l'article |
| `full_title` | `TEXT` | Titre complet |
| `number` | `TEXT` | Numéro de l'article (ex: `"D. 2014-513"`) |
| `category` | `TEXT` | Catégorie (décret, arrêté, circulaire…) |
| `url` | `TEXT` | URL Legifrance |
| `cid` | `TEXT` | Identifiant Legifrance (CID) |
| `chunk_text` | `TEXT` | Contenu textuel |
| `embedding_m3` | `vector(1024)` | Embedding Albert |
| `embedding_bge_scw` | `vector(3584)` | Embedding BGE Scaleway |

> Utilisée par le `ContextBuilder` pour résoudre les références juridiques citées dans les sections MATTE/SP (lookup par `number` → `cid`, `url`).

#### `rag_chunks_test`

Table de test pour l'ingestion de nouveaux documents. Les embeddings sont stockés dans une table séparée `rag_chunk_embeddings`.

| Colonne | Type | Description |
|---------|------|-------------|
| `chunk_id` | `VARCHAR(64)` PK | Identifiant unique |
| `chunk_text` | `TEXT` | Contenu textuel |
| `chunk_markdown` | `TEXT` | Contenu markdown |
| `section_id` | `UUID` | FK vers `rag_sections.section_id` |
| `doc_id` | `UUID` | FK vers `rag_documents.doc_id` |
| `metadata` | `JSONB` | Métadonnées libres |
| `chunk_tsv` | `tsvector` | Vecteur de recherche lexicale pondéré (titre A, heading B, contenu D) |

#### `rag_chunk_embeddings`

Embeddings associés aux chunks de `rag_chunks_test` (relation 1:1 avec FK CASCADE).

| Colonne | Type | Description |
|---------|------|-------------|
| `chunk_id` | `VARCHAR(64)` PK/FK | FK vers `rag_chunks_test.chunk_id` (ON DELETE CASCADE) |
| `embedding_raw` | `vector(1024)` | Embedding Albert |
| `embedding_bge` | `vector(3584)` | Embedding BGE Multilingual Gemma2 (Scaleway) |

**Index** : `idx_chunk_embeddings_bge_ivfflat` — IVFFlat sur `embedding_bge` (cosine, lists=100)

#### Mapping des colonnes par table

| Table | ID | Texte | Embed Albert | Embed BGE | has_sections |
|-------|-----|-------|-------------|-----------|-------------|
| `rag_chunks_matte` | `hash_id` | `chunk_text` | `embedding_m3` | `embedding_bge_scw` | oui |
| `rag_chunks_service_public` | `hash_id` | `chunk_text` | `embedding_m3` | `embedding_bge_scw` | oui |
| `rag_chunks_dgafp` | `chunk_id` | `chunk_text` | `embedding_m3` | `embedding_bge_scw` | non |
| `rag_chunks_rgrh` | `hash_id` | `chunk_text` | `embedding_m3` | `embedding_bge_scw` | oui |
| `rag_chunks_test` | `chunk_id` | `chunk_text` | via `rag_chunk_embeddings.embedding_raw` | via `rag_chunk_embeddings.embedding_bge` | oui |

> Ce mapping est centralisé dans `src/rag_v3_clean/config.py` (`CHUNK_TABLES` et `CHUNKS_TEST_TABLE`).

---

### 1.2 `rag_sections`

Sections markdown reconstruites à partir des chunks. Unité de contexte principale du pipeline V3.

| Colonne | Type | Description |
|---------|------|-------------|
| `section_id` | `UUID` PK | Identifiant unique |
| `doc_id` | `UUID` | FK vers `rag_documents.doc_id` |
| `heading` | `TEXT` | Titre de la section |
| `heading_path` | `TEXT` | Chemin hiérarchique (ex: `"Titre 1 > Chapitre 2 > Section A"`) |
| `section_markdown` | `TEXT` | Contenu markdown complet de la section |
| `markdown_content` | `TEXT` | Alias du contenu markdown (utilisé par le feedback analyzer) |
| `section_index` | `INTEGER` | Ordre dans le document |
| `parent_section_id` | `UUID` | FK vers section parente (hiérarchie) |
| `references_juridiques` | `JSONB` | Références juridiques associées. Format : `[{url, titre, type, source}]` |

**Index** : `idx_rag_sections_refs` — GIN sur `references_juridiques`

> Requêtée par le `SectionAggregator` via JOIN avec `rag_documents` pour enrichir les chunks avec le contexte documentaire complet.

---

### 1.3 `rag_documents`

Métadonnées des documents sources ingérés dans le pipeline RAG.

| Colonne | Type | Description |
|---------|------|-------------|
| `doc_id` | `UUID` PK | Identifiant unique (gen_random_uuid) |
| `source` | `TEXT` | Source (`matte`, `service_public`, `legifrance`, `rgrh`) |
| `source_url` | `TEXT` | URL originale du document |
| `storage_path` | `TEXT` | Chemin S3 (historique, actuellement NULL) |
| `title` | `TEXT` | Titre du document |
| `full_title` | `TEXT` | Titre complet |
| `short_id` | `VARCHAR` | Identifiant court (ex: `"F12"`, `"A03"`) |
| `publisher` | `TEXT` | Éditeur (MATTE, Service-Public, DGAFP, RGRH) |
| `doc_type` | `TEXT` | Type de document |
| `last_updated_date` | `DATE` | Date de dernière mise à jour |
| `publication_date` | `DATE` | Date de publication |
| `page_count` | `INTEGER` | Nombre de pages |
| `lang` | `TEXT` | Langue (défaut : `fr`) |
| `checksum` | `TEXT` | Hash du contenu pour déduplication |
| `parse_version` | `TEXT` | Version du parser |
| `parse_model` | `TEXT` | Modèle de parsing utilisé |
| `quality_flags` | `JSONB` | Drapeaux qualité (structure, erreurs…) |
| `doc_markdown` | `TEXT` | Contenu markdown complet |
| `doc_markdown_raw` | `TEXT` | Contenu markdown brut (avant nettoyage) |
| `doc_text_hash` | `TEXT` | Hash du texte |
| `token_count` | `INTEGER` | Nombre de tokens |
| `char_count` | `INTEGER` | Nombre de caractères |
| `line_count` | `INTEGER` | Nombre de lignes |
| `metadata` | `JSONB` | Métadonnées additionnelles |
| `doc_structure` | `JSONB` | Arbre hiérarchique du document (`{structure, errors, warnings, stats}`) |
| `legacy_doc_id` | `UUID` | FK vers `documents.id` (table legacy avec PDF bytea) |
| `created_at` | `TIMESTAMP` | Date de création |
| `updated_at` | `TIMESTAMP` | Date de mise à jour |

**Index** : `idx_rag_documents_legacy_doc_id` (partiel, WHERE NOT NULL), `idx_rag_documents_structure` (GIN)

---

### 1.4 `documents` (legacy)

Table historique stockant les PDF en binaire. Utilisée uniquement par le PDF Viewer comme fallback.

| Colonne | Type | Description |
|---------|------|-------------|
| `id` | `UUID` PK | Identifiant unique |
| `filename` | `TEXT` | Nom du fichier |
| `source_name` | `TEXT` | Nom de la source |
| `content` | `BYTEA` | Contenu binaire du PDF |

> Chaîne de résolution PDF : `legacy_doc_id` → fetch PDF depuis `documents` → sinon `source_url` → URL externe.

---

## 2. Tables de configuration

### 2.1 `rag_config`

Configuration runtime du pipeline RAG, stockée en JSONB (ligne unique).

| Colonne | Type | Description |
|---------|------|-------------|
| `id` | `INTEGER` PK | Toujours `1` (contrainte `CHECK (id = 1)`) |
| `config` | `JSONB` | Configuration complète (toutes les clés de `RuntimeRAGConfig`) |
| `updated_at` | `TIMESTAMP` | Date de dernière modification |
| `updated_by` | `VARCHAR(100)` | Auteur de la dernière modification |

Clés JSONB principales : `rag_version`, `v3_context_mode`, `v3_token_budget`, `v3_search_mode`, `v3_initial_top_k`, `v3_enable_selector`, `v3_generator_model`, `v3_system_prompt_name`, `llm_provider`, `embedding_model`, etc.

> Gérée via `src/rag_v3_clean/admin.py` et la page Admin Config.

---

### 2.2 `system_prompts`

Prompts système éditables utilisés par le pipeline (générateur, selector, intent).

| Colonne | Type | Description |
|---------|------|-------------|
| `name` | `VARCHAR(100)` PK | Nom du prompt (ex: `system_prompt_V6_optimized.md`) |
| `content` | `TEXT` | Contenu du prompt (supporte le placeholder `{today}`) |
| `description` | `VARCHAR(500)` | Description du prompt |
| `prompt_type` | `VARCHAR(50)` | Type : `generator`, `llm_selector`, `intent_gating` |
| `is_active` | `BOOLEAN` | Prompt actif ou désactivé (soft delete) |
| `created_at` | `TIMESTAMP` | Date de création |
| `updated_at` | `TIMESTAMP` | Date de modification |
| `updated_by` | `VARCHAR(100)` | Auteur de la modification |

> Le fallback si la DB est indisponible : fichiers locaux dans `src/rag_v3_clean/prompts/*.md`.

---

### 2.3 `acronyms`

Dictionnaire d'acronymes RH pour l'expansion de requêtes.

| Colonne | Type | Description |
|---------|------|-------------|
| `id` | `SERIAL` PK | Identifiant auto-incrémenté |
| `acronym` | `TEXT` UNIQUE | L'acronyme (ex: `RIFSEEP`, `NBI`) |
| `expansion` | `TEXT` | Expansion complète |
| `category` | `TEXT` | Catégorie (défaut : `general`) |
| `description` | `TEXT` | Description optionnelle |
| `priority` | `INTEGER` | Priorité (tri DESC pour le dictionnaire) |
| `created_at` | `TIMESTAMPTZ` | Date de création |
| `updated_at` | `TIMESTAMPTZ` | Date de mise à jour |

> Chargé en dict `{acronym: expansion}` par `get_acronym_dict()` dans le `QueryProcessor`.

---

### 2.4 `acronyms_missing`

Acronymes détectés dans les requêtes utilisateurs mais absents du dictionnaire.

| Colonne | Type | Description |
|---------|------|-------------|
| `acronym` | `TEXT` PK | L'acronyme inconnu |
| `query` | `TEXT` | Dernière requête contenant l'acronyme |
| `first_seen_at` | `TIMESTAMPTZ` | Première occurrence |
| `last_seen_at` | `TIMESTAMPTZ` | Dernière occurrence |
| `occurrence_count` | `INTEGER` | Nombre d'occurrences |
| `added_to_acronyms` | `BOOLEAN` | Marqué comme ajouté au dictionnaire |
| `notes` | `TEXT` | Notes libres |

---

## 3. Tables d'observabilité

### 3.1 `chat_runs`

Log complet de chaque interaction chatbot. Table principale d'observabilité (~120 colonnes).

#### Colonnes identifiantes

| Colonne | Type | Description |
|---------|------|-------------|
| `turn_id` | `VARCHAR` PK | Identifiant unique du tour (UUID tronqué) |
| `ts` | `TIMESTAMP` | Horodatage |
| `session_id` | `VARCHAR` | Identifiant de session |
| `conversation_id` | `VARCHAR` | Identifiant de conversation |
| `turn_index` | `INTEGER` | Index du tour dans la conversation |
| `user_group` | `VARCHAR(50)` | Groupe utilisateur (`allianceadmin`, `mattebeta`, `betatest`, `default`) |

#### Question / Réponse

| Colonne | Type | Description |
|---------|------|-------------|
| `question` | `TEXT` | Question de l'utilisateur |
| `answer` | `TEXT` | Réponse générée |
| `query_for_retrieval` | `TEXT` | Requête utilisée pour le retrieval (après enrichissement) |
| `direct_response` | `TEXT` | Réponse directe (si intent gating sans RAG) |

#### Configuration snapshot

| Colonne | Type | Description |
|---------|------|-------------|
| `rag_version` | `VARCHAR(10)` | Version RAG (`v1`, `v2`, `v3`) |
| `backend` | `VARCHAR` | Backend utilisé (ex: `rag_v3_standard`) |
| `system_prompt_name` | `VARCHAR` | Nom du prompt système |
| `top_k` | `INTEGER` | Nombre de résultats demandés |
| `use_reranker` | `BOOLEAN` | Reranker activé |
| `use_intent_gating` | `BOOLEAN` | Intent gating activé |
| `filters` | `JSONB` | Filtres appliqués |

#### V3 – Query Processing

| Colonne | Type | Description |
|---------|------|-------------|
| `v3_intent` | `TEXT` | Résultat intent (raison complète) |
| `v3_intent_name` | `VARCHAR(50)` | Nom de l'intent (`rag_query`, `chit_chat`, `out_of_scope`…) |
| `v3_intent_gating_enabled` | `BOOLEAN` | Intent gating activé |
| `v3_should_proceed` | `BOOLEAN` | Requête autorisée par l'intent gating |
| `v3_detected_theme` | `VARCHAR(50)` | Thème RH détecté |
| `v3_reformulated_query` | `TEXT` | Requête reformulée |
| `v3_was_enriched` | `BOOLEAN` | Requête enrichie avec l'historique |
| `v3_enriched_query` | `TEXT` | Requête après enrichissement (max 2000 chars) |
| `v3_acronyms_expanded` | `TEXT` | Acronymes détectés et expandés |
| `v3_needs_legal_llm` | `BOOLEAN` | Besoin de recherche juridique détecté par le LLM |
| `v3_intent_llm_response` | `TEXT` | Réponse brute du LLM intent (max 5000 chars) |

#### V3 – Retrieval & Agrégation

| Colonne | Type | Description |
|---------|------|-------------|
| `v3_chunks_retrieved_count` | `INTEGER` | Nombre de chunks récupérés |
| `v3_sections_count` | `INTEGER` | Nombre de sections agrégées |
| `v3_embedding_model` | `VARCHAR(100)` | Modèle d'embedding utilisé |
| `v3_search_mode` | `VARCHAR(20)` | Mode de recherche (`semantic`, `hybrid`, `lexical`) |
| `v3_reranker_enabled` | `BOOLEAN` | Reranker activé |
| `v3_rerank_top_k` | `INTEGER` | Top-K après reranking |
| `v3_retrieval_params` | `JSONB` | Paramètres de retrieval complets |
| `v3_chunks_before_rerank` | `JSONB` | Chunks avant reranking |
| `v3_chunks_after_rerank` | `JSONB` | Chunks après reranking |
| `v3_sections_before_rerank` | `JSONB` | Sections avant reranking |
| `v3_sections_after_rerank` | `JSONB` | Sections après reranking |
| `dist_after_rerank` | `JSONB` | Distribution par source après reranking |

#### V3 – Selector

| Colonne | Type | Description |
|---------|------|-------------|
| `v3_context_mode` | `VARCHAR(20)` | Mode de contexte (`narrow`, `standard`, `wide`) |
| `v3_selector_confidence` | `FLOAT` | Score de confiance du selector |
| `v3_selector_selected_count` | `INTEGER` | Items gardés |
| `v3_selector_decisions` | `JSONB` | Décisions détaillées (kept/removed) |
| `v3_selector_kept_indices` | `TEXT` | Indices des items gardés |
| `v3_selector_removed_indices` | `TEXT` | Indices des items retirés |
| `v3_selector_llm_response` | `TEXT` | Réponse brute du LLM selector |
| `v3_selector_fallback_used` | `BOOLEAN` | Fallback top-5 utilisé |
| `v3_selector_fallback_reason` | `TEXT` | Raison du fallback |
| `v3_source_distribution` | `JSONB` | Distribution par source après sélection |

#### V3 – Contexte

| Colonne | Type | Description |
|---------|------|-------------|
| `v3_context_items_count` | `INTEGER` | Nombre d'items de contexte |
| `v3_context_tokens` | `INTEGER` | Tokens totaux dans le contexte |
| `v3_doc_entire_count` | `INTEGER` | Documents inclus entièrement |
| `v3_context_items_summary` | `JSONB` | Résumé des items (ID, titre, score, publisher) |
| `v3_context_items_full` | `JSONB` | Items complets avec métadonnées |

#### V3 – Références juridiques

| Colonne | Type | Description |
|---------|------|-------------|
| `v3_legal_refs_total` | `INTEGER` | Total des références juridiques |
| `v3_legal_refs_from_expansion` | `INTEGER` | Références issues de l'expansion |
| `v3_legal_refs_from_dgafp` | `INTEGER` | Références résolues via DGAFP |
| `v3_legal_refs_details` | `JSONB` | Détails des références (`[{number, cid, title}]`) |

#### V3 – Génération & Prompts

| Colonne | Type | Description |
|---------|------|-------------|
| `v3_generator_prompt_name` | `VARCHAR(100)` | Nom du prompt générateur |
| `v3_full_prompt` | `TEXT` | Prompt complet envoyé au LLM (max 50k chars) |
| `v3_system_prompt_content` | `TEXT` | Contenu du prompt système (max 5000 chars) |
| `v3_response_length` | `INTEGER` | Longueur de la réponse en tokens |

#### V3 – Timing

| Colonne | Type | Description |
|---------|------|-------------|
| `total_time_ms` | `FLOAT` | Temps total |
| `pipeline_latency_ms` | `FLOAT` | Latence pipeline |
| `v3_query_processing_ms` | `INTEGER` | Temps de traitement de la requête |
| `v3_intent_ms` | `INTEGER` | Temps de classification intent |
| `v3_retrieval_ms` | `INTEGER` | Temps de retrieval |
| `v3_aggregation_ms` | `INTEGER` | Temps d'agrégation en sections |
| `v3_selector_ms` | `INTEGER` | Temps du selector |
| `v3_context_building_ms` | `INTEGER` | Temps de construction du contexte |
| `v3_generation_ms` | `INTEGER` | Temps de génération LLM |
| `v3_ttft_ms` | `INTEGER` | Time to First Token |
| `v3_chars_per_second` | `FLOAT` | Débit de génération (chars/s) |
| `v3_timing_breakdown` | `JSONB` | Breakdown complet en JSON |

**Index principaux** :
- `idx_chat_runs_rag_version` sur `rag_version`
- `idx_chat_runs_user_group` sur `user_group`
- `idx_chat_runs_group_date` sur `(user_group, ts DESC)`
- `idx_chat_runs_v3_context_mode` (partiel)
- `idx_chat_runs_selector_fallback` (partiel, WHERE TRUE)

---

### 3.2 `chat_feedbacks`

Feedbacks utilisateurs sur les réponses du chatbot + analyse LLM automatique.

| Colonne | Type | Description |
|---------|------|-------------|
| `id` | `SERIAL` PK | Identifiant auto |
| `ts` | `TIMESTAMP` | Horodatage du feedback |
| `turn_id` | `VARCHAR` | FK vers `chat_runs.turn_id` |
| `turn_idx` | `INTEGER` | Index du tour |
| `helpful` | `BOOLEAN` | Réponse utile (oui/non) |
| `reasons` | `TEXT` | Raisons (legacy) |
| `comment` | `TEXT` | Commentaire libre de l'utilisateur |
| `stars` | `INTEGER` | Note 0-4 (affichée 1-5 étoiles) |
| `reasons_positive` | `TEXT` | Raisons positives |
| `reasons_negative` | `TEXT` | Raisons négatives |
| `session_id` | `VARCHAR` | ID de session |
| `question` | `TEXT` | Question (dénormalisée pour analyse) |
| `answer` | `TEXT` | Réponse (dénormalisée) |
| `beta_scope` | `VARCHAR(10)` | Périmètre beta-test (`Oui`/`Non`) |
| `error_category` | `VARCHAR` | Catégorie d'erreur (analyse LLM automatique) |
| `ai_reason` | `TEXT` | Explication de l'erreur par le LLM |
| `ai_analyzed_at` | `TIMESTAMP` | Date de l'analyse LLM |

Catégories d'erreur possibles : `retrieval_issue`, `generator_hallucination`, `generator_incomplete`, `generator_wrong_interpretation`, `missing_document`, `chunk_quality`, `selector_misunderstanding`, `selector_wrong_priority`, `other`.

> L'analyse LLM est déclenchée par `feedback_analyzer.py` pour les feedbacks ≤ 3 étoiles.

---

### 3.3 `chat_reviews`

Suivi de la revue manuelle des conversations par l'équipe.

| Colonne | Type | Description |
|---------|------|-------------|
| `turn_id` | `VARCHAR` PK | FK vers `chat_runs.turn_id` |
| `reviewed` | `BOOLEAN` | Marqué comme revu |
| `question` | `TEXT` | Question (dénormalisée) |
| `answer` | `TEXT` | Réponse (dénormalisée) |
| `notes` | `TEXT` | Notes de revue |
| `ts` | `TIMESTAMP` | Horodatage |
| `updated_at` | `TIMESTAMP` | Date de mise à jour |

---

## 4. Tables d'évaluation

### 4.1 `goldset_questions_v2`

Questions d'évaluation avec réponses de référence pour mesurer la qualité du RAG.

| Colonne | Type | Description |
|---------|------|-------------|
| `id` | `SERIAL` PK | Identifiant auto |
| `question` | `TEXT` UNIQUE | Question d'évaluation |
| `gold_answer` | `TEXT` | Réponse de référence |
| `gold_sources` | `TEXT` | Sources attendues (short_id des documents) |
| `theme` | `VARCHAR` | Thème RH |
| `source` | `VARCHAR` | Origine (`manual`, `user`, `synthetic`) |
| `goldset_name` | `VARCHAR` | Nom du goldset (`beta_evaluated`, `auto_enriched`…) |
| `comment` | `TEXT` | Commentaire |
| `original_turn_id` | `VARCHAR` | Lien vers le `chat_runs.turn_id` d'origine |
| `difficulty` | `VARCHAR(20)` | Difficulté (`easy`, `medium`, `hard`) — CHECK constraint |
| `tags` | `TEXT[]` | Tags de capacité (array PostgreSQL) |
| `embedding_albert` | `vector(1024)` | Embedding Albert pré-calculé pour la question |
| `created_at` | `TIMESTAMPTZ` | Date de création |
| `updated_at` | `TIMESTAMPTZ` | Date de mise à jour |

**Index** :
- `idx_goldset_questions_difficulty` sur `difficulty`
- `idx_goldset_questions_tags` GIN sur `tags`
- `idx_goldset_questions_v2_embedding_albert` IVFFlat sur `embedding_albert` (cosine, lists=10)

---

### 4.2 `goldset_runs`

Résultats d'exécution du pipeline sur les questions goldset (référencée par `goldset_questions_v2`).

| Colonne | Type | Description |
|---------|------|-------------|
| `question_id` | `INTEGER` FK | FK vers `goldset_questions_v2.id` |
| … | | (détails spécifiques aux runs d'évaluation) |

---

### 4.3 `intent_eval_goldset`

Dataset annoté pour évaluer le module Intent Gater.

| Colonne | Type | Description |
|---------|------|-------------|
| `id` | `SERIAL` PK | Identifiant auto |
| `created_at` | `TIMESTAMPTZ` | Date de création |
| `question` | `TEXT` | Question à classifier |
| `conversation_history` | `JSONB` | Historique de conversation (`[{role, content}]`) |
| `expected_intent` | `VARCHAR(30)` | Intent attendu (`rag_query`, `chit_chat`, `out_of_scope`…) |
| `expected_theme` | `VARCHAR(50)` | Thème attendu (null pour non-RAG) |
| `expected_needs_legal` | `BOOLEAN` | Besoin de recherche juridique attendu |
| `expected_reformulated_query` | `TEXT` | Reformulation attendue (follow_up) |
| `category` | `VARCHAR(30)` | Catégorie du test (`normal`, `red_teaming`, `follow_up`, `ambiguous`) |
| `source` | `VARCHAR(50)` | Source (`manual`, `chat_runs`, `synthetic`, `goldset_v2`) |
| `source_id` | `TEXT` | Référence à l'original |
| `tags` | `TEXT[]` | Tags |
| `notes` | `TEXT` | Notes |

**Index** : `idx_intent_eval_category`, `idx_intent_eval_expected_intent`, `idx_intent_eval_tags` (GIN)

---

### 4.4 `intent_eval_experiments`

Résultats d'expériences d'évaluation de l'intent gater.

| Colonne | Type | Description |
|---------|------|-------------|
| `id` | `SERIAL` PK | Identifiant auto |
| `created_at` | `TIMESTAMPTZ` | Date |
| `name` | `TEXT` | Nom de l'expérience |
| `description` | `TEXT` | Description |
| `model` | `TEXT` | Modèle LLM testé |
| `prompt_name` | `TEXT` | Prompt utilisé |
| `n_questions` | `INTEGER` | Nombre de questions testées |
| `category_filter` | `TEXT[]` | Filtre de catégories |
| `intent_accuracy` | `FLOAT` | Précision de classification d'intent |
| `theme_accuracy` | `FLOAT` | Précision de détection de thème |
| `results_detail` | `JSONB` | Résultats par question |
| `confusion_matrix` | `JSONB` | Matrice de confusion |
| `total_time_seconds` | `FLOAT` | Temps total |

---

### 4.5 `pipeline_eval_experiments`

Résultats d'évaluation du pipeline complet (retrieval + génération).

| Colonne | Type | Description |
|---------|------|-------------|
| `id` | `SERIAL` PK | Identifiant auto |
| `created_at` | `TIMESTAMPTZ` | Date |
| `name` | `TEXT` | Nom de l'expérience |
| `theme` | `TEXT` | Thème auto-détecté (`LLM_Selector`, `Embeddings`, `Reranker`…) |
| `description` | `TEXT` | Notes |
| `n_questions` | `INTEGER` | Nombre de questions |
| `goldset_names` | `TEXT[]` | Goldsets utilisés |
| `tag_filter` | `TEXT[]` | Tags filtrés |
| `publisher_filter` | `TEXT` | Filtre par publisher |
| `configs` | `JSONB` | Configurations testées |
| `aggregate` | `JSONB` | Métriques agrégées par config (`{recall@1, mrr, …}`) |
| `per_question` | `JSONB` | Détail par question (nullable) |
| `best_config` | `TEXT` | Config avec le meilleur MRR |
| `best_mrr` | `FLOAT` | Meilleur MRR |
| `total_time_seconds` | `FLOAT` | Temps total |

**Index** : `idx_eval_experiments_theme`, `idx_eval_experiments_created` (DESC)

---

### 4.6 `retrieval_eval_runs`

Résultats de comparaison de configurations de retrieval.

| Colonne | Type | Description |
|---------|------|-------------|
| `id` | `SERIAL` PK | Identifiant auto |
| `run_name` | `VARCHAR(200)` | Label de l'exécution |
| `goldset_name` | `VARCHAR(100)` | Goldset évalué |
| `table_preset` | `VARCHAR(100)` | Preset de tables |
| `tables_used` | `JSONB` | Tables utilisées |
| `config` | `JSONB` | Config complète |
| `n_questions` | `INTEGER` | Nombre de questions |
| `total_elapsed_s` | `FLOAT` | Temps total |
| `raw_distribution` | `JSONB` | Distribution brute par source |
| `reranked_distribution` | `JSONB` | Distribution après reranking |
| `selected_distribution` | `JSONB` | Distribution après sélection |
| `n_questions_with_gold` | `INTEGER` | Questions avec gold_sources |
| `raw_recall` | `FLOAT` | Recall brut |
| `reranked_recall` | `FLOAT` | Recall après reranking |
| `selected_recall` | `FLOAT` | Recall après sélection |
| `question_details` | `JSONB` | Détail par question |
| `chunk_details` | `JSONB` | Métadonnées des chunks par question |
| `created_at` | `TIMESTAMP` | Date |

**Index** : `idx_retrieval_eval_runs_goldset`, `idx_retrieval_eval_runs_created` (DESC)

---

## 5. Colonnes pgvector

Récapitulatif des colonnes utilisant l'extension `pgvector` :

| Table | Colonne | Dimensions | Modèle |
|-------|---------|-----------|--------|
| `rag_chunks_matte` | `embedding_m3` | 1024 | Albert (BAAI/bge-m3) |
| `rag_chunks_matte` | `embedding_bge_scw` | 3584 | BGE Multilingual Gemma2 (Scaleway) |
| `rag_chunks_service_public` | `embedding_m3` | 1024 | Albert |
| `rag_chunks_service_public` | `embedding_bge_scw` | 3584 | BGE Scaleway |
| `rag_chunks_dgafp` | `embedding_m3` | 1024 | Albert |
| `rag_chunks_dgafp` | `embedding_bge_scw` | 3584 | BGE Scaleway |
| `rag_chunks_rgrh` | `embedding_m3` | 1024 | Albert |
| `rag_chunks_rgrh` | `embedding_bge_scw` | 3584 | BGE Scaleway |
| `rag_chunk_embeddings` | `embedding_raw` | 1024 | Albert |
| `rag_chunk_embeddings` | `embedding_bge` | 3584 | BGE Scaleway |
| `goldset_questions_v2` | `embedding_albert` | 1024 | Albert |

L'opérateur de distance cosinus `<=>` est utilisé pour la recherche sémantique. Le score de similarité est calculé comme `1 - (a <=> b)`.
