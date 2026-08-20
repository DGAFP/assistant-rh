# Optimisation du selector — synthèse de la campagne (17–18/08/2026)

Campagne d'optimisation du **ContextSelector** (filtre LLM post-rerank) issue
de l'issue #360 (« distinguer les sources RAG complémentaires »), conduite en
sept runs officiels appariés sur le panel `baseline_v1` (99 questions). Ce
dossier documente l'architecture, les résultats, la confrontation à l'état de
l'art et les décisions.

## Documents

| Fichier | Contenu |
|---|---|
| [01-architecture.md](01-architecture.md) | Les versions du selector (v2 → v4.1), la séparation perception/politique, le mode dark-metadata, l'inventaire des couplages aval |
| [02-resultats-experimentaux.md](02-resultats-experimentaux.md) | Tous les runs, la décomposition causale sélection vs couplages, le funnel par corpus, la taxonomie des pertes |
| [03-etat-de-l-art.md](03-etat-de-l-art.md) | Confrontation du design à la littérature 2024–2026 (validations, écarts, références) |
| [04-lecons-et-feuille-de-route.md](04-lecons-et-feuille-de-route.md) | Mécanismes validés/infirmés, incidents de protocole, plan recommandé et critère d'abandon |

## Résumé exécutif

**Point de départ.** Le selector v2 (prompt seul, règle redondance vs
complémentarité, PR #365) est le champion mesuré : **0,7374** de judge pass
(run #180), +6 nets sur la baseline précédente. Il est en production sur dev.

**Le pari architectural v4** — le LLM évalue chaque candidat indépendamment
(pertinence, rôles, extrait probant, redondance, contradiction), le **code
décide** (ancrage éditorial, exceptions, obsolescence, validation des
extraits) — est né de l'échec mesuré du v3 relationnel (run #186 : le LLM
choisissait lui-même le texte principal, −4 nets). Cette séparation
perception/politique est conforme aux recommandations convergentes de la
littérature 2025–2026 (cf. 03).

**Verdict expérimental** (juge souverain constant, qwen3-235b maj-3) :

| Configuration | Run | Judge pass |
|---|---|---:|
| v2 prompt seul (baseline) | #180 | **0,7374** |
| v4 sélection seule (dark, sans influence aval) | #190 | 0,6970 |
| v4.1 sélection corrigée (dark) | #191 | 0,6667 |
| v4 complet (sélection + couplages) | #187 | 0,6465 |

La décomposition causale attribue **−4 points à la couche de sélection** (très
contrastés : MATTE **+4**, manuel **−8**) et **−5 points aux couplages aval**
(instruction d'abstention, cadrage « texte principal », rôles rendus au
générateur). Les correctifs v4.1 ont validé la **mécanique de preuve**
(0/99 ancrage à extrait invalidé, contre 24/99) mais **infirmé la règle
d'applicabilité** telle qu'écrite (−3 nets vs #190).

**Acquis durables** indépendants du verdict :

- l'observabilité du funnel retrieval par étage et par corpus (PR #367,
  mergée sur dev) — recall mesuré à pool / sections top-20 / top-12 /
  selector / servi ;
- le mode `dark_metadata` (sélection + diagnostics sans influence aval),
  outil d'ablation réutilisable ;
- la mécanique de preuve verbatim corrigée et la disqualification d'ancrage ;
- le diagnostic déterminant : **le goulot dominant du corpus manuel est le
  retrieval (45 % de hit au pool), pas la sélection**.

**Recommandation** (détail en 04) : curer le goldset avant tout nouveau gate,
merger la branche en mode shadow, attaquer le retrieval manuel, puis une
unique itération « arbitrage de régimes » avec critère d'abandon explicite.
Aucun rebranchement de couplage avant que la sélection n'atteigne le niveau
du run #180.

## Références rapides

- Journal détaillé des runs : [`docs/evals/journal-experimentations-rag.md`](../../evals/journal-experimentations-rag.md)
- Lecture du funnel : [`docs/quality/evaluation.md`](../evaluation.md) § « Metrics To Review »
- Code : `packages/rag-pipeline/src/assistant_rh_rag_pipeline/context_selector.py`,
  prompt `prompts/selector.md`, config `SelectorConfig` (dont `dark_metadata`)
- Branche de campagne : `fix/issue-360-selector-legal-coverage-2`
