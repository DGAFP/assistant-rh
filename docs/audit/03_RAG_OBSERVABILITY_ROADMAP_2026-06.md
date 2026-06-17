# Observabilité RAG & Dashboards Grafana — Itération 2

> Document complémentaire à la planification itération 2 : [01_RAG_QUALITY_AUDIT_2026-06.md](01_RAG_QUALITY_AUDIT_2026-06.md)
> Date : 2026-06-11
> Objectif : rendre le RAG pilotable en production d'ici le 31 octobre 2026 : usage, qualité, latence, erreurs, traces et alerting.

---

## 1. Contexte

L'audit qualité RAG a montré un problème structurel d'observabilité : le système logge beaucoup de données dans `chat_runs`, mais les signaux ne sont pas encore consolidés dans des dashboards et des alertes exploitables. La panne du reranker Albert est restée invisible alors qu'elle dégradait directement la qualité des réponses.

Les manques prioritaires sont :
- visibilité usage : nombre de requêtes, sessions, utilisateurs, répartition par groupes ;
- visibilité qualité : no-answer, helpful rate, feedbacks négatifs, catégories `retrieval_issue` / `missing_document` ;
- visibilité latence : temps total, P50/P95/P99, temps par étape RAG, TTFT ;
- visibilité provider : erreurs Albert/Scaleway, fallbacks, timeouts, taux d'échec embeddings/rerank/LLM ;
- visibilité traces : chemin complet question -> query processing -> retrieval -> rerank -> selector -> contexte -> génération ;
- visibilité infra : métriques Serverless Containers, cold starts, concurrency, CPU/mémoire, erreurs HTTP.

## 2. Socle cible

Le socle cible est **Scaleway Serverless Containers + Cockpit/Grafana**.

- Scaleway documente le monitoring des logs et métriques des Serverless Containers via la page [Monitor container logs and metrics](https://www.scaleway.com/en/docs/serverless/containers/how-to/monitor-container/).
- Scaleway Cockpit est le point d'entrée observabilité pour visualiser métriques, logs et traces dans Grafana : [Cockpit](https://www.scaleway.com/en/docs/observability/cockpit/).
- `chat_runs` reste la table métier de référence pour les événements RAG et doit être corrélée aux logs/traces infra.
- `chat_feedbacks` reste la source de référence pour le retour utilisateur et l'analyse des causes de non-qualité.

Principe de mise en œuvre : ne pas remplacer `chat_runs` par Grafana, mais relier les deux niveaux. Grafana donne le pilotage temps réel et les alertes ; `chat_runs` donne le diagnostic métier détaillé et l'analyse a posteriori.

**Prérequis : refondre la persistance des traces RAG.** L'observabilité de niveau trace (chemin question → retrieval → rerank → selector → contexte) suppose de persister les **sets de chunks à chaque étape** — ce que `chat_runs` ne fait pas aujourd'hui (compteurs et agrégats seulement, cf. note [02](02_ARCHITECTURE_AUDIT_2026-06.md) A6). Cible : pour chaque `turn_id`, un enregistrement structuré (idéalement une table d'événements séparée, pas 30 colonnes de plus dans `chat_runs`) capturant par étape les identifiants de chunks, scores réels et décisions, permettant de **rejouer et expliquer** la perte d'un chunk pertinent. Ce chantier est jumeau de la rationalisation du schéma `chat_runs` (note [05](05_PLAN_AUDIT_ET_COUVERTURE.md) D16) : enlever le mort, ajouter le signal utile.

## 3. Dashboards Grafana v1

> **En préparation (PR dédiée)** : une première brique est déjà portée par [#115](https://github.com/DGAFP/assistant-rh/pull/115) « RAG data health monitoring » — exporter read-only + **dashboard Grafana « santé des données »** (corpus ingéré, complétude des embeddings, fraîcheur, intégrité) + alertes, remote-write vers Scaleway Cockpit. Les dashboards qualité / usage / latence ci-dessous s'y ajoutent. *Calendrier de déploiement à caler sur la [note 00 §4](00_SYNTHESE_ET_PRIORISATION.md).*

### 3.1 Vue exécutive

Objectif : suivre l'état global du service sans connaître le détail du pipeline.

| Signal | Source |
|---|---|
| Requêtes par heure/jour/semaine | logs applicatifs, `chat_runs` |
| Sessions actives et conversations | `chat_runs` |
| Helpful rate sur 4 semaines glissantes | `chat_feedbacks` |
| Taux de no-answer | `chat_runs` |
| Erreurs critiques applicatives et HTTP 5xx | logs Serverless Containers |
| Latence P95/P99 totale | `chat_runs`, métriques applicatives |

### 3.2 Santé RAG

Objectif : savoir quel étage dégrade les réponses.

| Signal | Source |
|---|---|
| Distribution des intents | `chat_runs.v3_intent` |
| Nombre de chunks/sections récupérés | `chat_runs` |
| Sources utilisées dans le contexte final | `chat_runs.v3_source_distribution` |
| Taux de rerank actif / échec / fallback | `chat_runs.v3_reranker_status` / `v3_reranker_error`, logs applicatifs |
| Taux de selector no-answer | `chat_runs.v3_should_proceed`, réponses |
| Taux de contextes vides ou insuffisants | `chat_runs` |

### 3.3 Latence

Objectif : isoler rapidement si la lenteur vient du RAG, d'un provider ou du container.

| Signal | Source |
|---|---|
| Temps total P50/P95/P99 | `chat_runs.total_time_ms` |
| Query processing | `chat_runs.v3_query_processing_ms` |
| Retrieval | `chat_runs.v3_retrieval_ms` |
| Aggregation | `chat_runs.v3_aggregation_ms` |
| Selector | `chat_runs.v3_selector_ms` |
| Context building | `chat_runs.v3_context_building_ms` |
| Génération | `chat_runs.v3_generation_ms` |
| TTFT et débit de génération | `chat_runs.v3_ttft_ms`, `chat_runs.v3_chars_per_second` |
| Cold starts et concurrency | métriques Serverless Containers |

### 3.4 Providers et infrastructure

Objectif : détecter les pannes ou dégradations Albert/Scaleway et les problèmes de capacité.

| Signal | Source |
|---|---|
| Taux d'erreur Albert embeddings/rerank/LLM | logs applicatifs, nouveaux compteurs provider |
| Taux d'erreur Scaleway fallback | logs applicatifs |
| Fallback rate Albert -> Scaleway | `chat_runs.fallbacks_used`, logs |
| Timeouts provider | logs applicatifs |
| HTTP 4xx/5xx container | logs Serverless Containers |
| CPU, mémoire, instances, concurrency | métriques Serverless Containers |

### 3.5 Feedback et qualité

Objectif : relier les signaux utilisateur aux étages techniques.

| Signal | Source |
|---|---|
| Volume et taux de feedback | `chat_feedbacks`, `chat_runs` |
| Feedbacks négatifs par catégorie | analyse IA des feedbacks, `chat_feedbacks` |
| Part `retrieval_issue` | analyse IA des feedbacks |
| Part `missing_document` | analyse IA des feedbacks |
| Questions sans réponse les plus fréquentes | `chat_runs`, `chat_feedbacks` |
| Évolution helpful rate / no-answer après changement config | `chat_runs`, `chat_feedbacks` |

## 4. Traces et corrélation

Chaque tour doit pouvoir être reconstruit de bout en bout à partir d'un identifiant commun.

Identifiant de corrélation cible :
- `turn_id` comme identifiant métier stable du tour ;
- `trace_id` comme identifiant technique de trace distribuée ;
- propagation des deux identifiants dans les logs applicatifs, appels providers, `chat_runs` et traces Cockpit/Grafana.

Trace cible par requête :

```text
HTTP request
  -> Streamlit / API entrypoint
  -> QueryProcessor
  -> Retriever
  -> SectionAggregator
  -> Reranker
  -> ContextSelector
  -> ContextBuilder
  -> Generator
  -> Provider calls Albert / Scaleway
  -> chat_runs + logs + traces
```

Règle de diagnostic : un incident qualité doit permettre de partir d'un feedback utilisateur, retrouver le `turn_id`, ouvrir la trace correspondante, voir les latences et statuts de chaque étage, puis revenir aux champs `chat_runs` pour analyser le contexte et les sources.

## 5. Instrumentation minimale

### 5.1 Champs à fiabiliser ou ajouter

- Fiabiliser `v3_reranker_status` / `v3_reranker_error` et compléter par `rerank_provider`, `rerank_model`, `rerank_error_type`.
- Listes ou résumés exploitables avant/après rerank, pas seulement des compteurs.
- Statut provider par étape : embeddings, rerank, selector, génération.
- `trace_id` dans `chat_runs`.
- Normalisation des erreurs : timeout, 4xx, 5xx, parse_error, empty_response, fallback_used.

### 5.2 Alertes v1

| Alerte | Seuil initial |
|---|---|
| Taux d'échec rerank | > 5 % sur 15 min |
| Taux d'échec LLM | > 2 % sur 15 min |
| Taux HTTP 5xx container | > 1 % sur 15 min |
| Latence P95 totale | régression > 30 % vs baseline |
| No-answer rate | dérive > 30 % vs baseline hebdo |
| Helpful rate | baisse > 10 points sur 4 semaines glissantes |

Les seuils initiaux doivent être calibrés en Phase 1 après baseline, mais l'alerte rerank doit être créée dès la correction du payload `/rerank`.

## 6. Plan itération 2 (juin → 31 octobre 2026)

> **Calendrier** : cale sur la [note 00 §4](00_SYNTHESE_ET_PRIORISATION.md), qui fait foi. Le câblage des diagnostics relève de P0 (dès maintenant), les dashboards de P1 ; les phases ci-dessous décrivent la montée en maturité de l'observabilité.

### Phase 1 — Cadrage et instrumentation (16 juin -> 18 juillet)

- Figer le schéma minimal des métriques production et des champs `chat_runs` nécessaires.
- Exploiter les statuts rerank déjà persistés et ajouter les statuts provider manquants dans les logs métier.
- Propager `turn_id` / `trace_id` dans les logs applicatifs.
- Construire les dashboards Grafana v1 : exécutif, santé RAG, latence, providers/infra, feedback qualité.
- Publier une baseline hebdomadaire : request rate, no-answer, helpful rate, P95/P99, provider errors.

Critère de succès : tout incident qualité ou latence peut être relié à un `turn_id`, une trace et un ensemble de métriques Grafana.

### Phase 2 — Alerting et exploitation (21 juillet -> 29 août)

- Activer les alertes v1 sur rerank, providers, HTTP 5xx, latence et no-answer.
- Mettre en place une revue hebdomadaire qualité/observabilité : top no-answer, top feedbacks négatifs, régressions latence, erreurs provider.
- Relier les extractions `missing_document` / `retrieval_issue` au backlog ingestion/retrieval.
- Documenter le runbook incident : quoi regarder, dans quel ordre, et quelle décision prendre.

Critère de succès : les pannes provider ou régressions RAG critiques sont détectées par alerte ou dashboard, pas par retour utilisateur tardif.

### Phase 3 — Pilotage continu (1er septembre -> 10 octobre)

- Comparer chaque changement retrieval/scoring/chunking avant/après via dashboard et goldset.
- Ajouter des vues par source documentaire, thème RH, groupe utilisateur et version de configuration RAG.
- Exploiter les traces pour réduire les latences P95/P99 par étage.
- Intégrer les signaux observabilité dans les critères d'acceptation des PRs RAG.

Critère de succès : la qualité et la performance du RAG deviennent pilotables en continu, avec des décisions fondées sur signaux production + goldset.

### Phase 4 — Stabilisation et bilan itération 2 (13 octobre -> 31 octobre)

- Stabiliser les dashboards et alertes qui doivent rester actifs en production.
- Documenter le runbook final : investigation latence, panne provider, baisse helpful rate, hausse no-answer.
- Produire le bilan observabilité : métriques disponibles, angles morts restants, coûts, dette instrumentation.
- Prioriser les compléments post-itération : traces plus fines, vues par source, alertes qualité avancées.

Critère de succès : l'équipe peut suivre l'exploitation RAG sans requêtes SQL ad hoc pour les signaux critiques.

## 7. Questions à confirmer avant implémentation

- Quel espace Scaleway Cockpit/Grafana est provisionné pour staging et production ?
- Quelles métriques Serverless Containers sont déjà disponibles sans instrumentation applicative supplémentaire ?
- Quel format de traces est attendu côté Scaleway Cockpit pour une intégration propre avec les logs applicatifs ?
- Quels champs `chat_runs` doivent rester en base longue durée, et lesquels peuvent être seulement des métriques agrégées ?
