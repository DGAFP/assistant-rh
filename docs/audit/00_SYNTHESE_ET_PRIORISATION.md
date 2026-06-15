# Synthèse & priorisation — itération 2

> Document de présentation pour validation. Dossier complet : voir [README](README.md).
> Date : 2026-06-09. Sources : notes 01-06 du dossier (constats vérifiés sur code + base locale copie staging, replays contre l'API Albert réelle).
> **Vérifié sur staging réel le 2026-06-15 : voir [note 07](07_VERIFICATION_STAGING_ET_PRIORISATION.md) pour les corrections de chiffres et la priorisation refondée. Changements majeurs : le trou de couverture Service-Public est déjà refermé sur staging (55/55), et DGAFP a 0 embedding (corpus éteint en sémantique), pas « un index manquant ».**

---

## 1. En une page

**Le produit a une vraie valeur quand les bonnes sources remontent** (≈ 74 % de feedbacks positifs sur 761 ; 54 % de 4-5★ sur l'export beta). **Mais une part importante des échecs vient de l'amont du pipeline, pas de la génération** — et plusieurs causes étaient invisibles faute d'observabilité.

Trois découvertes structurent l'itération 2 :

1. **Le reranker Albert était silencieusement cassé** (API `/rerank` 422, fallback sans alerte). Replay : 0/4 → 3/4 questions corrigées une fois réparé + hybride. **Déjà corrigé en quick win** ([#88](https://github.com/DGAFP/assistant-rh/pull/88), issue #87).
2. **Trou de couverture d'index** : 58 % des fiches Service-Public avaient des sections mais **zéro chunk** — invisibles au retrieval (cas SFT). Cause racine établie et corrigée côté code (issue [#89](https://github.com/DGAFP/assistant-rh/issues/89), PRs [#95](https://github.com/DGAFP/assistant-rh/pull/95)–[#98](https://github.com/DGAFP/assistant-rh/pull/98)). **Correction (2026-06-15, staging réel) : le rejeu est fait — 55/55 fiches SP ont des chunks, trou refermé. Le problème de couverture vivant est désormais MATTE (17/44 docs) et MSO (16/16, table jamais interrogée), pas SP.**
3. **L'observabilité a été conçue puis reste incomplète** : l'audit a relevé 154 colonnes dans `chat_runs`, dont 33 jamais écrites avant #88. Le statut reranker est maintenant câblé, mais les diagnostics retrieval exploitables restent absents ou partiels (chunks par étape, scores, listes avant/après). Et 3 des 4 tables de retrieval n'ont **aucun index vectoriel** (scans séquentiels).

À cela s'ajoute une disparité structurelle **auto-éval vs jugement expert** : tant qu'elle n'est pas mesurée, aucune métrique automatique ne peut arbitrer les futures modifications.

**Ligne directrice** : d'abord rendre le système **mesurable et observable**, ensuite corriger **retrieval/scoring/données**, puis étendre au **multi-ministère** — pas l'inverse.

---

## 2. Constats majeurs vérifiés

| # | Constat | Gravité | Statut | Détail |
|---|---|---|---|---|
| 1 | Reranker `/rerank` cassé (422 silencieux) | Critique | ✅ corrigé (#88) | Note [01](01_RAG_QUALITY_AUDIT_2026-06.md) §1 |
| 2 | Score RRF plat, sans amplitude de pertinence | Élevé | à traiter | Note 01 §2.1 |
| 3 | 58 % des fiches SP sans chunk (trou d'index, cas SFT) | Élevé | ✅ **résolu sur staging (55/55 au 15/06)** ; reste MATTE 17/44 + MSO + réconciliation CI | Notes 01 add. 1, [07](07_VERIFICATION_STAGING_ET_PRIORISATION.md) |
| 4 | Disparité auto-éval vs expert, goldset vide | Élevé | à traiter | Note 01 add. 2 |
| 5 | `chat_runs` : 154 col., diagnostics retrieval non câblés ou partiels | Élevé | à traiter | Note [06](06_AUDIT_CODE_ET_DB.md) §1.1 |
| 6 | Index vectoriels manquants (matte, rgrh) ; **DGAFP : 0 embedding (corpus éteint, pas un pb d'index)** | Élevé | à traiter | Notes 06 §2.2, [07](07_VERIFICATION_STAGING_ET_PRIORISATION.md) §2 |
| 7 | Erreurs « fail-open » sans métrique (selector, rerank, embedder) | Élevé | à traiter | Note 06 §3 |
| 8 | Aucune observabilité consolidée / alerting | Élevé | à traiter | Notes [02](02_ARCHITECTURE_AUDIT_2026-06.md) §4, [03](03_RAG_OBSERVABILITY_ROADMAP_2026-06.md) |
| 9 | Tests RAG dispersés/incomplets ; schéma DB non versionné | Moyen | à traiter | Note 02 §3, A4 |
| 10 | Sécurité UI (XSS rendu LLM, SQLi, root, RGPD conversations) | Moyen-élevé | à traiter | Note 02 §5 |
| 11 | Doublons SP 33 %, chunks-titres, sections géantes | Moyen | à traiter | Note 01 §2.2 |
| 12 | Données : fraîcheur juridique, embeddings RH non vérifiés | Moyen | à instruire | Notes [04](04_OBSERVATIONS_INITIALES_2026-06-05.md), [05](05_PLAN_AUDIT_ET_COUVERTURE.md) |

---

## 3. Les grandes priorités

| Prio | Objectif | Horizon |
|---|---|---|
| **P1 — Qualité du RAG** | Fiabiliser mesure, observabilité, scoring, retrieval, couverture d'index, abstention, génération | Itération 2 (juin → 31 oct.) |
| **P2 — Intégrer 5 nouveaux ministères** | Ingestion des sources, métadonnées normalisées, **scope serveur avant retrieval**, habilitations (ProConnect) | Itération 2, après fondations qualité |
| **P3 — Réemploi dans un autre produit** | Industrialiser le pipeline comme brique réutilisable | Itération 3 |

**Articulation décisive** : P2 (multi-ministère) vient après les fondations qualité. Des travaux préparatoires peuvent avancer en parallèle (sources, auth, métadonnées), mais l'extension réelle ne doit pas précéder P1 puis P1.5. Étendre à 6 périmètres un retrieval au scoring plat, à la couverture trouée et sans index vectoriel **démultiplierait les défauts et les coûts**.

---

## 4. Priorisation proposée (à valider)

### P0 — Quick wins (Aujourd'hui → semaine du 22 juin, sans dépendance)
- ✅ **Fix reranker** (#88) — fait, statut `v3_reranker_status` persisté.
- ✅ **Fix ingestion Service-Public** ([#95](https://github.com/DGAFP/assistant-rh/pull/95)–[#98](https://github.com/DGAFP/assistant-rh/pull/98)) — mergé **et rejeu staging fait (55/55 au 15/06)** ; reste réconciliation index en CI + couverture MATTE 17/44 et MSO.
- **Backfill embeddings DGAFP (0/3 992 `embedding_m3`) et RGRH (146/324)**, puis **créer les index vectoriels** (matte, dgafp, rgrh). DGAFP est aujourd'hui **éteint en recherche sémantique** (pas seulement non-indexé) → câbler l'index sans embedding ne sert à rien. cf [note 07](07_VERIFICATION_STAGING_ET_PRIORISATION.md).
- **Fixer `rag_chunks_test`** (activée en config, table absente sur staging → fail-open à chaque requête) : créer la table ou désactiver le flag, et rendre l'absence bloquante.
- **Câbler les colonnes de diagnostic déjà présentes** (chunks par étape, scores) → débloque l'observabilité retrieval sans changer le schéma.
- **Observabilité ingestion & monitoring DB** : dashboard Grafana/Cockpit sur l'état réel du corpus prod/staging — ce qui est **réellement ingéré** (docs → chunks), **complétude des embeddings** par corpus et par table (m3, bge), tables interrogées vs réellement présentes — avec **alertes** sur les écarts (corpus à 0 embedding, doc indexable sans chunk, table activée absente).
- **Vérification anti-doublons à l'ingestion** : contrôle de déduplication systématique (Service-Public à 33 % de doublons) pour détecter et bloquer les chunks dupliqués dès l'ingestion, pas en aval.
- **Compteurs + alertes sur les chemins fail-open** (rerank, embeddings, selector, table vide).

### P1 — Mesure & observabilité (à partir de la semaine du 29 juin) — *bloquant : rien n'est jugeable sans ça*
- **Goldset v2** (80-120 questions beta stratifiées) + **mesure de l'écart auto-éval / expert** (set de calibration, κ cible).
- **Harness d'éval reproductible** (recall, no-answer justifié, juge calibré) en CI/local.
- **Baseline chiffrée** avant/après ; **dashboards Cockpit/Grafana v1** (usage, qualité, latence, providers) + alertes.
- **Taxonomie d'erreurs partagée** (note 04, livrable 1) comme socle commun audit/éval/PR.

### P1.5 — Retrieval, scoring, données (après baseline — cible S2 juillet, ~2 semaines à 2 itérations/semaine)
- **Scoring v2** (score reranker comme signal aval, seuil d'abstention).
- **Dédup SP, filtrage chunks-titres, couverture `references_juridiques`.**
- **Classifier de question + policies RRF par type** ; **abstention stricte** sur contexte faible.
- **Fraîcheur des données** (droit périmé) ; **embeddings RH** (vocabulaire métier).
- **Retrieval hybride — conditionnel, non prioritaire** : une première tentative n'a **pas amélioré** les résultats (le sémantique seul reste devant). Ne le ré-ouvrir que si le goldset (P1) révèle un **déficit lexical résiduel** après le scoring v2, et uniquement en **A/B réversible, validé avant/après**. Ce n'est pas un chantier par défaut.

### P2 — Chantiers structurels multi-ministère (cible S4 juillet, ~2 semaines en focus ; amorce en parallèle possible dès S1 juillet si Luis peut prêter main-forte)
- **Scope ministériel côté serveur** (`ministry_id` + filtrage SQL avant retrieval), séparé en 3 niveaux : autorisation / priorité des sources / autorité documentaire.
- **Ingestion sources ministérielles** (MI, MSO, MASA, MEF) + **réconciliation index** (chaque doc indexable a ≥ 1 chunk) + métadonnées normalisées.
- **ProConnect + habilitations** (enforcement dans la retrieval, pas l'UI) + **blocage des données personnelles / RGPD**.

### P2.5 — Produit & UX (après baseline)
- Affichage enrichi des sources (dates, fraîcheur, contradictions, PDF page ciblée) ; rebond conversationnel ; historique ; disclaimer permanent.
- Rationalisation `chat_runs` + table de traces ; tests du package RAG ; migrations versionnées.
- Prompts EN testés ; test du LLM générateur (vs Mistral medium) ; API/Mastra observable.

### P3 — Exploratoire / itération 3
- Réemploi du pipeline dans un autre produit ; agentic deep research borné ; GraphRAG documentaire ; upload utilisateur. **Conditionnels** à des fondations stabilisées.

---

## 5. Comment on saura que ça marche (métriques de validation)

| Métrique | Baseline (à figer P1) | Cible itération 2 |
|---|---|---|
| Recall@10 (bonne source dans le contexte) | à mesurer | +20 pts |
| Taux de no-answer sur questions répondables | ~19 % | < 10 % |
| No-answer justifiés (vrai « hors corpus ») | — | ≥ 90 % |
| Helpful rate utilisateurs (4 sem. glissantes) | 74 % | ≥ 85 % |
| Accord juge↔expert (κ) | non mesuré | ≥ 0,7 |
| Taux d'échec rerank / embeddings / LLM | inconnu | < 1 %, alerté |
| Couverture d'index (docs indexables avec chunks) | trous SP 58 % | 100 %, CI |
| Latence P50/P95 par étage | à figer | pas de régression |

**Principe d'acceptation** : aucune bascule de config/scoring en prod sans run goldset complet avant/après, archivé.

---

## 6. Décisions demandées en validation

1. **Valider les grandes priorités** : P1 puis P1.5 qualité d'abord, P2 multi-ministère ensuite, P3 (autre produit) en itération 3 — et le principe « la qualité conditionne l'extension ».
2. **Valider le séquencement P0 → P2.5** et les cibles chiffrées du §5.
3. **Arbitrer 2 sujets transverses** : (a) rétention/anonymisation des conversations (RGPD, décision DPO) ; (b) cible Python vs Mastra à terme (la double maintenance pèse sur chaque évolution).
4. **Acter les quick wins P0** (backfill embeddings + index vectoriels, observabilité retrieval + ingestion/DB, vérification anti-doublons, alertes fail-open) — Aujourd'hui → semaine du 22 juin. *Le retrieval hybride n'est plus un quick win : déplacé en P1.5 et rendu conditionnel (cf. §4).*

## Sources

- Notes [01](01_RAG_QUALITY_AUDIT_2026-06.md) à [06](06_AUDIT_CODE_ET_DB.md) du présent dossier et le [plan d'audit](05_PLAN_AUDIT_ET_COUVERTURE.md).
- Issue [#83](https://github.com/DGAFP/assistant-rh/issues/83) ; quick win reranker [#88](https://github.com/DGAFP/assistant-rh/pull/88).
