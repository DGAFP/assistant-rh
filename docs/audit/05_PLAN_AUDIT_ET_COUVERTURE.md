# Plan d'audit & couverture

> Dossier d'audit : voir [README](README.md). Date : 2026-06-09.
> Objet : cartographier **ce qui a déjà été audité** (notes 01-04), **ce qu'il reste à couvrir** pour une visibilité complète, et **comment passer d'audits ponctuels à une visibilité continue**.

## 1. Principe

Les notes 01-04 sont des audits *ponctuels* : photographies prises à un instant, sur une copie locale de staging, par investigation manuelle. Pour une « vraie visibilité » il faut deux choses de plus :

1. **Compléter les dimensions non couvertes** (coûts, latence, fraîcheur des données, sécurité applicative en profondeur, RGPD, accessibilité, résilience).
2. **Rendre la visibilité continue et reproductible** — un audit qui se rejoue (baseline d'éval, dashboards, alertes) plutôt qu'une note qui se périme. C'est l'objet des notes 01 (Phase 1 Mesure) et 03 (observabilité), à industrialiser.

Ce plan distingue donc **couverture** (a-t-on regardé ?) et **instrumentation** (saura-t-on le revoir demain sans tout refaire ?).

## 2. État de couverture par dimension

Légende : ✅ couvert · ◐ partiel · ⬜ à faire.

| # | Dimension | Statut | Où | Limite actuelle |
|---|---|---|---|---|
| D1 | Qualité RAG (chunking, retrieval, scoring, selector, génération) | ✅ | Note 01 | Sur copie locale ; pas de mesure prod continue |
| D2 | Couverture documentaire / index (doc→section→chunk) | ✅ | Note 01 add. 1 | Quantifié SP/MATTE ; non rejoué en CI |
| D3 | Dispositif d'évaluation (auto vs expert) | ✅ | Note 01 add. 2 | Diagnostic posé ; baseline non encore construite |
| D4 | Architecture, qualité de code, CI/CD | ✅ | Note 02 | — |
| D5 | Sécurité applicative (XSS, SQLi, auth, conteneurs) | ◐ | Note 02 §5 | Revue de surface ; pas de pentest ni SAST/secrets-scan |
| D6 | Observabilité (logs, métriques, alerting) | ✅ (constat) / ⬜ (mise en œuvre) | Notes 02 §4, 03 | Roadmap posée, rien d'instrumenté |
| D7 | Vision produit/sécurité élargie (multi-ministère, habilitations) | ◐ | Note 04 | Cadré, non instruit techniquement |
| **D8** | **Coûts & consommation API** (tokens/€ par requête et par étage) | ⬜ | — | Jamais mesuré |
| **D9** | **Latence réelle en production** (P50/P95/P99 par étage) | ⬜ | — | Données dans `chat_runs`, non exploitées |
| **D10** | **Fraîcheur & qualité des données d'ingestion** (péremption juridique, parsing, doublons docs) | ⬜ | — | Risque métier : droit périmé |
| **D11** | **Sécurité du prompt & comportement conversationnel** (injection, multi-tours, hors-périmètre) | ⬜ | — | Assistant public, non testé adversarialement |
| **D12** | **RGPD / données personnelles** (rétention, anonymisation, registre) | ◐ | Note 02 S5 | Risque signalé, pas d'analyse DPO |
| **D13** | **Accessibilité (RGAA) & UX** | ⬜ | — | Obligation service public, jamais auditée |
| **D14** | **Infra / résilience / charge / backup** (Scaleway) | ⬜ | — | Dimensionnement, cold starts, PRA inconnus |
| **D15** | **Embeddings & vocabulaire métier RH** (CET, IFSE, CMO…) | ⬜ | — | Hypothèse note 04, non testée |
| **D16** | **Audit de code & schéma DB** (rationalisation `chat_runs`, colonnes mortes, traces RAG manquantes, index vectoriels, FK, fail-open) | ✅ (audit) / ⬜ (refonte) | Note 06 | Anti-patterns chiffrés ; refonte à exécuter |

## 3. Ce qui reste à faire — fiches d'audit

### D8 — Coûts & consommation API *(quick win, données disponibles)*
- **Objet** : coût par requête et par étage (3 appels LLM + embedding + rerank ; selector reçoit des sections non tronquées de 20-174 k chars).
- **Méthode** : reconstituer les volumes de tokens depuis `chat_runs` ; chiffrer coût actuel et économies potentielles (plafonner le selector, cache d'embeddings).
- **Données** : `chat_runs` (local ou export). **Dépendances** : aucune. **Effort** : S.

### D9 — Latence réelle en production *(quick win, données disponibles)*
- **Objet** : distributions P50/P95/P99 par étage ; goulots ; corrélation latence↔échec (query-processing observé à 5,3 s en local vs 200-500 ms documentés).
- **Méthode** : agrégation des colonnes `v3_*_ms` de `chat_runs`. **Dépendances** : aucune. **Effort** : S.
- *D8+D9 peuvent former une note 06 « Performance & coûts », mêmes données.*

### D10 — Fraîcheur & qualité des données d'ingestion *(risque métier élevé)*
- **Objet** : âge des sources vs aujourd'hui (Légifrance/Service-Public/MATTE) — risque de réponses sur du droit abrogé ; doublons de documents ; complétude du parsing PDF (Poppler/Tesseract) ; cohérence des `references_juridiques`.
- **Méthode** : SQL sur `last_updated_date`/`publication_date` de `rag_documents` ; échantillonnage de parsing ; vérification de dates d'effet. **Dépendances** : accès données. **Effort** : M.

### D11 — Sécurité du prompt & comportement conversationnel
- **Objet** : résistance à l'injection (corpus et requête — l'assistant est public), robustesse multi-tours (`follow_up` tronqué à 8 messages), comportement hors-périmètre / sujets sensibles, cohérence de l'anti-hallucination, fuite de prompt système.
- **Méthode** : batterie de questions adverses rejouée sur le pipeline ; à intégrer au benchmark (note 01 / livrable 2 de la note 04). **Dépendances** : pipeline exécutable. **Effort** : M.

### D12 — RGPD / données personnelles *(décision DPO)*
- **Objet** : `chat_runs`/`chat_feedbacks` stockent questions/réponses/prompts depuis oct. 2025, avec des situations RH individuelles potentiellement identifiantes ; aucune rétention/anonymisation.
- **Méthode** : cartographie des données personnelles, base légale, durée de conservation, registre de traitement, job de purge/anonymisation. **Dépendances** : DPO/juridique. **Effort** : M.

### D13 — Accessibilité (RGAA) & UX
- **Objet** : conformité RGAA (obligation service public) de l'UI Streamlit à HTML custom ; lisibilité des réponses, expérience de citation des sources.
- **Méthode** : audit RGAA (axe-core/lighthouse + revue manuelle) ; tests utilisateurs ciblés. **Dépendances** : UI déployée. **Effort** : M.

### D14 — Infra / résilience / charge / backup
- **Objet** : dimensionnement des Serverless Containers, cold starts, concurrency, CPU/mémoire ; stratégie de backup de la base ; plan de reprise (PRA).
- **Méthode** : revue config Scaleway + métriques Cockpit (cf. note 03) + test de charge. **Dépendances** : accès infra Scaleway. **Effort** : M-L.

### D15 — Embeddings & vocabulaire métier RH
- **Objet** : vérifier que les embeddings capturent le vocabulaire RH (CET, ASA, ARE, IJ, IFSE, IFC, CMO, CGM, RIFSEEP, RIAA…) et distinguent les paires confusables.
- **Méthode** : tests de similarité sur paires métier ; sinon dictionnaire de synonymes (query expansion) ou modèle d'embedding plus adapté. **Dépendances** : embeddings accessibles. **Effort** : S-M.

### D16 — Audit de code & schéma DB *(prérequis observabilité)* — ✅ audité, note [06](06_AUDIT_CODE_ET_DB.md)
- **Objet** : `chat_runs` est devenue une table obèse (~125 colonnes accumulées par strates de versions, beaucoup mortes ou redondantes — `use_query_rewriting`/`rewritten_query`/`reformulated_query`, colonnes v2) et **mal ciblée** : on logge des compteurs et agrégats, mais pas les **sets de chunks à chaque étape du retrieval** (par table → fusion → agrégation → rerank → selector → contexte), ce qui empêche de rejouer/expliquer la perte d'un chunk pertinent. Volet code associé : modules de logging (`chat_logger.py`, `build_log_row`) et duplication des écritures.
- **Méthode** : (1) cartographier l'usage réel de chaque colonne (lue/écrite/morte) via grep des consommateurs Python/TS/SQL + échantillon de valeurs ; (2) proposer un schéma cible — cœur `chat_runs` resserré + **table d'événements de trace séparée** (un enregistrement par étape : chunk_ids, scores réels, décisions) ; (3) migration versionnée (lié note 02 A4). 
- **Données** : `chat_runs`, code de logging. **Dépendances** : décision de schéma ; jumeau du chantier observabilité (note 03). **Effort** : M.

### Renforcement D5 — Sécurité applicative en profondeur
- Ajouter à la revue de surface : SAST Python (bandit/semgrep), scan de secrets (gitleaks), revue d'autorisation de bout en bout, et un pentest léger des pages admin et de l'endpoint Mastra. **Effort** : M.

## 4. Limites méthodologiques transverses (à lever)

- **Prod jamais auditée directement** : tous les constats viennent d'une copie locale de staging (accès prod non autorisé pendant l'audit). À confirmer sur prod : présence/absence de `rag_chunks_test`, état réel du reranker post-#88, distributions de latence/coût réelles.
- **Pas de baseline d'éval reproductible** : `goldset_questions_v2` vide, `goldset_runs` disparue, notebooks pointant des DSN retirés. Tant qu'elle n'existe pas, aucun audit qualité ne se rejoue (note 01, Phase 1).
- **Stats non rafraîchies** : fragilité par thème (note 04) à recalculer sur données courantes.
- **Point-in-time** : ces notes se périment ; sans instrumentation continue, il faudra les refaire à la main.

## 5. Du ponctuel au continu — instrumentation cible

Pour une visibilité qui dure, trois socles, déjà esquissés ailleurs, à industrialiser :

1. **Baseline d'éval rejouable** (note 01, Phase 1) : goldset stratifié + harness one-command + nightly CI → rejoue D1/D2/D3/D11 à chaque changement.
2. **Dashboards & alertes** (note 03) : usage, qualité, latence (D9), coûts (D8), erreurs/fallbacks providers → visibilité temps réel, fin des pannes silencieuses.
3. **Checks de couverture & contrats en CI** (note 02, actions 1-3) : réconciliation index (D2), contrats providers, dérive de schéma → la régression devient bloquante, pas découverte des mois après.

Cible : chaque dimension critique a soit un **test rejouable**, soit un **dashboard + alerte**, soit les deux.

## 6. Séquencement proposé

| Vague | Contenu | Justification |
|---|---|---|
| **Immédiat** | D8 + D9 (note « Performance & coûts ») ; confirmation prod des constats notes 01-02 | Données déjà là, complète le dossier de décision du 15 juin, sans dépendance |
| **Court terme** | Baseline d'éval (socle 1) ; D10 (fraîcheur données) ; D12 (RGPD) ; D16 (refonte `chat_runs` + traces) ; renforcement D5 | Conditionnent la mesure (D10/baseline), l'observabilité (D16, jumeau du socle 2) et couvrent les risques métier/conformité |
| **Itération 2** | D11 (sécurité prompt, via benchmark) ; D15 (embeddings) ; D7 (multi-ministère, prio P2) ; socles 2-3 d'instrumentation | S'appuient sur la baseline qualité et sur les chantiers structurels de la note 04 |
| **Selon priorités** | D13 (RGAA/UX) ; D14 (infra/résilience) | Obligations et robustesse, à caler avec produit et ops |

## Sources

- Notes [01](01_RAG_QUALITY_AUDIT_2026-06.md), [02](02_ARCHITECTURE_AUDIT_2026-06.md), [03](03_RAG_OBSERVABILITY_ROADMAP_2026-06.md), [04](04_OBSERVATIONS_INITIALES_2026-06-05.md) du présent dossier.
- Périmètre observé : code du monorepo, base locale (copie staging), `chat_runs`/`chat_feedbacks`.
