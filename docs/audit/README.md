# Dossier d'audit — Assistant RH

Audits du système réalisés en juin 2026, en préparation de la roadmap qualité (issue [#83](https://github.com/DGAFP/assistant-rh/issues/83), présentation du 15 juin 2026).

| Note | Contenu |
|---|---|
| [00 — Synthèse & priorisation (présentation validation)](00_SYNTHESE_ET_PRIORISATION.md) | Document de présentation : diagnostic en une page, constats majeurs vérifiés, grandes priorités (qualité RAG / multi-ministère / itération 3), priorisation P0→P3, métriques de validation et décisions demandées. Point d'entrée du dossier. |
| [01 — Audit qualité RAG & planification itération 2](01_RAG_QUALITY_AUDIT_2026-06.md) | Qualité du pipeline RAG : reranker cassé au moment de l'audit (422 silencieux), scoring RRF plat, chunking, couverture d'index (cas SFT : 58 % des fiches SP sans chunks), disparité métriques auto vs experts. Inclut la planification itération 2 (juin → 31 octobre 2026) et la proposition de métriques. |
| [02 — Audit architectural](02_ARCHITECTURE_AUDIT_2026-06.md) | Architecture, qualité de code, observabilité, sécurité, CI/CD : pannes silencieuses, schéma DB non versionné, tests RAG dispersés et couverture incomplète, XSS/SQLi/rétention des données, plan d'action priorisé. |
| [03 — Observabilité RAG & dashboards Grafana](03_RAG_OBSERVABILITY_ROADMAP_2026-06.md) | Rendre le RAG pilotable en production : usage, qualité, latence, erreurs, traces et alerting sur socle Scaleway Cockpit/Grafana corrélé à `chat_runs`. |
| [04 — Observations initiales (Day 1) réconciliées](04_OBSERVATIONS_INITIALES_2026-06-05.md) | Note d'onboarding du 2026-06-05 : vision stratégique large (scope multi-ministère, habilitations/ProConnect, classifier + RRF par type, abstention, autorité des sources, UX, taxonomie d'erreurs, benchmark), réconciliée avec les notes 01-03 (confirmé / nuancé / encore ouvert). Sert de cadre produit/sécurité de l'itération 2. |
| [05 — Plan d'audit & couverture](05_PLAN_AUDIT_ET_COUVERTURE.md) | Méta-note : ce qui a été audité (01-04), ce qui reste à couvrir (coûts, latence, fraîcheur des données, sécurité prompt, RGPD, RGAA, infra, embeddings), limites méthodologiques, et passage d'audits ponctuels à une visibilité continue. Inclut un séquencement. |
| [06 — Audit approfondi code & schéma DB](06_AUDIT_CODE_ET_DB.md) | Anti-patterns mesurés : `chat_runs` à 154 colonnes (33 jamais écrites dans le décompte pré-#88 ; diagnostics retrieval encore partiels), index vectoriels manquants sur 3 des 4 tables de retrieval (scans séquentiels), absence de FK, tables fantômes Scalingo, erreurs « fail-open » silencieuses, et métriques critiques à remonter dans Scaleway/Cockpit. |

Méthode commune : constats vérifiés et reproductibles (code, base locale copie staging, replays contre l'API Albert réelle), pas d'impressions. Chaque note se termine par des actions priorisées avec critères de succès. La note 04 est antérieure (Day 1, hypothèses) et explicitement réconciliée a posteriori.
