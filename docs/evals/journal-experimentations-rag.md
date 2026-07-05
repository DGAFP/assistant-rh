# Journal des expérimentations RAG — goldset `baseline_v1` (100 questions)

> **Règle d'équipe : chaque run d'eval lancé = une entrée dans ce journal**, avec
> ce qui a changé depuis le run précédent, les résultats agrégés et la lecture
> qu'on en fait. Sans ça, les chiffres sont ininterprétables deux jours plus tard.
> Les runs vivent dans `rag_quality_eval_runs` (staging) ; le CLI est
> `scripts/run_rag_quality_eval.py` (comparaison de baseline intégrée via
> `--baseline-run-id`).

## Panel et métriques

- **Panel** : questions taguées `baseline_v1` (~100 ; sélection `--any-goldset --tag baseline_v1`).
  Répartition : ~57 manual, 14 Service-Public, 12 MATTE, 11 synthetic, 4 MSO, 2 DGAFP
  (+ goldset `mi_mso_v1` créé le 06/07 : 10 MI + 6 MSO, tag séparé).
- **Métriques** : `judge_pass_rate` (judge qwen3-235b-a22b-instruct-2507, Scaleway),
  `doc_recall`/`hit_rate` (matching d'ids déterministe), RAGAS, et depuis le 06/07
  `retrieval_gap_rate` (part des questions sans aucun gold doc retrouvé).
- **Générateur & sélecteur** : `openweight-large` = `gpt-oss-120b` (Albert), temp 0,
  fenêtre 131k. Non déterministe malgré temp 0 → ne jamais conclure sur un
  single-shot, toujours comparer à périmètre constant.

---

## Run 19 — `baseline_v1_canonical_20260629` (29/06/2026) — LA baseline legacy

- **Corpus** : MATTE/MSO legacy (notebooks one-shot), avant Phase D.
- **Résultats** : judge_pass **0,58** — MATTE 0,67 · SP 0,71 · manual 0,53 · synthetic 0,73 · MSO 0,25 · DGAFP 0,50. doc_recall 0,297.
- **À savoir pour comparer** : les bons scores venaient en partie de l'**injection
  de documents entiers** (fiches MATTE complètes dans le contexte) ; `mso` absent
  des tables de retrieval (déjà) ; goldset avec défauts découverts plus tard
  (golds du régime privé, art. 2-8, etc.).

## Run 45 — `phase-d-rebuild-20260705` (05/07 soir) — post-rebuild, avant fixes

- **Changements** : reconstruction MATTE (49 docs, 2535 chunks) + MSO (16 docs,
  1219 chunks) via pipeline médaillon Grist+OCR (PR #266/#267), migration
  backfill-before-cutover, REINDEX IVFFLAT.
- **Résultats** : judge_pass **0,21** — MATTE 0,42 · SP 0,57 · manual 0,12 · synthetic 0,00.
- **Lecture (autopsie)** : PAS une régression de chunking — hit_rate MATTE stable
  (0,92), faithfulness stable. Trois mécanismes :
  1. **Sélecteur famélique** : 1,7 section servie en moyenne (sur 10 offertes) —
     tolérable avec les gros chunks legacy, fatal avec les sections QNA fines.
  2. **Doc-entier perdu** : les `doc_markdown` OCR sont ~3-5 % plus longs → la
     fiche MATTE 6 (5 144 tk) passait 144 tokens au-dessus du seuil
     `doc_entire_threshold_wide=5000` → section de 334 tk servie au lieu de la
     fiche → « je n'ai pas trouvé » sur du contenu présent.
  3. **Artefacts goldset** : gold_doc_ids legacy morts après rebuild.

## Run 52 — `post_ingestion_refactor_20260705` (nuit 05→06/07) — fixes contexte

- **Changements** (PR #271, mergée avant le run) :
  - `SelectorConfig.min_kept_sections=4` (plancher : complément au rang d'agrégation) ;
  - `doc_entire_threshold_wide` 5000 → 9000 ;
  - goldset : pont legacy→nouveau sur les 16 questions MATTE/MSO d'iteration2_V1.
- **Résultats** : judge_pass **0,26** — **MATTE 0,75 (> baseline 0,67 ✅)** ·
  SP 0,64 · manual 0,12 · synthetic 0,00 · MSO 0,25. Contextes servis : 3,2 en moyenne (vs 1,6).
- **Lecture (audit sur pièces, 45 dossiers + décomposition des caps)** : les 74 fails =
  **35 retrieval** (47 %) + **22 goldset** (30 %) + **9 juge trop sévère** (12 %) +
  **8 vraies fautes de génération** (11 %). L'écart global vs run 19 venait surtout
  du **cap dur `no_expected_source_retrieved`** (pass impossible si hit_rate=0 par
  matching d'ids — 29 cas dont 18 artefacts d'ids) et des golds défectueux.
  Le seul corpus modifié (MATTE) **dépasse sa baseline**.

## Run 54 — `retrieval_probes_topk_20260705` (06/07 matin) — élargissement amont

- **Changements** (PR #272 mergée) : `SET ivfflat.probes=15` (le retriever scannait
  **1 % des listes** d'index depuis toujours — défaut PostgreSQL probes=1) ;
  `initial_top_k` 15→30 (code) et `v3_initial_top_k` 20→30 (runtime staging).
- **⚠️ Run contaminé** : corrections goldset (ponts, golds réécrits) appliquées
  PENDANT le run — ses questions mélangent deux régimes de gold.
- **Résultats** : judge_pass **0,26** (= run 52) — mais retrieval en net progrès :
  hit_avg 0,36→**0,43**, doc_recall 0,241→**0,318**, questions sans gold 64→52.
- **Lecture** : les gains de retrieval ne convertissent pas car (1) l'entonnoir a
  un **étage rigide** : chunks bruts 110→160 mais sections offertes au sélecteur
  plafonnées à 10,0 pile (`section_rerank_top_k=10` / `v3_rerank_top_k`) ;
  (2) l'ancien juge (cap dur) tournait encore ; (3) flips de variance symétriques
  (6 gagnées / 6 perdues, toutes coté juge).

## Changements du 06/07 non encore mesurés par un run complet

- **PR #273 (mergée)** : cap retrieval → **soft** (ne bloque plus le pass, évalué
  sur le score pré-soft) + `retrieval_gap_rate` ; doctrine corpus réglementaire
  dans le prompt judge (jamais de savoir externe, refus correct sur contexte
  insuffisant = `retrieval_gap`, complétude ancrée ≠ contradiction,
  contradiction matérielle = citations exigées) ; **scope ministériel de l'eval**
  (`--ministry-scope`, défaut `per-question` : une question MATTE/MSO/MI/MASA est
  évaluée dans le scope de SON ministère, comme dans l'app — la sonde en scope
  « all » montrait des contaminations inter-ministères, ex. question MSO répondue
  depuis un doc MASA).
- **PR #274 (ouverte)** : `section_rerank_top_k` 10→16 (l'étage rigide) +
  overrides CLI `--selector-model` / `--section-rerank-top-k` pour les A/B
  (la config runtime est partagée, deux runs parallèles doivent surcharger localement).
- **Goldset** : `mi_mso_v1` créé (20 questions Paul : 10 MI + 10 MSO, dont 4 mises
  à jour en place sur iteration2_V1) ; 4 golds corrigés (177 : congé parental
  régime privé→FP ; 205/206 : art. 2-8 projet→art. 45 ; 219 : arithmétique) ;
  q195 (« name ») retirée du panel ; pont legacy étendu à tout `baseline_v1` ;
  références « Décret 86-83, Article N » résolues via `metadata->>'num_norm'`
  (93 docs Légifrance) — restent 7 questions citant des articles **abrogés**
  (recodification 2025) à réviser sur le fond.

## Runs 58 & 59 — A/B sections vs modèle sélecteur (06/07) — INTERROMPUS à 22/100

Les deux partageaient : probes+top_k, juge découplé+doctrine, goldset corrigé,
scope « all » (lancés avant le per-question), baseline de comparaison = run 52.

- **Run 58** `ab_sections20_20260706` : `--section-rerank-top-k 20`, sélecteur gpt-oss.
- **Run 59** `ab_selector_mistral_20260706` : `--selector-model mistral-medium-2508`, 10 sections.
- Sonde qualitative pré-run (6 questions × 2 variantes) : **0 refus / 12**,
  gold MSO dans le contexte (le scope fonctionne).
- **Résultats appariés à l'arrêt (22 questions communes, décision d'interrompre)** :
  - judge_pass identique : **0,59 (A) = 0,59 (B)** (réf. 52 sur ces questions : 0,64)
    → **le modèle du sélecteur n'est pas le levier** ; gpt-oss conservé.
  - Avantage secondaire à A : hit 0,73 vs 0,68, refus 1 vs 3
    → **20 sections retenues**.
  - **MSO 3/3 pass** dans les deux runs — première fois de la campagne
    (plafonné 0,25 depuis le run 19) : le triptyque scope + juge découplé +
    goldset enrichi débloque le corpus reconstruit.
  - **MATTE 0,56 vs 0,75 (run 52, mêmes questions)** dans les deux variantes :
    la contamination inter-ministères du scope « all » coûte cher (le
    Vademecum MSO pollue les réponses MATTE — il n'existait pas dans les
    tables du run 52). Confirme la décision de passer au scope per-question.
- **Décision** : arrêt à 22/100 (les deux hypothèses étaient tranchées), runs
  marqués `aborted` en base, remplacés par le run 60.

## Run 60 — `per_question_scope_20260706` — scope par ministère (en cours)

- **Changements vs 58/59** : `--ministry-scope per-question` (défaut du CLI
  depuis 4411ea1) — chaque question ministérielle évaluée dans le scope de SON
  ministère ({ministère} + service_public + dgafp), comme dans l'app ;
  `--section-rerank-top-k 20` (gagnant de l'A/B) ; sélecteur gpt-oss (idem).
- **Hypothèses** : récupérer les points MATTE perdus à la contamination ;
  conserver MSO à ~1,0 ; c'est la config candidate pour devenir la nouvelle
  baseline de référence.
- **⚠️ Chantier parallèle (06/07, Paul)** : correction en cours des erreurs de
  **mismatch doc_id / short_id** qui faussent plusieurs maillons (résolution
  des golds, matching retrieval, liaisons sections/chunks). Si les fixes
  touchent la base pendant le run 60, celui-ci sera partiellement contaminé
  (même précédent que le run 54) — à re-baseliner après la stabilisation.
- **INTERROMPU (id 61, aborted)** avant résultats : fixes doc_id/short_id en
  cours en base. À relancer après stabilisation — même config, plus la règle
  ci-dessous.
- **Règle de scope ajoutée (décision Paul 06/07)** : les questions `manual`
  (56 du panel, collectées auprès d'agents MATTE) suivent le **parcours
  MATTE** en mode per-question (scope matte + tables partagées), pas le scope
  complet. `synthetic`/`DGAFP`/`Service-Public` restent en scope complet.

## Run de référence — `reference_v1_20260706` (06/07) — EN COURS

**Le premier run avec tout le paquet cohérent**, et le premier à évaluer les
20 questions MI/MSO de Paul. Panel élargi : **115 questions** =
`baseline_v1` (99, dont q195 retirée) ∪ `mi_mso_v1` (16 nouvelles) — sélection
`--tag baseline_v1 --tag mi_mso_v1` (overlap = union). Répartition : manual 56
(→ scope MATTE), Service-Public 14, MATTE 12, synthetic 11, MSO 10, MI 10, DGAFP 2.

- **Config** (tous les changements du 06/07, mergés sur dev) :
  - retrieval : `ivfflat.probes=15`, `initial_top_k=30` (PR #272) ;
  - entonnoir : `--section-rerank-top-k 20` (override CLI ; gagnant de l'A/B 58/59) ;
    `min_kept_sections=4` (PR #271), `doc_entire_threshold_wide=9000` (PR #271) ;
  - sélecteur : gpt-oss-120b (l'A/B a montré que le modèle n'est pas le levier) ;
  - juge : cap retrieval découplé (soft) + doctrine corpus réglementaire (PR #273) ;
  - **scope : `per-question`** — chaque question ministérielle dans le scope de
    SON ministère, `manual`→MATTE (PR #275), pas de contamination inter-ministères ;
  - matching : durci #276 (n° décret/article, alias LEGIARTI↔doc_id,
    `rag_chunks_legifrance` exclu du crédit).
- **Comparaison** : run 52 comme référence sur le sous-ensemble commun (99
  questions baseline_v1) — calculée en SQL car le panel diffère (115 vs 100 ;
  `--baseline-run-id` marquerait « not_comparable »).
- **Substrat vérifié avant lancement** (workflow parallèle 5 axes : merge/tests,
  matching #276, intégrité goldset, câblage scope, config runtime).
- **Hypothèses** : MSO/MI passent enfin (scope + goldset) ; MATTE récupère les
  points perdus à la contamination ; devient la nouvelle baseline de référence.
- **Résultats : à compléter à la fin du run.**

## Backlog priorisé (état au 06/07)

1. Résultats 58/59 → merger #274, choisir le modèle du sélecteur.
2. Recherche hybride BM25+dense par défaut (index GIN déjà en place ; nbsp SP à
   normaliser d'abord — 46 % des chunks SP en contiennent, cf. reprise PR #183).
3. Reprise PR #183 : parser XML SP (Introduction/préambule perdus à l'ingestion)
   + ré-ingestion SP ; généraliser l'injection d'intro pour les docs > seuil doc-entier.
4. Multi-query (2-3 reformulations + fusion RRF).
5. Goldset : 7 questions sur articles abrogés ; régénérer les golds synthetic sur
   le nouveau corpus ; valider la question MI n°9 (formulée par l'assistant, cellule vide).
6. Contextual retrieval (préfixer titre doc + chemin de section avant embedding — re-embed).
