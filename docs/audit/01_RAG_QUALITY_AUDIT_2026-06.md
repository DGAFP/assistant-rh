# Audit qualité RAG & Planification itération 2

> Issue de référence : [#83](https://github.com/DGAFP/assistant-rh/issues/83)
> Date : 2026-06-09 — cadrage itération 2, de juin au 31 octobre 2026.
> Méthode : pipeline exécuté en local (Supabase, copie des données + 3 054 `chat_runs` + 761 `chat_feedbacks` réels), audit SQL du chunking, replay de requêtes réelles issues des feedbacks négatifs, traces bout-en-bout avec variantes contrôlées.

---

## 1. Synthèse pour décision

**Constat principal : le reranker Albert est silencieusement cassé en production.** L'API Albert `/rerank` a changé de schéma (`prompt`/`input` → `query`/`documents`) ; chaque appel renvoie HTTP 422 et le pipeline retombe sans alerte sur l'ordre d'agrégation brut. Or le scoring d'agrégation est un RRF cross-source qui jette l'amplitude de similarité : sans reranker, l'aval ne dispose plus d'un score calibré de pertinence ; il ne reçoit qu'un signal de rang fusionné, peu discriminant.

**Preuve d'impact (replay local, 4 questions en échec issues des feedbacks négatifs réels) :**

| Configuration | Réponses correctes |
|---|---|
| Production actuelle (rerank cassé, mode sémantique) | 0/4 |
| Rerank réparé (payload corrigé, 1 ligne) | 2/4 |
| Rerank réparé + mode hybride | 3/4 |

Le 4e cas (« Qu'est-ce que le RIFSEEP ? ») est un trou documentaire : le terme apparaît dans 18 chunks MATTE mais n'est jamais *défini* dans le corpus.

**Chiffres de cadrage (données réelles) :**
- 74 % de feedbacks positifs sur 761 ; en baisse à 69,5 % en janvier 2026.
- ~19 % des questions RAG légitimes (195/1 018) reçoivent « je n'ai pas trouvé ».
- 58 % des feedbacks négatifs catégorisés = `retrieval_issue`, 23 % = `missing_document`.
- Motif négatif n°1 : « Incomplet » (185 occurrences seul ou combiné).

**Recommandation :** corriger le reranker immédiatement (quick win, 1 ligne + test), puis dérouler la planification itération 2 (§5) : d'abord la mesure et l'observabilité, ensuite retrieval/scoring, ensuite données, chunking et stabilisation avant le 31 octobre.

---

## 2. Constats détaillés

### 2.1 Retrieval et scoring — facteur de non-qualité dominant

**C1. Reranker cassé (critique, vérifié).** `reranker.py` envoie `{"model", "prompt", "input", "top_n"}` ; l'API actuelle exige `{"model", "query", "documents"}` → 422 systématique, reproduit en local contre l'API réelle. Le fallback « keeping aggregated order » est silencieux : aucune métrique, aucun log d'alerte agrégé, le port TypeScript n'a pas encore branché le rerank. Avec le payload corrigé, l'endpoint répond normalement (scores 0,96 vs 1,6e-05 sur un test discriminant).

**C2. Le score fusionné perd l'amplitude de pertinence.** `_merge_cross_source_ranks` applique un RRF (k=60) entre 8 listes (4 tables × 2 chemins chunk/heading) puis normalise au plafond théorique. Le RRF conserve un signal de rang, mais il ne conserve pas l'amplitude des scores de similarité et ne fournit pas un score calibré de pertinence. Conséquences observées :
- scores quasi plats : 0,167 / 0,164 / … avec peu de discrimination entre sections très pertinentes et bruit ;
- le #1 d'une table sans aucun contenu pertinent pèse autant que le #1 d'une table avec une réponse exacte ;
- difficile de distinguer « rien de pertinent dans le corpus » de « réponse exacte trouvée » à partir du score seul — le no-answer repose surtout sur le selector LLM ;
- le score d'agrégation de section (0,5×max + 0,3×mean + 0,2×count) appliqué à des scores plats est dominé par le *nombre* de chunks, pas leur qualité.

**C3. Mode sémantique seul insuffisant sur les termes exacts.** En sémantique (défaut prod), « RIFSEEP » ne remonte aucun des 18 chunks qui le contiennent ; en hybride, 4 remontent mais noyés à 0,167. Les questions à acronymes/références (fréquentes en RH) sont structurellement pénalisées. La table DGAFP est déjà forcée en hybride pour les requêtes juridiques — le mécanisme existe, il n'est pas généralisé.

**C4. Chunks invisibles silencieusement.** Le retriever filtre `WHERE embedding_m3 IS NOT NULL` : 146/324 chunks RGRH (45 %) n'ont pas d'embedding m3 et sont donc exclus de la recherche sémantique sans aucune trace. (MATTE : 762 nulls sur `bge_scw`/`qwen3`, sans impact tant qu'Albert est primaire — mais le fallback embeddings BGE-Scaleway chercherait dans une colonne aux 762 trous.)

**C5. DGAFP quasi absente des réponses finales.** Les distributions de sources loggées sont dominées par MATTE/Service-Public ; DGAFP n'apparaît pas dans le top des combinaisons malgré 3 992 chunks. Intent gating conditionnel + scores plats + sections standalone (pas de regroupement) l'écartent. Point à confirmer avant arbitrage : sur un DSN actif consulté lors de la revue, `rag_chunks_dgafp` apparaît avec `embedding_m3 IS NULL` et `embedding_bge_scw IS NULL` pour 3 992/3 992 chunks. Si ce DSN est représentatif de staging/prod, DGAFP est invisible en recherche sémantique pure et ne peut revenir que par les chemins lexicaux/hybrides.

### 2.2 Chunking et structuration

| Table | n | Médiane (chars) | < 200 chars | Doublons exacts | Particularités |
|---|---|---|---|---|---|
| matte | 959 | 998 | 196 (20 %) | 2 | plafond dur à 1 500 |
| service_public | 1 554 | 466 | **512 (33 %)** | **518 (33 %)** | plafond dur à 1 500 |
| dgafp | 3 992 | 734 | 0 | 0 | 9 chunks > 4 000, max 20 510 |
| rgrh | 324 | 511 | 11 | 0 | 45 % sans embedding m3 |

- **Chunks-titres** : les chunks < 200 chars sont massivement des intitulés seuls (« ANNEXE 5 », « Durée du contrat », « La démission ») — du bruit qui consomme des places du top-20 par table.
- **Doublons Service-Public (33 %)** : mêmes textes indexés plusieurs fois → places gaspillées et sur-pondération RRF artificielle (un doublon classé 2 fois cumule deux contributions).
- **Sections déséquilibrées** : 1 887/5 962 sections < 300 chars (32 %) ; 36 sections > 20 000 chars, max 174 337. Le reranker ne voit que les 1 500 premiers chars d'une section ; le selector reçoit le texte intégral (coût tokens, dilution).
- **Pas de recouvrement** (overlap) entre chunks ; coupes dures à 1 500 chars sans garantie de frontière sémantique.

### 2.3 Données et ingestion

- **RIFSEEP jamais défini** dans le corpus alors que les agents le demandent — symptomatique : 23 % des feedbacks négatifs = `missing_document`. Pas de processus systématique « questions sans réponse → backlog d'ingestion ».
- Seules 279/5 962 sections (4,7 %) portent des `references_juridiques` exploitables par l'injection de réfs.
- `rag_chunks_test` (activée par défaut en prod, censée améliorer le recall multi-granularité) **n'existe pas dans le seed local** : l'environnement local ne reproduit pas la prod.
- Le goldset d'évaluation (`goldset_questions_v2`) existe en schéma mais est **vide** : aucune base de mesure.

### 2.4 Pipeline aval (selector, contexte, génération)

- Le **selector LLM est le seul garde-fou anti-hallucination** effectif (cf. C2). Il fait globalement son travail (fallback parsing : 20/1 133 runs) mais on lui fait porter la décision no-answer sans signal de score fiable. Feedbacks `selector_*` : 4 cas seulement — le selector n'est pas le problème dominant.
- Côté génération : 14 feedbacks `generator_*` (interprétation erronée, incomplétude, 3 hallucinations). Motif utilisateur n°1 « Incomplet » : cohérent avec un contexte construit sur des sections mal classées.
- **Écart config documentée / config réelle** : `create_pipeline()` sans argument utilise les défauts de la lib (top_k=15, selector OFF, STANDARD) tandis que la prod mappe `rag_config` (top_k=20, selector ON, WIDE) dans la page Streamlit. Tout script d'éval « naïf » mesure donc un autre système que la prod.
- Latence query processing observée jusqu'à 5,3 s en local (documentation : 200–500 ms) — à confirmer en prod via `chat_runs`.

### 2.5 Observabilité

Le logging `chat_runs` est riche (140+ colonnes) mais ne donne pas encore une vision production consolidée : usage, volumes de requêtes, latences P50/P95/P99, pannes provider, no-answer, rerank, traces et alerting.

La trajectoire détaillée est traitée dans le document dédié : [Observabilité RAG & Dashboards Grafana](./RAG_OBSERVABILITY_ROADMAP_2026-06.md). Points à garder dans cette roadmap qualité :
- l'échec du rerank n'est pas loggé comme tel (aucune colonne, aucun alerting) — une panne totale est restée invisible ;
- les dashboards Grafana doivent couvrir l'usage, la santé RAG, les latences, les providers/infra et les feedbacks qualité ;
- les traces doivent relier un `turn_id`/`trace_id` à chaque étape : retrieval, rerank, selector, contexte, génération et appels providers.

---

## 3. Pourquoi les résultats sont ce qu'ils sont (traces)

**Mauvais — « un contractuel peut-il faire des astreintes ? »** (feedback négatif réel, `retrieval_issue`)
1. Retrieval sémantique : les bons chunks MATTE (« 9 - ASTREINTE », instruction ministérielle 2011) sont dans le pool top-20 mais avec un score RRF plat 0,167, indiscernables du bruit (chunks licenciement/démission).
2. Rerank → 422 → ordre RRF conservé : les sections « astreinte » ne remontent pas dans le top-10.
3. Selector ne voit pas les bonnes sections → « je n'ai pas trouvé ».
4. **Avec rerank réparé : réponse correcte sourcée sur l'instruction ministérielle du 6 janvier 2011.**

**Mauvais — « réserviste opérationnel »** (feedback négatif réel) : même mécanique ; la réponse est dans MATTE Fiche 1 (motif d'absence L.332-6). Sémantique seul ne la fait pas remonter assez haut même avec rerank ; **hybride + rerank réparé : réponse correcte**.

**Mauvais — « RIFSEEP »** : retrieval sémantique ne remonte aucun chunk RIFSEEP ; hybride en remonte 4 mais le corpus ne contient pas de *définition* → no-answer légitime côté génération, mais frustrant. Cause racine : couverture documentaire + absence de boucle « question sans réponse → ingestion ».

**Bon — « indemnité de fin de contrat d'un CDD »** : la Fiche 6 MATTE matche sur les deux chemins (chunk + heading), cumule les contributions RRF (≈0,32, seul cas où le score se détache), domine l'agrégation → bonne réponse même sans rerank. Les cas qui marchent aujourd'hui sont ceux où *plusieurs chemins de retrieval convergent* sur le même document.

---

## 4. Facteurs de non-qualité, priorisés

| # | Facteur | Étage | Impact | Effort |
|---|---|---|---|---|
| 1 | Payload rerank obsolète → 422 silencieux | Reranking | Critique | 1 ligne + test |
| 2 | Score RRF plat sans amplitude de pertinence | Scoring | Élevé | Moyen |
| 3 | Mode sémantique seul sur termes exacts/acronymes | Retrieval | Élevé | Faible (config) |
| 4 | Goldset vide, aucune mesure reproductible | Mesure | Élevé (aveugle) | Moyen |
| 5 | Doublons SP (33 %) + chunks-titres (20–33 %) | Chunking | Moyen | Moyen |
| 6 | 45 % chunks RGRH sans embedding | Ingestion | Moyen | Faible |
| 7 | Trous documentaires (RIFSEEP…) sans boucle de rattrapage | Données | Moyen | Process |
| 8 | Pannes provider invisibles (pas d'alerting) | Observabilité | Moyen | Faible |
| 9 | Sections géantes (>20k) et naines (<300) | Structuration | Moyen | Moyen |
| 10 | Écart config locale/prod, `rag_chunks_test` absent du seed | Reproductibilité | Moyen | Faible |

---

## 5. Planification itération 2 (juin → 31 octobre 2026)

### Phase 0 — Quick wins (semaine du 16 juin)

| Chantier | Critère de succès |
|---|---|
| Fix payload `/rerank` (`query`/`documents`) + test unitaire + **alerte si taux d'échec rerank > 5 %** | Rerank actif en prod, vérifié dans les logs |
| Passer `search_mode` prod en **hybride** (config DB, réversible) | Replay des 8 questions de référence : ≥ 6/8 correctes |
| Backfill `embedding_m3` des 146 chunks RGRH | 0 chunk sans embedding sur colonnes actives |
| Logger l'état du rerank par run (`rerank_ok`, listes avant/après) | Colonne exploitable dans `chat_runs` |

*Dépendance : aucune. Risque : l'hybride change l'ordre des résultats → valider sur le jeu de questions avant bascule.*

### Phase 1 — Mesure, observabilité et pilotage production (16 juin → 18 juillet)

- Constituer le **goldset v1 : 80–120 questions** depuis `chat_feedbacks` (mix positifs/négatifs, tous thèmes, difficultés étiquetées) avec réponses et sources attendues.
- Harness d'éval automatisé (recall@k chunks/sections, présence de la bonne source dans le contexte final, no-answer justifié ou non, juge LLM sur la réponse) exécutable en CI et en local — en réutilisant `src/goldset/` et `tests/conformance/`.
- **Baseline chiffrée** avant/après Phase 0 ; tableau de bord hebdo (taux no-answer, helpful rate, échecs provider).
- Observabilité production : voir le document dédié [Observabilité RAG & Dashboards Grafana](./RAG_OBSERVABILITY_ROADMAP_2026-06.md) pour les dashboards Grafana, traces, alertes et métriques infra/RAG.
- Réparer l'analyse IA des feedbacks (crashs `NoneType`).
- Seed local aligné prod (`rag_chunks_test` incluse) ; script unique « run prod-config local ».

*Jalon 18/07 : baseline publiée, éval reproductible en 1 commande. Critère : toute PR retrieval peut être évaluée en < 15 min.*

### Phase 2 — Retrieval, scoring, données (21 juillet → 29 août)

- **Scoring v2** : conserver le score reranker comme signal principal en aval (agrégation pondérée par scores réels, pas par comptes) ; seuil de pertinence pour court-circuiter avant le selector quand tout est sous le seuil.
- Dédup Service-Public (518 doublons) + filtrage des chunks-titres à l'ingestion (ou fusion titre+contenu).
- Étendre la couverture `references_juridiques` (4,7 % → cible 30 % des sections MATTE/SP).
- Boucle **trous documentaires** : extraction mensuelle des no-answer + `missing_document` → backlog d'ingestion priorisé (RIFSEEP en premier).
- Revisiter la place de DGAFP (intent gating trop restrictif ? mesurer sur goldset).

*Jalon 29/08 : +X points de recall@10 sur goldset vs baseline (cible : −50 % de `retrieval_issue` sur le goldset). Dépend de Phase 1 pour être mesurable.*

### Phase 3 — Chunking, contexte, génération (1er septembre → 10 octobre)

- Re-chunking avec frontières sémantiques + overlap, re-découpage des 36 sections > 20k ; arbitrer la multi-granularité (`rag_chunks_test`) sur mesures.
- Budget de contexte : prioriser par score reranker, plafonner les sections envoyées au selector (coût/latence).
- Prompts génération : traiter le motif « Incomplet » (consignes de complétude, citations systématiques) ; A/B sur goldset.
- UX feedback : catégories de feedback alignées sur les étages du pipeline pour boucler le diagnostic automatiquement.

*Jalon 10/10 : helpful rate ≥ 85 % sur 4 semaines glissantes ; no-answer < 10 % avec ≥ 90 % de no-answer justifiés (vérifiés goldset).*

### Phase 4 — Stabilisation et bilan itération 2 (13 octobre → 31 octobre)

- Stabiliser les configurations validées : search mode, scoring, prompts, budgets de contexte, alertes et dashboards.
- Publier un bilan itération 2 : métriques avant/après, décisions prises, chantiers reportés, risques restants.
- Préparer la suite : backlog priorisé novembre/décembre, arbitrages ingestion, Mastra/conformance, dette observabilité.

*Jalon 31/10 : bilan itération 2 partageable, critères de succès vérifiés ou écarts explicités.*

### Risques transverses

- **Dépendance Albert** : le schéma d'API peut encore changer → tests de contrat provider en CI (smoke quotidien embeddings/rerank/LLM).
- **Volume de feedback en baisse** (8 feedbacks en avril) : le helpful rate devient non significatif → relancer la collecte côté UI en Phase 1.
- **Conformance Mastra** : tout changement d'ordre (hybride, scoring v2) casse les tests de conformance sensibles à l'ordre → synchroniser les baselines.

---

## 6. Limites de validation / points à confirmer

- Les scripts de replay et d'audit SQL utilisés pour produire cette note ne sont pas commités dans cette PR ; les résultats doivent donc être traités comme des constats reproductibles localement, mais pas encore comme un harness d'évaluation maintenu.
- Les chiffres DB proviennent d'une copie locale Supabase et de vérifications ponctuelles en lecture seule ; il faut confirmer les écarts sensibles sur le DSN staging/prod qui servira d'arbitrage.
- Le statut des embeddings DGAFP est à confirmer rapidement : si les 3 992 chunks DGAFP sont bien sans `embedding_m3` et sans `embedding_bge_scw` sur l'environnement actif, leur absence des réponses finales est un problème d'indexation/retrieval plus direct que le seul intent gating.
- La bascule globale en mode hybride doit rester réversible et précédée au minimum d'un mini-jeu de replay/goldset archivé. Le fix du payload reranker, lui, peut être traité comme un bug urgent et isolé.

---

## 7. Métriques de suivi proposées

| Métrique | Source | Baseline (à figer en Phase 1) | Cible itération 2 |
|---|---|---|---|
| Recall@10 sections (bonne source dans le top) | Goldset | à mesurer | +20 pts |
| Taux de no-answer sur questions répondables | Goldset | ~19 % global actuel | < 10 % |
| No-answer justifiés (vrai « pas dans le corpus ») | Goldset | — | ≥ 90 % |
| Helpful rate utilisateurs (4 sem. glissantes) | `chat_feedbacks` | 74 % | ≥ 85 % |
| Taux d'échec rerank / embeddings / LLM | logs + alerting | inconnu (non mesuré) | < 1 %, alerté |
| Part `retrieval_issue` dans les feedbacks négatifs | `chat_feedbacks` (analyse IA réparée) | 58 % | < 30 % |
| Latence P50/P95 par étage | `chat_runs` | à figer | pas de régression |
| Observabilité production | Grafana + `chat_runs` + logs/traces Scaleway | dashboards absents | dashboards v1 + alertes rerank/provider |

**Critère d'acceptation général** : aucune bascule de config/scoring en prod sans run goldset complet avant/après, archivé.

---

## 8. Décision demandée pour l'itération 2

1. **Valider le déploiement immédiat des quick wins Phase 0** (fix rerank en premier).
2. Valider le séquencement Phase 1 → 4, jusqu'au 31 octobre 2026, et les cibles chiffrées du §7.
3. Arbitrer la bascule en mode hybride : directe (recommandé, réversible) ou après goldset v1.

---

## Addendum (2026-06-09) — Trou de couverture index : cas SFT généralisé

Approfondissement suite au signalement « SFT : la fiche est indexée mais ne remonte jamais, et la limite d'âge n'est jamais citée ». **Vérifié et reproduit, y compris avec rerank réparé + hybride** : le problème n'est pas un réglage de retrieval mais un trou structurel entre l'ingestion documentaire et l'index de retrieval.

### Mécanique du cas SFT (trace complète)

1. La fiche Service-Public **F32513 « Supplément familial de traitement (SFT) »** existe dans `rag_documents` (16 sections dans `rag_sections`, dont la règle de la limite d'âge : « le versement du SFT cesse […] pour un enfant atteignant l'âge de 20 ans »).
2. Mais elle a **zéro chunk** dans `rag_chunks_service_public` : aucune unité de retrieval, donc invisible à la recherche sémantique, lexicale et hybride.
3. La recherche par titres/headings ne peut pas la rattraper : `_search_table_headings` part de la table de chunks (`FROM rag_chunks_* JOIN rag_sections`) — **un document sans chunk est invisible même quand son titre matche exactement la question**.
4. `rag_chunks_test` (censée apporter le recall multi-granularité, `v3_enable_chunks_test=true` en config) **n'existe pas en base staging** (vérifié en lecture seule) ni en local ; l'erreur « relation does not exist » est avalée par le ThreadPool du retriever (log error puis continue). Statut en prod non vérifié (accès non autorisé pendant cet audit) — à confirmer, mais la config seule ne garantit rien.
5. La table `rag_chunks_mso` (1 262 chunks, dont 18 sur le SFT incluant la limite d'âge via le Vademecum MSO) **n'est pas dans la liste des tables du retriever** (`CHUNK_TABLES` = matte, service_public, dgafp, rgrh, test) : jamais interrogée.
6. Résultat : la seule matière « SFT » atteignable est constituée de mentions incidentes dans d'autres fiches (licenciement F515, temps partiel F18029). La réponse générée définit le SFT à partir de la fiche temps partiel, cite cette fiche en source, et **ne peut pas** mentionner la limite d'âge — l'information n'entre jamais dans le contexte.

### Généralisation (mesuré en local, copie staging)

| Corpus | Documents avec sections mais **zéro chunk** |
|---|---|
| Service-Public | **31 / 53 fiches (58 %)** — dont SFT, CET, congés maladie/maternité/parental, télétravail, RTT, vacataire, prime de précarité, Ircantec, fiche de paie… |
| MATTE | **17 / 44 documents** — majoritairement des circulaires, dont plusieurs ingérées en 2025 |
| MSO | **16 / 16 documents** au niveau du retrieval effectif (table `rag_chunks_mso` jamais interrogée) |

Chronologie explicative : `rag_documents`/`rag_sections` SP ont été peuplés par le nouveau pipeline (janv. 2026, sections jusqu'à mai 2026), mais `rag_chunks_service_public` n'a été (re)généré (23 avr.–1er mai) que pour 24 fiches. **Deux générations d'ingestion coexistent : le retrieval ne lit que l'ancienne ; le corpus récent est du poids mort.**

### Conséquences sur l'audit et la roadmap

- Les replays sur questions cibles (§1, §3) **sous-estiment** le problème : une question dont le document n'a pas de chunks échoue quelle que soit la qualité du retrieval. La part réelle de `missing_document` dans les échecs est probablement bien supérieure aux 23 % mesurés sur les feedbacks — beaucoup d'échecs classés `retrieval_issue` sont en réalité des trous d'index.
- **Nouveau quick win prioritaire (à intégrer en Phase 0/1)** : job de **réconciliation ingestion → index** : pour chaque document `is_indexable`, vérifier ≥ 1 chunk avec embedding non nul dans une table effectivement interrogée par le retriever ; rapport d'écart + backfill ; test CI qui échoue si la couverture régresse. Décider du sort de `rag_chunks_mso` (intégrer au retriever ou retirer du corpus) et de `rag_chunks_test` (créer la table ou désactiver le flag — un flag actif sur une table absente doit être une erreur visible, pas un warning avalé).
- **Métrique à ajouter au §6** : taux de couverture index = % de documents indexables disposant d'unités de retrieval effectives (cible : 100 %, alerte en CI). C'est une mesure indépendante du jeu de questions — elle corrige le biais « questions cibles » du goldset.

---

## Addendum 2 (2026-06-09) — Disparité métriques auto vs évaluation experte

Audit du dispositif d'évaluation suite au constat d'écart entre les métriques automatiques (RAGAS, LLM judges, page Pipeline_Evaluation) et le jugement des experts métier. **Conclusion : les deux dispositifs ne mesurent ni la même chose, ni la même population de questions, ni le même état du système.** La disparité est structurelle, pas un bruit de mesure.

### Pourquoi les métriques auto sont systématiquement plus optimistes

**1. Biais de population — les échecs dominants sont exclus par construction.**
- Le goldset `golden_beta` (`golden_beta_builder.ipynb`, étape 3) **exclut explicitement les questions « Document manquant » et « Hors périmètre »** — précisément le mode d'échec dominant constaté par les experts (cf. Addendum 1 : 58 % des fiches SP sans unité de retrieval).
- La page `09_Pipeline_Evaluation` exclut toute question sans `gold_sources` renseigné → seules les questions dont la source est connue *et indexée* sont mesurées.
- Le goldset synthétique (`goldset_synthetic_generation.ipynb`) génère les Q/A **à partir des documents ingérés** (`rag_documents`) : circulaire par construction, il ne peut pas détecter un trou de couverture.
- L'auto-enrichissement (`src/goldset/auto_enrich.py`) ajoute les questions des feedbacks **sans gold_answer ni gold_sources** → elles retombent dans le filtre d'exclusion ci-dessus.

**2. Conditionnement des métriques — RAGAS note la cohérence, l'expert note la vérité.** `faithfulness` et `answer_relevancy` sont conditionnées au **contexte récupéré**, pas à la vérité terrain : une réponse partielle mais fidèle à un mauvais contexte, ou un « je n'ai pas trouvé », scorent bien. Aucune métrique auto ne mesure la **complétude vs réponse attendue** — alors que « Incomplet » est le motif n°1 des feedbacks négatifs (121+ occurrences). L'expert juge exactitude + complétude par rapport au droit applicable ; la machine juge la cohérence interne d'un contexte potentiellement vide ou hors sujet.

**3. Matching de sources trop rigide.** `_is_hit` (page 09) = égalité stricte `short_id == gold_sources` avec **une seule source gold par question** : retrouver une source alternative également valide (fiche MATTE vs fiche SP vs article CGFP — cas fréquent) compte comme un échec ; inversement, toucher le bon document via un chunk-titre vide compte comme un succès.

**4. Juges non calibrés et hétérogènes.** RAGAS tourne avec `gpt-4o-mini` (+ prompts internes RAGAS en anglais, sur du droit français), les judges Golden Beta avec `gpt-4.1`, l'analyse de feedbacks avec un 3e dispositif (qui crashe : `'NoneType' object has no attribute 'strip'`). **Aucune mesure d'accord juge↔expert n'existe** (pas de kappa, pas de set de calibration) ; le seuil `faithfulness < 0.5` est arbitraire.

**5. Dérive de snapshot — on ne mesure pas le même système.** Les notebooks d'éval pointent vers des DSN retirés (`SCALINGO_POSTGRESQL_URL`, `TUNNEL_DSN`) ; la table `goldset_runs` n'existe plus dans la base courante ; `goldset_questions_v2` est vide. Les métriques auto en circulation ont été calculées sur **un ancien corpus et d'anciennes configs**, tandis que les experts évaluent la prod actuelle (reranker cassé + trous d'index). Aucun run auto n'est reproductible aujourd'hui.

**6. Le canal expert est lui-même faible.** 12 lignes seulement dans `chat_reviews` ; les CSV de revue humaine (HF privé) ne sont pas réinjectés en base ; les critères experts (exactitude, complétude, sources) ne sont pas mappés formellement sur les axes des juges LLM ni sur les motifs de feedback utilisateur.

### Pistes pour un dispositif d'éval fiable

| # | Piste | Effet attendu |
|---|---|---|
| 1 | **Rubrique unique partagée** (exactitude / complétude / sourçage / clarté, échelle commune) utilisée par les experts ET les juges LLM, mappée sur les motifs de feedback UI | Comparabilité directe auto↔expert |
| 2 | **Set de calibration** : ~100 réponses notées par experts ; mesurer l'accord juge↔expert (kappa, accuracy par axe) ; itérer le prompt juge jusqu'à κ ≥ 0,7 ; re-calibrer à chaque changement de modèle juge | La métrique auto devient un proxy validé du jugement expert |
| 3 | **Juger contre la vérité terrain, pas le contexte** : gold_answer + gold_sources multiples (liste, niveau section) comme référence principale ; garder faithfulness uniquement comme garde-fou anti-hallucination | Supprime l'angle mort « fidèle mais faux/incomplet » |
| 4 | **Réintégrer les questions exclues** : missing_document (comportement attendu = no-answer honnête + escalade documentée), hors-gold (étiquetées « réponse attendue : aucune ») ; stratifier par thème/difficulté/type et **ne jamais publier une moyenne globale seule** | Les métriques couvrent enfin le mode d'échec dominant |
| 5 | `gold_sources` en **liste multi-sources** + hit au niveau section ; recall = au moins une source valide | recall/MRR cessent de sous- et sur-estimer |
| 6 | **Re-fonder l'exécution** : goldset_runs recréée dans la base courante, run one-command sur snapshot prod actuel, nightly CI (le script `check_nightly_goldset_readiness.py` existe déjà), versionner modèle juge + hash de prompt avec chaque score | Reproductibilité, fin de la dérive de snapshot |
| 7 | **Boucle experte régulière** : lot hebdomadaire de 10–20 réponses à noter (réutiliser `chat_reviews`, aujourd'hui 12 lignes), alimentant le set de calibration en continu | Le canal expert devient un flux, pas un événement |

Ces pistes s'insèrent dans la **Phase 1 (Mesure)** de la roadmap §5 : les points 1–2–6 conditionnent tout le reste — tant que l'accord juge↔expert n'est pas mesuré, aucune métrique auto ne devrait servir de critère de décision.

## Sources

- Code : `packages/rag-pipeline/src/assistant_rh_rag_pipeline/` (retriever, reranker, section_aggregator, config).
- Données : base locale Supabase (copie staging) — `rag_chunks_*`, `rag_sections`, `rag_documents`, `chat_runs` (3 054, oct. 2025 → juin 2026), `chat_feedbacks` (761), `rag_config`, `goldset_questions_v2`.
- Replays : scripts d'audit exécutés le 2026-06-09 contre l'API Albert réelle (modèles, `/rerank`) et la base locale.
