# Revue — deepseek-v4-flash comme générateur (et sélecteur ?) — 20/08/2026

Note de travail : hypothèses, tests effectués et résultats de la journée.
Runs consignés au fil de l'eau dans `journal-experimentations-rag.md`
(entrée `candidate_generator_dsv4flash_20260820`). Environnement : premier
usage du banc local (`docs/LOCAL_DEV.md`) + staging pour la validation.

## Contexte

`deepseek-v4-flash` est apparu au catalogue de l'API Albert (gratuite pour le
projet). Question posée : peut-il remplacer `openweight-large` (gpt-oss-120b)
comme générateur — et, question ouverte en fin de journée, comme sélecteur ?

## Hypothèses et verdicts

| # | Hypothèse | Verdict | Preuve |
|---|---|---|---|
| H1 | deepseek-v4-flash ≥ openweight-large comme générateur, à périmètre constant | **Soutenue** (non significative seule, convergente sur 2 étages) | Runs local 217 et staging #215 vs baseline #206 |
| H2 | Les réponses deepseek sont qualitativement meilleures (utilisables), pas seulement mieux notées | **Soutenue** (revue manuelle des 29 flips) | Revue humaine ci-dessous |
| H3 | deepseek a deux défauts de caractère : abstention (refus de conclure a contrario) et hallucination de chiffres | **Moitié confirmée, moitié RÉTRACTÉE (21/08)** : abstentions réelles (q3/q4/q227) ; « hallucination » q13 **requalifiée** — le barème est verbatim dans le corpus ET conforme à la fiche SP en ligne du 08/08/2026 : réforme 2026 du barème, c'est la gold answer qui est périmée. deepseek était fidèle. | Contexts q13 (runs #215/#217), fiche F31094 live ; run #206 = faux positif (corpus pré-ré-ingestion) |
| H4 | Le judge_pass « habituel » (0,67–0,74) n'est pas comparable : il dépend du juge | **Confirmée** | Juge mistral-medium-3.5-128b plus sévère : baseline #206 = 0,602 (vs qwen3/grok 0,67–0,74 sur mêmes périmètres) |
| H5 | Le sélecteur est un poste de pertes significatif et pourrait bénéficier de deepseek | **Confirmée sur le mécanisme** : golds jetés par le sélecteur divisés par 2 (6→3), sélection plus large (3,6 vs 2,1 docs) ; gain global modeste (+1, bruit) | Run local 218 vs 217, funnel ci-dessous |

## Tests effectués (chronologie)

1. **Banc local monté** : Postgres pgvector (docker, :55432) seedé d'un dump
   staging ; validation de parité d'embeddings par table (7 tables corpus).
   Piège rencontré et documenté : le premier dump a capturé
   `rag_chunks_service_public` en plein re-embedding staging (256/4991
   embeddings) → run local 216 **invalide** (hit_rate SP effondré, d'abord
   attribué à tort au modèle). Resync table par table → parité OK.
2. **Run local 217** `local_dsv4flash_judgemistral_20260820b` — générateur
   deepseek (rag_config locale), juge mistral 1 vote, sans RAGAS, 98 q :
   **0,663**. Apparié au staging #206 (même juge) : 17 gains / 11 pertes.
3. **Flag `--generator-model`** ajouté à `src/goldset/eval.py` (commit
   `ffb05d1`) : override au run, config partagée intacte — prérequis pour
   tester un générateur sur staging sans muter `rag_config`.
4. **Run staging #215** `candidate_generator_dsv4flash_20260820` — protocole
   complet (juge souverain mistral **maj-3**, panel `baseline_v1` 98 q, config
   live staging, seule surcharge = générateur) vs baseline #206 :
   **0,6531 vs 0,6020 (+5,1 pts)** ; apparié 17 gains / 12 pertes (net +5) ;
   doc_recall et gap plats (retrieval inchangé par construction). Coût juge
   3,22 €.
5. **Revue manuelle des 29 flips** (les deux réponses + gold lues
   intégralement) — synthèse :
   - Gains de fond (~8/17) : réponses résolues et actionnables là où
     openweight cite des références sans les déplier (q192 PACTE, q4534,
     q33), meilleure complétude (q20, q17, q177), meilleure discipline de
     périmètre (q206, et en miroir les hors-sujet d'openweight q4538).
   - Pertes de fond (~5/12) : **abstentions a contrario** (q3, q4, q227 —
     contenu correct exposé mais refus de conclure « non ») ;
     **hallucination de barèmes** q13 (tableau faux affirmé avec aplomb) ;
     imprécisions q19/q4528/q4530.
   - Le reste : variance juge single/maj-3 et sélecteur non déterministe
     (contextes différents entre runs à config identique : q2, q29).
6. **Funnel des échecs par étage** (instrumentation `stages` des items) —
   run #215 : 34 échecs = 7 hors pool / 5 coupés rerank-agrégation /
   **6 jetés par le sélecteur** / 16 gold servi mais réponse recalée.
   Baseline #206 quasi identique (9/8/7/15) : le sélecteur actuel
   (openweight-large) jette le gold dans ~18 % des échecs, quel que soit le
   générateur. Trois « pertes deepseek » de la revue (q188, q4528, q4530)
   sont en réalité des golds jetés par le sélecteur.
7. **Run local 218** `local_selector_dsv4flash_20260820` — A/B une seule
   variable : `--selector-model deepseek-v4-flash` (générateur deepseek
   constant), vs run local 217. Résultats :
   - judge_pass **0,6735 vs 0,6633** (+1 question) ; apparié très stable :
     seulement 5 flips (3 gains : q186, q189, q190 / 2 pertes : q16, q221),
     contre 29 flips sur l'A/B générateur — le swap sélecteur bouge peu le
     score global.
   - **Funnel : « selector jette le gold » passe de 6 à 3** (divisé par 2) ;
     hors-pool et coupe-rerank inchangés (7/7, amont identique) ; en aval,
     génération 13 → 15 (le gold mieux servi ne suffit pas toujours).
   - L'hypothèse « prudence excessive → sélection maigre » est **réfutée** :
     deepseek-sélecteur garde PLUS (3,6 docs en moyenne vs 2,1) et mieux
     (kept_hit 0,58 vs 0,54).
   - Cibles : **q186 et q4528 converties** ; q188/q4530 échouent encore
     (catégories aval), q174/q223 restent des cas retrieval.
   - Verdict H5 : **mécanisme confirmé** (moitié moins de golds jetés, sans
     rétrécir la sélection), gain net global modeste (+1, niveau bruit). Le
     sélecteur deepseek est au moins équivalent et corrige la pathologie
     visée ; la confirmation sous juge souverain reste à faire si on veut
     l'adopter.

## Addendum 21/08 — reconfirmation + correctifs de prompt (run #217)

Run staging #217 `candidate_dsv4flash_promptV7_20260821` : config #215 + seul
changement `system_prompt_V7_ancrage.md` (nouvelle ligne additive dans
`system_prompts`, branchée via le nouveau flag `--system-prompt-name` ; la
config runtime reste sur V6). Résultats :

- **Niveau deepseek reconfirmé** : 0,6327 — troisième run souverain de la
  config générateur deepseek (0,6531 / 0,6429 / 0,6327), tous nettement
  au-dessus d'openweight (0,6020). La bande ±2 questions = variance maj-3.
- **V7 non adopté** : net −2 (pertes = flip-floppers connus), a contrario ne
  convertit que q4 (q3/q227 s'abstiennent encore, abstentions totales 4=4).
- **q13 requalifiée** (voir H3) : réforme 2026 du barème de rupture
  conventionnelle → gold périmée, corpus à jour, deepseek fidèle. Ouvre un
  chantier : **audit des golds chiffrées vs corpus SP ré-ingéré le 20/08**.

## Décisions ouvertes

- **Adoption générateur** : signal favorable (staging +5,1 pts sous juge
  souverain, qualité d'usage supérieure en revue manuelle) mais deux
  correctifs de prompt à tester AVANT bascule :
  1. autoriser la conclusion a contrario quand les sources couvrent le sujet
     (« les textes ne prévoient pas X ») — ~3 questions récupérables ;
  2. interdire tout chiffre/barème absent du verbatim des sources (risque
     q13 = confiance utilisateur).
  Le cas échéant : `update_rag_config` staging hors fenêtre de run,
  rollback en 1 UPDATE.
- **Sélecteur** : testé jusqu'au bout. Le run 218 (local) validait le
  mécanisme ; le run staging **#216** (paquet gen+sel, maj-3, vs #215) le
  **réplique** (golds jetés 6→3, sélection 3,9 vs 2,2 docs) mais le score
  ne suit pas : 0,6429 vs 0,6531 (net −1, bruit), les golds sauvés étant
  mangés par l'étage génération (16→19 échecs gold-servi). Local (+1) et
  staging (−1) concordent : **neutre au score**. Proposition : adopter le
  générateur seul, garder le sélecteur openweight, re-tester le sélecteur
  deepseek après les correctifs de prompt (le plafond est redevenu la
  génération).
- Commit `ffb05d1` (flag + banc local + journal) non poussé — push ou PR
  `--base dev` à décider.

## Sources

- `journal-experimentations-rag.md` — entrée du 20/08 (protocoles et
  tableaux complets)
- Runs : staging #206 (baseline), #215 (candidat) ; local 216 (invalide),
  217 (générateur), 218 (sélecteur, en cours)
- Artefacts : `data/eval-local/*20260820*/` ; revue des flips menée sur les
  paires réponses/gold des runs #215/#206
