# RAG V3 Clean — Architecture du pipeline

> Document technique à destination du développeur IA qui reprend le projet.
> Dernière mise à jour : 2026-02-27

## Vue d'ensemble

Le pipeline RAG V3 Clean est un système de **Retrieval-Augmented Generation** pour un chatbot RH destiné aux agents publics du Ministère de la Transition Écologique. Il répond à des questions sur les contractuels de la fonction publique d'État en s'appuyant sur les tables documentaires configurées, avec `dgafp` conditionnel via l'intent gater.

```
┌─────────────────────────────────────────────────────────────────────┐
│                         PIPELINE FLOW                              │
│                                                                     │
│  Query ─→ QueryProcessor ─→ Retriever ─→ SectionAggregator        │
│              (intent,           (4 tables      (grouper chunks     │
│               acronymes,         en //)         → sections,        │
│               thème)                            reranking)         │
│                                                                     │
│         ─→ ContextSelector ─→ ContextBuilder ─→ StreamingGenerator │
│              (LLM filtre      (budget tokens,    (LLM avec         │
│               sections)        doc-entier,        fallback)        │
│                                triangulation,                       │
│                                réfs juridiques)                     │
└─────────────────────────────────────────────────────────────────────┘
```

### Flux de données

```
Query (str)
  │
  ▼
QueryProcessResult          ← intent, thème, acronymes, needs_legal_search
  │
  ▼
List[RetrievedChunk]        ← chunks récupérés en parallèle depuis les tables configurées
  │
  ▼
List[AggregatedSection]     ← chunks groupés par rag_sections, scorés, rerankés (top 10)
  │
  ▼
List[AggregatedSection]     ← sections filtrées par le Selector (ou rejet total → réponse canned)
  │
  ▼
List[ContextItem]           ← sections converties, budget tokens appliqué, réfs juridiques injectées
  │
  ▼
PipelineResult              ← réponse streamée, sources, timing, métadonnées
```

---

## Modules détaillés

### 1. `query_processor.py` — Pré-traitement de la requête

**Rôle** : Classifier l'intention, détecter le thème RH, expanser les acronymes, et décider si la question nécessite une recherche juridique — le tout en **un seul appel LLM**.

**Entrée** : `query: str`, `conversation_history: list[dict]`
**Sortie** : `QueryProcessResult`

| Champ clé | Description |
|---|---|
| `intent` | `rag_query`, `chit_chat`, `out_of_scope`, `clarification`, `follow_up`, `document_request` |
| `should_proceed` | `True` si intent = `rag_query` ou `follow_up` (sinon le pipeline court-circuite avec une réponse directe) |
| `needs_legal_search` | Si `True`, la table `rag_chunks_dgafp` est incluse dans le retrieval |
| `query_for_retrieval` | Requête reformulée par le LLM (enrichie avec le contexte conversationnel et les acronymes) |
| `detected_acronyms` | Dict `{acronyme: expansion}` détectés par regex dans la question |
| `expanded_acronyms` | Sous-ensemble effectivement utilisé par le LLM dans la reformulation |

**Prompt** : `intent_unified.md` (DB `system_prompts` > fichier local `prompts/intent.md`)

**Latence** : ~200-500ms (appel LLM `openweight-medium`)

**Trade-offs** :
- Un seul appel LLM au lieu de 3 (classification + reformulation + détection thème) → moins de latence mais plus de risque si le modèle se trompe sur un aspect
- Le modèle `openweight-medium` est plus rapide mais moins fiable que `openweight-large`
- L'expansion d'acronymes est regex-based (pas de NLP) : un acronyme dans un mot composé peut matcher à tort

**Limites** :
- Le `follow_up` dépend de la qualité du contexte conversationnel fourni (tronqué à 8 messages, 300 chars chacun)
- Si le LLM ne parvient pas à parser le JSON, le fallback est `rag_query` avec confiance 0.5

---

### 2. `retriever.py` — Recherche sémantique multi-tables

**Rôle** : Encoder la requête en embedding, puis chercher les chunks les plus proches dans 4 tables PostgreSQL (pgvector) **en parallèle** via un ThreadPoolExecutor.

**Entrée** : `query: str` (via `QueryProcessResult.query_for_retrieval`)
**Sortie** : `List[RetrievedChunk]` trié par score décroissant

| Table | Publisher | A des sections | Colonnes spéciales | tsvector |
|---|---|---|---|---|
| `rag_chunks_matte` | MATTE | ✅ | `section_id`, `source_document_id` | `text_tsv` |
| `rag_chunks_service_public` | Service-Public | ✅ | `section_id`, `source_document_id` | `text_tsv` |
| `rag_chunks_dgafp` | DGAFP | ❌ | `cid`, `number`, `url`, `full_title` | `chunk_text_tsv` |
| `rag_chunks_rgrh` | RGRH | ❌ | `source_document_id` | `text_tsv` |

**Comportement conditionnel DGAFP** :
- Si `needs_legal_search = False` → la table `dgafp` est **exclue** du retrieval (réduction du bruit)
- Si `needs_legal_search = True` → la table `dgafp` est incluse **et forcée en hybride** (RRF) quel que soit le `search_mode` global, pour maximiser la correspondance sur les termes juridiques exacts (numéros d'articles, références de décrets)

**Modes de recherche** (configurable via `search_mode`) :
- **Sémantique** (défaut) : Recherche par cosine distance sur les vecteurs d'embedding pgvector
- **Hybride** : Reciprocal Rank Fusion (RRF) combinant sémantique + lexical (tsvector). Disponible sur toutes les tables (toutes ont une colonne tsvector). Le poids est contrôlé par `alpha` (défaut 0.5)
- **Lexical** : Recherche pure par `ts_rank_cd` sur les colonnes tsvector. Même disponibilité que l'hybride

**Embedding** : `FallbackEmbedder` avec circuit breaker (Albert → fallback Scaleway BGE).

**Latence** : ~300-800ms (embedding ~100ms + 4 requêtes pgvector en parallèle ~200-700ms)

**Trade-offs** :
- `initial_top_k = 20` par table → ~100 chunks max (5 tables). Plus = meilleur rappel mais plus lent
- Le mode hybride (RRF) améliore la couverture de termes techniques exacts mais ajoute une légère latence. La DGAFP est automatiquement forcée en hybride pour les requêtes juridiques
- Le parallélisme masque les latences individuelles mais multiplie les connexions DB

**Limites** :
- Pas de cache d'embeddings (chaque requête = un appel API)
- Les chunks DGAFP et RGRH n'ont pas de `section_id` → traités comme standalone dans l'agrégation

---

### 3. `section_aggregator.py` — Agrégation + Reranking

**Rôle** : Regrouper les chunks par leur section parente (`rag_sections`), calculer un score agrégé, puis reranker les sections.

**Entrée** : `List[RetrievedChunk]`
**Sortie** : `List[AggregatedSection]` (top K après rerank)

**Score d'agrégation** :
```
score = 0.5 × max(chunk_scores) + 0.3 × mean(chunk_scores) + 0.2 × normalized_chunk_count
```

**Étapes** :
1. Requête SQL `rag_sections JOIN rag_documents` pour récupérer le markdown complet et les métadonnées
2. Groupement des chunks par `section_id` (chunks sans section = standalone)
3. Calcul du score agrégé
4. Reranking via `AlbertReranker` (`openweight-rerank`) → top `section_rerank_top_k` (défaut: 10)

**Latence** : ~200-500ms (requête SQL ~50ms + reranking API ~150-400ms)

**Trade-offs** :
- Le reranking améliore significativement la pertinence mais ajoute ~200ms
- `section_rerank_top_k = 10` réduit le nombre de sections, risquant d'éliminer des sources marginalement pertinentes
- Les chunks DGAFP/RGRH sans section restent en tant que standalone (markdown = texte du chunk uniquement)

**Limites** :
- Le texte envoyé au reranker est tronqué à 2000 chars par section
- Si le reranking échoue (API down), fallback vers l'ordre d'agrégation

---

### 4. `context_selector.py` — Filtrage LLM (optionnel)

**Rôle** : Un LLM évalue chaque section et décide lesquelles sont réellement pertinentes pour répondre à la question. Peut **rejeter toutes les sections**.

**Entrée** : `List[AggregatedSection]` (post-rerank)
**Sortie** : `List[AggregatedSection]` filtrée (peut être vide si rejet total)

**Comportement** :
- **Activé** (`SelectorConfig.enabled = True`) : appel LLM → JSON `{selected_ids: [...], reason: "..."}`
- **Désactivé** : no-op, toutes les sections passent
- **Rejet total** (`selected_ids: []` explicite) : retourne une liste vide → le pipeline court-circuite avec un message "pas de réponse trouvée" sans appeler le générateur
- **Échec de parsing** (JSON malformé) : fallback vers les **top 5 sections** (par score reranker) plutôt que toutes

**Prompt** : `v3_selector_business.md` (DB `system_prompts` > fichier local `prompts/selector.md`)

**Latence** : ~300-800ms (appel LLM `openweight-large`)

**Trade-offs** :
- **Précision ↑** : élimine les sections hors-sujet, réduit les hallucinations
- **Rappel ↓** : peut éliminer à tort une section pertinente si le LLM se trompe
- **Latence ↑** : ajoute un appel LLM complet au pipeline
- **Rejet total** : protège contre les hallucinations quand aucune source n'est pertinente, mais peut frustrer l'utilisateur si le Selector est trop strict

**Architecture** : Classe `ContextSelector` instanciée par le pipeline (`self._selector`). L'état (décisions, reasoning, raw response) est porté par l'instance, pas par des variables globales — thread-safe si chaque requête a sa propre instance de `Pipeline`.

**Limites** :
- Le texte complet de chaque section est envoyé au LLM (pas de troncature), ce qui peut augmenter le coût en tokens pour des sections très longues

---

### 5. `context_builder.py` — Construction du contexte

**Rôle** : Sélectionner les sections à inclure dans le prompt LLM sous un **budget de tokens**, enrichir avec les **références juridiques**, et garantir la **diversité des sources**.

**Entrée** : `List[AggregatedSection]` (post-selector)
**Sortie** : `List[ContextItem]`

**4 étapes internes** :

**Deux modes** configurables via `ContextMode` (Admin panel) :

| Paramètre | STANDARD | WIDE |
|---|---|---|
| `token_budget` | 8 000 | 12 000 |
| `max_full_docs` | 1 | 2 |
| `doc_entire_threshold` | 3 500 | 5 000 |
| `max_sections` | 12 | 20 |
| `legal_refs_budget` | 1 000 | 2 000 |

**4 étapes internes** :

| # | Étape | Description | Budget check |
|---|---|---|---|
| 1 | **Doc-entier** | Si un document est petit (< `doc_entire_threshold`), charge le `doc_markdown` complet depuis `rag_documents` et l'inclut en bloc | ✅ |
| 2 | **Remplissage top sections** | Ajouter les sections par score décroissant tant que le budget le permet | ✅ |
| 3 | **Triangulation** | Ajouter `triangulation_sections` (= 2) sections de publishers non-primaires pour la diversité | ❌ (ignore le budget) |
| 4 | **Injection réfs juridiques** | Collecter les `references_juridiques` des sections, résoudre les CID depuis `rag_chunks_dgafp`, et injecter le texte | ✅ (budget dédié) |

**Attribut important** : `last_resolved_refs` — dict `{numéro_article: {cid, url, title}}` des refs résolues depuis DGAFP.

**Latence** : ~50-100ms (1-2 requêtes SQL légères)

**Trade-offs** :
- La triangulation **ignore le budget** pour garantir la diversité des sources. Cela peut dépasser légèrement le budget
- Le mode **WIDE** (recommandé en prod) augmente le budget et permet d'inclure jusqu'à 2 documents entiers
- Le doc-entier charge directement `doc_markdown` depuis `rag_documents` (pas un réassemblage de sections)
- Le budget réfs juridiques est séparé du budget principal
- L'estimation tokens est simpliste : `len(text) // 4`

**Limites** :
- La résolution CID nécessite une requête SQL sur `rag_chunks_dgafp` — si la table ne contient pas le numéro d'article, la ref n'est pas résolue
- Pas de prioritisation des réfs juridiques (toutes traitées égalitairement)

---

### 6. `generator.py` — Génération streamée

**Rôle** : Formater le prompt final (système + utilisateur) et streamer la réponse token par token.

**Entrée** : `query: str`, `List[ContextItem]`, `history: list[dict]`
**Sortie** : `Generator[str, None, None]` (tokens)

**Architecture LLM** : `FallbackLLMClient` — si le provider primaire (Albert) échoue **avant le premier token**, le fallback (Scaleway Llama) prend le relais. Si l'erreur survient **pendant le streaming**, la réponse partielle est conservée.

**Prompt système** : `system_prompt_V6_optimized.md` (DB `system_prompts` > fichier local)
**Prompt utilisateur** : template fixe avec `{context}` et `{question}`

**Latence** : ~1-5s (TTFT ~500-2000ms selon le modèle et la taille du contexte)

**Trade-offs** :
- `openweight-large` est le meilleur modèle Albert mais le plus lent
- Le fallback Scaleway ajoute de la fiabilité mais avec un modèle potentiellement moins adapté au domaine
- L'historique de conversation est passé au LLM → réponses contextuelles mais prompt plus long

**Limites** :
- Pas de contrôle du format de sortie (le LLM peut ignorer les instructions du system prompt)
- Le `last_full_prompt` est stocké pour le debugging mais peut être très long (50k+ chars)

---

## Modules de support

### `llm_client.py` — Client LLM unifié

Wrapper autour de l'API OpenAI compatible (Albert, Scaleway) avec :
- `LLMClient` : client simple, un provider
- `FallbackLLMClient` : primary + fallback automatique
- Circuit breaker implicite (pas d'appels redondants)

### `embedder.py` — Embedding avec fallback

- `FallbackEmbedder` : Albert (primaire) → Scaleway BGE (fallback)
- `_CircuitBreaker` : si Albert échoue, cooldown de 60s avant retry
- Normalisation L2 automatique des vecteurs

### `reranker.py` — Reranker Albert

- `AlbertReranker` : appel API `openweight-rerank`
- Reçoit les textes + query, retourne `List[(index, score)]` trié

### `config.py` — Configuration centralisée

- Dataclasses imbriquées : `RAGConfig` > `RetrievalConfig`, `SelectorConfig`, etc.
- Runtime config DB-backed (`rag_config` table)
- Gestion des prompts (DB `system_prompts` > fichiers locaux)
- Chargement des acronymes depuis la table `acronyms`

### `models.py` — Modèles de données

```
RetrievedChunk → AggregatedSection → ContextItem → PipelineResult
```

Chaque modèle est un `@dataclass` avec des champs typés. `AggregatedSection.token_estimate` est une propriété calculée (`len(markdown) // 4`).

### `citation_extractor.py` — Extraction de citations

Regex-based : extrait les mentions d'articles de loi et de décrets dans le texte de la réponse LLM, puis les matche avec les `references_juridiques` du contexte.

### `feedback_analyzer.py` — Analyse des feedbacks

Module asynchrone qui analyse les retours négatifs des utilisateurs via un LLM pour catégoriser les types d'échec (hors-sujet, information incomplète, source obsolète, etc.).

### `admin.py` — Administration

CRUD pour la config runtime, les prompts, et les acronymes. Utilisé par `pages/04_Admin_Config.py`.

---

## Observabilité

Chaque étape du pipeline enregistre des métriques dans `_timing` (latence) et `_stage_refs` (données intermédiaires). Ces données sont loggées dans la table `chat_runs` pour analyse dans `pages/02_Chat_Logs.py`.

| Métrique | Source | Description |
|---|---|---|
| `query_processing_ms` | QueryProcessor | Intent gating + expansion acronymes |
| `retrieval_ms` | Retriever | Embedding + recherche 4 tables |
| `aggregation_ms` | SectionAggregator | Agrégation + reranking |
| `selector_ms` | ContextSelector | Filtrage LLM (si activé) |
| `context_build_ms` | ContextBuilder | Budget + doc-entier + triangulation + réfs |
| `generation_ms` | Generator | Streaming LLM complet |
| `ttft_ms` | Pipeline | Time to first token |
| `total_time_ms` | Chatbot page | Wall-clock (début requête → fin réponse) |

### Colonnes Chat Logs par rubrique

- **Intent** : intent, thème, DGAFP oui/non, acronymes détectés/expansés
- **Retrieval** : chunks retrievés, top K, sections avant/après rerank, distribution
- **Sélection** : taux sélection, sélectionnés, décisions JSON, raisonnement
- **Context** : mode, items finaux, distribution, titres, tokens, docs entiers
- **Réfs juridiques** : réfs citées (dans sections), réfs injectées (résolues DGAFP), détail
- **Génération** : modèle, prompt, TTFT, longueur réponse (tokens)
- **Timing** : Intent → Retrieval → Agrég.+Rerank → Selector → ContextBuild → Génération → Total

---

## Configuration clé

```python
RAGConfig(
    retrieval=RetrievalConfig(
        search_mode=SearchMode.SEMANTIC,
        embedding_model=EmbeddingModel.ALBERT,
        initial_top_k=20,            # chunks par table
        tables=["matte", "service_public", "dgafp", "rgrh"],
    ),
    aggregation=SectionAggregationConfig(
        enable_section_reranker=True,
        section_rerank_top_k=10,     # sections après rerank
    ),
    context=ContextBuildConfig(
        context_mode=ContextMode.WIDE,  # STANDARD ou WIDE
        # Les valeurs ci-dessous sont les défauts WIDE (via getters)
        # token_budget=12000, max_full_docs=2, doc_entire_threshold=5000
        triangulation_sections=2,    # diversité publishers
    ),
    selector=SelectorConfig(
        enabled=True,                # LLM Selector on/off (toggle Admin panel)
        model="openweight-large",
        prompt_name="v3_selector_business.md",
    ),
    generation=GenerationConfig(
        model="openweight-large",
        system_prompt_name="system_prompt_V6_optimized.md",
        fallback_model="llama-3.1-70b-instruct",
    ),
    query_processor=QueryProcessorConfig(
        enable_intent_gating=True,   # filtre dgafp conditionnel
        enable_acronym_expansion=True,
    ),
)
```

> **Note** : En production, la config est chargée dynamiquement depuis la table `rag_config` (Admin panel). Le notebook d'évaluation `eval_v3clean_generation.ipynb` utilise `load_prod_config()` pour se brancher directement dessus.

---

## Schéma des tables PostgreSQL

```
rag_chunks_matte          ─┐
rag_chunks_service_public  │
rag_chunks_dgafp           ├── Retriever (pgvector cosine, parallèle)
rag_chunks_rgrh            │
                            ─┘
                             │
                             ▼
rag_sections ◄──── section_id ────► SectionAggregator (JOIN pour markdown + métadonnées)
    │
    ├── references_juridiques (JSON)  ──► ContextBuilder (injection)
    └── doc_id ──► rag_documents (titre, URL, date, publisher)
                        │
                        └── legacy_doc_id ──► documents (PDF pour le viewer)
```

---

## Graphe de dépendances des modules

```
pipeline.py
├── query_processor.py ──→ config.py, llm_client.py
├── retriever.py ──→ config.py, embedder.py, models.py
├── section_aggregator.py ──→ config.py, models.py, reranker.py
├── context_selector.py ──→ config.py, llm_client.py, models.py
├── context_builder.py ──→ config.py, models.py
└── generator.py ──→ config.py, context_builder.py, llm_client.py, models.py

(standalone, pas de dépendance upstream)
├── embedder.py
├── reranker.py
├── llm_client.py
├── citation_extractor.py
├── feedback_analyzer.py
└── admin.py ──→ config.py
```

**Zéro dépendance** vers `src/rag/` ou `src/rag_v2/`. Le module est 100% autonome.

---

## Latence typique (pipeline complet)

| Étape | Latence typique | % du total |
|---|---|---|
| QueryProcessor (intent) | 200-500ms | 10-15% |
| Retriever (embedding + pgvector) | 300-800ms | 15-25% |
| SectionAggregator (SQL + rerank) | 200-500ms | 10-15% |
| ContextSelector (LLM) | 300-800ms | 10-25% |
| ContextBuilder (SQL + logique) | 50-100ms | 2-5% |
| Generator (streaming LLM) | 1000-5000ms | 40-60% |
| **Total** | **2-8s** | 100% |

Le TTFT (Time to First Token) visible par l'utilisateur inclut toutes les étapes avant la génération + le temps jusqu'au premier token LLM.
