# Revue architecturale RAG & comparaison à l'état de l'art 2025-2026

> Dossier d'audit : voir [README](README.md). Date : 2026-06-17.
> Objet : **revue d'architecture du pipeline RAG (`packages/rag-pipeline/`)** confrontée à l'état de l'art des systèmes RAG en production (fin 2025 / 2026). But : juger si l'architecture actuelle est saine, identifier les leviers d'amélioration **structurels** (au-delà des défauts opérationnels déjà documentés par les notes 01-07), et proposer une feuille de route adossée à des références publiées.
> Méthode : (1) cartographie code du pipeline (`packages/rag-pipeline/src/assistant_rh_rag_pipeline/`), (2) recherche web ciblée sur 5 axes (frameworks 2026, chunking & observabilité, retrieval avancé & reranking, RAG agentique & évaluation, anti-hallucination & RAG juridique), (3) confrontation aux constats déjà vérifiés sur staging (notes 06-07).
> **Cette note est complémentaire** : elle ne réinstancie pas les défauts opérationnels (reranker, DGAFP sans embedding, `chat_runs` obèse, goldset absent) — ils restent traités en P0 par les notes 00, 01, 06, 07. Elle traite **ce que l'architecture pourra faire de mieux une fois ces fondations en place.**

---

## 0. Verdict

**L'architecture du pipeline RAG est bonne — au-dessus de la moyenne des systèmes RAG de production en 2026.** Elle implémente correctement la quasi-totalité des patrons modernes (recherche hybride sémantique + lexical avec RRF, reranking cross-encoder, fallback de provider avec circuit breaker, triangulation multi-source, abstention par routage d'intention, streaming, configuration runtime en base). Une réécriture sur LangGraph, LlamaIndex, Haystack ou RAGFlow serait **une régression** pour ce périmètre.

Deux écarts ressortent quand on confronte le pipeline à la littérature 2025-2026, et ils ne sont **pas** dans la liste des défauts opérationnels déjà documentés :

1. **Le pipeline s'appuie sur le générateur pour refuser quand les sources sont insuffisantes** ([generator.md](../../packages/rag-pipeline/src/assistant_rh_rag_pipeline/prompts/generator.md) : « Si les sources ne permettent pas de répondre, dites-le explicitement »). Une étude ICLR 2025 (arXiv:2411.06037) mesure que **le RAG dégrade activement l'abstention** : Claude 3.5 Sonnet passe de 84,1 % d'abstention (hors RAG, sur questions sans réponse) à **52 %** ; Gemini 1.5 Pro de 100 % à **18,6 %**. *« Le contexte, même non pertinent, rend le modèle confiant. »* Pour un chatbot de l'administration, c'est un risque produit, pas un sujet académique.
2. **Le système n'a aucun mécanisme de vérification post-génération** (NLI / entailment sur les segments cités). C'est le complément naturel au goldset et à RAGAS que la note 01 prévoit.

Le reste du dossier liste des évolutions **incrémentales** de l'architecture existante (Contextual Retrieval, SAC, CRAG, Adaptive-RAG, self-querying retriever, OpenInference / Langfuse), pas une refonte.

---

## 1. Articulation avec les notes 00-07

| Note | Périmètre | Recouvrement avec la 08 |
|---|---|---|
| [01](01_RAG_QUALITY_AUDIT_2026-06.md) | Qualité RAG : reranker, RRF, chunking, couverture, goldset | **Complète** : la 08 propose des évolutions architecturales qui ne pourront être mesurées qu'**après** que le goldset et les métriques RAGAS de la 01 existent. |
| [02](02_ARCHITECTURE_AUDIT_2026-06.md) | Architecture / qualité de code / sécurité / CI | **Complète** : la 02 traite des pannes silencieuses internes ; la 08 traite de la **forme architecturale globale** et de sa comparaison au marché. |
| [03](03_RAG_OBSERVABILITY_ROADMAP_2026-06.md) | Observabilité RAG sur Scaleway Cockpit / Grafana | **Précise** : la 08 nomme la spec (OpenInference) et le backend self-host candidat (Langfuse) compatibles avec la trajectoire de la 03. |
| [04](04_OBSERVATIONS_INITIALES_2026-06-05.md) | Vision produit : multi-ministère, abstention, autorité des sources | **Confirme** : l'axe abstention/autorité de la 04 est **renforcé** par les évidences empiriques (cf. §3.1). |
| [06](06_AUDIT_CODE_ET_DB.md) | Anti-patterns code/DB, fail-open, métriques absentes | **Indépendant**. |
| [07](07_VERIFICATION_STAGING_ET_PRIORISATION.md) | Vérification staging, priorisation refondée | **Indépendant**, mais la 08 confirme la ligne directrice (mesurer avant de modifier). |

**À lire après les notes 00 et 07.** La 08 prend pour hypothèse que les P0 « réparer ce qui est cassé » (DGAFP embeddings, observabilité de base, goldset) sont en cours ou planifiés.

---

## 2. Cartographie architecturale (synthèse)

Le pipeline `packages/rag-pipeline/` est un système modulaire à 6 étages, orchestré par une classe `Pipeline` ([pipeline.py](../../packages/rag-pipeline/src/assistant_rh_rag_pipeline/pipeline.py), 911 lignes) qui maintient un `_RunState` par requête.

```
Query
  → QueryProcessor           intent + acronymes + reformulation + drapeau légal (1 appel LLM unifié)
  → Retriever                recherche parallèle (ThreadPool) sur 4 tables avec RRF
  → SectionAggregator        regroupement chunk→section, score pondéré, rerank Albert /rerank
  → ContextSelector          filtrage LLM optionnel, désactivé par défaut, court-circuit no-answer
  → ContextBuilder           budget tokens, full-doc, triangulation, refs juridiques
  → Generator                streaming avec fallback Albert → Scaleway
```

Choix techniques marquants (et conformes aux meilleures pratiques 2026) :

- **Recherche hybride avec RRF par table et entre tables** ([retriever.py](../../packages/rag-pipeline/src/assistant_rh_rag_pipeline/retriever.py)) — `alpha=0.5`, `K=60`. C'est le baseline indiscutable de la littérature (Anthropic Contextual Retrieval, RAGFlow 2025, RAG in 2026 Blueprint).
- **Reranker cross-encoder** via l'endpoint Albert `/rerank` ([reranker.py](../../packages/rag-pipeline/src/assistant_rh_rag_pipeline/reranker.py), [section_aggregator.py](../../packages/rag-pipeline/src/assistant_rh_rag_pipeline/section_aggregator.py)). Le rerank est cité comme *« l'un des upgrades à plus haut ROI »* du RAG moderne.
- **Parent-document / small-to-big** : les chunks servent au retrieval, les sections au contexte ([section_aggregator.py](../../packages/rag-pipeline/src/assistant_rh_rag_pipeline/section_aggregator.py)). C'est exactement le patron `ParentDocumentRetriever` de LangChain et `HierarchicalNodeParser` de LlamaIndex, implémenté ici de manière custom et propre.
- **Triangulation** ([context_builder.py:129](../../packages/rag-pipeline/src/assistant_rh_rag_pipeline/context_builder.py:129)) — ≥ 2 sections d'éditeurs non principaux, **hors budget tokens**. Patron rare et bien pensé : adresse directement le risque de monopolisation par un éditeur (typique du RAG juridique).
- **Fallback de provider + circuit breaker** ([embedder.py](../../packages/rag-pipeline/src/assistant_rh_rag_pipeline/embedder.py), [llm_client.py](../../packages/rag-pipeline/src/assistant_rh_rag_pipeline/llm_client.py)) — Albert primaire, Scaleway secours, cooldown 60 s. Production-grade.
- **Court-circuit no-answer** ([pipeline.py:209](../../packages/rag-pipeline/src/assistant_rh_rag_pipeline/pipeline.py:209)) — quand le selector rejette tout, message dédié et retry possible. Bonne intention anti-hallucination, mais **fragile** comme seul rempart (cf. §3.1).
- **Configuration runtime en base** (table `rag_config`, [db_helpers.py](../../packages/rag-pipeline/src/assistant_rh_rag_pipeline/db_helpers.py)) — sous-coté ; permet d'ajuster en prod sans redéploiement.

Choix présents dans la `RAGConfig` mais **non implémentés** :

- `QueryProcessorConfig.enable_hyde` : option exposée, aucun code ne la consomme. → recommander de supprimer (§5).
- `RetrievalConfig.enable_chunk_reranker` ([config.py:122](../../packages/rag-pipeline/src/assistant_rh_rag_pipeline/config.py:122)) : retourne l'identité. → implémenter ou supprimer. À distinguer de `SectionAggregationConfig.enable_section_reranker` ([config.py:142](../../packages/rag-pipeline/src/assistant_rh_rag_pipeline/config.py:142)), qui lui est bien câblé et utilisé.

---

## 3. Constats nouveaux issus de la littérature 2025-2026

### 3.1 Le RAG **dégrade** l'abstention — c'est un risque produit, pas un détail

**Référence** : arXiv:2411.06037 (ICLR 2025), confirmé indépendamment dans plusieurs publications (HALT-RAG, Contextual AI GLM).

Mesure : sur un ensemble de questions sans réponse possible, les modèles **refusent significativement moins** quand un contexte est ajouté, **même si ce contexte est non pertinent**.

| Modèle | Abstention hors RAG | Avec RAG |
|---|---|---|
| Claude 3.5 Sonnet | 84,1 % | **52 %** |
| Gemini 1.5 Pro | 100 % | **18,6 %** |

Le pipeline actuel s'appuie sur deux mécanismes pour refuser :

1. **Le ContextSelector** ([context_selector.py](../../packages/rag-pipeline/src/assistant_rh_rag_pipeline/context_selector.py)) avec court-circuit no-answer (pipeline.py:209) si tout est rejeté. Mais le selector est **désactivé par défaut** (`SelectorConfig.enabled=False`) — et même actif, il dépend d'un LLM qui peut se tromper.
2. **L'instruction système** (« Si les sources ne permettent pas de répondre, dites-le explicitement »). C'est précisément le mécanisme que l'étude ICLR 2025 montre comme **non fiable** en présence de contexte.

**Conséquence opérationnelle pour un chatbot de l'État** : un agent contractuel peut se voir affirmer comme « clair » un sujet dont les sources sont insuffisantes (ex. cas particulier de cumul d'activités non couvert par MATTE/SP/DGAFP). Le risque réputationnel est connu (cf. l'épisode « Albert France services » de 2024 sur le coût de renouvellement d'une CNI).

**Recommandation** : ajouter un **vérificateur post-génération** entre `Generator` et le retour utilisateur. La référence de production est HALT-RAG (Goswami & Kurra, arXiv:2509.07475, sept. 2025) : ensemble NLI calibré qui score chaque claim généré contre son segment source, F1 = 0,9786 sur QA. Léger (deux modèles NLI gelés + classifieur méta), agnostic au générateur. Décision : si entailment < seuil → abstention forcée ou marquage « incertain ».

C'est la **suite logique** de la note 04 (axe « abstention » et « autorité des sources ») et complète le goldset prévu par la note 01. Sans goldset, le seuil n'est pas calibrable ; avec, il l'est.

### 3.2 Contextual Retrieval et SAC : deux variantes, deux coûts

**Références** : [Anthropic, Contextual Retrieval (sept. 2024)](https://www.anthropic.com/news/contextual-retrieval) ; Reuter et al., *Summary-Augmented Chunking* (arXiv:2510.06999, oct. 2025).

Anthropic mesure sur corpus mixtes (code, fiction, ArXiv, science) :

| Configuration | Taux d'échec top-20 |
|---|---|
| Embeddings standard + BM25 standard | 5,7 % |
| + Embeddings contextualisés | 3,7 % (**−35 %**) |
| + BM25 contextualisé | 2,9 % (**−49 %**) |
| + Reranking | **1,9 % (−67 %)** |

**Idée** : préfixer chaque chunk d'une phrase générée par LLM situant le chunk dans son document (~50-100 tokens), **avant embedding et indexation BM25**. Coût : ~$1,02 / M tokens de document avec prompt caching Claude Haiku.

**Variante moins chère : SAC** (Summary-Augmented Chunking, Reuter et al.) — un résumé de ~150 caractères **par document** (pas par chunk), préfixé à tous les chunks du document. Une seule génération par doc. Résultat empirique : réduit la « Document-Level Retrieval Mismatch » (chunks remontés depuis le mauvais document).

**Recommandation pour Assistant RH** :

1. Étape 1 — **SAC sur tout le corpus** (MATTE, SP, DGAFP, RGRH) : ~5 000 documents → ~5 000 appels LLM, coût marginal, gain attendu surtout sur DGAFP (où les articles sans section sont génériquement nommés).
2. Étape 2 — **Contextual Retrieval complet sur DGAFP uniquement** : la table la plus exigeante en rappel (chercher *l'article exact* d'un décret), où le coût par chunk est justifié.

Les deux passent dans `packages/data-engineering/` à la ré-ingestion, **pas** dans le pipeline runtime. Aucune latence ajoutée en requête.

### 3.3 CRAG et Adaptive-RAG formalisent ce que fait déjà le pipeline

**Références** :
- CRAG : Yan, Gu, Zhu, Ling, *Corrective Retrieval Augmented Generation*, arXiv:2401.15884 (janv. 2024).
- Adaptive-RAG : Jeong et al., NAACL 2024, arXiv:2403.14403.

**CRAG** ajoute un **évaluateur léger** (T5-0,77B fine-tuné) qui score la pertinence des chunks récupérés. Trois branches : `Correct` → contexte tel quel ; `Incorrect` → fallback web search + recomposition ; `Ambiguous` → mix. Reporté : +36,6 % sur PubHealth, +19 % sur PopQA. Plug-and-play (pas de réentrainement du générateur).

**Le pipeline actuel a déjà la plomberie** : `pipeline.py:337-400` implémente un retry du retrieval quand le selector LLM rejette tout (`enable_selector_retry=True`, `selector_retry_top_k=30`, `selector_retry_search_mode=HYBRID`). Mais le signal est **binaire** (rejet total) et **dépendant du même LLM** qui génère. CRAG propose d'**en faire un signal calibré, indépendant**.

**Recommandation** : remplacer le booléen `selector_all_rejected` par un score de pertinence calibré (modèle léger ou LLM-as-judge avec rubrique 0/1/2), seuils explicites. La logique de retry existe ; seul l'évaluateur change.

**Adaptive-RAG** ajoute un classifieur de complexité de requête qui route entre **pas de retrieval** / **single-hop** / **iterative**. Sur HotpotQA : +8,3 F1 ; sur les requêtes complexes : **78 % vs 34 %** de précision pour l'agentique sur le single-shot.

Le `QueryProcessor` ([query_processor.py](../../packages/rag-pipeline/src/assistant_rh_rag_pipeline/query_processor.py)) classifie déjà l'intent (`CHIT_CHAT`, `RAG_QUERY`, `OUT_OF_SCOPE`…). **Étendre la classification à la complexité** (`simple` / `multi_hop`) est une ligne JSON supplémentaire dans [intent.md](../../packages/rag-pipeline/src/assistant_rh_rag_pipeline/prompts/intent.md), pas un nouvel étage. Le pipeline route ensuite vers : `top_k` réduit / `top_k` étendu + décomposition.

**Recommandation** : sur les questions composées (« différences CDD/CDI **pour rémunération et congés** »), une décomposition en sous-requêtes est probablement le levier qualité le plus rapide à mettre en place une fois le goldset disponible.

### 3.4 Self-querying retriever : filtres métadonnées extraits de la requête

**Référence** : `SelfQueryRetriever` de LangChain ; cité par Anthropic, Cohere, et la littérature de retrieval juridique comme *« critical for legal / regulatory corpora »* (filtres date, juridiction, éditeur).

Le pipeline actuel **ne filtre pas en pré-retrieval** sur les métadonnées des tables. Exemples de requêtes pour lesquelles ce serait utile :

- « Quels sont les **derniers** décrets sur les CDD ? » → filtre par date sur `rag_chunks_dgafp`.
- « Les règles **du MATTE** pour les contractuels » → filtre éditeur.
- « L'article 6 du décret 86-83 » → filtre exact `article_id` (aujourd'hui résolu en post-traitement uniquement).

**Recommandation** : étendre la sortie JSON du `QueryProcessor` avec un champ `filters: { publisher?, date_range?, article_id?, theme? }`, appliqué dans les `WHERE` SQL du `Retriever`. C'est **un champ JSON supplémentaire** dans l'appel LLM déjà payé ; pas de surcoût.

### 3.5 Observabilité : nommer la spec et le backend

La note 03 trace la trajectoire (Scaleway Cockpit / Grafana corrélé à `chat_runs`). La recherche 2026 ajoute deux choix structurants qui se posent **maintenant**, pas plus tard :

1. **Convention sémantique des spans** : **OpenInference** (Arize, github.com/Arize-ai/openinference) est devenu la spec RAG-spécifique de facto. Elle définit `openinference.span.kind` avec les valeurs `RETRIEVER`, `RERANKER`, `EMBEDDING`, `LLM`, `CHAIN`, `TOOL`, `AGENT`, `EVALUATOR` — qui mappent 1-pour-1 sur les étages du pipeline. Les attributs (`document.score`, `retrieval.documents`, `reranker.input_documents/output_documents`, `embedding.model_name`) correspondent à ce que `PipelineResult.metadata` contient déjà. La spec OTel GenAI plus générale est encore expérimentale en 2026 (nécessite `OTEL_SEMCONV_STABILITY_OPT_IN=gen_ai_latest_experimental`).
2. **Backend candidat** : **Langfuse self-hosted** (MIT, Postgres + ClickHouse, parité avec la version cloud). Avantages pour le contexte État : **résidence des données** (déployable sur Scaleway), **pas de dépendance vendor** (vs LangSmith cloud-only, Arize AX SaaS), **support natif d'OpenInference**.

**Recommandation** : décider OpenInference comme spec d'instrumentation **avant** d'écrire le câblage évoqué dans la note 03. La spec est portable (Langfuse / Phoenix / Datadog la consomment tous). Cette décision a un coût nul si prise maintenant, et un coût de réécriture si prise après.

### 3.6 Reranking : connaître ce qu'on a, préparer un repli

Repères 2026 :

| Reranker | Contexte | Performance | Notes |
|---|---|---|---|
| Voyage rerank-2.5 (août 2025) | 32K | +7,94 % vs Cohere 3,5 (93 datasets) | instruction-following, hors-souveraineté pour l'État |
| Cohere Rerank 3.5 (déc. 2024) | 4K | baseline juridique solide | idem |
| BGE-reranker-v2-gemma (BAAI) | — | 0,568 nDCG@10 BEIR | **poids ouverts**, auto-hébergeable |
| Elastic Rerank (DeBERTa v3 184M) | — | 0,565 nDCG@10 BEIR (~10× plus petit) | **poids ouverts**, auto-hébergeable |
| Albert `/rerank` | inconnu publiquement | non benchmarké publiquement | **utilisé en prod** |

**Le pipeline appelle Albert `/rerank`** sans connaître précisément le modèle sous-jacent ni son comportement comparé. Le contexte souveraineté/État disqualifie Voyage et Cohere. En revanche, **BGE-reranker-v2-gemma et Elastic Rerank sont auto-hébergeables** et constituent un **repli défendable** si Albert `/rerank` venait à se dégrader ou s'avérer non compétitif sur le goldset.

**Recommandation** : après mise en place du goldset (P1 de la note 01), passer Albert `/rerank` vs BGE-reranker-v2-gemma auto-hébergé sur le goldset. C'est une décision **conditionnée à la mesure**, pas à prendre maintenant.

### 3.7 Échéance opérationnelle Albert : 15 février 2026

DINUM (Etalab) a annoncé l'abandon des alias `albert-*` au profit des alias `openweight-*`, avec **dual-alias jusqu'au 15 février 2026** (source : github.com/etalab-ia/albert). Le code utilise déjà ces alias :

- LLM : `openweight-large` et `openweight-medium` dans [config.py:194,205,219](../../packages/rag-pipeline/src/assistant_rh_rag_pipeline/config.py:194) (génération, fallback, intent).
- Embeddings : `openweight-embeddings` dans [embedder.py:28,68](../../packages/rag-pipeline/src/assistant_rh_rag_pipeline/embedder.py:28) (le model id Albert ; côté `RAGConfig`, l'`EmbeddingModel` enum n'utilise que les valeurs logiques `"albert"` / `"bge_scaleway"`, qui mappent ensuite vers ces alias dans `embedder.py`).

**À vérifier** :

- Tout `albert-*` résiduel dans les variables d'environnement, secrets GitHub, scripts d'ingestion, app Mastra.
- `apps/mastra-pipeline/` (port TS) en particulier.

Action : 30 min de `grep` avant la deadline. Pas un sujet de fond, mais un **calendrier**.

### 3.8 Embeddings : le plafond de qualité

**Référence** : [Legal RAG Bench, Isaacus 2025](https://huggingface.co/blog/isaacus/legal-rag-bench). Un embedder fine-tuné juridique (Kanon 2) bat GPT-5.2 et `text-embedding-3-large` de **~17,5 points** en correctness. Conclusion citée : *« Embedding choice sets the ceiling ; generator selection cannot compensate for retrieval failure. »*

Le pipeline utilise `openweight-embeddings` (Albert, 1024 dims) et `bge-multilingual-gemma2` (Scaleway, 3584 dims) — bons généralistes multilingues, **non spécialisés sur le droit français**. Il n'existe pas d'équivalent public français de Kanon 2 à notre connaissance en juin 2026. Si DINUM annonce un embedder « code-civil-tuned », ce serait probablement **le plus gros levier qualité** sur DGAFP, bien plus que toute optimisation du pipeline. À surveiller.

---

## 4. Anti-patterns écartés (ce qu'il **ne faut pas** faire)

| Tentation | Pourquoi écarter |
|---|---|
| **Porter le pipeline sur LangChain / LangGraph / Haystack / RAGFlow** | Le pipeline custom **implémente déjà** les patrons de ces frameworks (hybride + RRF, parent-document, fallback, anti-hallucination, observabilité). Coût de port >> bénéfice. Le port Mastra existant (`apps/mastra-pipeline/`, en pause selon la note 00) est déjà un front d'expérimentation framework ; n'en ouvrir pas un second. |
| **Boucle agentique complète (plan → retrieve → reflect → re-retrieve)** | Les modèles de raisonnement sont 10-74× plus coûteux ; pur GraphRAG : 100-1 000× d'appels LLM en plus ; LazyGraphRAG ramène ça à 0,1 % mais reste un sujet pour des corpus relationnels. Cas d'usage HR Q&A : latence cible < 5 s, sources bornées, gain attendu marginal. **CRAG + Adaptive-RAG (§3.3) plafonnent ici.** |
| **HyDE comme baseline** | L'option `enable_hyde` est dans `RAGConfig` mais non implémentée. La recherche 2026 la qualifie d'outil de niche (zero-shot domain transfer, requêtes très courtes), **pas de défaut**. Latence et risque d'hallucination dans le query path. **Recommandation : supprimer le flag**, ne pas implémenter. |
| **Sémantique chunking systématique** | Plusieurs études 2026 (Chroma Research, dev.to) montrent que la chunking sémantique apporte ~0 vs chunking récursif classique en deçà de ~5 000 tokens. SAC + Contextual Retrieval (§3.2) sont meilleurs ROI. |
| **Stop sur unicité d'un métrique auto** | RAGAS a échoué sur 83,5 % des exemples de production dans un benchmark indépendant ; DeepEval sur 58,9 %. **Aucune métrique auto n'est la vérité.** Le combo recommandé : **goldset humain + RAGAS + alerting drift** (cf. note 01 et §5). |

---

## 5. Feuille de route (priorisation issue de la recherche)

> **Hypothèse** : les P0 « réparer ce qui est cassé » des notes 00/01/07 (DGAFP embeddings, observabilité de base, `chat_runs`, RRF, goldset) sont en cours ou planifiés. Cette feuille de route concerne **les évolutions structurelles**, à séquencer **après ou en parallèle** des fondations.

| # | Item | Type | Lien notes | Risque | Quand |
|---|---|---|---|---|---|
| 1 | **Goldset 50-200 questions + triade RAGAS** (faithfulness, context_precision, context_recall, answer_relevancy) avec **modèle juge plus fort que le générateur** ; méthodologie inspirée de **LegalBench-RAG** (citations attendues au niveau snippet) | Mesure | 01 | Bas | **P1** — préalable à tout le reste |
| 2 | **Vérificateur post-génération HALT-RAG-style** (NLI calibré, seuil d'abstention) | Anti-hallu. | 04 (axe abstention) | Moyen | **P1.5** — dès que le goldset existe pour calibrer le seuil |
| 3 | **SAC** sur tout le corpus → puis **Contextual Retrieval** ciblé DGAFP | Ingestion | — | Bas | **P2** — coût et bénéfice mesurables sur goldset |
| 4 | **Instrumentation OpenInference → Langfuse self-hosted** (Scaleway) | Observa. | 03 | Bas | **P2** — à faire **avant** le câblage Cockpit/Grafana pour éviter ré-écriture |
| 5 | **Self-querying retriever** : champ `filters` ajouté à `QueryProcessor`, appliqué en SQL `WHERE` | Retrieval | — | Bas-Moyen | **P2** — extension de l'appel LLM déjà payé |
| 6 | **CRAG-style** : score de pertinence calibré remplaçant le booléen `selector_all_rejected` | Retrieval | — | Moyen | **P3** — la plomberie de retry existe déjà |
| 7 | **Adaptive-RAG** : champ `complexity` (`simple` / `multi_hop`) dans `QueryProcessor`, routage `top_k` + décomposition | Routage | — | Moyen | **P3** |
| 8 | **Bake-off reranker** Albert `/rerank` vs BGE-reranker-v2-gemma auto-hébergé sur le goldset | Décision | — | Bas | **P3** — décide conditionnellement à la mesure |
| 9 | `grep` des alias `albert-*` résiduels (env, secrets, Mastra) avant 15 fév. 2026 | Hygiène | — | Bas | **À faire avant le 15/02/2026** |
| — | Supprimer `enable_hyde` du `RAGConfig` (non implémenté + non recommandé) | Nettoyage | — | Nul | Quand pratique |
| — | Implémenter **ou** supprimer `enable_chunk_reranker` (no-op aujourd'hui) | Nettoyage | — | Nul | Quand pratique |

**Logique de séquencement** :

- (1) conditionne tout : sans mesure, aucun item suivant n'est arbitrable.
- (2) est plus haut que (3) parce que l'évidence du risque d'abstention dégradée (§3.1) en fait un sujet de sécurité produit, pas une amélioration.
- (3) et (4) sont indépendants et parallélisables.
- (5) est l'extension la moins risquée parmi les évolutions retrieval — pas de nouveau modèle, juste une colonne dans un appel LLM déjà payé.
- (6) et (7) évoluent du code existant, pas une réécriture.
- (8) est conditionnel à ce que la mesure révèle.

---

## 6. Comparaison synthétique avec les frameworks 2026

| Framework | Spécificité | Équivalent dans le pipeline | Verdict |
|---|---|---|---|
| **LangGraph 1.0** (oct. 2025) | Orchestration graphe + checkpointers | `Pipeline` linéaire + `_RunState` + retry selector | LangGraph gagne sur les flux multi-tours agentiques ; HR Q&A n'en a pas besoin. |
| **LlamaIndex** | RAG agentique hiérarchique doc-level, response synthesizers | `SectionAggregator` + phases `ContextBuilder` | Forme similaire, version locale plus sensible au juridique (refs Légifrance). |
| **Haystack 2.x** | Graphe de composants avec boucles + outputs partiels | Étages avec diagnostics explicites | Plus simple côté DGAFP ; Haystack utile si plug-and-play voulu — pas le cas. |
| **RAGFlow** | DeepDoc layout parsing, template chunking | Vit dans `packages/data-engineering/` (séparé) | DeepDoc mérite une comparaison **séparée** sur l'ingestion PDF/Markdown — hors périmètre de cette note. |
| **Elysia (Weaviate, août 2025)** | Decision-tree agentique + DSPy | `QueryProcessor` + `ContextSelector` | Elysia échange latence contre flexibilité ; le choix latence-first ici est correct. |

---

## 7. Sources & références

### Architecture et frameworks RAG 2026

- *RAG in 2026: A Practical Blueprint*, dev.to. https://dev.to/suraj_khaitan_f893c243958/-rag-in-2026-a-practical-blueprint-for-retrieval-augmented-generation-16pp
- *LangChain vs LangGraph Comparison 2026*, Digital Applied. https://www.digitalapplied.com/blog/langchain-vs-langgraph-comparison-2026
- *Best Open-Source RAG Frameworks (2026)*, Firecrawl. https://www.firecrawl.dev/blog/best-open-source-rag-frameworks
- *RAGFlow 0.23.0 release notes*, InfiniFlow Medium. https://medium.com/@infiniflowai/ragflow-0-23-0-advancing-memory-rag-and-agent-performance-e5901a853b09
- *Elysia: Building an end-to-end agentic RAG app*, Weaviate. https://weaviate.io/blog/elysia-agentic-rag

### Retrieval avancé & chunking

- Anthropic, *Introducing Contextual Retrieval* (sept. 2024). https://www.anthropic.com/news/contextual-retrieval
- Reuter et al., *Towards Reliable Retrieval in RAG Systems for Large Legal Datasets — Summary-Augmented Chunking (SAC)*, arXiv:2510.06999 (oct. 2025).
- Günther, Mohr, Williams, Wang, Xiao, *Late Chunking: Contextual Chunk Embeddings Using Long-Context Embedding Models*, arXiv:2409.04701 (sept. 2024, rev. juil. 2025).
- *ColBERT in Practice*, Sease.io (nov. 2025). https://sease.io/2025/11/colbert-in-practice-bridging-research-and-industry.html
- Khattab & Zaharia, *ColBERT*, arXiv:2004.12832 ; Santhanam et al., *ColBERTv2*, arXiv:2112.01488.

### Reranking

- *Voyage rerank-2.5 / rerank-2.5-lite* (août 2025). https://www.mongodb.com/company/blog/product-release-announcements/rerank-2-5-and-rerank-2-5-lite-instruction-following-rerankers
- *Cohere Rerank 3.5* (déc. 2024). https://cohere.com/blog/rerank-3pt5
- *Elastic Semantic Reranker — Part 2*. https://www.elastic.co/search-labs/blog/elastic-semantic-reranker-part-2

### RAG agentique

- Asai et al., *Self-RAG*, arXiv:2310.11511 (ICLR 2024).
- Yan, Gu, Zhu, Ling, *Corrective Retrieval Augmented Generation (CRAG)*, arXiv:2401.15884 (janv. 2024).
- Jeong, Baek, Cho, Hwang, Park, *Adaptive-RAG*, arXiv:2403.14403 (NAACL 2024).
- Sarthi et al., *RAPTOR*, arXiv:2401.18059 (ICLR 2024).

### Évaluation

- *RAGAS*. https://github.com/explodinggradients/ragas
- Saad-Falcon, Khattab, Potts, Zaharia, *ARES*, arXiv:2311.09476 (nov. 2023, rev. mars 2024).
- *TruLens (Snowflake)*. https://www.trulens.org/
- *LegalBench-RAG (Isaacus)*. https://huggingface.co/blog/isaacus/legal-rag-bench

### Anti-hallucination & abstention

- *Hallucination Detection in LLMs Using Calibrated NLI Ensembles* (HALT-RAG), arXiv:2509.07475 (sept. 2025).
- *Does RAG Improve LLM Calibration?* (le constat « RAG dégrade l'abstention »), arXiv:2411.06037 (ICLR 2025).
- Contextual AI, *Grounded Language Model (GLM)* (mars 2025). https://contextual.ai/blog/introducing-grounded-language-model
- Anthropic, *Citations API* (jan. 2025). https://simonwillison.net/2025/Jan/24/anthropics-new-citations-api/

### RAG juridique / gouvernemental

- Harvard Library Innovation Lab, *Open French Law RAG* (janv. 2025). https://lil.law.harvard.edu/blog/2025/01/21/open-french-law-rag/
- de Martim, *An Ontology-Driven Graph RAG for Legal Norms (LRMoo)*, arXiv:2505.00039v4 (août 2025).
- AgentPublic, *legi* (Légifrance pré-chunkée). https://huggingface.co/datasets/AgentPublic/legi
- Etalab-IA, *Albert source*. https://github.com/etalab-ia/albert

### Observabilité

- *OpenInference Semantic Conventions*. https://github.com/Arize-ai/openinference/blob/main/spec/semantic_conventions.md
- *OpenTelemetry GenAI Spans*. https://opentelemetry.io/docs/specs/semconv/gen-ai/gen-ai-spans/
- *Langfuse vs Phoenix vs LangSmith*. https://langfuse.com/faq/all/best-phoenix-arize-alternatives
- *Agent Observability Platforms 2026*, Digital Applied. https://www.digitalapplied.com/blog/agent-observability-platforms-langsmith-langfuse-arize-2026
