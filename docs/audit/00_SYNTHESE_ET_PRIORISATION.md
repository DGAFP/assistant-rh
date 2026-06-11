# Synthèse & priorisation — itération 2

> Document de présentation pour validation. Dossier complet : voir [README](README.md).
> Date : 2026-06-09. Sources : notes 01-06 du dossier (constats vérifiés sur code + base locale copie staging, replays contre l'API Albert réelle).

---

## 1. En une page

**Le produit a une vraie valeur quand les bonnes sources remontent** (≈ 74 % de feedbacks positifs sur 761 ; 54 % de 4-5★ sur l'export beta). **Mais une part importante des échecs vient de l'amont du pipeline, pas de la génération** — et plusieurs causes étaient invisibles faute d'observabilité.

Trois découvertes structurent l'itération 2 :

1. **Le reranker Albert était silencieusement cassé** (API `/rerank` 422, fallback sans alerte). Replay : 0/4 → 3/4 questions corrigées une fois réparé + hybride. **Déjà corrigé en quick win** ([#88](https://github.com/DGAFP/assistant-rh/pull/88), issue #87).
2. **Trou de couverture d'index** : 58 % des fiches Service-Public ont des sections mais **zéro chunk** — elles sont indexées au sens documentaire mais invisibles au retrieval (cas SFT). Deux générations d'ingestion coexistent.
3. **L'observabilité a été conçue puis jamais câblée** : `chat_runs` a 154 colonnes dont 33 jamais écrites — précisément les colonnes de diagnostic (chunks par étape, scores). Et 3 des 4 tables de retrieval n'ont **aucun index vectoriel** (scans séquentiels).

À cela s'ajoute une disparité structurelle **auto-éval vs jugement expert** : tant qu'elle n'est pas mesurée, aucune métrique automatique ne peut arbitrer les futures modifications.

**Ligne directrice** : d'abord rendre le système **mesurable et observable**, ensuite corriger **retrieval/scoring/données**, puis étendre au **multi-ministère** — pas l'inverse.

---

## 2. Constats majeurs vérifiés

| # | Constat | Gravité | Statut | Détail |
|---|---|---|---|---|
| 1 | Reranker `/rerank` cassé (422 silencieux) | Critique | ✅ corrigé (#88) | Note [01](01_RAG_QUALITY_AUDIT_2026-06.md) §1 |
| 2 | Score RRF plat, sans amplitude de pertinence | Élevé | à traiter | Note 01 §2.1 |
| 3 | 58 % des fiches SP sans chunk (trou d'index, cas SFT) | Élevé | à traiter | Note 01 add. 1 |
| 4 | Disparité auto-éval vs expert, goldset vide | Élevé | à traiter | Note 01 add. 2 |
| 5 | `chat_runs` : 154 col., 33 de diagnostic jamais écrites | Élevé | à traiter | Note [06](06_AUDIT_CODE_ET_DB.md) §1.1 |
| 6 | Index vectoriels manquants (matte, dgafp, rgrh) | Élevé | à traiter | Note 06 §2.2 |
| 7 | Erreurs « fail-open » sans métrique (selector, rerank, embedder) | Élevé | à traiter | Note 06 §3 |
| 8 | Aucune observabilité consolidée / alerting | Élevé | à traiter | Notes [02](02_ARCHITECTURE_AUDIT_2026-06.md) §4, [03](03_RAG_OBSERVABILITY_ROADMAP_2026-06.md) |
| 9 | 0 test dans le package RAG ; schéma DB non versionné | Moyen | à traiter | Note 02 §3, A4 |
| 10 | Sécurité UI (XSS rendu LLM, SQLi, root, RGPD conversations) | Moyen-élevé | à traiter | Note 02 §5 |
| 11 | Doublons SP 33 %, chunks-titres, sections géantes | Moyen | à traiter | Note 01 §2.2 |
| 12 | Données : fraîcheur juridique, embeddings RH non vérifiés | Moyen | à instruire | Notes [04](04_OBSERVATIONS_INITIALES_2026-06-05.md), [05](05_PLAN_AUDIT_ET_COUVERTURE.md) |

---

## 3. Les grandes priorités

| Prio | Objectif | Horizon |
|---|---|---|
| **P1 — Qualité du RAG** | Fiabiliser mesure, observabilité, scoring, retrieval, couverture d'index, abstention, génération | Itération 2 (juin → 31 oct.) |
| **P2 — Intégrer 5 nouveaux ministères** | Ingestion des sources, métadonnées normalisées, **scope serveur avant retrieval**, habilitations (ProConnect) | Itération 2, en parallèle |
| **P3 — Réemploi dans un autre produit** | Industrialiser le pipeline comme brique réutilisable | Itération 3 |

**Articulation décisive** : P2 (multi-ministère) ne doit pas précéder P1 (qualité). Étendre à 6 périmètres un retrieval au scoring plat, à la couverture trouée et sans index vectoriel **démultiplierait les défauts et les coûts**. P1 et P2 avancent en parallèle, mais les fondations qualité (mesure + scoring + index) conditionnent l'extension réelle.

---

## 4. Priorisation proposée (à valider)

### P0 — Quick wins (semaine du 16 juin, sans dépendance)
- ✅ **Fix reranker** (#88) — fait.
- **Créer les index vectoriels** (matte, dgafp, rgrh) → latence/coût, prérequis multi-ministère.
- **Câbler les colonnes de diagnostic déjà présentes** (chunks par étape, scores) → débloque l'observabilité sans changer le schéma.
- **Compteurs + alertes sur les chemins fail-open** (rerank, embeddings, selector, table vide).
- **Passer le retrieval en hybride** (réversible), validé sur le jeu de questions.

### P1 — Mesure & observabilité (16 juin → 18 juillet) — *bloquant : rien n'est jugeable sans ça*
- **Goldset v1** (80-120 questions beta stratifiées) + **mesure de l'écart auto-éval / expert** (set de calibration, κ cible).
- **Harness d'éval reproductible** (recall, no-answer justifié, juge calibré) en CI/local.
- **Baseline chiffrée** avant/après ; **dashboards Cockpit/Grafana v1** (usage, qualité, latence, providers) + alertes.
- **Taxonomie d'erreurs partagée** (note 04, livrable 1) comme socle commun audit/éval/PR.

### P1.5 — Chantiers structurels multi-ministère (en parallèle, longs)
- **Scope ministériel côté serveur** (`ministry_id` + filtrage SQL avant retrieval), séparé en 3 niveaux : autorisation / priorité des sources / autorité documentaire.
- **Ingestion sources ministérielles** (MI, MSO, MASA, MEF) + **réconciliation index** (chaque doc indexable a ≥ 1 chunk) + métadonnées normalisées.
- **ProConnect + habilitations** (enforcement dans la retrieval, pas l'UI) + **blocage des données personnelles / RGPD**.

### P2 — Retrieval, scoring, données (après baseline)
- **Scoring v2** (score reranker comme signal aval, seuil d'abstention).
- **Dédup SP, filtrage chunks-titres, couverture `references_juridiques`.**
- **Classifier de question + policies RRF par type** ; **abstention stricte** sur contexte faible.
- **Fraîcheur des données** (droit périmé) ; **embeddings RH** (vocabulaire métier).

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

1. **Valider les grandes priorités** : P1 qualité + P2 multi-ministère en parallèle, P3 (autre produit) en itération 3 — et le principe « la qualité conditionne l'extension ».
2. **Valider le séquencement P0 → P2.5** et les cibles chiffrées du §5.
3. **Arbitrer 2 sujets transverses** : (a) rétention/anonymisation des conversations (RGPD, décision DPO) ; (b) cible Python vs Mastra à terme (la double maintenance pèse sur chaque évolution).
4. **Acter les quick wins P0** (index vectoriels, câblage observabilité, alertes fail-open, hybride) dès la semaine du 16 juin.

## Sources

- Notes [01](01_RAG_QUALITY_AUDIT_2026-06.md) à [06](06_AUDIT_CODE_ET_DB.md) du présent dossier et le [plan d'audit](05_PLAN_AUDIT_ET_COUVERTURE.md).
- Issue [#83](https://github.com/DGAFP/assistant-rh/issues/83) ; quick win reranker [#88](https://github.com/DGAFP/assistant-rh/pull/88).
