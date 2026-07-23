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
  (57/100 du panel, collectées auprès d'agents MATTE) suivent le **parcours
  MATTE** en mode per-question (scope matte + tables partagées), pas le scope
  complet. `synthetic`/`DGAFP`/`Service-Public` restent en scope complet.

## Runs 67 / 68 / 69 — ANNULÉS : bug de gold_doc_ids (régression #276)

Le run 67 (id 67) puis les relances 68/69 ont révélé une **régression de #276**
qui corrompt les métriques retrieval et **biaise le juge** :

- **Symptôme** : `hit_rate=0` généralisé sur MATTE/MSO/MI alors que le gold est
  **servi dans le contexte** (recompute avec la colonne → hit=1.0). Le juge
  reçoit `retrieval_diagnostics` (hit_rate, `missing_gold_sources`) → nourri de
  faux « sources manquantes », il pénalise à tort les corpus à gold-UUID.
- **Cause racine** (après une première fausse piste `_column_exists`) :
  `run_eval` **écrasait** `question.gold_doc_ids` (colonne pré-résolue : pont
  par similarité de titres MATTE/MSO + résolution décrets) par
  `resolve_gold_doc_ids(gold_sources, maps)` — qui ne sait PAS mapper les
  F-codes/annexes MATTE vers un doc_id (short_ids ministériels = hex). run 52
  (pré-#276) utilisait la colonne → MATTE hit=1.0 ; runs 67+ re-résolvaient →
  hit=0.
- **Fix (PR #277)** : deux commits — (a) `_column_exists` supprimé (probe sur
  connexion séparée qui dégradait en silence → fail loud sur la connexion
  partagée) ; (b) `merge_gold_doc_ids` = **UNION** colonne curée ∪ résolution
  runtime, au lieu d'écraser. Vérifié en run réel (70) : q3 hit 0.0 → **1.0**.
- **Signaux directionnels avant annulation** (à re-mesurer proprement) : global
  provisoire ~0,65 ; **apparié vs run 52 = +12 pts** ; vs run 19 = +3 (mais
  DÉPRIMÉ par le bug sur MATTE/MSO/MI, donc plancher) ; MI 0,80, manual 0,78,
  MSO 0,50 (2× le run 19), SP 0,55. **SP** : le retrieval SP est réglé (5/6
  échecs ont le doc), les fails sont désormais en aval (1 génération fautive,
  1 sélecteur `all_rejected` → 0 section servie, nuances juge).

## Run 70 — `reference_v1_union_20260706` (run id **70**) — RÉFÉRENCE PROPRE ✅

Identique au run 67 (115 q, per-question + manual→MATTE, 20 sections, gpt-oss,
juge découplé+doctrine) **plus le fix gold_doc_ids (PR #277)**. Premier run où
le juge reçoit des diagnostics retrieval corrects sur tous les corpus.

- **Résultats (115/115)** : judge_pass **0,643** · hit_rate **0,791** (honnête,
  était corrompu à 0 sur MATTE/MSO avant le fix) · doc_recall 0,591.
  Par corpus : MI **0,80** · synthetic 0,73 · SP 0,64 · manual 0,63 · MSO 0,60 ·
  MATTE 0,58 · DGAFP 0,50.
- **Comparaisons appariées (SQL, sous-ensemble commun)** :
  - vs **run 52** (dégradé) : **+34 pts** (0,61 vs 0,27) — spectaculaire mais
    flatté par le fait que run 52 était un mauvais run (cap dur + gpt-oss du soir).
  - vs **run 19** (vraie baseline legacy) : **+4 pts** (0,61 vs 0,57) — LE signal
    honnête. Modeste, avec redistribution : MSO/MI/manual up, **MATTE en retrait**
    (0,58 vs 0,73 : le rebuild Phase D perd l'injection de fiches entières du legacy).
- **Audit des 10 échecs MATTE/SP (workflow 10 agents)** — **7 fixables / 3 plancher** :
  - **SÉLECTEUR affame le générateur (q11, q16, q17)** = levier majeur : q16
    `all_rejected` → 0 section servie ; q11/q17 doc gold récupéré (hit=1) mais
    mauvaises sections servies (heading générique au lieu du passage porteur).
  - **JUGE trop sévère (q15, q23)** : pénalise la complétude vs des détails du
    gold ABSENTS du contexte servi (issus d'une autre fiche non servie) → juger
    la complétude vs le servable, pas vs un gold plus large.
  - **GOLD défectueux (q32)** : omet l'IM 545 déconcentré (la réponse le donne).
  - **RETRIEVAL_MISS (q2)** : fiche 1 L332-22 pas récupérée (cas historique).
  - **Plancher irréductible (q1, q3, q21)** = vraies fautes de génération (refus
    CDIsation obligatoire ; IFC « suspendue » à tort ; 100 % vs 90 % congé
    maladie) — le juge a raison, doctrine réglementaire. Fidélité de génération.
- **Bilan** : MATTE/SP ~0,56 n'est PAS un plafond dur — 70 % des échecs fixables
  (sélecteur surtout), 30 % plancher de fidélité. Fixer sélecteur + juge ≈ +5-7 pts.

### Diagnostic ISO-JUGE — régression MATTE legacy → Phase D (expérience du 06/07)

Question de Paul : le brut MATTE 0,73 (run 19) → 0,58 (run 70) est-il une vraie
régression, ou du sous-jugement historique + dérive du juge ? **Méthode** :
re-juger les réponses FIGÉES de run 19 (legacy) ET de run 70 (Phase D) avec le
MÊME juge actuel + gold corrigé (`scratchpad/rejudge_matte.py`, 2 passes/question
pour lisser le bruit). Ça isole la qualité de génération.

- **Résultat** : run 19 re-jugé **0,750** vs run 70 re-jugé **0,667** → **vraie
  régression de ~8 pts** (pas un artefact). Le « −15 » brut était surestimé (dérive
  juge + bug gold). run 19 était à 0,73 ancien juge → 0,75 nouveau juge : **legacy
  PAS significativement sous-évalué** sur MATTE.
- **Mécanisme (2 questions, même cause)** : q2 (L332-22) — legacy récupérait la
  fiche 1, Phase D ne la récupère plus (retrieval/chunking) ; q11 (proration RTT)
  — legacy servait la **fiche entière** (passage porteur inclus), Phase D sert
  3,5 sections mais pas la bonne (famine sélecteur). **L'injection fiche-entière
  du legacy garantissait le passage-réponse ; le chunking fin le délivre moins
  fiablement.** = exactement le levier priorité 1. Régression réelle mais bornée
  et diagnostiquée ; le correctif sélecteur devrait la refermer.
- À faire : même re-jugement iso-juge sur SP/MSO (SP non rebuild → une baisse y
  serait pipeline, pas corpus).

## Ablation config + candidat v2 — récupération MATTE/SP (06/07)

**Question de Paul** : « à la base on n'avait pas les probes, on était à k30, c'était
plus simple » — mes changements de config Phase D (tunés en partie contre des
métriques CORROMPUES par le bug gold_doc_ids) ont-ils fait régresser MATTE/SP ?

**Diff config run 19 (bon MATTE/SP) → run 70** : 5 deltas, tous introduits par moi :
`initial_top_k` 20→30, `ivfflat_probes` (1)→15, `section_rerank_top_k` 10→20,
`doc_entire_threshold_wide` 5000→9000, `min_kept_sections` off→4.

**Ablation (20 cas, appariés vs run 70 : MATTE 0,56 / SP 0,50)** — un param reverté à la fois :
- reverter `probes` OU `min_kept` → MATTE↑ ET SP↑ (les deux aident les deux corpus).
- reverter `top_k` OU `rerank` → MATTE↑ mais SP indifférent ; reverter les 4 (full simple)
  → MATTE 0,78 mais **SP 0,40** (top_k=20+rerank=10 affament SP).
- **Sweep offline retrieval** : `probes=5` = sweet spot (recall 0,92 comme 15, mais
  Alan 0,25 vs 0,58) ; `probes=1` perd du recall (0,83) ; `probes=15` le plus bruité.

**Scope SP corrigé (question de Paul)** : les questions source=Service-Public/synthetic/
DGAFP tournaient en scope COMPLET (`matte,mso,mi,masa,sp,dgafp`) = pool le plus bruité,
irréaliste. Or 55/68 des questions manual+MATTE ont leur gold dans SP/Légifrance (donc
matte-path suffit) et aucun agent réel n'interroge tous les ministères → **toute source
non ministérielle suit désormais le parcours MATTE** (matte+sp+dgafp).

**Candidat v2** (`probes=5` + `min_kept=0` + scope matte-path pour SP ; top_k=30/rerank=20/
doc_entire=9000 inchangés) — 20 cas appariés vs run 70 :
| Corpus | candidat v2 | run 70 | run 19 (cible) |
|---|---|---|---|
| MATTE | **0,78** | 0,56 | 0,73 ✅ |
| Service-Public | **0,70** | 0,50 | 0,71 ✅ |
| GLOBAL | **0,75** | 0,55 | — |
- Décomposition : fix scope SP → SP +20 ; probes 15→5 → MATTE +22 ; min_kept→0 aide les deux.
- Les 3 changements sont justes **sur le fond** (probes 15 sur-bruité, min_kept force des
  sections = risque d'invention réglementaire, scope complet irréaliste), pas des hacks d'éval.
- **`candidate_v2_20260706` = full run 115 — RÉSULTATS** : judge_pass **0,678**
  (run 70: 0,643). Apparié **+4 vs run 70**, **+9 vs run 19** (99 communes: 0,68 vs 0,59).
  Par corpus : MI 0,90 · **SP 0,79** (run 19 0,71, dépassé) · synthetic 0,73 ·
  **MATTE 0,75** (run 19 0,73, récupéré) · manual 0,63 · **MSO 0,40 (régression)** · DGAFP 1,00.
  **Objectif MATTE/SP atteint.** Seule ombre : MSO 0,60→0,40 (le plancher min_kept
  l'aidait). Accepté temporairement — fix propre = **override config PAR MINISTÈRE**
  (MSO garde le plancher, les autres non), au backlog.
- **Défauts de config committés** : `ivfflat_probes` 15→5, `min_kept_sections` 4→0
  (config.py, code-only, non mappés runtime). Scope SP→matte-path = eval-only
  (l'app scope par groupe). Ces 3 changements sont justes sur le fond, pas des hacks.

## Backlog priorisé (état au 06/07)

0. **[PRIORITÉ 1] Famine du sélecteur** (audit run 70 : 3/10 échecs MATTE/SP) —
   deux sous-fixes : (a) plancher `all_rejected` : servir un top-N minimum quand
   le pool contient un hit (q16 : 0 section servie → refus forcé) ; (b) garantir
   que la section PORTEUSE d'un doc `hit=1` survive à la sélection (q11/q17 :
   heading générique servi au lieu du passage-réponse). Généralise au-delà de MATTE/SP.
0bis. **[PRIORITÉ 2] Calibration juge : complétude vs servable** (q15, q23) — ne
   pas pénaliser des détails du gold absents du contexte servi (les attribuer au
   retrieval/sélecteur, pas à la génération).
0ter. **Nettoyer la colonne `gold_doc_ids`** : le pont a APPENDU les UUID aux
   labels bruts → double-comptage qui sous-estime `doc_recall` (pas `hit_rate`).
0quater. **Fidélité de génération** (plancher : q1/q3/q21) — vraies fautes
   réglementaires malgré contexte correct ; prompt/vérification/modèle.
1. Résultats 58/59 → merger #274, choisir le modèle du sélecteur.
2. Recherche hybride BM25+dense par défaut (index GIN déjà en place ; nbsp SP à
   normaliser d'abord — 46 % des chunks SP en contiennent, cf. reprise PR #183).
3. Reprise PR #183 : parser XML SP (Introduction/préambule perdus à l'ingestion)
   + ré-ingestion SP ; généraliser l'injection d'intro pour les docs > seuil doc-entier.
4. Multi-query (2-3 reformulations + fusion RRF).
5. Goldset : 7 questions sur articles abrogés ; régénérer les golds synthetic sur
   le nouveau corpus ; valider la question MI n°9 (formulée par l'assistant, cellule vide).
6. Contextual retrieval (préfixer titre doc + chemin de section avant embedding — re-embed).

---

## Runs 112 (qwen3) + 113 (Claude) — `baseline_v1_*_20260716` (16/07) — protocole 2 runs : dérive + bascule de juge

**Objet** : établir la référence propre du Jalon 3 sur le vrai goldset (tag `baseline_v1`, **99 Q réelles résolues** : manual 56, Service-Public 14, MATTE 12, synthetic 11, MSO 4, DGAFP 2), et pouvoir se fier au run Claude comme nouvelle baseline en isolant deux effets.

**Protocole — deux runs, MÊME panel (99 Q), MÊME config `rag_config`, `--ministry-scope per-question`, `--skip-ragas`** :
- **Run A — juge Scaleway `qwen3-235b`** (identique à Run 70) : *contrôle de dérive*. Si `hit_rate`/`doc_recall` PAR CORPUS ≈ Run 70, le système (retrieval/génération sur la DB staging courante) n'a pas dérivé depuis le 05-06/07.
- **Run B — juge OpenRouter `anthropic/claude-sonnet-4.5`** (PR #318) : *nouvelle baseline*. À panel + système identiques à A, seul le juge change ⇒ B − A = effet pur de la bascule de juge.

**Changements depuis Run 70** : juge migré qwen3 → Claude (#318) ; corpus MASA re-traité par la re-passe vision (#319 + durcissement #320) — mais **`baseline_v1` sans question MASA**, donc ces runs ne mesurent pas le fix OCR (sonde dédiée `contrat-avenant-schemas`, 5/6). Panel 99 vs 115 au Run 70 (union plus large) → comparaison per-corpus, pas 1:1.

**Réf Run 70** (juge qwen3, n=115) : pass=0.643, hit_rate=0.791, doc_recall=0.591.

**Résultats** (99 Q, `--skip-ragas`, `--ministry-scope per-question`) :

| | Run A #112 (qwen3) | Run B #113 (Claude) |
|---|---|---|
| judge_pass_rate | **0.657** | **0.556** |
| hit_rate_avg | 0.747 | 0.747 |
| doc_recall_avg | 0.598 | 0.598 |
| retrieval_gap_rate | 0.253 | 0.253 |

Métriques déterministes (hit/recall/gap) **identiques** entre A et B (même panel + même système, seul le juge change) ⇒ l'écart de pass est l'effet PUR du juge.

`judge_pass` par corpus (qwen3 → Claude) :
| source | n | qwen3 | Claude | hit |
|---|---|---|---|---|
| manual | 56 | 0.57 | 0.48 | 0.63 |
| Service-Public | 14 | 0.79 | 0.71 | 1.00 |
| MATTE | 12 | 0.75 | 0.58 | 0.92 |
| synthetic | 11 | 0.64 | 0.73 | 0.91 |
| MSO | 4 | 1.00 | 0.75 | 0.75 |
| DGAFP | 2 | 1.00 | 0.00 | 0.50 |

Échecs Claude (44/99) par catégorie : **incomplete 18, retrieval_gap 16**, wrong_law 5, quality_gate_failed 3, gold_answer_alignment 1, refusal 1.

**Lecture** :
1. **Dérive (A #112 vs Run 70, même juge qwen3)** : `doc_recall` 0.598 ≈ 0.591, `pass` 0.657 ≈ 0.643 → **aucune dérive** ; le système sur la DB staging est stable depuis le 05-06/07. Le run Claude est donc une baseline fiable. (L'écart de `hit_rate` global 0.747 vs 0.791 vient de la composition du panel : 99 vs 115 questions, non d'une régression.)
2. **Bascule de juge (B − A = −0.101)** : **Claude ~10 pts plus sévère** que qwen3 à réponses identiques (temp 0). Plus marqué sur MATTE (−0.17) et MSO/DGAFP (petits n, bruit) ; Claude est même un peu plus indulgent sur synthetic (+0.09).
3. **Baseline J3 (Claude) = 0.556**. Deux leviers dominent les échecs : **génération incomplète (18)** et **trou de retrieval (16)** — le corpus `manual` (56 Q, hit=0.63) porte l'essentiel des trous. Cohérent avec le constat #67 (le générateur refuse/incomplet malgré un contexte parfois correct). MASA non couvert par ce goldset (sonde `contrat-avenant-schemas` 5/6 en parallèle).

**Caveats** : juge single-shot (variance ±1 dimension par question) ; DGAFP n=2 et MSO n=4 non significatifs ; RAGAS sauté (judge + déterministe seulement).

---

## Run 115 — `baseline_v1_claude_minkept4_20260716` (16/07) — A/B P1 : largeur de contexte (min_kept_sections 0→4)

**Hypothèse** (deep-dive J3, scratchpad `j3-diagnostic-retrieval-granularite`) : le levier dominant est le **retrieval de granularité** — le bon DOC est retrouvé (hit=1.0) mais la SECTION-réponse n'est pas servie (selector élague à 1-2 contextes, min_kept=0). Servir ≥4 sections devrait inclure la section-réponse borderline (cf. #67 : schéma rang 19, frontière rerank_top_k=20).

**Setup** : panel `baseline_v1` (99 Q), juge Claude, `--ministry-scope per-question`, `--skip-ragas`, **seul changement vs #113** : `min_kept_sections` 0 → 4.

**Résultats** (vs baseline #113) :
| | #113 (min_kept=0) | #115 (min_kept=4) |
|---|---|---|
| judge_pass_rate | 0.556 | **0.596** (+0.040) |
| échecs retrieval_gap | 16 | **12** (−4) |
| échecs incomplete | 18 | 17 |
| hit / recall / gap | 0.747 / 0.598 / 0.253 | **identiques** (déterministe) |

Flips : 8 gagnées (fail→pass), 4 perdues (pass→fail) → net +4 Q.

Par corpus : **manual 0.48→0.57 (+0.09)**, **MATTE 0.58→0.67 (+0.09)**, **Service-Public 0.71→0.57 (−0.14)** ; MSO/synthetic/DGAFP inchangés.

**Lecture** :
1. Le diagnostic est **confirmé** : plus de largeur de contexte réduit les `retrieval_gap` (16→12) et remonte les corpus **sous-servis** (manual, MATTE) — la section-réponse borderline est incluse.
2. Mais **Service-Public régresse (−0.14)** : déjà bien servi (hit=1.0), forcer 4 sections y ajoute du bruit → détails non étayés / mauvaise section. **Réplique la tension du 06/07** (min_kept 4→0 réduit pour ça).
3. Un `min_kept` **uniforme est sous-optimal** → **plancher PAR CORPUS/ministère** (élevé manual/MATTE/ministères, 0 pour SP). Valide le levier « min_kept par ministère » du plan.

**Caveats** : +0.040 modeste (proche variance juge single-shot ±0.02) ; le signal PAR CORPUS (SP −0.14 vs manual/MATTE +0.09) est plus robuste que le global.

**Prochain** : soit min_kept par corpus (capture les gains sans la régression SP), soit le levier reranker (remonter la section-réponse au lieu de forcer la largeur — plus principiel).

---

## Run 116 — `baseline_v1_qwen37max_20260716` (16/07) — bascule de juge Claude → Qwen 3.7 Max + baseline J3 propre

**Contexte** : spot-check de 99 réponses (run #113) re-jugées par 4 modèles OpenRouter. `claude-sonnet-4.5` (juge en place depuis #318) s'est révélé **over-strict** : il recale des réponses correctes en `incomplete`/`retrieval_gap`/`gold_answer_alignment`, parfois **en contredisant sa propre rationale** (#200, #204, #225). `glm-5.2` rend des verdicts **incohérents** (quality_gate_failed à rationale vide) → inutilisable. `muse-spark-1.1` géo-bloqué US (403). **`qwen/qwen3.7-max`** : verdicts cohérents avec la rationale, corrige les faux négatifs de Claude (15 corrections / 1 durcissement), **~71 % moins cher** ($4.42 vs $15 /M out). Adopté comme juge par défaut (PR #324) sous l'hypothèse, invalidée le 21/07 par #329/#331, que `data_collection: deny` garantissait la ZDR.

**Résultats** (baseline_v1, 99 Q, juge Qwen 3.7 Max, `per-question`, `--skip-ragas`) :
| | Run #113 (Claude, biaisé) | **Run #116 (Qwen)** |
|---|---|---|
| judge_pass_rate | 0.556 | **0.670** (+0.114) |
| hit / recall / gap | 0.747 / 0.598 / 0.253 | identiques (déterministe) |

Taxonomie des échecs (32/99 sous Qwen) : **`retrieval_gap` 16** (INCHANGÉ vs Claude), `wrong_law` 7, `incomplete` **6** (vs 18 chez Claude), `quality_gate_failed` 3.

Par corpus (Qwen) : MATTE 0.92, synthetic 0.82, Service-Public 0.64, **manual 0.61** (56 Q, le plus faible), MSO 0.50 (n=4), DGAFP 0.00 (n=2).

**Lecture** :
1. **Claude sous-estimait la qualité de 11 points** (0.556 → 0.670). Les 12 « incomplete » en trop étaient du **bruit de juge** (Claude recalait des réponses correctes) — confirmé par les métriques déterministes identiques.
2. Sous un juge fiable, **le seul bloc d'échec dominant est le `retrieval_gap` (16)** — les deux juges sont d'accord dessus = c'est le VRAI problème. Cela **valide et resserre** le diagnostic J3 (retrieval de granularité : le bon doc trouvé, la section-réponse pas servie).
3. **Nouvelle baseline J3 = 0.670** (juge Qwen). Tous les A/B suivants s'y comparent. L'A/B min_kept=4 (#115, +0.040) avait été mesuré sous Claude → à re-juger sous Qwen (re-jugement des réponses stockées, gratuit).

**Caveat** : Qwen conserve 1 cas trop strict (#19). Grok 4.5 ($6/M) est un backup viable (comportement proche, ZDR). GLM 5.2 rejeté malgré son prix ($2.87) pour incohérence des verdicts.

---

## Run 118 — `rebaseline_v2_goldsetfix_qwen37max_20260717` (17/07) — rebaseline post-curation goldset + découverte du σ single-shot

**Changements vs run 116** (pipeline et config STRICTEMENT identiques — seuls le goldset et la comptabilité changent) — curation en 3 volets, **36 questions** :
1. **5 sources fausses re-résolues** (gold answer non dérivable du doc pointé, vérifié à la main) : q198→CGFP L215-1 (au lieu de R214-1 hygiène/sécurité) ; q210→+fiche SP F537 (durées adoption) ; q216→fiche SP F34670 ; q220/q223→+Décret 84-972 art. 3.
2. **82 refs LEGIARTI de version re-keyées → cids chroniques sur 28 questions** (q1, q2, q173-194, q199, q200, q229, q660) — dette de la migration d'identité #289 ; résolution via `config/legifrance_article_cids.json`, 0 échec.
3. **3 questions d'articles 86-83 ABROGÉS 2025 requalifiées CGFP** (codification — vérif PISTE : 92 VIGUEUR + 37 ABROGE, exclusion par design) : q201→R331-7+R331-6 ; q226→L121-6+L121-7 ; q227→R331-2.

Re-juge offline des 2 items 429 du run 116 : **q228 = PASS** (pur artefact rate-limit), **q660 = FAIL réel** (`incomplete`).

**Résultats** (99 Q, juge Qwen, `per-question`, `--skip-ragas`) :
| | Run 116 | **Run 118** |
|---|---|---|
| judge_pass_rate | 0.670 | **0.646** |
| retrieval_gap_rate | ~0.30 | **0.232** |
| hit SP / MATTE | — | **1.00 / 0.92** |

Flips vs 116 : 8 gagnées (q28, 194, 197, 221, 223, 226, 228, 926), 9 perdues (q3, 4, 18, 20, 33, 199, 211, 215, 676).

**Lecture** :
1. **La curation a marché** : hit=1.0 sur les questions curées (q198/201/210/220/223/226/227/660), +5 conversions attribuables (q194/221/223/226/228). Les curées encore en échec ont basculé d'« ingagnables par construction » à échecs standard de pipeline (cibles des vagues suivantes).
2. **Le 0.646 n'est PAS une régression — c'est le bruit** : les 9 perdues ont hit=1.0 dans les DEUX runs à config identique (même retrieval, même contexte servi) ; seule la génération (gpt-oss non déterministe à temp 0) + le juge single-shot ont flippé. Avec les 3 gagnées par le même hasard : **~12 flips aléatoires = σ ≈ ±0.05-0.06 par run**.
3. **Conséquence méthodologique majeure** : un run unique ne peut PAS mesurer un effet +0.02-0.05. Éclaire rétroactivement la « régression min_kept » du 06/07 et le « SP −0.14 » du run 115 (bruit). **Protocole désormais requis** : référence = verdict majoritaire à 3 runs par question ; A/B en diff apparié par question + re-run des flips ; `--dedupe-scope config-and-git` pour les changements code-only ; goldset gelé pendant tout run.

**Caveats** : curation appliquée en DB staging uniquement (à versionner au repo) ; les backups fichiers de pré-curation ont été perdus avec le scratchpad de session (les anciennes valeurs restent documentées ci-dessus et dans `revue-strategies-qualite-rag.md`).

**Prochain** : 2 runs de stabilisation (référence majoritaire-à-3), puis vague 1 (`v3_rerank_input_k=40` + fix intent gating). Voir la revue stratégique complète : `docs/evals/revue-strategies-qualite-rag.md`.

---

## Runs 123-124 — stabilisation vague 0 : référence majoritaire-à-3 Qwen + σ mesuré (21/07, historique)

**Protocole historique** : 2 runs supplémentaires à config STRICTEMENT identique au run 118 (goldset curé gelé, juge Qwen alors considéré ZDR — hypothèse invalidée le 21/07), lancés sur **GitHub Actions** (le réseau du poste local bloque les ports DB et a tué les runs 119-121 — orphelins `running` à nettoyer). Workflow enrichi (`any_goldset`, `run_label_suffix`, concurrency par suffixe — branche #327). Incident : 32 (run 124) + 22 (run 123) appels juge en erreur `401 Provider returned error` → **re-jugés offline 54/54** depuis la VM pont (verdicts flaggés `rejudged_offline` en base).

**Résultats bruts (3 runs, config identique)** : run 118 = 0.646, run 123 = 0.667, run 124 = 0.697.

**RÉFÉRENCE MAJORITAIRE HISTORIQUE QWEN (verdict 2/3 par question) = 67/99 = 0.677.** Elle est remplacée comme référence officielle par le re-jugement Scaleway maj-3 du 22/07 ci-dessous.

| Mesure | Valeur |
|---|---|
| Questions instables (≥1 flip sur 3 runs) | **18/99 (18 %)** |
| Flips par paire de runs | 11-13 (±0.11-0.13 de churn par question) |
| Par corpus (réf. majoritaire) | manual 0.64, SP 0.71, MATTE 0.75, synthetic 0.82 |
| **Échecs stables historiques sous Qwen (3/3 fail)** | **24** : q3, 11, 16, 17, 23, 30, 181, 183, 191, 192, 198, 201, 205, 210, 212, 213, 216, 217, 220, 227, 229, 676, 4531, 4535 |

**Lecture** :
1. Le churn par question (11-13 flips/paire) est **plus grand encore** que le σ agrégé (±0.05) — les flips se compensent partiellement dans le global. Enseignement durable : aucun A/B ne doit être lu autrement qu'en verdicts appariés contre une référence construite avec le même juge. Les 24 échecs ci-dessus restent la photographie historique Qwen, pas les cibles officielles actuelles.
2. Sous cette référence historique Qwen, les cibles des leviers étaient q17/q192 (rerank_input_k=40), q220/q229/q191 (renvois), q212/q213/q217/q229/q30 (R2), q4535 (intent gating). Le re-jugement officiel Scaleway maj-3 ci-dessous reclasse notamment q220/q229 comme instables.
3. Instables notables : q19/q28/q926 (complétude borderline, déjà repérées), q223/q226 (curées, désormais à cheval), q660 (FFP).

**Prochain au 21/07 (historique)** : vague 1 (P1 `rerank_input_k=40` + P2 intent gating derrière flag, PR #327 à merger d'abord) mesurée en apparié. Le protocole et les cibles ont été remplacés le 22/07 par la référence officielle ci-dessous.

---

## Sélection du juge par la mesure + référence officielle souveraine (21-22/07)

**Contexte** : l'exigence ZDR stricte (revue #329/#331 : `data_collection: deny` ≠ Zero Data Retention chez OpenRouter) a révélé que **qwen3.7-max, juge depuis le 16/07, n'a aucun endpoint ZDR**. Qualification systématique de 5 juges par leur **bruit propre** (les 99 réponses FIGÉES du run 118 re-jugées 2x — tout flip = bruit du juge, le générateur étant hors de cause) :

| Juge | Bruit propre | Accord qwen | Sévérité | Conformité |
|---|---|---|---|---|
| qwen3.7-max (OpenRouter) | 2,0 % | — | 0,65 | ❌ deny-only, pas de ZDR |
| **scaleway qwen3-235b** | **5,1 %** | **86,9 %** | 0,60 | ✅ **souverain EU** |
| grok-4.5 | 6,1 % | 78,8 % | 0,68 | ✅ ZDR xAI |
| gemini-2.5-pro | 7,1 % | 73,7 % | 0,59 | ✅ ZDR |
| gpt-5.2 | 9,1 % | 68,7 % | **0,35 (sur-strict)** | ✅ ZDR |

**Décision (utilisateur, 22/07) — protocole à DEUX étages** :
- **OFFICIEL (gates d'adoption staging/prod uniquement)** : juge souverain **Scaleway qwen3-235b en vote majoritaire à 3 appels** (`--judge-votes 3`, PR #333) — bruit effectif ~0,8 %, posture DINUM la plus défendable, sortie de la dépendance OpenRouter pour les décisions.
- **SCREENING intermédiaire** : grok-4.5 single-shot (coût minimal ; sa référence des 3 runs existe déjà en cache).

**RÉFÉRENCE OFFICIELLE** (runs 118/123/124 re-jugés offline en Scaleway maj-3, 891 votes, verdicts qwen préservés en base) :

| Mesure | Valeur |
|---|---|
| **Référence majoritaire officielle** | **67/99 = 0,677** (continuité parfaite avec la référence qwen : même chiffre) |
| Par run (maj-3) | 0,616 / 0,646 / 0,667 |
| Votes partagés (bruit juge résiduel) | **10/297 (3,4 %)** — le maj-3 écrase le bruit juge |
| Questions instables inter-runs | 22 (= variance du GÉNÉRATEUR, désormais isolée du bruit juge) |
| **Échecs STABLES = cibles officielles des A/B** | **26** : q3, 11, 16, 17, 23, 30, 174, 181, 183, 191, 192, 198, 203, 205, 210, 211, 213, 215, 216, 217, 221, 222, 223, 676, 4531, 4535 |

**Cibles des leviers validées sous la référence officielle** : P1 `rerank_input_k=40` → **q17, q192** (stables ✓) ; R2 résumés-articles → **q213, q217, q221, q30** stables (+ q212/q229 instables = bonus) ; intent gating → **q4535** ✓ ; renvois → **q191** ✓ (q220/q229 instables).

**Incidents d'infra consignés** (journée du 21-22/07, tous de la famille « réseau silencieux ») : sockets à moitié mortes gelant les workers (fix : timeout HTTP client 45 s obligatoire sur tout script de juge — à porter dans `judge_answer`), pièges pgrep auto-matchants (3 occurrences), venv nu des worktrees. Runs locaux 119-121 = statuts orphelins à nettoyer.

**Prochain** : vague 1 — P1 `rerank_input_k=40` (prérequis plomberie de R2 : un article remonté par R2 au rang ~25 doit franchir l'entrée du rerank, coupée à 20), screening grok, puis R2 (#332), puis gate d'adoption Scaleway maj-3 sur le paquet.

---

## Run 145 — `candidate_w1_rerankinput40_20260722` (22/07) — screening vague 1 : P1 `rerank_input_k=40`

**Changements vs baseline** : dev @10ee6e3 (#335 mergée) + `--rerank-input-k 40` (entrée du reranker 20→40, sortie inchangée). Juge : **grok single-shot (étage screening)** — lecture contre la **référence grok** des runs 118/123/124 (0,707 ; 16 échecs stables-grok), jamais contre la référence officielle Scaleway (juges non comparables).

**Résultats** :
| Lecture | Valeur |
|---|---|
| Global (ininterprétable en single-shot, σ-grok 6,1 %) | 0,677 vs réf 0,707 |
| Flips sur questions STABLES-grok | +3 (q175, q223, q657) / −6 (q6, q16, q200, q204, q212, q4534) — **dans la bande de bruit grok (~5 attendus)** |
| **Cibles nommées P1** | **q17 : FAIL→PASS ✓** ; **q218 : FAIL→PASS ✓** (bonus — le contrefactuel le donnait non réparé) ; q192 : déjà PASS-stable sous grok (c'est sous le juge officiel qu'elle est une cible) |

**Lecture** : le mécanisme fait ce que le funnel prédisait — les sections-réponse aux pré-rangs 21-40 atteignent désormais le reranker et convertissent (q17, q218). Le net −3 sur les stables est indistinguable du bruit du juge de screening : c'est PRÉCISÉMENT pourquoi l'adoption passe par le gate Scaleway maj-3, seul étage capable de trancher.

**Prochain** : P1 qualifié pour le **gate d'adoption** — un seul gate Scaleway maj-3 pour le paquet P1+R2 dès que #332 est mergée et le corpus R2 appliqué (économie de runs).
