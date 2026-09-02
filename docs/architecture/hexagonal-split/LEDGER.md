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
| 2026-09-02 | A3 — [issue #444](https://github.com/DGAFP/assistant-rh/issues/444) | [Matrice Streamlit et surface API](08-streamlit-api-parity.md) arbitrées sans nouvelle perte : parcours produit en clients HTTP, corpus/goldset/évals dans RAG-ops, endpoints D1/D2 et permissions figés, documents limités aux sources citées, feedback propriétaire, groupes protégés et bundle de bearers serveur avec rotation sans coupure. |

## Écarts d'isolation A5

> Ces lignes décrivent une dette d'extraction, pas une autorisation de modifier le comportement historique. Statuts : `ouvert`, `en cours`, `clos`.

| ID | Écart constaté | Propriétaire | Statut |
|---|---|---|---|
| A5-01 | `Pipeline`, `StreamingGenerator`, `ContextBuilder` et les snapshots selector exposent encore `last_*`, `_timing` ou `last_result`; le logger relit ces objets après le run. | C6 | ouvert — remplacer par `RunContext`/résultats explicites |
| A5-02 | `FallbackEmbedder.last_model_used` peut être écrasé entre embedding et SQL ; le circuit breaker `_cb` est global, fondé sur l'horloge murale et non synchronisé. | B3 + C3 | ouvert — outcome par appel et breaker d'adaptateur sûr |
| A5-03 | Fraîcheur hétérogène : config Streamlit TTL 15 s, acronymes au constructeur, prompt generator sans invalidation, query/selector par appel. | B1/B2 + C2/C5 | ouvert — révisions, TTL et snapshot par requête |
| A5-04 | `Retriever`, `SectionAggregator` et `ContextBuilder` mélangent SQL avec RRF, gates, agrégation, budget, triangulation et références. | C3/C4 | ouvert — séparation `SearchPort`/`ContentStorePort` selon A5 |
| A5-05 | Le fan-out retrieval ouvre jusqu'à deux connexions par table ; l'introspection est cachée sans TTL et `SET ivfflat.probes` deviendrait fuyant sur un pool réutilisé. | B1 + C3 | ouvert — pool borné, cache synchronisé, `SET LOCAL` |
| A5-06 | `chat_runs`, `rag_trace_events` et export OTLP sont trois opérations non atomiques ; l'export se fait dans un thread daemon non drainé. | B2 + C6/C7 | ouvert — finalisation durable atomique puis sink géré |
| A5-07 | UUID, dates et durées sont générés dans pipeline, tracing, logger, admin et UI ; les `turn_id` historiques sont tronqués à 8 hex. | C6/C7 | ouvert — `ClockPort`/`IdGeneratorPort`, ids complets dans `RunContext` |
| A5-08 | `config.py` lit l'environnement à l'import, réexporte les helpers DB et expose des dictionnaires mutables ; `admin.DEFAULT_CONFIG` est un singleton mutable de fallback. | B1 | ouvert — config pure/immuable et wiring sans effet de bord |
| A5-09 | Des consommateurs utilisent des privés : `09_Pipeline_Evaluation.py` appelle `_retriever/_aggregator/_context_builder`, le logger lit `_context_builder`, le goldset importe `_fold`, le chat importe `_append_csv_row`. | D3 + E1/E2 + F3 | ouvert — ports/runners/clients publics puis retrait rollback |
| A5-10 | Updates config read-modify-write sans révision et batch feedback sans claim : perte de mise à jour et double analyse possibles. Le DDL est encore déclenché depuis l'admin. | B2 + D1/D2 | ouvert — CAS/transactions, claim idempotent, migrations hors runtime |
| A5-11 | Plusieurs départages reposent sur la stabilité implicite de Python ou des ensembles/requêtes sans ordre (`sections.sort(score)`, `list(set(...))`, refs/acronymes sans second tri). | C2/C3/C4 | ouvert — ordinal/clé totale et fixtures d'égalité |
| A5-12 | Les erreurs DB/provider sont traduites de façon hétérogène en vide, fallback, résultat partiel, texte d'erreur streamé ou exception ; origine du fallback souvent perdue. | B2/B3 + C2–C5 | ouvert — erreurs/outcomes typés en conservant la matrice historique |

## Reports depuis le runtime existant

> Format : date · commit/PR du runtime existant · fichiers touchés · reporté vers `assistant_rh_api.core` · tests/preuve · statut (`reporté` / `à reporter`).

_(vide — aucun report en attente)_

## Améliorations notées pour après le merge

> Idées d'amélioration pipeline/API survenues pendant la reconstruction — interdites dans le chantier (iso-fonctionnel), à instruire après.

_(vide)_
