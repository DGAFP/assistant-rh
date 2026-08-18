# Leçons, incidents et feuille de route

## Mécanismes : validés / infirmés / en attente

**Validés (à conserver) :**

- Séparation perception LLM / politique code — chaque perte des runs #189-191
  est attribuable à une règle nommée avec déclencheur visible dans la trace ;
  les post-mortems prennent une heure de SQL au lieu d'un jour de conjecture.
- Mécanique de preuve v4.1 : matcher verbatim corrigé (tirets typographiques,
  pipes Markdown) + disqualification d'ancrage sur extrait invalide + « pas
  de principal plutôt qu'un principal non étayé » — 24/99 → 0/99 ancrage
  invalide, q17 ancre le bon représentant.
- Rétention du gold par l'appartenance v4 sur les corpus étagés (MATTE 83 %
  vs 58 % en v2 à l'étape « gardées »).
- Mode `dark_metadata` comme outil d'ablation et candidat au déploiement
  shadow.
- Observabilité du funnel par étage/corpus (PR #367) et entrées kept/removed
  joignables aux gold doc_ids.

**Infirmés (ne pas reproduire tels quels) :**

- LLM décideur du texte principal (v3, run #186).
- Portes dures non calibrées branchées sur le générateur : l'instruction
  d'abstention `insuffisante` se déclenche sur ~45 % des questions avec une
  conformité stochastique — dommages dans les deux sens (couplages : −5 nets
  mesurés à juge constant).
- Règle d'applicabilité v4.1 telle qu'écrite : contraint `complementaire`
  alors que le mélange de régimes se joue entre `directe` concurrents
  (q205/q211 : sélection inchangée), et élague des compléments légitimes
  (q15 « quelles alternatives ? », pertes MATTE/SP q222/q660/q676).

**En attente de mesure propre :**

- Arbitrage de régimes au niveau `directe` (design ci-dessous).
- Coupe top-12 (coût mesuré : q174, −4 à −10 pts d'entrée) — lever lié à la
  simplification de la sortie (latence).
- Rebranchement sélectif des couplages (cadrage aide 6 questions, nuit à 11).

## Incidents de protocole (à ne pas répéter)

1. **Juge par défaut ≠ protocole officiel** (run #189) : `eval.py` juge par
   défaut openrouter/grok-4.5 single-shot ; un launcher sans
   `--judge-provider scaleway --judge-votes 3` produit un run non comparable
   (verdict grok +0,0101 vs souverain −0,0404 sur les MÊMES réponses : biais
   de +5 pts). Corrigé dans le template du skill `run-rag-eval` ; le
   rejugement a posteriori des réponses stockées (run #190,
   `rejudge_of_run_id`) est la procédure de rattrapage.
2. **Comparer des agrégats plats** masque des corpus en sens opposés
   (manuel 56 vs MATTE 12) : lecture par corpus obligatoire.
3. **Valider sur les cas qui ont motivé le correctif** (q3/q6 en pré-#187)
   n'est pas une validation : ce sont des données d'entraînement du design.

## Feuille de route recommandée (18/08/2026)

Ordre choisi par levier/coût, décidé après le run #191 :

1. **Curation du goldset avant tout nouveau gate.** Re-annoter le cluster
   instable (q28, q30, q32, q174, q213, q215, q926, q4530) et trancher les
   golds en litige (q3, q229 : abstention vs inférence ; q926 : erreur
   acceptée puis sanctionnée). Marquer ces questions pour double lecture des
   verdicts appariés (avec/sans). Le bruit actuel (±2-3 flips) est du même
   ordre que les effets recherchés.
2. **Merger la branche sur dev en mode shadow** (selector v4.1 en
   `dark_metadata`, aucun impact utilisateur) : clôt la branche longue, met en
   production l'instrumentation, et fait des requêtes réelles un jeu de
   calibration gratuit (distributions de couverture, fréquence des splits de
   régime, taux `insuffisante` sur vraies questions).
3. **Attaquer la falaise pool→top-20 du corpus manuel** (62 % → 42 % de hit :
   le gold retrouvé en chunks ne survit pas au rerank d'agrégation de
   sections), puis le pool lui-même. Leviers au journal : architecture
   3-pipelines, input64 revisité, retrieval conscient de la supersession.
   Progrès directement lisible via les métriques d'étage natives.

   *Exécuté le 18/08 (audit goldset, étapes 1–3 de la curation)* : 16
   questions purgées de leurs UUID fantômes (`gold_doc_ids` re-résolus,
   sauvegarde conservée) ; trou d'ingestion décret 86-83 art. 3/3-1/4
   identifié → issue #369 (q196/q202/q203 étaient des faux « échecs
   retrieval ») ; 8 questions taguées `juge_borderline` + double lecture
   appariée avec/sans dans l'eval (PR #370).
4. **Une itération d'arbitrage de régimes, avec critère d'abandon.** Design :
   plusieurs `directe` de régimes incompatibles ⇒ split explicite (les servir
   comme régimes concurrents identifiés, pas silencieusement côte à côte),
   exemption quand la question demande alternatives/exceptions. Gate dark sur
   panel curé. **Critère d'abandon convenu d'avance** : si la sélection dark
   n'atteint pas le niveau #180, conserver l'appartenance v2 et garder v4 en
   perception pure (métadonnées/abstention/observabilité) — les gains MATTE et
   la mécanique de preuve survivent dans cet hybride.
5. **Rebranchement des couplages un par un** (après 4, chacun son gate) :
   cadrage principal restreint aux directs validés (label « source de mise en
   œuvre »), puis answerability en signal au niveau de l'affirmation (« l'effet
   de X n'est pas documenté ») plutôt qu'interdiction globale de oui/non.
6. **Préalables à toute adoption par défaut** : simplification de la sortie
   structurée (latence 11 s → cible ~2-4 s, qui finance aussi le retour à 20
   candidats), vérificateur post-génération (HALT-RAG, audit de juin), éval
   de sécurité dédiée pour la classe q3/q229.

Item indépendant bon marché : test de conformance du marquage de périmètre
actif (`_is_active_scope_section`) par corpus ministériel — indice q30 d'un
document MSO classé « partagé ».

## État des artefacts (18/08/2026)

- **dev** : v2 selector actif (champion 0,7374) + observabilité funnel #367.
- **Branche `fix/issue-360-selector-legal-coverage-2`** : v4.1 complet +
  `dark_metadata` + merge de dev — candidate au merge shadow (étape 2).
- **Runs staging** : #180 (baseline), #186, #187, #189 (grok, audit), #190
  (rejugement souverain), #191 — tous consignés au journal avec traces
  complètes ; #191 porte les premières métriques d'étage natives.
