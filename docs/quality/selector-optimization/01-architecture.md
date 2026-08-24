# Architecture du selector — v2 → v4.1

Le ContextSelector intervient **après** le rerank de sections : il reçoit les
sections agrégées ordonnées et décide lesquelles servir au générateur. Le
scope de retrieval (un corpus ministériel + tables partagées Service-Public /
Légifrance) est fixé en amont par l'identité de l'utilisateur, jamais par la
question ; le selector n'y touche pas.

## Chronologie des versions

### v2 — prompt seul, redondance vs complémentarité (commit `6a99b83`, PR #365, run #180)

- Une seule sortie plate : `{"selected_ids": [...], "reason": "..."}`.
- Toute la logique éditoriale dans le prompt : hiérarchie ministère >
  Service-Public > DGAFP « en départage uniquement », règle clarifiée
  redondant vs complémentaire.
- Code minimal : parse, dédup, plancher `min_kept_sections`, repli top-5.
- **Résultat : 0,7374 (+6 nets), adopté sur dev. C'est la baseline.**

### v3 — relationnel (commit `e38e233`, run #186, non adopté)

- Le LLM choisissait `primary_id` et des relations (complémentaire /
  redondant / contradictoire) ; labels enrichis (article, éditeur, nature,
  statut, dates).
- **Résultat : −4 nets (0,6970).** Post-mortem : inférences non étayées,
  compléments nécessaires éliminés. Leçon fondatrice : **laisser le LLM à la
  fois juger et décider perd des réponses.**

### v4 — contrat structuré, « le LLM perçoit, le code décide » (commit `a7e7635`, run #187)

Le LLM ne décide plus rien. Par candidat (bornés aux **12** mieux rerankés,
contre des réponses tronquées à 20) il émet :

```
id, evidence (extrait verbatim), relevance (directe | complementaire |
redondante | hors_sujet), roles (9 rôles fonctionnels : fondement_juridique,
mise_en_oeuvre_interne, procedure, bareme, condition, exception,
modalite_legale, definition, synthese_pratique), contribution,
redundant_with, contradicts
```

plus `question_scope` (perimetre_actif | juridique_ou_interministeriel),
`answerability`, et une `primary_exception` **proposée** parmi 4 codes.

La **politique déterministe** (`_apply_structured_policy`) applique ensuite :

- appartenance : garde `directe`/`complementaire`, écarte `hors_sujet` et les
  obsolètes (statut/date), garde une `redondante` si son remplaçant déclaré
  n'est pas retenu ;
- validation **verbatim** des extraits contre la source
  (`_evidence_is_verbatim`) ;
- ancrage éditorial : présomption du périmètre actif (source active directe
  ou portant un rôle de mise en œuvre), exceptions validées par le code
  (aucune source active directe, question strictement juridique, source
  active obsolète, contradiction de norme supérieure via un rang d'autorité
  constitution > code/loi > décret > arrêté) ;
- couverture lexicale question↔preuves → rétrogradation `insuffisante` ;
- toute évaluation manquante ⇒ conservation prudente du candidat.

**Résultat : −9 nets (0,6465), gate échoué.** Diagnostic : la politique
fonctionne, mais deux portes dures étaient non calibrées (matcher verbatim à
fort taux de faux négatifs, seuils de couverture) et les couplages aval
nuisent (cf. inventaire ci-dessous).

### Mode dark-metadata (commit `0630b69`, runs #189/#190)

`SelectorConfig.dark_metadata = True` (override CLI
`--selector-dark-metadata`) : **sélection et diagnostics v4 intégraux, zéro
influence aval** — aucune métadonnée `selector_*` écrite sur les sections,
ordre servi = ordre du reranker. Sert d'ablation propre : un seul commutateur
rend inertes les quatre couplages à la fois.

### v4.1 — mécanique de preuve et applicabilité (commit `4650a28`, run #191)

- Matcher verbatim : normalisation des tirets typographiques
  (U+2010…U+2212), suppression symétrique des pipes de tableaux Markdown et
  de l'emphase — les deux causes mécaniques de faux négatifs (barème q32,
  « ci‑après »).
- Un extrait invalide **disqualifie l'ancrage** ; sans aucune preuve
  vérifiée, pas de principal du tout (q17 : stub de titre préféré au doublon
  porteur de réponse).
- Contrat v4.1 : `evidence` obligatoire et placé **avant** `relevance`
  (l'extrait précède la classification qu'il justifie) ; `complementaire`
  exige la même population / type de contrat / situation juridique.
- **Résultat : mécanique de preuve validée (0/99 ancrage invalide) ; règle
  d'applicabilité infirmée telle qu'écrite** (mal scopée : le mélange de
  régimes se joue au niveau `directe` ; sur-élagage des compléments légitimes,
  cf. 02 et 04).

## Inventaire des couplages aval (ce que le mode dark débranche)

1. **Instruction d'abstention** : si le principal porte
   `answerability=insuffisante`, le prompt générateur impose « ne répondez ni
   oui ni non ». Mesuré : se déclenche sur ~45 % des questions, conformité du
   générateur stochastique — abstentions à tort ET réponses déformées.
2. **Cadrage « texte principal »** : label `Source N — texte principal` dans
   le prompt → le générateur organise sa réponse autour de l'ancre (utile
   quand elle est bonne, toxique sinon, cf. q200).
3. **Influence du principal sur l'assemblage** : tri des documents, budget,
   triangulation et `primary_publisher` dans le context-builder lisent
   `selector_primary`.
4. **Rôles/périmètre rendus** : lignes `Rôles système : …` par source dans le
   prompt générateur.

Décomposition mesurée à juge constant : couplages = **−5 nets** (11 questions
récupérées en les coupant, 6 perdues — l'effet est net-négatif mais non
uniforme : le cadrage aide parfois).

## Invariants transverses

- Échec quelconque du selector ⇒ retour de toutes les sections (biais
  disponibilité).
- Rejet explicite total ⇒ liste vide + logique de retry pipeline.
- Prompt versionné : un prompt DB sans le marqueur v4 est remplacé par le
  contrat embarqué (`_ensure_relational_prompt`).
- Traces : `selector_decisions` complet par requête (évaluations, politique,
  exceptions demandées/acceptées/rejetées, couverture, entrées kept/removed
  avec `section_id`/`document_id` joignables aux gold doc_ids).
- Latence : ~2,4 s (v2) → ~10,9–11,3 s (v4/v4.1, sortie structurée 12
  candidats) — non mergeable en défaut production en l'état.
