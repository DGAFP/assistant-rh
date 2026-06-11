# Dossier d'audit — Assistant RH

Audits du système réalisés en juin 2026, en préparation de la roadmap qualité (issue [#83](https://github.com/DGAFP/assistant-rh/issues/83), présentation du 15 juin 2026).

| Note | Contenu |
|---|---|
| [01 — Audit qualité RAG](01_RAG_QUALITY_AUDIT_2026-06.md) | Qualité du pipeline RAG : reranker cassé (422 silencieux), scoring RRF plat, chunking, couverture d'index (cas SFT : 58 % des fiches SP sans chunks), disparité métriques auto vs experts. Inclut la roadmap 3 mois et la proposition de métriques. |
| [02 — Audit architectural](02_ARCHITECTURE_AUDIT_2026-06.md) | Architecture, qualité de code, observabilité, sécurité, CI/CD : pannes silencieuses, schéma DB non versionné, absence de tests du package critique, XSS/SQLi/rétention des données, plan d'action priorisé. |

Méthode commune : constats vérifiés et reproductibles (code, base locale copie staging, replays contre l'API Albert réelle), pas d'impressions. Chaque note se termine par des actions priorisées avec critères de succès.
