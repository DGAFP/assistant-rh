# Audit A5 — I/O, état mutable et consommateurs du runtime RAG

> Périmètre audité : `packages/rag-pipeline/src/assistant_rh_rag_pipeline/` à la révision `013a236` de `origin/dev`, le 2026-09-02.
> Décisions cibles : [D3 à D5 et D13](06-decisions.md). Écarts suivis dans le [LEDGER](LEDGER.md#écarts-disolation-a5).

Cet inventaire est la baseline d'isolation du runtime Python servi. Il couvre les 21 modules Python du package, les prompts embarqués et leurs consommateurs directs. Il décrit le comportement à préserver ; il n'autorise aucune amélioration de qualité pendant l'extraction.

## Règle bloquante avant une extraction de phase C

Toute PR de phase C qui extrait ou remplace un module du runtime historique doit, **avant le déplacement du code** :

1. repartir de la ligne correspondante de cet audit et refaire les recherches de consommateurs dans `apps/`, `packages/`, `src/`, `tests/`, `scripts/` et `.github/workflows/` ;
2. compléter la ligne si une I/O, un état, un champ de diagnostic ou un consommateur manque ;
3. lier chaque dépendance à une donnée pure, un port, un adaptateur ou au `RunContext` ;
4. ajouter au LEDGER tout nouvel écart, avec un propriétaire de phase/PR et un statut ;
5. figer les sorties et l'ordre concernés dans M0b ou dans une fixture de conformance dédiée.

La revue de la PR est bloquée si une dépendance reste « implicite », si un écart n'a pas de propriétaire, ou si l'ordre observable ne possède pas de règle de départage. Cette règle s'applique aussi aux extractions partielles : C2 pour le query processor, C3 pour le retrieval, C4 pour l'agrégation/contexte, C5 pour selector/generator et C6/C7 pour l'orchestration, l'état et le streaming.

Commandes de contrôle minimales, à adapter au module :

```bash
rg -n "assistant_rh_rag_pipeline|packages/rag-pipeline" apps packages src tests scripts .github/workflows
rg -n "os\.getenv|psycopg|sqlalchemy|requests|OpenAI|last_|cache|time\.|datetime|uuid|Thread|Executor" \
  packages/rag-pipeline/src/assistant_rh_rag_pipeline
```

## Classification cible

| Classe | Contenu autorisé | Exemples de cet audit |
|---|---|---|
| **Donnée pure** | valeur immuable et sérialisable, sans lecture d'environnement ni ressource ouverte | `RAGConfig`, scope ministère, chunks, sections, règles de tri |
| **Port** | capacité requise par un cas d'usage, exprimée dans `assistant_rh_api.core` | `SearchPort`, `PromptStorePort`, `ClockPort`, `LLMPort` |
| **Adaptateur** | DSN, SQL, pool, HTTP, fichier, cache technique, thread ou SDK | `db/search.py`, gateways Albert/Scaleway, export OTLP |
| **`RunContext`** | état mutable appartenant à une seule requête, jamais partagé | ids, snapshots, timings, tentatives, diagnostics, prompts rendus, statut d'annulation |

Les pools, clients réellement sûrs et caches techniques peuvent être partagés par les adaptateurs. Leur contrat doit alors préciser la synchronisation, la durée de vie, l'invalidation et la fermeture. Aucun résultat métier ni diagnostic de requête ne peut vivre dans ces objets partagés.

## Registre des consommateurs

Les codes courts employés dans les cartes de modules renvoient à cet inventaire exact.

| Code | Consommateurs directs constatés |
|---|---|
| `CHAT` | `apps/streamlit-ui/pages/01_Chatbot.py`; `src/ui/chatbot_llm.py`, `chatbot_logging.py`, `chatbot_sources.py`, `citation_deduplicator.py`, `db_utils.py`, `feedback_dashboard.py`, `llm_selector.py`, `user_groups_store.py` |
| `ADMIN` | `apps/streamlit-ui/pages/03_Feedback_Dashboard.py`, `04_Admin_Config.py`, `06_Goldset_Explorer.py`, `14_User_Groups.py` |
| `EVAL-UI` | `apps/streamlit-ui/pages/08_Chunking_Evaluation.py`, `09_Pipeline_Evaluation.py`, `10_Intent_Gater_Evaluation.py`; archive `11_Golden_Beta_Analysis.py` |
| `GOLD` | `src/goldset/eval.py`; `src/suivi_tests/campaign.py` |
| `SCRIPTS` | `capture_rag_parity_evidence.py`, `dump_stage_baselines.py`, `generate_human_review_answers.py`, `migrate_ministry_agnostic_prompts.py`, `parity_revision.py`, `probe_rag_query.py`, `run_candidate_conformance.py`, `suivi_tests_replay.py` |
| `TESTS` | `test_chat_logging.py`, `test_chatbot_sources.py`, `test_citation_extractor.py`, `test_conformance_legifrance_jsonl.py`, `test_conformance_metrics.py`, `test_context_selector.py`, `test_core_modules.py`, `test_db_helpers_dsn_resolution.py`, `test_feedback_analyzer.py`, `test_issue_300_persona_prompt.py`, `test_ministry_prompt_rendering.py`, `test_ministry_scope.py`, `test_pipeline_e2e.py`, `test_rag_quality_eval.py`, `test_rag_tracing.py`, `test_reranker_contract.py`, `test_retriever_determinism.py`, `test_retriever_probes.py`, `test_retriever_r2_dedup.py`, `test_section_aggregator_r2_dedup.py`, `test_sp_recall_goldset.py` |
| `CI` | `.github/workflows/ci-tests.yml` importe trois modules ; `.github/workflows/rag-quality-eval.yml` déclenche l'éval lors d'un changement du package |

`apps/api` n'importe pas encore le runtime : à cette révision, il ne contient que le squelette et le healthcheck A4/A6. Les dépendances entre modules du package sont indiquées par `INT` dans les tableaux suivants.

La recherche ne trouve aucun import direct dans `packages/data-engineering` ni `packages/shared-config`. `packages/rag-pipeline` reste cependant une dépendance workspace de l'application racine et entre dans les images Streamlit.

## Carte du chemin de production

| Module | Consommateurs | Dépendances et état actuels | Cible d'extraction |
|---|---|---|---|
| `pipeline.py` | `INT` (`__init__`, typing de `chat_logger`); `CHAT`; `EVAL-UI`; `GOLD`; `SCRIPTS`; `TESTS`; `CI` | Résout le DSN si absent ; construit tous les stages ; mesure avec `time.time()` ; crée un trace id aléatoire ; accumule tentatives, timings et traces. `_RunState` est local, mais `last_result`, `last_query_result`, `_timing`, `_selector` et les diagnostics du générateur restent partagés. Le streaming oblige le consommateur à lire `last_result` après épuisement. | `core/pipeline/orchestration.py` reçoit ports, config et un `RunContext` par appel. Les méthodes retournent un résultat explicite ; le flux final transporte aussi son résultat terminal. Horloge et ids sont injectés au cas d'usage. Aucun `last_*`. |
| `query_processor.py` | `INT` (`pipeline`); `EVAL-UI`; `GOLD` importe aussi le privé `_fold`; `TESTS` | Charge les acronymes par SQL au constructeur ; charge le prompt DB/fichier et instancie un client LLM à chaque classification ; rend le ministère ; normalise NFC ; règles regex légales pures. `_acronyms` est un snapshot mutable dont la fraîcheur dépend de la durée de vie de l'objet. | Règles dans `core/pipeline/steps/query_processor.py`; `AcronymStorePort`, `PromptStorePort`, `LLMPort`. Snapshot acronymes/prompt, révisions et réponse brute vont dans le `RunContext`; regex et parsing restent purs. |
| `retriever.py` | `INT` (`pipeline`); `EVAL-UI` appelle directement `pipe._retriever`; `TESTS` | Appel embeddings, `ThreadPoolExecutor`, connexions psycopg par table et recherche de titres, introspection de schéma, SQL vectoriel/lexical/hybride, `SET ivfflat.probes`, RRF, normalisation, dédup R2 et chronométrage sont mêlés. `_embedder.last_model_used` et `_table_columns_cache` sont partagés. Les erreurs non scopées donnent des résultats partiels ; les erreurs scopées remontent. | `EmbeddingPort` + `SearchPort` retournent candidats/rangs bruts et erreurs typées. `db/search.py` porte uniquement allowlist, SQL, mapping de lignes et session DB. Fusion/RRF, score plafond, heading match, dédup R2, gates, top-k et politique strict/partiel restent dans `core/.../retrieval.py`. Cache de colonnes dans l'adaptateur, borné et synchronisé ; diagnostics dans le `RunContext`. |
| `section_aggregator.py` | `INT` (`pipeline`); `EVAL-UI` appelle `pipe._aggregator`; `TESTS` | Charge sections/documents par SQL, groupe et score, puis appelle le reranker HTTP. Lazy `_reranker` partagé ; `time.time()` ; mutation du score des objets sélectionnés. Les diagnostics sont déjà retournés comme valeur. | `ContentStorePort` fournit les métadonnées ; `RerankerPort` renvoie indices/scores et issue provider. Agrégation, pondération, fusion R2, troncature et départages restent purs dans `core/.../aggregation.py`; diagnostics ajoutés au `RunContext`. |
| `context_selector.py` | `INT` (`pipeline`, export package); `EVAL-UI`; `TESTS` | Lit prompt DB/fichier, appelle LLM, rend le ministère. `_last_decisions`, `_last_raw_response`, `_last_reasoning`, `_last_prompt_chars` sont mutés. Échec/parsing invalide conserve le top 5 ou toutes les sections selon le point d'échec ; rejet JSON vide déclenche le no-answer/retry. | `PromptStorePort` + `LLMPort`; composition/parsing/top-up purs. Retourner `SelectionResult(sections, decisions, raw, reason, outcome)` ; copier ses diagnostics dans le `RunContext`. |
| `context_builder.py` | `INT` (`pipeline`, `generator` pour le formatter); `EVAL-UI` appelle `pipe._context_builder`; `TESTS` | SQL vers `rag_documents` et `rag_chunks_dgafp`, budget, document entier, triangulation et refs dans la même classe. `last_resolved_refs` est partagé ; les `ContextItem` sont enrichis/mutés. | `ContentStorePort` charge documents et références en lot. Les règles de budget/triangulation/ordre restent dans `core/.../context_builder.py`. Retour explicite `ContextBuildResult(items, resolved_refs, diagnostics)` ; refs dans le `RunContext`. Formatter pur séparé pour éviter l'import croisé du générateur. |
| `generator.py` | `INT` (`pipeline`); `TESTS` | Charge le prompt DB/fichier, ajoute une règle en code, rend le ministère et appelle le fallback LLM. Cache `_base_prompt` sans invalidation, client `_llm` partagé, compteurs/provider par requête et `last_full_prompt`/`last_system_prompt` mutables. | Composition dans `core/prompt_policy.py` et step generator ; `PromptStorePort` + `LLMPort`. `GenerationResult`/événement terminal porte provider, modèle, fallback, prompts ou hashes. Cache par révision dans l'adaptateur ; aucun diagnostic sur le client partagé. |

## Carte des modules de support et de contrôle

| Module | Consommateurs | Dépendances et état actuels | Cible d'extraction |
|---|---|---|---|
| `__init__.py` | `CHAT`; `EVAL-UI`; `GOLD`; `SCRIPTS`; `TESTS` | Réexporte core, clients réseau et `get_dsn`; `create_pipeline()` déclenche la résolution du DSN via `Pipeline`. | `assistant_rh_api.core` n'expose que données/ports/cas d'usage sans effet de bord. La factory avec environnement vit dans le wiring (`handlers/app.py` ou runner d'éval). |
| `config.py` | Presque tous les modules `INT`; `CHAT`; `ADMIN`; `EVAL-UI`; `GOLD`; `TESTS`; `CI` | Dataclasses mutables, mais noms des tables de comparaison lus via `os.getenv()` à l'import ; `CHUNK_TABLES` dict global mutable ; réexports tardifs de `db_helpers` malgré l'annonce « pure ». | Schémas/config immuables dans `core/config.py`. Noms physiques et variables d'environnement deviennent configuration d'adaptateur. Supprimer les réexports DB ; snapshot de config + révision dans le `RunContext`. |
| `models.py` | Tous les stages principaux `INT`; `CHAT`; `GOLD`; `SCRIPTS`; `TESTS`; `CI` | Dataclasses et sérialisation pures ; métadonnées libres et modèles mutables ; `Chunk` est un type de compatibilité UI. | Modèles immuables/DTO du core et résultats de stage explicites. Adaptateurs exclusivement publics supprimés en F3 ; compatibilité encore requise par l'admin conservée jusqu'à son repointage. |
| `ministry_scope.py` | `INT` (`pipeline`, query/selector/generator, export package); `CHAT`; `ADMIN`; `EVAL-UI`; `GOLD`; `SCRIPTS`; `TESTS` | Catalogue et placeholders en dictionnaires globaux ; dataclasses gelées ; aucune I/O. `resolve_ministry` échoue volontairement en mode souple, contrairement à `build_retrieval_scope`. | Données/règles pures dans `core/ministry_scope.py`; catalogue rendu immuable. Conserver et tester séparément les contrats fail-closed d'autorisation et fallback générique de rendu. |
| `db_helpers.py` | `INT` (config/admin et quatre stages); `CHAT`; `ADMIN`; `EVAL-UI`; `GOLD`; `SCRIPTS`; `TESTS` | Lit DSN/env, ouvre des connexions psycopg, crée des engines/pools SQLAlchemy, CRUD config/prompts/acronymes, lit les prompts fichiers et appelle l'horloge. Plusieurs échecs deviennent `{}`, `None` ou fallback local. | Éclater en `db/dsn.py`, `db/config_store.py`, `db/prompt_store.py`, `db/acronym_store.py` et adaptateur de ressources embarquées. Ports distincts ; pool créé/fermé par le wiring ; erreurs/fallbacks typés ; `ClockPort` pour `{today}`. |
| `llm_client.py` | `INT` (query/selector/generator, export package); `CHAT` (`chatbot_llm`, `llm_selector`); `TESTS` indirects | Lit clés/URLs env, construit le SDK OpenAI. `FallbackLLMClient.last_provider_used` et `fallback_count` sont partagés ; fallback pré-premier-token, mais erreur milieu de stream émet un texte partiel. | Gateways Albert/Scaleway implémentant `LLMPort`, configurés au wiring. Chaque appel retourne son outcome provider/fallback ; préserver exactement les règles pré-token/mid-stream jusqu'à une décision séparée. |
| `embedder.py` | `INT` (`retriever`); `TESTS` | HTTP `requests`, env, normalisation numpy. `_cb` est un circuit breaker singleton process fondé sur `time.time()`, sans verrou ; `FallbackEmbedder.last_model_used` est mutable. | Gateways `EmbeddingPort`. Résultat explicite `(vector, model, dimensions, fallback)` ; circuit breaker d'adaptateur synchronisé, horloge injectée, politique et métriques testables. Aucun singleton dans le core. |
| `reranker.py` | `INT` (`section_aggregator`); `EVAL-UI`; `TESTS` | HTTP `requests`, env, lots de 40 ; scores inter-lots approximatifs ; fallback synthétique `1 - i*0,001`; tri `(-score, index)`. | `RerankerPort` + gateway Albert. Politique de lot/fallback et outcome explicites ; règle de tri reste dans le core ou dans un résultat contractuel testé. |
| `tracing.py` | `INT` (`pipeline`, `chat_logger`); `TESTS` | UUID et horloge murale, env OTEL, `requests.post`, thread daemon non rejoint. Reconstruit les timestamps à l'export depuis les durées ; échec non bloquant. Helpers de projection/JSON purs mêlés à l'export. | Helpers purs dans le core ; `TraceSinkPort` et adaptateur OTLP géré par le cycle de vie API. Ids/timestamps du `RunContext`; export asynchrone borné, drainable et sans secret dans les événements. |
| `chat_logger.py` | `CHAT`; `SCRIPTS`; `TESTS` | Construit la ligne en relisant `pipeline.last_result`, `pipeline._context_builder.last_resolved_refs`, prompts `last_*` et `_timing`; prend aussi l'état de session Streamlit. Horloge UTC, SQL dynamique, transaction `chat_runs`, seconde transaction `rag_trace_events`, CSV + `FileLock`, puis OTLP. | Builder pur à partir de `PipelineResult` + `RunContext` finalisés, jamais de l'objet pipeline. `ChatRunStorePort.finalize()` persiste run + événements atomiquement ; CSV et Postgres sont adaptateurs. Observabilité via port séparé après état durable. |
| `admin.py` | `CHAT`; `ADMIN`; `GOLD`; `SCRIPTS`; `TESTS` indirects | Validation/mapping mêlés au SQL/DDL, psycopg + engines créés par appel, horloge, transactions. `DEFAULT_CONFIG` est un objet global mutable retourné lors d'un échec DB. Les updates config font un read-modify-write non verrouillé. | Validation/mapping purs dans `core/config.py` pour les besoins du runtime ; Streamlit admin conserve temporairement ses adaptateurs directs. Révision/CAS, retrait du DDL runtime et éventuel `AdminService` deviennent un durcissement séparé ; défaut neuf/immuable. |
| `feedback_analyzer.py` | `ADMIN` (page feedback); `TESTS` | SQL lecture/résolution/update, engine, appel Albert `requests` avec retries, clé env, URL figée à l'import, horloge et classification pure mêlés. Batch sélectionne puis traite sans claim transactionnel. | `FeedbackService` + `FeedbackStorePort`, `ContentStorePort`, `LLMPort`, `ClockPort`. Claim/idempotence dans l'adaptateur DB ; règles de catégories/attribution pures. |
| `citation_extractor.py` | `CHAT` (`chatbot_sources`); `TESTS` | Regex et catalogue de décrets codé en dur ; aucune I/O ; ensembles puis tri final explicite. | Politique pure du core ou mapper de présentation selon le contrat sources ; catalogue comme donnée immuable versionnée. |
| `conformance.py` | `SCRIPTS` (`run_candidate_conformance`); `TESTS` | Calculs purs ; `statistics.quantiles`; le top-k est tranché avant un Jaccard qui ne mesure pas l'ordre interne. | Reste dans l'outillage d'éval, hors runtime déployé ; compléter par assertions de rang exact dans les fixtures M0b. |
| `prompts/*.md` | `db_helpers.load_prompt`, puis query/selector/generator ; `capture_rag_parity_evidence.py` lit le dossier directement | Ressources paquet après lookup DB ; fallback par nom (`intent.md`, `selector.md`, `generator.md`) puis parfois constante en code. `{today}` dépend de l'horloge ; politiques complémentaires peuvent être ajoutées en code. | Adaptateur composite `PromptStorePort` retourne contenu brut, origine et révision. Rendu date/ministère et règles obligatoires dans `prompt_policy.py`; hash/origine consignés dans le `RunContext`. |

## Diagnostics à rendre explicites

| Lecture historique | Données requises par le logger, l'éval ou la trace | Sortie cible |
|---|---|---|
| `pipeline.last_result` | réponse, sources, context items, metadata et timings | `PipelineResult` retourné au `ChatService` |
| `pipeline.last_query_result` et `_timing` | intent, thème, reformulation, acronymes, legal-search et durée query | `QueryProcessResult` + événement/timing du `RunContext` |
| `ContextSelector.last_*` | kept/removed, rejet total, raison, réponse brute, tailles prompt/réponse | `SelectionResult` attaché à chaque tentative retrieval |
| `ContextBuilder.last_resolved_refs` | nombres, CID, URL et titre effectivement résolus/injectés | `ContextBuildResult.resolved_refs` |
| `StreamingGenerator.last_*`, `provider_used`, `fallback_count` | prompts utilisateur/système, provider/modèle réel et bascule | `GenerationResult` ou événement terminal de stream |
| `FallbackEmbedder.last_model_used` | modèle, dimension/colonne et fallback de l'embedding courant | `EmbeddingResult` retourné par le port |
| diagnostics d'agrégation | chunks avant/après, nombre de sections, statut/erreur reranker | `AggregationResult`, déjà proche du contrat cible |
| `session_state` Streamlit + horloge du logger | conversation/groupe/index de tour, ids, timestamp et environnement | commande `ChatRequest` + identité/horloge du `RunContext`; label d'environnement fourni au sink |

Les contenus bruts nécessaires à la preuve peuvent rester bornés dans le résultat de diagnostic, mais le record de persistance ne doit jamais reconstruire ces valeurs à partir d'un singleton ou d'un objet de stage.

## I/O, transactions et cycle de vie

| Sujet | Contrat historique à caractériser | Frontière cible |
|---|---|---|
| Résolution DSN | Si `APP_DB_TARGET` est défini, seule la valeur `scaleway` est acceptée et exige `SCW_POSTGRES_DSN`. Sinon priorité `SCW_POSTGRES_DSN` → `APP_POSTGRES_DSN` → `STREAMLIT_POSTGRES_DSN`. Conversion SQLAlchemy et ajout de `sslmode=require`. | Settings validés au démarrage dans le wiring ; `db/dsn.py` ne fuit ni DSN ni environnement dans le core. |
| Connexions/pool | Les stages ouvrent des connexions psycopg courtes. Le retrieval peut lancer jusqu'à deux tâches par table (chunks + headings), plus l'introspection. `create_engine_from_env` crée un pool `2 + 1 overflow`, mis en cache seulement par certains consommateurs Streamlit. | Un pool applicatif borné, métriqué et fermé au shutdown. Budget de concurrence retrieval aligné sur le pool ; aucun pool créé dans une méthode métier. |
| État de session SQL | `ivfflat.probes` est posé avec `SET` sur une connexion dédiée aujourd'hui. Sur un pool, ce réglage pourrait contaminer la requête suivante. Tables/colonnes sont interpolées depuis les catalogues, les valeurs sont bindées. | Allowlist dans l'adaptateur ; transaction dédiée avec `SET LOCAL ivfflat.probes`; reset garanti et test de non-fuite entre deux requêtes. |
| Transactions admin | Config et certains CRUD font read puis write sur des connexions différentes ; aucun verrou/révision ne protège les mises à jour concurrentes. Les DDL d'initialisation sont exécutés depuis les pages. | Migrations versionnées ; transaction unique et compare-and-swap sur révision pour les mutations. |
| Persistance d'un run | `chat_runs` et `rag_trace_events` sont upsertés séparément ; l'OTLP part dans un thread daemon. Une réponse peut donc exister sans événements, ou un processus finir avant export. | `ChatRunStorePort.finalize` atomique pour le durable. Export OTLP après commit, best effort mais géré ; en streaming, persister avant le chunk final et `[DONE]`. |
| Feedback batch | Les feedbacks non analysés sont lus puis mis à jour un par un, sans réservation ; deux workers peuvent analyser la même ligne. | Claim transactionnel (`FOR UPDATE SKIP LOCKED` ou état équivalent), traitement idempotent et propriétaire explicite. |
| Prompts/config/acronymes | Config Streamlit : cache 15 s. Acronymes : snapshot au constructeur du query processor. Prompt generator : cache pour la vie du générateur ; query/selector : lookup par appel. DB indisponible et valeur absente sont souvent confondues avec un fallback. | Stores avec révision + cache TTL explicite ; snapshot cohérent par requête. Origine, révision, fallback et erreurs figurent dans le résultat/trace. |
| Providers | LLM via SDK OpenAI ; embeddings/reranker/feedback/OTLP via `requests`. Timeouts, retries et fallback diffèrent par module. | Gateways configurés, timeouts/deadlines/cancellation cohérents, outcomes typés. Secrets uniquement dans les adaptateurs. |

## `RunContext` minimal

Une instance est créée à la frontière du `ChatService` et n'est accessible qu'au worker de la requête. Elle peut être mutée pendant le run puis figée dans le résultat.

- identité : `turn_id` non tronqué, `trace_id`, `conversation_id`, groupe, modèle/ministère et `RetrievalScope` autorisé ;
- contrôle : instant UTC de départ, horloge monotone pour les durées, deadline, statut `running|completed|failed|cancelled` et signal d'annulation ;
- snapshots : config runtime + révision, acronymes + révision, prompts bruts/rendus + nom/origine/révision/hash ;
- sorties de stages : query process result, tentatives retrieval, chunks/sections/context retenus, références résolues et décisions de selector ;
- providers : modèle réellement utilisé, fallback, timeouts/erreurs et compteurs **de cette requête** ;
- observabilité : timings, événements bornés, erreurs et données nécessaires au `ChatRunRecord`.

Le `RunContext` ne contient jamais de DSN, secret, pool, connexion, client HTTP/SDK, cache process ni objet Streamlit.

## Frontière sensible retrieval métier / SQL

Le port ne doit pas reproduire la classe `Retriever` actuelle sous un autre nom.

```text
core retrieval                         SearchPort / db-search adapter
───────────────────────────────────    ─────────────────────────────────────
scope et tables autorisées          -> table logique déjà autorisée
mode + force-hybrid DGAFP           -> requêtes vectorielle/lexicale/heading
RRF intra-table et inter-source     <- candidats, scores/rangs bruts, métadonnées
fusion heading/chunk et dédup R2       mapping lignes -> RawScoredChunk
normalisation source-ceiling           SQL pgvector/tsvector et bind des valeurs
politique erreur stricte/partielle      SET LOCAL ivfflat.probes
top-k + départages déterministes        transaction/pool/timeout
```

L'adaptateur peut optimiser le transport SQL, mais il ne décide ni la pondération `alpha`, ni la fusion, ni le gate, ni la déduplication, ni le nombre final d'éléments. Une optimisation qui fait calculer un rang par Postgres doit exposer assez de données pour prouver la même décision pure en replay.

Autres frontières à ne pas franchir :

- `ContentStorePort` charge sections, documents et références ; il ne décide pas document-entier, budget ou triangulation ;
- `PromptStorePort` fournit contenu/origine/révision ; la composition ministère/date et les règles anti-hallucination restent dans le core ;
- les gateways provider transportent appels/fallbacks ; la sélection de contexte, le no-answer et les traces métier restent dans le core ;
- `ChatRunStorePort` reçoit un record complet ; il ne relit jamais le pipeline ni l'état du handler.

## Déterminisme à préserver et à renforcer

| Étape | Règle observable |
|---|---|
| Entrée | Normaliser la question en NFC avant LLM, cache et retrieval. Les fixtures fournissent horloge, ids, config, prompts, acronymes et réponses provider. |
| SQL classé | Tout `ORDER BY score/rank` finit par un identifiant stable. Une collection issue de `set`, `ANY(...)` ou d'une requête sans ordre est remappée par clé ou triée avant de devenir observable. |
| Parallélisme retrieval | L'ordre de fin des futures n'intervient jamais. Trier les sources logiques, puis chaque liste par `(-score, -heading_match_score, table_source, chunk_id, section_id)`. |
| Fusion | RRF parcourt les sources triées ; la sortie fusionnée reprend la clé complète `(-score, -heading_match_score, table_source, chunk_id, section_id)`, y compris après normalisation. La normalisation conserve le nombre exact de sources ayant participé, y compris la politique actuelle sur les sources vides/échouées. |
| Agrégation | Les groupes suivent le rang retrieval. Score descendant ; à égalité, ordinal du premier chunk puis identifiants stables. Reranker : `(-score, original_index)`. Son fallback conserve l'ordre d'entrée. |
| Selector | Indices LLM dans l'ordre rendu, doublons supprimés sans réordonner ; top-up par indices d'entrée croissants ; distinguer rejet explicite vide, parse invalide et exception. |
| Context builder | Document entier et sections partent de l'ordre agrégé ; tri final `(-score, is_doc_entire)` puis ordinal d'entrée. Triangulation suit l'ordre d'entrée et peut dépasser le budget comme aujourd'hui. Les refs sont ordonnées avant sérialisation. |
| Citations/traces | Citations triées articles puis décrets et numéro ; ids/tables dédupliqués triés ; JSON canonique avec clés triées lorsqu'il sert de preuve/hash. |
| Mesures | Utiliser une horloge monotone pour les durées. Horodatages, UUID et latence sont injectés ou exclus des comparaisons exactes ; ils ne départagent jamais un résultat métier. |

Les tris stables implicites du code historique ne suffisent pas comme contrat cible : chaque port doit fournir un ordinal ou une clé stable. Les trous connus sont suivis par `A5-11` dans le LEDGER.

## Risques de concurrence prioritaires

1. Deux appels sur le même `Pipeline` peuvent croiser `last_result`, `_timing`, les prompts du générateur et son provider/fallback ; le logger peut alors attribuer le contexte d'une requête à une autre.
2. Deux embeddings concurrents peuvent écraser `FallbackEmbedder.last_model_used` entre le retour du vecteur et le choix de colonne, mélangeant dimension/modèle et SQL. Le circuit breaker `_cb` est lui aussi non synchronisé.
3. Un cache de prompt/acronymes/config sans révision peut servir deux tenants avec des snapshots d'âges différents ou ignorer une mise à jour admin.
4. Le fan-out de connexions du retrieval se multiplie par le nombre de workers API et peut dépasser le pool/DB.
5. Les mises à jour config et le batch feedback peuvent perdre une mutation ou dupliquer un traitement.
6. Le thread OTLP daemon et les deux transactions de logging ne donnent aucune garantie de complétude à la fin du stream.

Ces risques sont tolérés uniquement dans le chemin historique à courte durée de vie. Le wiring API ne doit pas partager les objets actuels en attendant C6 : les adaptateurs B1/B3 partagés doivent être sûrs avant d'être branchés au core.
