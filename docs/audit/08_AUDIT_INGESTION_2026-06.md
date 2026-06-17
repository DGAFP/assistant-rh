# Audit de l'ingestion — état du pipeline vs. références 2025/2026

> Dossier d'audit : voir [README](README.md). Note précédente : [07 — Vérification staging](07_VERIFICATION_STAGING_ET_PRIORISATION.md).
> Date : 2026-06-17. Périmètre : `apps/data-ingestion-cli/` + `packages/data-engineering/` (Service-Public, Légifrance, MATTE/RGRH à venir, embeddings, ingestion DB), vu sous l'angle de la SOTA RAG 2025/2026. Cartographie de code vérifiée par lecture des modules ; références externes citées en §7.

---

## 1. Synthèse

L'architecture d'ingestion repose sur des **bases saines** (médaillon bronze/silver/gold, séparation CLI/jobs/pipeline, hashes déterministes, fallback providers) qui n'ont pas besoin d'être remplacées. Mais elle accuse un **retard d'~12 mois** sur les pratiques convergentes du domaine en 5 points précis :

1. **Pas de Contextual Retrieval** (Anthropic, sept. 2024) — devenu baseline ; gain documenté de 35 % seul / 49 % avec BM25 / 67 % avec reranker sur le taux d'échec top-20.
2. **Chunking sliding-window sur Légifrance** alors que la SOTA légale 2025 est structure-aware article-level.
3. **Pas de suivi incrémental** façon `RecordManager` — chaque run est un full reprocess, les orphelins ne sont pas nettoyés.
4. **Table-par-source** devenu antipattern depuis pgvector 0.8 (oct. 2024) et son filtrage HNSW itératif.
5. **Pas de versioning d'embeddings ni de quality gate Ragas** en CI.

Aucune de ces dettes n'est bloquante — le système fonctionne — mais chacune coûte en qualité de retrieval, en compute ou en agilité opérationnelle. **Trois actions tirent ~80 % du gain** : prepend Contextual Retrieval, chunking article-level Légifrance, record manager content-hashé. Voir §6 pour la priorisation P0→P3.

Cet audit ne couvre **pas** la qualité ranking (note [01](01_RAG_QUALITY_AUDIT_2026-06.md)), l'observabilité runtime (note [03](03_RAG_OBSERVABILITY_ROADMAP_2026-06.md)) ni le schéma `chat_runs` (note [06](06_AUDIT_CODE_ET_DB.md)). Il complète l'audit architectural [02](02_ARCHITECTURE_AUDIT_2026-06.md) sur l'axe ingestion exclusivement.

---

## 2. État des lieux

### 2.1 Cartographie

Le pipeline est un **ETL batch médaillon hand-rolled**, sans framework de DAG.

```
Source HTTP / tarball
  → Bronze (raw XML/JSON sur disque, manifest par run)
  → Silver (parsing XML, splitter v2 hiérarchique, UUID v5 stables)
  → Gold  (chunking + embeddings M3 local + BGE Scaleway, JSONL/Parquet)
  → Object Storage Scaleway (prefixes staging/prod, sync via aws s3 CLI)
  → PostgreSQL pgvector (UPSERT ON CONFLICT hash_id)
```

| Couche | Implémentation principale | Notes |
|---|---|---|
| CLI | [apps/data-ingestion-cli/src/assistant_rh_data_ingestion_cli/main.py](apps/data-ingestion-cli/src/assistant_rh_data_ingestion_cli/main.py) | Registre statique de 9 commandes, import dynamique de modules `jobs/*` |
| Service-Public Bronze | [packages/data-engineering/src/assistant_rh_data_engineering/service_public/bronze.py](packages/data-engineering/src/assistant_rh_data_engineering/service_public/bronze.py) | Fetch ZIP data.gouv.fr, fallback URL hardcodée |
| Service-Public Silver | [service_public/silver.py](packages/data-engineering/src/assistant_rh_data_engineering/service_public/silver.py) + [section_splitter.py](packages/data-engineering/src/assistant_rh_data_engineering/service_public/section_splitter.py) | `splitter_v2`, sections hiérarchiques par heading + FIGURE_TEXT |
| Service-Public Gold | [service_public/gold.py](packages/data-engineering/src/assistant_rh_data_engineering/service_public/gold.py) + [qna_chunking.py](packages/data-engineering/src/assistant_rh_data_engineering/service_public/qna_chunking.py) | Chunking QNA pattern-matching (Q:/A:), embeddings M3 + BGE |
| Légifrance Bronze | [legifrance/bulk_dump.py](packages/data-engineering/src/assistant_rh_data_engineering/legifrance/bulk_dump.py) + [bronze.py](packages/data-engineering/src/assistant_rh_data_engineering/legifrance/bronze.py) | DILA LEGI bulk dump (full + deltas) |
| Légifrance Gold | [legifrance/gold.py](packages/data-engineering/src/assistant_rh_data_engineering/legifrance/gold.py) | `split_legal_chunks(text, max=1200, min=350, overlap=100)` |
| MATTE / RGRH | — | Tables exposées au retrieval mais ingestion **non implémentée** en jobs |
| Embeddings backfill | [jobs/embeddings_backfill.py](packages/data-engineering/src/assistant_rh_data_engineering/jobs/embeddings_backfill.py) | Manifest-driven, retry/backoff 6 tentatives, ThreadPoolExecutor |
| Health export | [jobs/rag_health_exporter.py](packages/data-engineering/src/assistant_rh_data_engineering/jobs/rag_health_exporter.py) | Serveur Prometheus, métriques chunk/section/doc par source |

Storage côté DB : 6 tables `rag_chunks_*` (service_public, dgafp legacy, legifrance, matte, rgrh, test) + `rag_documents` + `rag_sections` + index partiels uniques sur `(short_id)` et `(doc_id, section_index)`.

### 2.2 Chunking actuel

| Source | Stratégie | Paramètres |
|---|---|---|
| Service-Public | QNA pattern matching, heading Q/R + parent SHA1, rôle question/answer | Pas de taille fixe — découpe sur la structure QNA |
| Légifrance | Sliding-window paragraphe, merge des petits, hard wrap | `max=1200`, `min=350`, `overlap=100` chars ; option `single_chunk_per_article=True` par défaut |
| MATTE / RGRH | N/A | — |

### 2.3 Idempotence et déduplication

- **Hash chunk déterministe** : `hash_id = sha1(source_name | qa_id | role | chunk_index | text[:256])` ([service_public/gold.py:68-77](packages/data-engineering/src/assistant_rh_data_engineering/service_public/gold.py)). Même algorithme côté Légifrance ([legifrance/db.py:88-90](packages/data-engineering/src/assistant_rh_data_engineering/legifrance/db.py)).
- **UPSERT** par `hash_id` (chunks), `short_id` ou `(doc_id, section_index)` (sections/docs) — [service_public/db.py:154-198](packages/data-engineering/src/assistant_rh_data_engineering/service_public/db.py).
- **Aucune table `ingestion_runs`** ni colonne `model_version`/`embedded_at` sur les chunks.
- **Aucun delete-then-upsert** pour fiches supprimées en amont : les chunks orphelins persistent silencieusement.
- **Le bronze fetch ne fait pas de conditional GET** (`If-None-Match` / `Last-Modified`) : chaque run télécharge la ZIP/tarball complet.

---

## 3. Points forts (à conserver)

- **Médaillon explicite** : aligné sur les références Unstructured / Databricks / LlamaIndex. Replay et debug facilités par les artefacts JSONL/Parquet sur disque.
- **Séparation CLI ↔ jobs ↔ pipeline classes** : les jobs sont réutilisables Docker / Scaleway Serverless / Mastra futur (cf. note [02 §A1](02_ARCHITECTURE_AUDIT_2026-06.md)).
- **Hash déterministe** : c'est exactement ce que fait `RecordManager` de LangChain en interne. La fondation pour le tracking incrémental est déjà là.
- **Pluggable embedders** (M3 local + BGE Scaleway via interface `BaseBatchEmbedder`) — facilite l'ajout futur de Qwen3-Embedding.
- **Health exporter Prometheus** — la plupart des projets équivalents n'en ont pas.
- **Retry/backoff sur l'API d'embedding** ([jobs/embeddings_backfill.py:94-130](packages/data-engineering/src/assistant_rh_data_engineering/jobs/embeddings_backfill.py)) : 6 tentatives, distingue 429/5xx transient vs 4xx final.

---

## 4. Écarts par rapport aux références 2025/2026

### 4.1 Pas de Contextual Retrieval (impact retrieval majeur)

**Constat.** Le pipeline embedde le texte brut du chunk. Aucun résumé de document n'est préfixé avant embedding ; aucun contexte amont n'est injecté.

**Référence.** [Anthropic — Contextual Retrieval (sept. 2024)](https://www.anthropic.com/news/contextual-retrieval) : préfixer 50-100 tokens de contexte généré par LLM réduit l'échec top-20 de **35 % seul, 49 % combiné BM25, 67 % avec rerank ajouté**. Coût mesuré ~1 USD/M tokens document avec prompt caching. C'est devenu **table stakes** dans les stacks RAG sérieuses ; ECIR 2025 ([arXiv 2504.19754](https://arxiv.org/abs/2504.19754)) confirme qu'il préserve mieux la cohérence sémantique que le late chunking.

**Pour ce projet.** Albert est déjà disponible pour générer les résumés — pas de provider supplémentaire à câbler. L'implémentation va dans le builder gold (un appel par document, caché sur le `text_hash` calculé en silver), avec préfixage du chunk avant chaque embedder.

**Coût d'implémentation.** ~1 sprint pour Service-Public, idem pour Légifrance. Backfill : peut être incrémental en ne re-embeddant que les chunks dont le préfixe a changé (cf. §4.5).

### 4.2 Légifrance : chunking sliding-window au lieu d'article-level

**Constat.** [`split_legal_chunks(text, max_chars=1200, min_chars=350)`](packages/data-engineering/src/assistant_rh_data_engineering/legifrance/gold.py) découpe par paragraphes `\n\n`, sans aligner les frontières sur les marqueurs `Article L.` / `R.` / `D.`. Le helper [`normalize_article_number()`](packages/data-engineering/src/assistant_rh_data_engineering/legifrance/helpers.py) existe déjà mais n'est pas utilisé dans le découpage.

**Référence.** [arXiv 2510.06999 (oct. 2025) — Towards Reliable Retrieval in RAG for Large Legal Datasets](https://arxiv.org/html/2510.06999v1) : Summary-Augmented Chunking (split récursif 500 chars + résumé document 150 chars) **divise par 2 le Document-Level Retrieval Mismatch** sur ContractNLI. Pour les codes français, le motif standard est un split aligné sur `Article L./R./D.` (cf. [HamzaG737/legal-code-rag](https://github.com/HamzaG737/legal-code-rag)). À noter : les **résumés génériques battent les résumés "expert légal"** — les cues sémantiques larges généralisent mieux que la dense legalese.

**Pour ce projet.** L'article a déjà une identité (`article_id`, `num_article`, `cid`). Aligner la chunk boundary sur l'article est trivial et préserve la citation graph (qui est précisément ce sur quoi BM25 brille en légal — voir §4.4). À combiner avec le résumé document de §4.1.

### 4.3 Pas de tracking incrémental façon RecordManager

**Constat.** Chaque run médaillon retraite toute la source (sauf filtrage explicite par `--fiche-id`). Pas de table de runs, pas de delete des orphelins, pas d'audit du diff run-over-run. Le bronze fetch ne fait pas non plus de conditional GET.

**Référence.** [LangChain `index()` + `RecordManager`](https://python.langchain.com/docs/how_to/indexing/) reste le pattern canonique (maintenant `langchain-classic` — maturité, pas dépréciation). Stocke `(content_hash, source_id, write_time)` ; quatre modes cleanup standards : `None | incremental | scoped_full | full`. Équivalent LlamaIndex : [`IngestionPipeline` + `DocstoreStrategy.UPSERTS`](https://developers.llamaindex.ai/python/examples/ingestion/document_management_pipeline/).

**Pour ce projet.** Implémentable en pur SQL sans dépendance externe : table `ingestion_records(source, source_id, content_hash, embedding_model_version, embedded_at, is_active)` + flag `--cleanup-mode` sur les jobs médaillon. À coupler avec `If-None-Match`/`ETag` côté bronze pour court-circuiter les downloads inutiles ; data.gouv.fr et DILA exposent les deux headers. Coût : ~1 semaine de dev. Bénéfice opérationnel : un run no-op (même source, même config) devient quasi gratuit ; les fiches supprimées en amont sont marquées `is_active=false` (tombstones) ; audit en clair de ce qui bouge à chaque run.

### 4.4 Table-par-source : devenu antipattern

**Constat.** 6 tables `rag_chunks_*` distinctes (service_public, dgafp legacy, legifrance, matte, rgrh, test). Index HNSW, schéma, RLS et chemin de requête dupliqués par source. Le retriever fan-out en parallèle sur les tables ([rag-pipeline/retriever](packages/rag-pipeline/)) — c'est une union explicite qui mime ce que ferait un `WHERE source = ANY(...)` sur une table unique.

**Référence.** Consensus 2025/2026 ([Supabase](https://supabase.com/docs/guides/ai/hybrid-search), [Nile multi-tenant RAG](https://www.thenile.dev/blog/multi-tenant-rag), [Crunchy Scaling Vector Data](https://www.crunchydata.com/blog/scaling-vector-data-with-postgres), [dbi-services mars 2026](https://www.dbi-services.com/blog/pgvector-a-guide-for-dba-part-2-indexes-update-march-2026/)) : **une table `chunks` unique + colonne `source` + B-tree sur `source`**, avec `PARTITION BY LIST(source)` seulement si cardinalité ou isolation d'écriture l'exige (typiquement au-delà de quelques dizaines de sources, ou si une source domine les écritures).

**Pourquoi c'est devenu viable.** pgvector 0.8 (oct. 2024) a livré `hnsw.iterative_scan = 'relaxed_order'` et `hnsw.max_scan_tuples`. Un `WHERE source = ANY($1)` sur HNSW + B-tree retourne maintenant en **~1 ms à 25k lignes** à 8.9 % de sélectivité (benchmark dbi-services mars 2026). Avant 0.8, le filtrage post-ANN scan retournait souvent 0-2 lignes parce que l'ANN s'arrêtait à `ef_search = 40` candidats — ce qui justifiait historiquement la séparation. Cette contrainte n'existe plus.

**Pour ce projet.** Gain : un seul index HNSW à maintenir, une migration unique pour changer de modèle d'embedding, un chemin de requête `Retriever` unifié, RRF natif sur l'union via la SQL canonique Supabase. Coût : migration avec shadow-table cutover + adaptation du retriever et des conformance tests (sensibles à l'ordre). **À planifier mais pas à précipiter** — les items §4.1 et §4.2 pèsent plus en bénéfice immédiat. Préalable utile : note [01](01_RAG_QUALITY_AUDIT_2026-06.md) signale que 3/4 des tables n'ont pas d'index vectoriel correctement déployé ; la migration single-table est aussi l'occasion de régler ce problème en une fois.

### 4.5 Embeddings sans versioning

**Constat.** Les colonnes `embedding_m3` (1024) et `embedding_bge_scw` (3584) existent, ainsi qu'`embedding_qwen3` (4096) sur `rag_chunks_dgafp`. Mais aucune métadonnée par chunk : pas de `model_version`, `embedded_at`, `normalized`, ni de hash du texte effectivement embeddé. Impossible de distinguer un chunk re-embeddé d'un chunk historique, ni de savoir avec quelle version exacte du modèle un vecteur a été produit.

**Référence.** Pattern 2026 ([dbi-services embedding versioning](https://www.dbi-services.com/blog/rag-series-embedding-versioning-with-pgvector-why-event-driven-architecture-is-a-precondition-to-ai-data-workflows/)) : `(model_name, model_version, is_current)` avec index partial ; dual-write pendant les migrations ; jamais de full re-embed forcé. Le re-embed se fait sur le subset qui a effectivement changé.

**Pour ce projet.** Particulièrement pertinent pour passer à Qwen3-Embedding (Scaleway le sert déjà à dimensions custom via Matryoshka 32-4096). Implémentation minimale : ajouter `embedding_metadata jsonb` sur chaque table chunks, écrit systématiquement par `db.py`. Pas de migration destructive ; les chunks pré-existants ont `embedding_metadata IS NULL`, interprété comme "version inconnue" (à backfill au prochain re-embed).

### 4.6 Pas de quality gate en CI

**Constat.** Les tests de conformance (note [01 §4](01_RAG_QUALITY_AUDIT_2026-06.md)) détectent la dérive d'ordre, pas la dégradation qualitative. Si le format XML d'une source change subtilement et casse silencieusement le parsing — sans crash, juste avec des chunks tronqués ou des sections manquantes — rien ne s'allume tant qu'un humain ne pose pas une question pertinente. C'est exactement le scénario "fail-open silencieux" déjà identifié en note [02 §1](02_ARCHITECTURE_AUDIT_2026-06.md) comme cause racine, transposé au pipeline ingestion.

**Référence.** Stack OSS la plus citée 2025 : [Langfuse + Ragas](https://visiononedge.com/building-reliable-rag-pipelines-with-langfuse-and-ragas/). Trois métriques minimum : Faithfulness, Context Precision, Context Recall. CI/CD gates qui bloquent le merge en cas de régression est devenu le pattern standard (cf. [Dextralabs — Production RAG 2025](https://dextralabs.com/blog/production-rag-in-2025-evaluation-cicd-observability/)).

**Pour ce projet.** Un step CI sur un goldset DGAFP figé de ~50 questions, en PR, seuil bloquant à -2pts. Goldset déjà partiellement disponible (note 01). À coupler avec la note [03 — Observabilité](03_RAG_OBSERVABILITY_ROADMAP_2026-06.md) côté runtime pour fermer la boucle ingestion → retrieval → réponse.

### 4.7 Versionnage temporel Légifrance (priorité basse)

**Constat.** L'ingestion ne capture que la version courante de chaque article. Les colonnes `start_date` / `end_date` existent ([rag_chunks_dgafp](packages/data-engineering/src/assistant_rh_data_engineering/legifrance/db.py)) mais aucune logique de snapshot historique : un article modifié en amont écrase la version précédente.

**Référence.** [arXiv 2505.00039 — Graph RAG for Legal Norms](https://arxiv.org/html/2505.00039v5) : séparer Work / Temporal Version (CTV) / Language Version, indexer **toutes** les versions, réutiliser les CTV enfants inchangés via agrégation, sélectionner au query time avec `valid_start ≤ t < valid_end`.

**Pour ce projet.** Pertinent **si et seulement si** "quelle était la règle au {date X}" devient un cas d'usage produit. Si le périmètre reste "currently in force", **à ne pas faire** — l'augmentation de stockage et la complexité de retrieval (filtre temporel sur chaque requête) ne se paient pas.

---

## 5. Ce qu'il ne faut pas changer

- **Ne pas migrer vers `IngestionPipeline` LlamaIndex** wholesale. Cherry-picker les patterns (cache content-hash, upsert strategy) — pas adopter le framework. Le hand-rolled actuel est intégré aux specificités Scaleway (Object Storage CLI, Serverless jobs, BGE endpoint) et raisonnablement testé. Le coût de réécriture ne se paie pas.
- **Ne pas introduire de semantic chunking.** Les benchmarks récents (NAACL 2025, ECIR 2025) montrent qu'il bat rarement un recursive splitter bien réglé sur du texte structuré. Pour QNA Service-Public et article-level Légifrance : compute pur sans gain de qualité.
- **Ne pas découpler davantage `apps/data-ingestion-cli` de `packages/data-engineering`.** Le niveau actuel de séparation est le bon (le CLI est routing, les jobs sont la frontière de stabilité, les `pipeline.py` sont la logique).
- **Ne pas remplacer l'orchestration par Dagster/Prefect maintenant.** Le couple GitHub Actions + Scaleway Serverless jobs fonctionne. Revisiter quand MATTE et RGRH seront ingérés et qu'on aura des dépendances cross-source qui justifient un asset graph. Avant ça, le coût de migration est supérieur au bénéfice.

---

## 6. Plan d'action priorisé

### P0 — Quality wins immédiats (≤ 2 sprints)

1. **[Contextual Retrieval]** Préfixer 50-100 tokens de résumé par chunk avant embedding. LLM = Albert. Cache du résumé sur `text_hash` calculé en silver. À implémenter dans [service_public/gold.py](packages/data-engineering/src/assistant_rh_data_engineering/service_public/gold.py) puis [legifrance/gold.py](packages/data-engineering/src/assistant_rh_data_engineering/legifrance/gold.py). **Critère de succès** : -20 % minimum sur le taux de no-answer du goldset DGAFP existant (note 01). Backfill incrémental possible grâce à §4.5.

2. **[Chunking article-level Légifrance]** Aligner `split_legal_chunks` sur les marqueurs `Article L./R./D.` via [`normalize_article_number()`](packages/data-engineering/src/assistant_rh_data_engineering/legifrance/helpers.py) déjà présent. **Critère de succès** : chaque chunk Légifrance contient au plus un article ; nb de citations correctes par réponse en hausse mesurable sur le goldset.

### P1 — Fondations opérationnelles (1 trimestre)

3. **[Incremental tracking]** Table `ingestion_records(source, source_id, content_hash, embedding_model_version, embedded_at, is_active)` + flag `--cleanup-mode {none|incremental|scoped_full|full}` sur les jobs médaillon. Bonus : `If-None-Match` / `Last-Modified` sur le bronze fetch Service-Public et DILA. **Critère de succès** : un run no-op (même source, même config) écrit 0 chunk en DB et fait 0 appel embedding ; les fiches supprimées en amont sont marquées `is_active=false`.

4. **[Embedding versioning]** Ajouter `embedding_metadata jsonb` sur les tables `rag_chunks_*` + écriture systématique dans `db.py` (model_name, model_version, normalized, embedded_at, text_hash). **Critère de succès** : `SELECT (embedding_metadata->>'model_version'), count(*) GROUP BY 1` répond la vérité ; les migrations futures peuvent dual-write sans full re-embed forcé.

5. **[Quality gate Ragas en CI]** Step Ragas (Faithfulness / Context Precision / Context Recall) sur goldset DGAFP figé de ~50 questions, exécuté en PR, seuil bloquant à -2pts par défaut (override explicite documenté dans la PR). **Critère de succès** : un changement de chunking qui dégrade le retrieval ne mergeable sans override conscient ; la régression silencieuse devient impossible.

### P2 — À planifier (trimestre+1)

6. **[Consolidation single-table]** Migration `rag_chunks_*` → `rag_chunks` unifié + colonne `source` + B-tree. Shadow-table, dual-write, cutover. Préalables : verrouiller les contrats `Retriever` (note [02 §A1](02_ARCHITECTURE_AUDIT_2026-06.md), trancher Python vs Mastra), instrumenter pour mesurer l'avant/après en termes de latence retrieval et qualité. **Critère de succès** : un seul index HNSW à maintenir, schéma migration unique, RRF natif sur l'union ; pas de régression latence ni qualité retrieval sur le goldset. Saisir cette migration comme moment opportun pour fixer les index vectoriels manquants identifiés en note 01.

### P3 — Hors périmètre actuel

7. **[Versionnage temporel Légifrance]** Seulement si "point-in-time legal queries" entre au backlog produit. Coût d'ingénierie et de stockage non négligeable. Référence d'implémentation : arXiv 2505.00039 (cf. §4.7).

---

## 7. Sources

### Références architecturales

- [Anthropic — Contextual Retrieval](https://www.anthropic.com/news/contextual-retrieval)
- [LangChain Indexing API + RecordManager](https://python.langchain.com/docs/how_to/indexing/)
- [LlamaIndex IngestionPipeline + Document Management](https://developers.llamaindex.ai/python/framework/module_guides/loading/ingestion_pipeline/)
- [LlamaIndex — Document Management Pipeline](https://developers.llamaindex.ai/python/examples/ingestion/document_management_pipeline/)
- [Mastra — RAG overview](https://mastra.ai/docs/rag/overview)
- [Vespa — RAG Blueprint](https://vespa.ai/solutions/retrieval-augmented-generation/the-rag-blueprint/)
- [RAGFlow — RAG 2025 review (PTI: Parse → Transform → Index)](https://ragflow.io/blog/rag-review-2025-from-rag-to-context)

### Chunking légal / réglementaire

- [arXiv 2510.06999 — Towards Reliable Retrieval in RAG for Large Legal Datasets (oct. 2025)](https://arxiv.org/html/2510.06999v1)
- [arXiv 2504.19754 — Reconstructing Context, ECIR 2025](https://arxiv.org/abs/2504.19754)
- [Jina — Late Chunking in Long-Context Embedding Models](https://jina.ai/news/late-chunking-in-long-context-embedding-models/)
- [HamzaG737/legal-code-rag — French legal Code RAG (GitHub)](https://github.com/HamzaG737/legal-code-rag)
- [Structure-Aware Chunking for Legal Documents, JUST-NLP 2025](https://aclanthology.org/2025.justnlp-main.19/)

### Multi-source / schéma pgvector

- [Supabase — Hybrid Search + RRF](https://supabase.com/docs/guides/ai/hybrid-search)
- [Nile — pgvector 0.8.0 deep dive](https://www.thenile.dev/blog/pgvector-080)
- [Nile — Multi-tenant RAG](https://www.thenile.dev/blog/multi-tenant-rag)
- [Crunchy Data — Scaling Vector Data with Postgres](https://www.crunchydata.com/blog/scaling-vector-data-with-postgres)
- [dbi-services — pgvector DBA guide, indexes (mars 2026)](https://www.dbi-services.com/blog/pgvector-a-guide-for-dba-part-2-indexes-update-march-2026/)
- [pgvector 0.8.0 release announcement](https://www.postgresql.org/about/news/pgvector-080-released-2952)

### Embeddings ops & observabilité

- [dbi-services — Embedding versioning with pgvector](https://www.dbi-services.com/blog/rag-series-embedding-versioning-with-pgvector-why-event-driven-architecture-is-a-precondition-to-ai-data-workflows/)
- [Langfuse + Ragas — Cookbook](https://visiononedge.com/building-reliable-rag-pipelines-with-langfuse-and-ragas/)
- [Dextralabs — Production RAG 2025: CI/CD & Observability](https://dextralabs.com/blog/production-rag-in-2025-evaluation-cicd-observability/)
- [DZone — Why Embedding Pipelines Break at Scale](https://dzone.com/articles/why-embedding-pipelines-break-at-scale)
- [Scaleway — embedding models catalog](https://www.scaleway.com/en/docs/generative-apis/reference-content/supported-models/)
- [Sentence Transformers — Matryoshka Embeddings](https://www.sbert.net/examples/sentence_transformer/training/matryoshka/README.html)

### Légifrance versions

- [arXiv 2505.00039 — Graph RAG for Legal Norms](https://arxiv.org/html/2505.00039v5)
- [Albert API DINUM — Etalab](https://ia.numerique.gouv.fr/outils-ia/albert-api)
