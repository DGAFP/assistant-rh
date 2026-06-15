# Audit approfondi — code & schéma DB

> Dossier d'audit : voir [README](README.md). Date : 2026-06-09.
> Méthode : analyse statique du code (croisement colonnes écrites/lues, motifs d'erreurs) + introspection du schéma sur la base locale (copie staging) : `information_schema`, `pg_constraint`, `pg_indexes`, tailles réelles.
> Objet : anti-patterns, conception DB faible, erreurs silencieuses critiques, et métriques critiques absentes à remonter dans Scaleway/Cockpit. Approfondit la note [02](02_ARCHITECTURE_AUDIT_2026-06.md) A6 et alimente la note [05](05_PLAN_AUDIT_ET_COUVERTURE.md) D16.

---

## 1. `chat_runs` — anti-patterns de table de log

**Chiffres (base locale)** : **154 colonnes**, 51 Mo pour 3 058 lignes (**~17 Ko/ligne** ; snapshot du 09/06 légèrement postérieur aux 3 054 lignes de la note 01), 1 seule contrainte (PK), **0 clé étrangère**, **14 index**. **Vérifié (2026-06-15, staging réel)** : **156 colonnes**, 52 Mo pour 3 083 lignes, 1 PK, **0 FK**, 14 index — tous les ordres de grandeur tiennent.

### 1.1 Colonnes provisionnées et jamais écrites (le point le plus grave)
Le logger écrit ses lignes via deux builders ; la table en a **154** (156 sur staging). **~33 colonnes existaient en base mais n'étaient jamais alimentées par le code** dans le décompte au 2026-06-09, avant l'ajout de `v3_reranker_status` écrite par #88. **Précision (2026-06-15)** : `build_log_row` (chemin RAG) écrit **79 colonnes**, `build_non_rag_row` ~80 ; l'union des deux ≈ 123 colonnes distinctes — d'où ~33 colonnes jamais écrites sur 156 (le « 126 » initial conflait les deux builders ; le fond — ~33 colonnes mortes — tient). Et ce ne sont pas des reliquats anodins — ce sont précisément **des colonnes de diagnostic** :

```
v3_chunks_raw, v3_sections_raw, v3_chunks_before_rerank, v3_chunks_after_rerank,
v3_context_before_selector, v3_selector_input_items, v3_top1_score, v3_avg_chunk_score,
v3_avg_section_score, v3_retrieval_params, v3_aggregation_params, v3_dgafp_level,
v3_need_more_context, v3_missing_topics, v3_escalation_tier, v3_selector_fallback_used …
```

**Conséquence directe** : l'observabilité du retrieval (les chunks à chaque étape, les scores réels, l'état du selector/escalade) a été **conçue dans le schéma puis jamais câblée**. C'est l'explication mécanique de symptômes relevés ailleurs :
- la note 01 notait « `v3_top1_score` médian = 0, peu fiable » → en réalité **la colonne n'est jamais écrite** ;
- les traces bout-en-bout de la note 01 §3 ont dû être reconstituées à la main → les colonnes qui les contiendraient (`v3_chunks_raw`, `v3_chunks_before/after_rerank`) sont vides ;
- l'`error_category` des feedbacks ne peut pas être recoupée avec les chunks réellement vus, faute de persistance.

C'est exactement le « certains trucs ne remontent pas en DB » : le besoin a été identifié à la conception (les colonnes sont là) mais l'écriture n'a jamais suivi.

### 1.2 Colonnes lues uniquement par les pages de debug
Sur les ~123 colonnes écrites (union des deux builders), une large part n'est **relue que par les pages Streamlit admin/debug** (affichage), jamais par du code applicatif ni de la conformance. Beaucoup correspondent à des **features de pipeline retirées (v2)** que le logger remplit encore : `hyde_document`, `use_hyde`, `cascade_source`, `rewritten_query`, `sparse_method`, `pick_mode`, `chunks_before_pick`/`chunks_after_pick`, `boost_weights`… (écrites par `build_non_rag_row` / `_prepare_data`, pas par `build_log_row`). On paie l'écriture et le stockage de concepts morts à chaque tour.

### 1.3 Blobs inline et largeur de ligne
21 colonnes `jsonb` + 47 `text` + 21 `varchar`. Des prompts complets sont stockés en clair par ligne (`v3_full_prompt`, `v3_system_prompt_content`, `system_prompt`, `prompt`, `retrieved`, `sources_used_content`) → ~17 Ko/ligne, table qui gonfle linéairement sans rétention (cf. note 02 S5, RGPD). Ces blobs devraient être tronqués, déportés, ou référencés.

### 1.4 Index inutiles sur une table d'écriture
14 index, dont plusieurs sur des booléens / faible cardinalité ou des features mortes : `idx_chat_runs_use_hyde` (feature retirée), `idx_chat_runs_use_query_rewriting`, `idx_chat_runs_v3_need_more_context` (colonne… jamais écrite), `idx_chat_runs_v3_escalation_tier` (jamais écrite), `idx_chat_runs_selector_fallback`, `idx_chat_runs_retrieval_mode`. Indexer des booléens peu sélectifs sur une table **principalement en écriture** ajoute du coût d'INSERT sans bénéfice de lecture (les analyses se font par `ts`/`user_group`, déjà indexés).

---

## 2. Schéma DB — conception faible

### 2.1 Aucune intégrité référentielle
**0 FK sur `chat_runs`** ; à l'échelle de toute la base, **3 FK seulement** (vérifié staging 2026-06-15 ; la note disait 30 — la copie locale en portait davantage), aucune ne relie la chaîne documentaire `rag_chunks_* → rag_sections → rag_documents`. Or `rag_chunks_matte.section_id` et `rag_sections.section_id` sont **tous deux `uuid`** : la FK est techniquement possible, elle est juste absente. Résultat concret déjà observé (note 01) : des chunks pointant des sections inexistantes, des documents sans chunks, **sans aucun garde-fou en base** — l'intégrité repose entièrement sur la discipline des jobs d'ingestion, qui a déjà échoué.

### 2.2 Index vectoriels manquants sur les tables réellement interrogées *(perf + coût)*
Le retriever fait une recherche pgvector (`embedding_m3 <=> query`) sur matte, service_public, dgafp, rgrh, **en parallèle, à chaque requête**. Or :

| Table interrogée | Lignes avec emb m3 (staging 15/06) | Index vectoriel | Effet |
|---|---|---|---|
| `rag_chunks_service_public` | **2 782 / 2 782** | ✅ ivfflat m3 | ok |
| `rag_chunks_matte` | **959 / 959** | ❌ | **scan séquentiel** (le seul vrai cas) |
| `rag_chunks_dgafp` | **0 / 3 992** | ❌ | **invisible en sémantique** — 0 embedding, l'index est un faux sujet, il faut **backfiller** |
| `rag_chunks_rgrh` | **178 / 324** (146 NULL) | ❌ | scan séquentiel (petit) + 45 % exclus faute d'embedding |

3 des 4 tables n'ont **aucun index vectoriel**. **Correction (2026-06-15, staging réel)** : le vrai cas de scan séquentiel coûteux est **MATTE** (959 vecteurs m3 pleins, sans index). **DGAFP n'a pas un problème d'index mais d'embedding : 0/3 992** — la clause `WHERE embedding_m3 IS NOT NULL` du retriever l'exclut entièrement, donc rien à scanner et rien à indexer tant que les embeddings ne sont pas backfillés. RGRH est petit (324) mais 45 % sans embedding. Le coût se dégradera linéairement avec le multi-ministère (prio P2). Paradoxe confirmé : un index ivfflat existe sur `rag_chunks_mso` (1 262 vecteurs m3 pleins)… une table que le retriever n'interroge même pas — et un index HNSW existe sur le fantôme `rag_chunks_dgafp_scalingo`, là où vivent les embeddings DGAFP absents de la table vive.

### 2.3 Prolifération de colonnes d'embedding majoritairement NULL
matte : 5 colonnes d'embedding ; rgrh : 4 ; dgafp : 3. Plusieurs modèles coexistent (`embedding_m3`, `embedding_bge_scw`, `embedding_qwen3`, `embedding_ctx`, `embedding_bge`) et la plupart sont NULL (vérifié staging 15/06 : 762 NULL bge matte, 146 m3 rgrh ; **DGAFP : les 3 colonnes — m3, bge_scw, qwen3 — sont à 3 992/3 992 NULL**, corpus entièrement non embeddé). Stockage gaspillé (un vecteur 1024-dim ≈ 4-8 Ko), schéma ambigu (laquelle fait foi ?), et risque fonctionnel : le fallback embeddings BGE-Scaleway lit une colonne aux trous (note 01 C4).

### 2.4 Tables fantômes dupliquées *(legacy Scalingo/scw)*
La base porte des doublons complets de migration, avec données **et** index :

```
rag_chunks_dgafp_scalingo : 3 992   rag_chunks_dgafp_scw : 3 992   (= duplicata de dgafp)
rag_chunks_legifrance : 429   _scalingo : 429   _scw : 429
rag_chunks_service_public_scalingo : 1 140
```

`AGENTS.md` indique que Scalingo est retiré ; ces tables sont du poids mort (stockage, confusion, risque de requêter la mauvaise). Couplé à la non-gouvernance du schéma (note 02 A4 : seulement 2 migrations pour ~22 tables), personne ne sait quelle table fait référence sans lire le code.

---

## 3. Erreurs silencieuses critiques — tout échoue « fail-open »

Motif systémique : chaque garde-fou, en cas d'échec, **se dégrade silencieusement et ne remonte aucune métrique**. ~60 `except` dans le pipeline, dont les plus critiques :

| Lieu | Comportement en cas d'échec | Pourquoi c'est critique |
|---|---|---|
| `context_selector.py:192` | selector échoue → **garde toutes les sections** | Le filtre anti-hallucination tombe ouvert : du contexte non pertinent passe à la génération, sans trace |
| `section_aggregator.py:229-231` | rerank échoue → **ordre d'origine conservé** | La panne #87/#88 (422) est restée invisible des mois ; depuis #88 le statut est persisté (`v3_reranker_status`), l'alerte reste à créer |
| `embedder.py:82,106` | embedding échoue → **`return None`** → retriever `return []` | Question sans aucun résultat, vécue comme « no-answer » par l'utilisateur, loggée en `warning` |
| `retriever.py:271-272` | une table échoue dans le ThreadPool → **résultat partiel** | `rag_chunks_test` absente avalée ; le recall chute sans signal |
| `retriever.py:903` | `rag_chunks_test` KO → **warning + continue** | Idem : table activée en config mais absente = non-événement |
| `retriever.py:407` | introspection de colonnes échoue → **warning** | Peut désactiver silencieusement hybrid/heading sur une table |

Aucun de ces chemins n'incrémente de compteur ni n'émet d'événement structuré. La dégradation est invisible jusqu'à ce qu'un utilisateur se plaigne — ce qui est exactement ce qui s'est produit.

---

## 4. Métriques critiques absentes — à remonter dans Scaleway/Cockpit

Ce qui devrait être mesuré et alerté (cf. note [03](03_RAG_OBSERVABILITY_ROADMAP_2026-06.md)), et la source pour chaque métrique :

| Métrique critique | Source / comment | Alerte cible |
|---|---|---|
| Taux d'échec **rerank** | agrégation de `v3_reranker_status` + alerte sur le chemin fail-open du `section_aggregator` | > 1 % |
| Taux d'échec / fallback **embeddings** (Albert→Scaleway) | `embedder.py` circuit breaker | tout déclenchement |
| Taux de fallback / échec **LLM** (Albert→Scaleway) | `FallbackLLMClient` | tendance |
| **Selector fail-open** (toutes sections gardées par défaut) | `context_selector.py:192` | tout déclenchement |
| **Table de retrieval vide / partielle** (ex. chunks_test absente) | `retriever.py` ThreadPool | tout déclenchement |
| Taux de **no-answer** | réponse vs `should_proceed` | dérive |
| **Latence P50/P95/P99 par étage** | colonnes `v3_*_ms` (déjà écrites) | régression |
| **Santé index retrieval** (scan séquentiel vs index) | `pg_stat_user_tables` / EXPLAIN | seq_scan en hausse |
| **Scores de pertinence réels** (top1, moyenne) | colonnes `v3_top1_score`/`v3_avg_chunk_score` — **à câbler d'abord** (§1.1) | effondrement |
| **Coût en tokens** par requête/étage | à instrumenter (non mesuré) | budget |

Note : plusieurs de ces métriques ont déjà une **colonne dédiée non écrite** (§1.1) — le premier travail n'est pas d'ajouter des colonnes mais de **câbler celles qui existent**, puis de les agréger dans Cockpit/Grafana.

---

## 5. Recommandations priorisées

| # | Action | Gain | Effort |
|---|---|---|---|
| 1 | **Câbler les colonnes de diagnostic déjà présentes** (`v3_top1_score`, `v3_chunks_before/after_rerank`, `v3_chunks_raw`…) ou les déporter vers une table d'événements de trace | Débloque l'observabilité du retrieval sans changer le schéma | S–M |
| 2 | **Backfill embeddings DGAFP (0/3 992) puis RGRH (146/324)**, ensuite **créer les index vectoriels** (ivfflat/hnsw, m3) sur matte, dgafp, rgrh | Recall (DGAFP éteint) + latence/coût CPU, prérequis multi-ministère | M (backfill) + S (index) |
| 3 | **Compteurs sur les chemins fail-open** (selector, rerank, embedder, table vide) → alertes Cockpit | Fin des pannes silencieuses | S–M |
| 4 | **Rationaliser `chat_runs`** : supprimer colonnes mortes (v2/jamais écrites), retirer les index inutiles, tronquer/déporter les blobs de prompt | Schéma lisible, écritures allégées, moins de stockage/RGPD | M |
| 5 | **Ajouter les FK** rag_chunks→rag_sections→rag_documents (ou un check de réconciliation en CI) | Intégrité référentielle, détecte les trous d'index (note 01) | S–M |
| 6 | **Supprimer les tables fantômes** `_scalingo`/`_scw` après vérification | Lève l'ambiguïté, allège la base | S |
| 7 | **Consolider les colonnes d'embedding** (garder le modèle actif, archiver le reste) | Stockage, clarté du schéma | M |
| 8 | **Tout sous migrations versionnées** (note 02 A4) ; interdire le DDL hors migration | Gouvernance, fin de la dérive | M |

Les actions 1-3 sont des quick wins à fort effet (observabilité + perf) et s'intègrent à la Phase 0/1 de la note 01 ; les 4-8 relèvent du chantier D16 (note 05).

## Sources

- Code : `packages/rag-pipeline/src/assistant_rh_rag_pipeline/` (`chat_logger.py`, `retriever.py`, `reranker.py`, `embedder.py`, `context_selector.py`), pages `apps/streamlit-ui/pages/`.
- Schéma : base locale (copie staging) — `information_schema.columns`, `pg_constraint`, `pg_indexes`, `pg_total_relation_size`, comptages réels (2026-06-09).
- Notes liées : [01](01_RAG_QUALITY_AUDIT_2026-06.md), [02](02_ARCHITECTURE_AUDIT_2026-06.md) A6, [03](03_RAG_OBSERVABILITY_ROADMAP_2026-06.md), [05](05_PLAN_AUDIT_ET_COUVERTURE.md) D16.
