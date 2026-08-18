# Résultats expérimentaux — runs #180 à #191

Protocole constant : 99 questions `baseline_v1` (`gate_adoption_p1r2`), scope
ministériel `per-question`, RAGAS sauté, comparaison appariée contre le run
#180, tolérance de gate −0,05. Juge souverain : Scaleway
`qwen3-235b-a22b-instruct-2507`, vote majoritaire à 3 (sauf incident #189,
cf. 04). Détail au journal :
[`journal-experimentations-rag.md`](../../evals/journal-experimentations-rag.md).

## Vue d'ensemble

| Run | SHA | Configuration | Judge pass | Net vs #180 |
|---|---|---|---:|---:|
| #156 | — | baseline précédente (23/07) | 0,677 | — |
| **#180** | `8891397` | **v2 prompt complémentarité** | **0,7374** | **référence** |
| #186 | `c619140` | v3 relationnel (LLM choisit le principal) | 0,6970 | −4 |
| #187 | `9417941` | v4 structuré complet (sélection + couplages) | 0,6465 | −9 |
| #189 | `9f6a0eb` | v4 dark (juge grok-4.5 ×1 — **non comparable**) | (0,7475) | (+1) |
| #190 | = #189 | rejugement souverain des réponses de #189 | 0,6970 | −4 |
| #191 | `c434e87` | v4.1 dark (preuve corrigée + applicabilité) | 0,6667 | −7 |

Le doc recall est **identique (0,6410) sur tous les runs** : aucune de ces
configurations ne change le retrieval ; tout l'écart se joue en aval.

## Décomposition causale (juge constant qwen maj-3)

```
#180 (0,7374) ──[couche de sélection v4]──> #190 (0,6970)   −4 nets
#190 (0,6970) ──[couplages aval v4]───────> #187 (0,6465)   −5 nets
```

- **Sélection** : 6 gains / 10 pertes. Effet fortement contrasté par corpus
  (cf. ci-dessous).
- **Couplages** : 11 questions récupérées en les coupant (dont toutes les
  abstentions à tort q3/q17/q229 et les cadrages q200/q214), 6 perdues
  (q177, q202, q206, q211, q224, q4529 — le cadrage aide parfois).

## Le contraste par corpus — la lecture décisive

Judge pass par corpus (n, #191 v4.1-dark, #180, #190 dark) :

| Corpus | n | v4.1 #191 | #180 | #190 |
|---|---:|---:|---:|---:|
| manual | 56 | 34 | **41** | 33 |
| MATTE | 12 | 9 | 7 | **11** |
| Service-Public | 14 | 11 | 13 | 13 |
| synthetic | 11 | 7 | 7 | 7 |
| MSO | 4 | 4 | 4 | 3 |
| DGAFP | 2 | 1 | 1 | 2 |

La sélection v4 **gagne sur MATTE** (questions à sources étagées : fondement +
modalités + mise en œuvre — la complémentarité par rôles fonctionne) et
**perd sur manuel** (questions courtes à gold unique et précis, entourées de
régimes voisins : la sur-inclusion dilue ou mélange). Un agrégat plat peut
masquer 68/99 questions qui bougent en sens opposés : **la lecture par corpus
est obligatoire pour tout gate selector.**

## Funnel retrieval par étage (métriques natives du run #191, hit %)

| Étape | manual | MATTE | SP | synthetic |
|---|---:|---:|---:|---:|
| pool avant rerank | 62 | 92 | 100 | 100 |
| sections top-20 | 42 | 92 | 93 | 90 |
| sections top-12 (entrée selector) | 38 | 83 | 93 | 80 |
| gardées par le selector | 31 | 75 | 86 | 60 |
| **servies (avec doc entier + retries)** | **61** | **92** | **100** | **91** |

> Correctif du 18/08 : une première version de ce tableau (rétro-calcul sur
> les traces du #189) donnait manuel à 45 % au pool — sous-compte causé par
> des `gold_doc_ids` périmés (UUID fantômes d'une ancienne ingestion du
> décret 86-83, corrigés depuis, cf. 04). Les chiffres ci-dessus viennent des
> métriques d'étage natives (PR #367), calculées avec la résolution runtime.

Enseignements :

1. **La falaise dominante du corpus manuel est pool→top-20 (−20 pts)** : le
   gold est retrouvé en chunks mais ses sections ne survivent pas au rerank
   d'agrégation. Le pool lui-même (62 %) reste le second chantier ; « servi »
   ≈ « pool » — les mécanismes de secours récupèrent presque tout ce qui a
   été retrouvé.
2. **La coupe 20→12 a un coût réel** (−4 pts manuel, −9 MATTE, −10 synthetic ;
   perte q174 : gold au rang 13–20).
3. **La rétention du gold par la sélection v4 est MEILLEURE que v2** :
   étape « gardées », MATTE 83 % contre 58 %, SP 86 % contre 71 %, MSO 75 %
   contre 50 %. Le problème « le selector jette le gold » (juillet) est
   résolu ; le problème restant est ce qu'il garde **en plus**.
4. Les mécanismes de secours (document entier, retries, recherche légale)
   récupèrent beaucoup de golds après le selector (manuel : 31 % → 61 %).

Depuis la PR #367 (mergée sur dev, native au run #191), ces métriques sont
calculées par le run lui-même (`deterministic_metrics.stages` par item,
`aggregate.stage_metrics` par run).

## Taxonomie des 10 pertes du run #190 (sélection seule)

| Cause | Questions | Enseignement |
|---|---|---|
| Trou de retrieval + flip de juge | q30, q213 | gold ingéré mais jamais retrouvé ; #180 « passait » sur un contenu non-gold |
| Trou d'INGESTION (audit 18/08) | q202, q203 | articles 3/4 du décret 86-83 absents du corpus — issue #369 |
| Coupe top-12 | q174 | gold au rang 13–20 |
| Sélection a écarté le gold | q206 | seul vrai cas ; corrigé en v4.1 |
| Gold servi, dilution/mélange de régimes | q205, q211 | cible de l'arbitrage de régimes (04) |
| Gold récupéré par doc entier, réponse incomplète | q215, q224 | aval de la sélection |

## v4.1 (run #191) : une validation et une infirmation

- **Validé — mécanique de preuve** : ancrage à extrait invalidé **0/99**
  (24/99 au #189) ; q17 ancre le doublon porteur de réponse ; conversions
  q30/q202/q224 vs #190.
- **Infirmé — règle d'applicabilité** (−3 nets vs #190) : (a) cible manquée —
  sur q205/q211 la sélection est bit-identique au #189, le mélange de régimes
  étant porté par deux `directe` concurrents alors que la règle ne contraint
  que `complementaire` ; (b) dommages collatéraux — q15 (« et sinon, quelles
  possibilités ? » : les dispositifs alternatifs SONT la réponse) et recul
  MATTE (9 vs 11) / SP (11 vs 13) sur q222/q660/q676.

## Bruit de mesure

- Cluster instable identifié sur l'ensemble de la série : q28, q30, q32,
  q174, q213, q215, q926, q4530 — verdicts contradictoires entre runs sur des
  réponses quasi identiques (variance juge résiduelle ~±1 % par question en
  maj-3, non-déterminisme du générateur à température 0).
- Ordre de grandeur : **±2–3 flips par run**, du même ordre que les effets
  recherchés (2–4 pts). D'où la recommandation de curation du goldset avant
  tout nouveau gate (04).
