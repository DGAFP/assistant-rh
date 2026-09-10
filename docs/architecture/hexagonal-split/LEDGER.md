# LEDGER — journal du chantier hexagonal-split

> Journal append-only. **Chaque PR du chantier amende ce fichier.** C'est le point d'entrée pour connaître l'état du chantier sans lire les PRs.
> Trois sections : Avancement · Reports depuis le runtime existant (dette de parité, vide aux jalons M1/M2/M3/M4) · Améliorations pour après le merge.

## Avancement

| Date | PR / jalon | Quoi |
|---|---|---|
| 2026-08-21 | PR 0 | Plan validé (grilling) ; docs `docs/architecture/hexagonal-split/` créées. Prochaine étape : jalon M0 (re-baseline goldset) puis création de `feat/hexagonal-api`. |
| 2026-08-25 | PR 0 — amendement | Revue d'architecture intégrée : `packages/rag-core` séparé, reconstruction parallèle sur `dev`, audit d'isolation B0 élargi, état par requête/SSE spécifiés, spikes `conversations` + Scaleway avancés en phase 0, API dark, bascule Streamlit sous feature flag, preuves déterministe/live séparées et suppression de l'ancien runtime reportée après stabilité. |
| 2026-08-25 | PR 0 — second amendement | Feedback de revue intégré : décisions sorties de l'overview et catégorisées ; core replacé dans `assistant_rh_api/core` ; prompts reliés explicitement au pipeline ; ordre DB/adaptateurs → auth → models → completion par étapes ; extraction comportementale plutôt que déplacement de fonctions ; auth admin alignée sur les groupes/rôles ; seulement local + VM homelab avant le premier déploiement Scaleway staging. |
| 2026-08-26 | PR 0 — lisibilité | Contexte, architecture cible, contrat et six séquences conservés ; répétitions raccourcies, préparation regroupée sans retirer les étapes DB/adaptateurs puis extraction ; premier Mermaid corrigé (point-virgule non échappé). |
| 2026-09-01 | A1 — [PR #445](https://github.com/DGAFP/assistant-rh/pull/445), [issue #440](https://github.com/DGAFP/assistant-rh/issues/440) | Ancien pipeline TypeScript, dépendances, skill, workflows et documentation active supprimés. Les fixtures, contrats et outils Python/génériques de conformance sont conservés pour la reconstruction API ; aucun changement du runtime RAG servi. |
| 2026-09-01 | #439 — jalon M0a/M0b | Références de parité figées au commit `9bf1cf0` sans réglage qualité : run live #240, 98/98 sans erreur et métriques globales identiques à #230 ; [preuve M0a](../../evals/evidence/m0a_api_parity_dev_20260901.json) `ead6beec…` et [journal](../../evals/journal-experimentations-rag.md). Bundle M0b exact 7 fixtures / 56 artefacts, [mode d'emploi](../../../tests/conformance/M0_REPLAYS.md) et [manifest](../../../tests/conformance/baselines/m0-api-parity-dev-9bf1cf0/manifest.json) `f5e9ffef…`, vérifiés offline. q214/q676 restent incluses et la dette goldset #421 demeure hors jalon. |
| 2026-09-01 | A4/A6 — [PR #447](https://github.com/DGAFP/assistant-rh/pull/447), [issue #441](https://github.com/DGAFP/assistant-rh/issues/441) | Squelette installable `apps/api`, frontières `core`/`handlers`/`db`/`gateways`, probe `/healthz` sans initialisation RAG/provider, gardes import-linter, image API et base locale pgvector strictement synthétique ajoutés. Aucun changement du runtime RAG servi. |
| 2026-09-02 | A4/A6 — [PR #448](https://github.com/DGAFP/assistant-rh/pull/448), suivi de [#447](https://github.com/DGAFP/assistant-rh/pull/447) | Assets API regroupés sous `docker/api`, stack locale API + PostgreSQL synthétique lançable par `moon run api:local`, Moon 2.5.3 épinglé via Proto et tâches locales Moon validées. Le probe conteneurisé répond `200` sans accès aux données de staging. |
| 2026-09-02 | A4/A6 — [PR #450](https://github.com/DGAFP/assistant-rh/pull/450), suivi de [#447](https://github.com/DGAFP/assistant-rh/pull/447) | Hook `pre-push` ajouté pour exécuter les checks Moon ciblés de l'API (`smoke`, lint, frontières d'imports et tests) avant envoi ; le test PostgreSQL synthétique complet reste obligatoire en CI. |
| 2026-09-02 | A5 — [PR #451](https://github.com/DGAFP/assistant-rh/pull/451), [issue #442](https://github.com/DGAFP/assistant-rh/issues/442) | [Audit initial](07-runtime-isolation-audit.md) des 21 modules du runtime Python, prompts et consommateurs : I/O/transactions, état mutable, concurrence, frontières retrieval/SQL, cibles donnée/port/adaptateur/`RunContext` et règles de déterminisme consignés. Re-audit rendu bloquant avant chaque extraction C2–C7 ; aucun changement du runtime servi. |
| 2026-09-02 | A2 — [issue #443](https://github.com/DGAFP/assistant-rh/issues/443), local + homelab | Replay et probes reproductibles validés avec OpenAI SDK 2.38, la couche provider et l'instance Django de `conversations` 0.0.22 (Pydantic-AI 2.22 / OpenAI 2.52). Contrat amendé : texte en parts, limites 1 Mio/32 messages/64 Kio, tools client ignorés, chunk usage, erreur SSE OpenAI, catalogue statique et feedback du fork. Le catch client post-headers est consigné pour le fork ; proxy à revalider en D4. |
| 2026-09-04 | A3 — [issue #444](https://github.com/DGAFP/assistant-rh/issues/444) | [Matrice Streamlit et périmètre API](08-streamlit-api-parity.md) amendés : M4 borne le chemin public HTTP sans DB/pipeline direct ; l'admin/ops Streamlit garde un accès DB allowlisté et ses dépendances historiques sans bloquer la bascule ; endpoints admin et RAG-ops reportés ; sessions groupe 8 h, capabilities documentaires 15 min couvrant PostgreSQL legacy/S3 et feedback idempotent étoiles/raisons/commentaire figés. Grafana/Tempo, LangSmith, agentic RAG et `admin-hardening` restent hors chemin critique. |
| 2026-09-04 | A3 — durcissement après relecture de #469 | Migration feedback sans perte avant unicité, verrou du run pour les premières soumissions concurrentes, réanalyse IA après modification et audit par groupe/session pseudonyme actés. `chat_run_sources` devient l'autorité des seules sources finales ; toutes les étapes restent tracées. La présignature S3 est bornée à la capability et le frontend retente une fois par nouvelle demande authentifiée. La topologie documente le runtime legacy des évaluations admin sans imposer d'avertissement UI. |
| 2026-09-04 | A3 — clôture de relecture de #469 | Perte d'état Streamlit → réauthentification et logout → révocation API actés. Le deep-link passwordless `?group=<slug>` est discontinué dans les deux modes, rollback compris, et `default` exclu du catalogue. Les quotas login sont placés aux frontières Streamlit/API avec backoff temporaire sans lockout de groupe. Le clic source conserve la session par navigation Streamlit ; `_PDF_Viewer` rédime côté serveur un 200 PDF ou une 302 S3 et retente une seule fois, les refus restant terminaux. L'admin M4 documente ses deux chemins d'auth actuels ; les credentials DB minimaux restent dans `admin-hardening`. |
| 2026-09-07 | B1 — [PR #476](https://github.com/DGAFP/assistant-rh/pull/476), [issue #453](https://github.com/DGAFP/assistant-rh/issues/453) | Ports minimaux config/prompts/acronymes/horloge/ids et snapshots immuables, résolution DSN explicite, pool async borné géré par lifespan, transactions avec rollback/annulation, erreurs DB stables, révisions de contenu et cache TTL invalidable. Healthcheck branché sur cette fondation. 40 tests API sur PostgreSQL local exclusivement synthétique, mypy, Ruff, 3 gardes d'import et image Docker validés ; revue indépendante sans constat restant. Aucun changement du runtime RAG servi. Contrats search/auth/persistance/provider à préciser avec B2/B3 pour éviter des types métier spéculatifs. |
| 2026-09-08 | B1 — correction de revue [PR #476](https://github.com/DGAFP/assistant-rh/pull/476) | Contrôle de connexion supervisé sans annulation préalable de psycopg : fermeture du socket avant annulation pour borner les réponses réseau perdues. Deux tests via proxy TCP réel couvrent échéance, annulation du demandeur et remplacement de la connexion ; ils échouent sur le code initial. 42 tests API passent sur PostgreSQL synthétique local avec pgvector, Ruff et 3 gardes d'import verts ; revue indépendante sans constat restant. |
| 2026-09-08 | B2 — [PR #505](https://github.com/DGAFP/assistant-rh/pull/505), [issue #454](https://github.com/DGAFP/assistant-rh/issues/454) | Repositories PostgreSQL derrière ports typés : groupes/rôles/sessions, snapshots config/prompts/acronymes, recherche brute/contenu, finalisation atomique runs/sources/traces, feedback courant/audit/analyse. Migration additive avec archivage avant unicité, compatibilité INSERT Streamlit, IDs complets et clés étrangères historiques. 98 tests API sur PostgreSQL/pgvector local exclusivement synthétique ; 182 tests des consommateurs historiques passent, 1 test avec tunnel DB ignoré. Ruff, mypy et 3 gardes d'import verts ; deux P2 de revue indépendante corrigés et testés. Aucun déploiement ni accès staging/production. |

## Écarts d'isolation A5

Revue B2 — [PR #505](https://github.com/DGAFP/assistant-rh/pull/505) : raisons de feedback corrigées en collections immuables `tuple[str, ...]` dans le core, conformément au contrat A3. L'adaptateur seul encode/décode les chaînes historiques séparées par `;` ; aller-retour, retry sans audit, valeurs vides/NULL et représentations ambiguës couverts. 103 tests API sur base synthétique locale, Ruff, mypy et 3 gardes d'import passent.

Correction de coexistence B2 — [PR #505](https://github.com/DGAFP/assistant-rh/pull/505) : l'analyseur Streamlit capture `api_revision` avant l'appel LLM et conditionne l'écriture à cette génération encore non analysée ; le SQL reste compatible avec le schéma antérieur à la migration. Le verrou parent des écritures API devient `FOR NO KEY UPDATE`, compatible avec le `KEY SHARE` implicite des premières insertions legacy sous clé étrangère. Régressions sur remplacements API/legacy, A → B → A, analyse concurrente, schéma pré-migration et concurrence mixte avec FK : 109 tests API et 183 tests historiques passent, 1 test avec tunnel DB ignoré ; Ruff, mypy et 3 gardes d'import verts. Déployer l'analyseur protégé et terminer les anciens workers avant d'activer le trigger de remplacement. Aucun déploiement ni migration distante dans cette validation.

Suivi B2 — [issue #454](https://github.com/DGAFP/assistant-rh/issues/454) : adaptateurs PostgreSQL derrière ports explicites livrés pour groupes/rôles/sessions, config/prompts/acronymes, recherche brute/contenu, runs/sources/traces atomiques et feedback courant/audit/analyse. A5-03 : lectures fraîches sans cache implicite (TTL 0) ; composition du snapshot de requête et fallback ressources restent C2/C5. A5-04/05/11 : SQL allowlisté, probes locaux et départages explicites côté adaptateurs ; extraction/fusion métier et preuve de parité restent C3/C4. A5-06/07 : transaction de finalisation et stockage des IDs complets disponibles ; branchement moteur/stream/OTLP reste C6/C7. A5-10 : unicité feedback précédée d'archivage, verrou du parent, retry sans écriture et génération anti-analyse périmée livrés ; claim de batch admin reste hors M4. A5-12 : absence et indisponibilité distinctes.

Écarts découverts B2 : `acronyms.priority` absent du DDL admin historique (lecture compatible avec/sans colonne) ; markdown de section historiquement nommé `section_markdown` ou `markdown_content` ; Service-Public peut avoir un document sans section résolue (métadonnées canoniques conservées). Le trigger de compatibilité maintient les INSERTs Streamlit après unicité et mémorise l'ID de dernière soumission pour les insertions reçues hors ordre à timestamp égal. Les décisions produit B4/D1, les projections diagnostiques du moteur C6 vers les vues admin et les grants du rôle runtime sur l'audit restent à brancher avant exposition publique ; aucune bascule du runtime ni validation staging/production dans B2.

Suivi A5 B1 — A5-03 : primitive de cache synchronisée et révisions de contenu livrées ; TTL métier et cohérence entre stores/requêtes restent B2/C2/C5. A5-05 : pool borné et isolation transactionnelle `SET LOCAL` éprouvés ; extraction SQL/cache d'introspection reste C3. A5-08 : nouvelle configuration DB explicite, immuable et wiring sans I/O à l'import ; extraction de `RAGConfig` historique reste à faire avec les stores B2. Aucun écart de parité supplémentaire découvert ; ces dettes historiques ne sont pas déclarées closes par la seule fondation.

> Ces lignes décrivent une dette d'extraction, pas une autorisation de modifier le comportement historique. Statuts : `ouvert`, `en cours`, `clos`.

| ID | Écart constaté | Propriétaire | Statut |
|---|---|---|---|
| A5-01 | `Pipeline`, `StreamingGenerator`, `ContextBuilder` et les snapshots selector exposent encore `last_*`, `_timing` ou `last_result`; le logger relit ces objets après le run. | C6 | ouvert — remplacer par `RunContext`/résultats explicites |
| A5-02 | `FallbackEmbedder.last_model_used` peut être écrasé entre embedding et SQL ; le circuit breaker `_cb` est global, fondé sur l'horloge murale et non synchronisé. | B3 + C3 | ouvert — outcome par appel et breaker d'adaptateur sûr |
| A5-03 | Fraîcheur hétérogène : config Streamlit TTL 15 s, acronymes au constructeur, prompt generator sans invalidation, query/selector par appel. | B1/B2 + C2/C5 | ouvert — révisions, TTL et snapshot par requête |
| A5-04 | `Retriever`, `SectionAggregator` et `ContextBuilder` mélangent SQL avec RRF, gates, agrégation, budget, triangulation et références. | C3/C4 | ouvert — séparation `SearchPort`/`ContentStorePort` selon A5 |
| A5-05 | Le fan-out retrieval ouvre jusqu'à deux connexions par table ; l'introspection est cachée sans TTL et `SET ivfflat.probes` deviendrait fuyant sur un pool réutilisé. | B1 + C3 | ouvert — pool borné, cache synchronisé, `SET LOCAL` |
| A5-06 | `chat_runs`, sources affichées, `rag_trace_events` et export OTLP ne forment pas aujourd'hui une finalisation canonique unique ; l'export se fait dans un thread daemon non drainé. | B2 + C6/C7 | ouvert — persister atomiquement run + `chat_run_sources` + traces, puis appeler le sink géré |
| A5-07 | UUID, dates et durées sont générés dans pipeline, tracing, logger, admin et UI ; les `turn_id` historiques sont tronqués à 8 hex. | C6/C7 | ouvert — `ClockPort`/`IdGeneratorPort`, ids complets dans `RunContext` |
| A5-08 | `config.py` lit l'environnement à l'import, réexporte les helpers DB et expose des dictionnaires mutables ; `admin.DEFAULT_CONFIG` est un singleton mutable de fallback. | C2/C3/C5/C6 + admin-hardening | en cours — mapping RAG immuable et chargeur API livrés ci-dessous ; consommateurs historiques et environnement des tables restent à extraire |
| A5-09 | Des consommateurs utilisent des privés : `09_Pipeline_Evaluation.py` appelle `_retriever/_aggregator/_context_builder`, le logger lit `_context_builder`, le goldset importe `_fold`, le chat importe `_append_csv_row`. | C6 + D3 + F3 + admin-hardening | ouvert — retirer les usages publics en F3 ; repointer séparément l'admin avant suppression du package historique |
| A5-10 | Updates config read-modify-write sans révision et batch feedback sans claim : perte de mise à jour et double analyse possibles. Le DDL est encore déclenché depuis l'admin. | B2 + D1 + admin-hardening | ouvert — feedback sécurisé dans D1 ; CAS, claim et migrations admin suivis hors chemin critique M4 |
| A5-11 | Plusieurs départages reposent sur la stabilité implicite de Python ou des ensembles/requêtes sans ordre (`sections.sort(score)`, `list(set(...))`, refs/acronymes sans second tri). | C2/C3/C4 | ouvert — ordinal/clé totale et fixtures d'égalité |
| A5-12 | Les erreurs DB/provider sont traduites de façon hétérogène en vide, fallback, résultat partiel, texte d'erreur streamé ou exception ; origine du fallback souvent perdue. | B2/B3 + C2–C5 | ouvert — erreurs/outcomes typés en conservant la matrice historique |

Livraison B3 — [PR #514](https://github.com/DGAFP/assistant-rh/pull/514), [issue #455](https://github.com/DGAFP/assistant-rh/issues/455) : gateways HTTP async Albert/Scaleway pour chat complet/stream et embeddings, reranker Albert par lots de 40 derrière ports du core sans SDK ni HTTPX. Outcomes immuables et diagnostics par tentative/requête ; fallback avant contenu seulement, vecteur et clé de modèle indissociables, cooldown monotone synchronisé propre à l'adaptateur, erreurs/retries/deadlines/taille bornés et fermeture protégée lors d'annulation ASGI. 188 tests API passent sur PostgreSQL synthétique local, dont 79 tests gateways avec HTTP simulé et réseau réel interdit ; 1334 tests historiques passent, 15 ignorés ; Ruff, mypy et 3 gardes d'import passent. Revue indépendante validée après correction de la fermeture sous annulation AnyIO. Aucun wiring du runtime RAG servi ni déploiement.

Écarts B3 à reprendre en C2–C7 : erreurs de stream partiel typées au lieu de texte injecté ; delta vide ne bloque plus le fallback ; EOF sans `[DONE]`, vecteurs nuls/non finis/de mauvaise dimension et réponses de reranking incomplètes refusés ; score zéro conservé ; retries explicites bornés. La politique lexicale, le rendu des erreurs partielles, la préservation des scores d'entrée en cas de panne reranker et la compatibilité sémantique des modèles d'embedding avec les index restent à composer et rejouer avec le moteur. A5-02/A5-12 : les nouvelles primitives sont livrées, mais les consommateurs historiques et leurs `last_*` restent ouverts jusqu'à l'extraction. L'approximation des scores entre lots de reranker est conservée. Validation locale/fake uniquement, sans preuve de qualité RAG ni appel live aux providers.

Correctif de revue B3 — PR #514 : fermeture HTTPX automatique à EOF et fermeture explicite des streams protégées contre timeouts et annulations asyncio répétées, avec tâche de nettoyage bornée et attendue avant propagation. Huit cas de régression ajoutés, dont sept échouent sur la révision initiale ; les tests HTTPCore vérifient la libération de la connexion et de la requête du pool sans ouvrir de socket. Validation du correctif : 87 tests gateways passent ; suite API sans DSN synthétique, 117 passent / 79 ignorés ; Ruff, mypy et les trois contrats d'import passent. Revue indépendante validée après ajout du point de propagation de l'annulation AnyIO différée. Aucun appel provider ni accès DB dans cette passe.

Livraison B4 — [PR #523](https://github.com/DGAFP/assistant-rh/pull/523), [issue #456](https://github.com/DGAFP/assistant-rh/issues/456) : le périmètre D6/A3 et le contrat API courant remplacent le texte historique de l'issue sur les tokens permanents et le bootstrap admin. Catalogue public filtré, login mot de passe vers session opaque de huit heures, resolver bearer commun, scope ministère explicite, `/me` et logout livrés derrière ports injectés. Empreinte de bearer indexée sans scan PBKDF2 ; vérification des mots de passe compatible Streamlit, travail crypto borné hors boucle async, erreurs uniformes et sans secrets. Aucun endpoint admin ni token permanent ajouté.

Sécurité B4 : révision monotone des identifiants et de la politique groupe maintenue par trigger compatible avec les UPDATEs Streamlit ; reset concurrent et retour A → B → A ne restaurent pas de session. Quotas temporaires source/slug/globaux atomiques en PostgreSQL partagés entre processus ; refus sans prolongation du blocage, identités hachées et purge à échéance. Source réseau directe seulement, en-têtes proxy ignorés au point d'entrée Uvicorn, body login borné avant parsing. Migration additive et script de rollback transactionnel avec révocation préalable ; les mots de passe existants restent intacts. Le quota visiteur Streamlit appartient à E1 et le test ingress à D4 ; pas de bascule ni migration distante dans B4.

Validation B4 : **259 tests API passent** sur PostgreSQL/pgvector synthétique local ; **38 tests historiques** groupes/scope/prompts passent ; Ruff, mypy (38 fichiers) et les trois contrats d'import passent. Couverture domaine/HTTP, PBKDF2 réel avec mot de passe synthétique, parcours FastAPI complet, concurrence quotas, reset/login, absence de résurrection, migration idempotente et rollback. Revue indépendante sans finding restant après corrections Unicode/parsing et maintien du plafond de quatre KDF sous annulations asyncio répétées ; régressions dédiées. Aucun appel provider, migration distante ni déploiement.

## Reports depuis le runtime existant

> Format : date · commit/PR du runtime existant · fichiers touchés · reporté vers `assistant_rh_api.core` · tests/preuve · statut (`reporté` / `à reporter`).

_(vide — aucun report en attente)_

## Améliorations notées pour après le merge

> Idées d'amélioration pipeline/API survenues pendant la reconstruction — interdites dans le chantier (iso-fonctionnel), à instruire après.

- **2026-09-04 — observabilité admin** : évaluer Grafana/Tempo pour métriques et traces opérationnelles, et LangSmith pour l'inspection RAG/LLM si l'hébergement, la rétention et le masquage des données RH sont approuvés. LangSmith reste optionnel et n'impose pas LangChain.
- **2026-09-04 — agentic RAG** : prototyper après M4 un agent borné (sélection de source, reformulation ou retry limité) et le comparer au pipeline déterministe sur le goldset avant toute bascule produit.
- **2026-09-04 — admin-hardening** : restreindre l'accès réseau, créer des identifiants DB dédiés et bornés, auditer les actions sensibles, déplacer le DDL runtime historique vers des migrations, repointer tous les consommateurs admin puis supprimer `packages/rag-pipeline`. Réévaluer ensuite une extraction vers endpoints admin ou RAG-ops, sans bloquer la migration publique.

### Correctifs de revue B4 — PR #523

Catalogue aligné sur Streamlit (`priority DESC`, `slug ASC`). La stack locale
initialise les fixtures synthétiques et les migrations B2/B4 dans une transaction,
avec un groupe de démonstration public ; la CI vérifie le parcours HTTP réel.
Les sessions expirées/révoquées sont purgées par lots indexés bornés à la création
et chaque minute pendant le lifespan, sans supprimer les sessions actives ni
l'audit. Les tests couvrent les lots concurrents, les lignes verrouillées et la
reprise après indisponibilité DB et le shutdown pendant une transaction.
Validation : 267 tests API, 38 tests historiques, mypy sur 39 fichiers et trois
contrats d’import passent ; bootstrap vierge et smoke HTTP réel validés. Aucun ajout d'utilisateurs individuels ni
changement des droits ministériels dans cette correction.


Livraison B5 — [PR #538](https://github.com/DGAFP/assistant-rh/pull/538), [issue #457](https://github.com/DGAFP/assistant-rh/issues/457) :
`GET /v1/models` branché sur le resolver bearer B4 et son groupe courant chargé par
`GroupStore`. `ModelService` pur, catalogue ministériel canonique commun à l'auth,
modèles immuables triés par id et dédupliqués, enveloppe OpenAI typée et réponses
`no-store`. L'alias d'entrée `assistant-rh` résout uniquement le défaut autorisé ;
modèle inconnu → 404, ministère non autorisé → 403. C1 fixe l'utilisation de cette
résolution ; C6 la branchera sur Chat Completions, absent de B5.

Politique B5 : zéro ministère, ministère inconnu ou défaut absent/interdit donnent
une erreur de configuration explicite, sans fallback implicite. B4 conserve les
exclusions du catalogue de groupes et du login ; la résolution vérifie d'abord
expiration/révocation/révision/identifiants (401), puis la politique d'une session
encore valide (500 `ministry_configuration_error` si corrompue). Les modifications
normales de politique en DB continuent de révoquer les sessions via B4.

Preuves B5 : 301 tests API passent sur PostgreSQL 18.4/pgvector éphémère exclusivement
synthétique, dont les tests du SDK OpenAI Python 2.38.0 avec validation stricte et
le parcours lifespan PostgreSQL → login → catalogue → révocation. Les tests couvrent
zéro/un/plusieurs ministères, doublons/ordre, isolation entre groupes, alias, erreurs
SDK et sessions invalides. 38 tests historiques groupes/ministères/prompts passent ;
smoke HTTP local et SDK synchrone contre Uvicorn/PostgreSQL réels verts, avec rejet
après logout. Ruff et les trois contrats d'import passent. Docker indisponible en
local : build image et smoke Compose à vérifier en CI. Aucun
changement du runtime RAG servi, aucune migration ou activation distante.


### A5-08 — préparation de la configuration RAG pour le moteur API — [PR #539](https://github.com/DGAFP/assistant-rh/pull/539)

Reconstruction pure du `RAGConfig` historique et du mapping
`RuntimeRAGConfig → RAGConfig` dans le core API, sans import du package historique,
lecture d'environnement ou helper DB. Les sous-configurations sont gelées, les
tables sont un tuple ordonné ; `to_dict()` conserve le format historique et rend
une copie détachée. Les défauts du constructeur et les défauts admin, différents
pour le selector et l'intent gating, sont conservés séparément. Aucun nouveau
fichier de valeurs ni changement de `rag_config`, de l'admin ou du runtime servi.

Le lifespan assemble `RAGConfigurationService` avec le `ConfigStore` B2 et charge
un premier snapshot pour valider les types avant de servir les requêtes. Une
configuration malformée interrompt le démarrage et ferme le pool. Le futur
point d'entrée moteur devra appeler `load()` une fois par requête puis transmettre
le même snapshot à chaque étape. Ce chargement initial ne devient pas un cache :
chaque appel relit le store, conserve sa révision et voit les UPDATEs
admin suivants, sans altérer les snapshots déjà remis. Absence/panne DB conservent
le fallback historique vers des défauts frais, avec motif explicite ; annulation
et types malformés ne sont pas masqués. La validation ne réapplique pas les bornes
admin, qui excluraient certaines configurations déjà évaluées.

**Reliquats affectés, A5-08 non déclaré clos** : C2/C5 extraient les lectures
supplémentaires de `get_runtime_config()`, prompts/acronymes et leur cohérence de
requête (A5-03). C3 résout au wiring les overrides environnement
`SERVICE_PUBLIC_COMPARE_TABLE` / `DGAFP_COMPARE_TABLE` et le catalogue de tables,
puis les injecte aux adaptateurs sans global lu à l'import. C6 branche le chargeur
sur le `ChatService` réel et propage le snapshot au `RunContext` ; il n'existe pas
encore de route completion utilisant cette configuration. Le retrait des
réexports DB et du singleton `admin.DEFAULT_CONFIG` dans les consommateurs admin
historiques appartient à admin-hardening ; ils restent intacts conformément à la
reconstruction parallèle et à l'exception admin A3. La sémantique du cache
Streamlit historique (15 s) reste inchangée. Le périmètre livré est une fondation
RAG pour l'API, pas une intégration ni une preuve de parité du moteur C2–C7.


Validation A5-08 : **321 tests API** passent sur PostgreSQL 18.4/pgvector local
exclusivement synthétique. Tests de parité contre le mapping historique, snapshots
profondément immuables, UPDATE SQL entre deux requêtes, fallback/reprise et lifespan
couverts. Suite historique : 1367 tests passent et 16 sont ignorés dans le sandbox ;
les 13 cas HTTP bloqués par les sockets locales sont validés par une relance du
module replay complet (14/14 passent). Ruff sur les chemins CI, mypy sur les deux
nouveaux modules et le wiring et les trois contrats d'import passent ; la garde core interdit
aussi le package RAG historique. Revue indépendante favorable, aucun constat
bloquant après correction du test de redémarrage avec un nouveau pool.
Chargement/validation initial, fermeture du pool sur configuration invalide et
reprise après correction couverts. Aucun appel provider, migration distante, déploiement ni bascule RAG.

## C1 — contrat Chat Completions fixé avant C6 (2026-09-10)

[Issue #458](https://github.com/DGAFP/assistant-rh/issues/458) : [contrat détaillé](09-chat-completions-contract.md), exemples JSON et matrice de cas attendus reliés à la [preuve A2/#443](07-openai-client-spike.md) et aux contrats B4/#456, B5/#457. Le plan et le contrat v1 sont alignés : C1 est documentaire, C2–C5 extraient les étapes indépendamment, #463 porte le handler non-stream et tous ses tests HTTP ; #464 conserve le SSE. Aucun handler ni `ChatService` fake/replay ajouté.

Décisions figées : dernier user et cinq couples complets selon l'algorithme A2 ; instructions système/developer et tools ignorés ; parts texte concaténées ; 1 Mio/32 messages/64 Kio UTF-8 ; résolution B5 et scope B4 ; enveloppe et sources finales cohérentes ; erreurs sûres et succès persisté avant réponse/terminal SSE. Le code 403 retenu est `ministry_forbidden`, déjà livré/testé en B4/B5, remplaçant `model_forbidden` du replay A2. Les types stricts, métadonnées, frontières exactes de taille et lecture sans `Content-Length` sont distingués des comportements déjà éprouvés et attribués à C6 dans la matrice.

Preuve locale du 2026-09-10, sur `dev` de départ `913a80fac356629de641af12f2b3eddb2ec26a74` avec uniquement des modifications documentaires : **48 tests existants passent** (SDK OpenAI 2.38.0, replay A2, `ModelService` et HTTP catalogue/auth avec stores en mémoire). Reproduction après `uv sync --package assistant-rh-api --group dev` :

```bash
uv run --no-sync python -m pytest tests/test_openai_contract_probe.py apps/api/tests/core/test_model_service.py apps/api/tests/handlers/test_models_http.py -q
```

Les **15 exemples JSON** des documents concernés sont valides ; les enveloppes completion passent `ChatCompletion.model_validate(..., strict=True)` et les assertions d'identifiants, ministère, nombre/titres des sources. Liens locaux et noms des tests cités vérifiés ; `git diff --check` passe. La preuve de l'instance `conversations`/homelab reste celle d'A2 du 2026-09-02, pas une nouvelle exécution. La matrice indique les validations restant en C6/C7/D4 ; aucun accès à une base distante, appel provider, migration ou déploiement.

## C2 — query processor dans le core pur (2026-09-10)

[Issue #459](https://github.com/DGAFP/assistant-rh/issues/459) : extraction dans
`core/pipeline/steps/query_processor.py` et `legal_search.py`, avec résultats et
diagnostics immuables dans `core/models/query_processing.py`. Ports async B2/B3
pour acronymes, prompts et classification ; fallback ressource embarqué dans
l'API, identique au prompt historique. Intent, réponses directes, reformulation,
NFC, casse/frontières et ordre des acronymes, rendu ministère/date/historique,
parsing permissif, flag LLM et heuristique légale conservés sans réglage qualité.
La [carte A5](07-runtime-isolation-audit.md#relecture-c2--query-processor-459-2026-09-10)
détaille les règles et branches relues sur `dev` au `c3c1786`.

A5-03/11/12 : snapshots et révisions propres à chaque appel, tuples ordonnés,
erreurs DB consignées, annulation propagée, panne classification conservant la
question sans expansion ni heuristique. Les remplacements successifs imbriqués,
doublons et priorité de l'erreur d'historique sur l'absence du prompt sont figés
par tests. Le step ne trie pas le tuple reçu : les départages SQL et le support
de `acronyms.priority` appartiennent à B2. Les acronymes API sont relus par
requête, conformément à A5 ; le snapshot au constructeur du runtime servi reste
intact. La cohérence entre stores et la propagation au `RunContext` restent C6,
les autres étapes restent C3–C5 ; A5-03/08/09/11/12 ne sont pas déclarés clos.

**Frontière C6 explicite** : injecter la gateway Albert seule, liée au
`config.intent_model` (le classifier historique n'utilise pas de fallback
Scaleway), transmettre le ministère canonique déjà autorisé et la date locale
capturée via l'horloge. Les retries et erreurs sûres B3 sont conservés ; les
messages réseau bruts historiques ne sont pas réintroduits. Aucun handler HTTP,
service replay, wiring du runtime servi ou changement de consommateur historique.

**Preuve de parité** : les sept sorties M0b sont comparées exactement, types JSON
inclus, ainsi que les réponses directes et métadonnées disponibles. Le bundle
original ne contient pas les réponses LLM brutes, prompts DB ni acronymes : les
réponses injectées dans `query_processor_m0b_ports.json` sont explicitement
**reconstituées**, pas des enregistrements originaux. Voir les
[limites et commandes](../../../tests/conformance/M0_REPLAYS.md#c2-query-processor-extraction-459).
Les tests différentiels comparent la sortie complète et le prompt envoyé au
runtime historique pour les erreurs, coercions et branches absentes de M0b.
Cette preuve d'étape ne vaut pas validation M1 du moteur complet ou qualité live.

Validation locale : **125 tests C2 passent** (dont sept M0b), suite API **359 passent / 105 ignorés** faute de
DSN synthétique local ; **139 tests historiques ciblés passent**. Vérificateur
M0b : sept fixtures et 56 artefacts intacts, fingerprint
`f5e9ffefe588248a352d7ac18a556df7bff6270a2879e7ddd32acc128733e02b`.
Ruff, mypy sur six modules, trois contrats d'import et import isolé du step
passent. La roue API construite contient le step et le prompt de fallback
vérifié octet pour octet. Revue indépendante sans constat bloquant ; la dernière simplification
préserve aussi la priorité d'erreur historique avec régression dédiée.
Aucun accès DB distant, appel provider, migration, déploiement ou éval live.

### Rangement C2 — PR #545

Valeurs et types d'inférence déplacés dans `core/models/inference.py` ;
`InferenceFailure` rejoint `core/errors.py`, sans dépendance circulaire. Tous les
imports et la référence active du README sont adaptés. Les commentaires et
docstrings de `legal_search.py` sont raccourcis en conservant les subtilités des
regex ; son AST exécutable reste identique. Aucun changement fonctionnel.
Validation : 285 tests core/gateways passent, Ruff, mypy sur 14 modules et les
3 contrats d'import passent.

### Durcissement du repli de classification — revue PR #545

Décision utilisateur : continuer en mode dégradé pour les échecs attendus de
classification, comme le runtime historique, plutôt que bloquer systématiquement.
Le `catch Exception` disparaît : `ClassificationFailure` chaîne sa cause
provider/décodage/conversion et seul ce type déclenche le repli. Les valeurs
restent `rag_query`, confiance 0,5, question NFC originale, aucune expansion ni
reformulation, flag juridique false, signal LLM null et `should_proceed=true`.

Différences de parité **volontaires et autorisées** :

- Le statut de classification est `disabled`, `completed` ou `degraded` ; la cause
  sûre `provider_failure`/`invalid_response` est explicite dans les diagnostics et
  remplace `str(exception)` dans `intent_reason`. La cause chaînée reste interne,
  les tentatives B3 et la completion reçue restent disponibles séparément.
- Prompt absent/malformé → `RAGConfigurationError` avec cause ; données d'appel
  invalides, bugs, erreurs de port non traduites et annulations se propagent.
  Rejets fournisseur (identifiants/modèle/payload) et completion partielle
  remontent également : ils ne deviennent plus un résultat RAG de repli.
- JSON non objet, limites du décodeur, confiance non convertible/non finie ou
  champs textuels consommés inutilisables → repli explicite, sans fuite d'objets
  mutables ni nombre non sérialisable. Les coercions utilisables sont conservées :
  fences, intent inconnu, thème inconnu, booléens, confiance numérique convertible
  sans nouvelle borne 0–1, valeurs falsy de reformulation/retrieval.
- Les fallbacks attendus des stores DB et le traitement normal du hors-périmètre
  restent inchangés. Les sept fixtures M0b nominales ne sont pas modifiées.

Les anciens tests de parité totale sur erreurs sont remplacés par assertions
explicites des changements autorisés : seul le motif sûr diffère pour les
échecs auparavant dégradés ; prompt/historique invalides doivent maintenant
lever. De nouvelles régressions couvrent causes chaînées, bugs, configuration,
annulation, données LLM inutilisables et reprise après panne. La limite des grands
entiers de `json.loads()` est traduite au seul point de décodage, conformément au
constat de revue indépendante.

C6 devra consommer le statut dégradé et sa cause pour l'orchestration et la trace,
au lieu de déduire un statut `ok` de la seule absence d'exception. Le contrat est
précisé dans `apps/api/README.md` ; aucun logger I/O dans le core, handler HTTP ou
moteur C6 ajouté. Aucun nouvel appel LLM, accès DB, push ou déploiement.

Validation du durcissement : **322 tests core/gateways passent**, dont les sept
replays M0b ; Ruff, mypy sur les trois modules modifiés et les trois contrats
d'import passent. Revue indépendante favorable après correction de la limite des
grands entiers JSON. Modification extérieure de `legal_search.py` laissée intacte
et exclue du commit.

### Rangement des erreurs par domaine — revue PR #545

`core/errors/` remplace le module unique : base commune, stockage, inférence,
RAG (configuration/classification) et accès (ministère/modèle). Le point d'entrée
réexporte les mêmes classes ; définitions, héritages, codes et comportement
restent inchangés, sans cycle. README et test du chemin de ressource adaptés.
Aucune modification de la politique de repli : le constat de revue sur
`DatabaseConflict` à la lecture des acronymes reste ouvert pour discussion.

Validation locale : 396 tests API passent, 105 ignorés faute de DSN synthétique ;
Ruff sur les chemins CI, mypy sur le package et trois contrats d'import passent.
Identité des réexports, héritages et égalité AST des définitions vérifiés.
Modification extérieure de `legal_search.py` préservée et exclue du commit.

### Repli acronymes sur conflit DB et avertissement — revue PR #545

Correction autorisée du constat `DatabaseConflict` : le chargement des acronymes
reprend avec un dictionnaire vide pour ce conflit, comme pour `DatabaseFailure`
et `DatabaseUnavailable`. Le code reste dans `store_errors` ; la classification
se poursuit normalement. Les bugs, erreurs de configuration et annulations
continuent de remonter. La politique des prompts reste inchangée.

Chaque repli acronymes émet désormais un vrai record `WARNING` via `logging`
standard, au point de décision dans le step. Exception ciblée à l'absence de
logging dans le core : l'adaptateur DB ne connaît pas la décision de poursuivre,
et aucun consommateur C6 des diagnostics n'existe encore. Aucun handler n'est
configuré dans le core ; seul le code sûr et la poursuite sans acronymes sont
journalisés, sans donnée de requête, message d'exception ni traceback. C6 devra
éviter de réémettre cet avertissement.

Régressions couvrant les trois erreurs récupérables avec/sans classification,
la capture d'un seul avertissement réel et sûr, la poursuite de l'appel LLM,
les diagnostics et la propagation inchangée des bugs/configurations invalides.
Validation : 403 tests API passent (dont 162 query processor), 105 ignorés faute
de DSN synthétique ; Ruff, mypy sur le step et trois contrats d'import passent.
Modification extérieure de `legal_search.py` préservée et exclue du commit.

### Repli prompt sur conflit DB — revue PR #545

Le chargement du prompt récupère également `DatabaseConflict`, traduction B2
des erreurs de sérialisation et deadlocks, comme `DatabaseFailure` et
`DatabaseUnavailable`. Il poursuit la résolution DB puis ressource embarquée
dans l'ordre existant et conserve le code sûr de chaque lecture échouée dans
`store_errors`. Le runtime historique récupérait déjà ces erreurs SQL ; le step
API ne doit pas interrompre la classification lorsqu'un prompt embarqué existe.
Les erreurs de configuration, bugs et annulations continuent de remonter.

Les deux nouvelles régressions conflit échouent sur `e30b2ff` puis passent avec
le correctif. Neuf cas ajoutés couvrent les trois erreurs DB récupérables avec
les noms `intent_unified.md` et `intent.md`, l'ordre des lectures, le vrai prompt
embarqué, les diagnostics, la sortie historique et la propagation des erreurs
inattendues/configuration. Validation locale : **412 tests API passent**, 105
ignorés faute de DSN synthétique ; Ruff sur les chemins CI, formatage des deux
fichiers Python et trois contrats d'import passent. Aucun accès DB distant ou
appel provider ; validation CI du nouveau commit à vérifier après publication.

## C3 — retrieval métier extrait, gate M0b ouvert (2026-09-10)

[Issue #460](https://github.com/DGAFP/assistant-rh/issues/460) : extraction dans
`apps/api/src/assistant_rh_api/core/pipeline/steps/retrieval.py`, derrière `SearchPort` et
`EmbeddingPort`, sans dépendance SQL/psycopg ni import du package historique.
Le core possède fusion hybride, rang des absents, RRF inter-sources, plafond de
normalisation, filtre headings, top-k et dédup R2. Résultats/configuration et
métadonnées sont immuables ; chaîne embedding sélectionnée par requête, modèle
réel conservé après fallback, erreurs partielles explicites et scope ministériel
strict. Les tables de comparaison nécessitent un catalogue injecté ; les alias
physiques dupliqués sont refusés avant I/O pour garantir le déterminisme.

L'adaptateur expose les deux lanes hybrides brutes dans un unique snapshot SQL,
sans fusion métier. Il conserve l'ordre et la représentation des scores de
chaque lane, les probes transactionnels et le pool borné B1. Le fallback B2
implicite vers `to_tsvector(chunk_text)` quand la colonne manque est retiré :
le runtime historique échoue dans ce cas. La recherche heading reste indépendante.
Pas de reranker de chunks ajouté (option historiquement sans implémentation).
Aucun handler ni branchement Streamlit ; C2/#459 est livré par #545, et
C6/#463 porte l’assemblage des étapes.

Preuve locale : **381 tests API passent** sur PostgreSQL 18.4/pgvector 0.8.2 local
exclusivement synthétique. Les 15 comparaisons différentielles exécutent les SQL
historiques et le nouveau core sur le même corpus : modes semantic/lexical/hybrid,
mode non scopé et quatre ministères, scores exacts, ordre et métadonnées complets.
S'ajoutent égalités, doublons R2, pools vides, absence d'embedding, erreurs partielles,
colonne lexicale absente, annulation, immutabilité et isolation des requêtes.
Ruff CI, mypy sur les quatre modules et les trois contrats d'import passent.
Revue indépendante favorable pour un brouillon après correction du marqueur
lexical, des collisions de catalogue et de la sélection embedding par config.
Suite historique : 1376 tests passent et 16 sont ignorés dans le sandbox ; les
13 cas HTTP bloqués par les sockets locales sont validés dans une relance du
module complet `test_openai_contract_probe.py` (14/14). Reproduction API après
`uv sync --all-packages --group dev`, avec le DSN synthétique local gardé par la
fixture : `uv run --no-sync python -m pytest apps/api/tests -q`. Aucun test
historique n'a été supprimé ni assoupli.

**Gate non satisfait, issue maintenue ouverte** : M0b vérifie bien 7 fixtures et
56 artefacts, mais ne contient pas les inputs bruts du `SearchPort`. Son auto-check
ne prouve pas la conformance de C3. Le [complément nécessaire](../../../tests/conformance/M0_REPLAYS.md#c3-retrieval-extraction-missing-port-inputs)
est documenté ; aucun candidat n'est reconstruit depuis les résultats attendus,
aucune baseline n'est remplacée. L'[audit A5](07-runtime-isolation-audit.md)
consigne aussi l'ambiguïté de section déjà départagée côté B2 et le fait que
DGAFP est interrogé même avec `needs_legal_search=false` dans M0b. C3 préserve
cette politique, sans introduire le nouveau gate suggéré par le libellé de #460.
A5-02/04/05/08/11 progressent côté API mais ne sont pas déclarés clos : la preuve
M0b, les étapes C4/C6 et les consommateurs historiques restent à traiter.
Aucun accès DB distant, appel provider, migration distante ou déploiement.


## C3 — intégration de C2 depuis dev / #545 (2026-09-10)

Fusion de `dev` (`a86719f`, #545) dans la branche de #546. Les imports du
retrieval et de ses tests utilisent désormais `core.models.inference.Embedding`
et `core.errors.inference.InferenceFailure`. Le test d’isolation importe C2 et
C3 ensemble et conserve les interdictions I/O des deux branches. Les entrées
C2/C3 du LEDGER et des replays sont conservées ; #459 est livré, tandis que le
gate M0b retrieval de #460 reste ouvert et #546 reste en brouillon.

Avant correction, le test d’import reproduit `ModuleNotFoundError` sur
`assistant_rh_api.core.inference`. Après correction : **204 tests ciblés passent**,
puis **1 948 tests API et historiques passent, 16 sont ignorés** avec
`API_SYNTHETIC_POSTGRES_DSN` pointant exclusivement vers PostgreSQL 18.4/pgvector
0.8.2 local synthétique et `PYTHON_DOTENV_DISABLED=1` :
`uv run --no-sync python -m pytest apps/api/tests tests --ignore=tests/archive -q`.
Ruff sur les chemins CI, mypy sur les cinq modules retrieval/modèles/port/catalogue/
adaptateur et les trois contrats d’import passent. Revue indépendante de la
fusion favorable. Les règles de retrieval sont inchangées ; aucune preuve M0b
supplémentaire ni bascule runtime n’est revendiquée. CI à recontrôler sur le
commit publié ; aucun accès DB distant, appel provider ou déploiement.


## C3 — alignement du step et caractérisation des erreurs DB (2026-09-10)

Le retrieval rejoint `core/pipeline/steps/retrieval.py`, conformément au plan
cible et au placement de C2. Le contenu du module est identique octet pour octet
à celui de `2a13158` ; seuls son emplacement, les imports et les références
changent. Le test d’isolation importe les deux steps au même emplacement.

Six caractérisations couvrent une panne `DatabaseUnavailable` dans les trois
modes, sans scope puis avec ministère : résultat vide avec diagnostics des
lanes en mode partiel ; `ScopedRetrievalError` en mode ministériel strict.
La cause DB n’est pas conservée dans ces diagnostics et le step ne journalise
pas lui-même les erreurs. Le README explicite aussi l’interception large des
exceptions et l’absence de mapping HTTP completion C6. La politique d’erreur
reste inchangée par ce déplacement.

Validation : **32 tests ciblés et 565 tests API passent** sur PostgreSQL local
synthétique, incluant les tests existants de perte de réponse réseau et de
traduction des erreurs DB. Ruff sur les chemins CI, mypy du module déplacé et
les trois contrats d’import passent ; revue indépendante favorable. Le gate
M0b C3 reste ouvert et la PR reste en brouillon. Aucun accès DB distant, appel
provider ou déploiement ; CI à vérifier sur la nouvelle tête publiée.
