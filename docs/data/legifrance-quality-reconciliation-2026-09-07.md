# Réconciliation Légifrance du 7 septembre 2026

Ce relevé documente le diagnostic de l'issue #483. Le cache
`config/legifrance_article_cids.json`, généré le 12 juillet 2026, attendait
4 165 articles. Une nouvelle résolution des 25 textes actifs depuis Grist et
de leurs TOC PISTE au 7 septembre 2026 en attend 4 634 : 499 ajouts et 30
retraits.

La base examinée en lecture seule contient les 4 634 CIDs du nouvel attendu
dans `rag_documents` et `rag_chunks_dgafp`. Les 30 IDs signalés comme manquants
par le gate précédent sont donc un drift du cache, pas une perte d'ingestion.

## Les 30 retraits

Une interrogation `getArticle` sur chaque version confirme le même motif pour
les 30 entrées : état PISTE `ABROGE`, avec `dateFin` au 1er août 2026. Elles
sont regroupées ci-dessous par texte suivi et numéro d'article.

### Décret n° 86-83 — `JORFTEXT000000699956` (2)

- `LEGIARTI000006486566` — article 28-1
- `LEGIARTI000006486629` — article 50

### Décret n° 2016-151 — `JORFTEXT000032036983` (13)

- `JORFARTI000032037005` — article 1
- `JORFARTI000032037008` — article 2
- `LEGIARTI000041852785` — article 2-1
- `JORFARTI000032037012` — article 3
- `JORFARTI000032037013` — article 4
- `JORFARTI000032037014` — article 5
- `JORFARTI000032037015` — article 6
- `JORFARTI000032037016` — article 7
- `JORFARTI000032037017` — article 8
- `JORFARTI000032037018` — article 9
- `JORFARTI000032037020` — article 10
- `JORFARTI000032037029` — article 13
- `JORFARTI000032037030` — article 14

### Décret n° 2017-928 — `JORFTEXT000034640143` (13)

- `JORFARTI000034640193` — article 3
- `LEGIARTI000039639317` — article 3-1
- `LEGIARTI000039639319` — article 3-2
- `JORFARTI000034640209` — article 4
- `JORFARTI000034640210` — article 5
- `JORFARTI000034640212` — article 6
- `JORFARTI000034640215` — article 7
- `JORFARTI000034640216` — article 8
- `JORFARTI000034640218` — article 9
- `JORFARTI000034640219` — article 10
- `LEGIARTI000039639424` — article 10-1
- `LEGIARTI000039639426` — article 10-2
- `JORFARTI000034640326` — article 17

### Décret n° 2020-524 — `JORFTEXT000041849917` (2)

- `JORFARTI000041849954` — article 9
- `JORFARTI000041849955` — article 10

## Table contrôlée

Le delta Légifrance sert les articles depuis `rag_chunks_dgafp` et y met à
jour `updated_at`. `rag_chunks_legifrance` ne contient plus que 429 lignes de
textes historiques, n'est plus alimentée par le delta et est explicitement en
cours de décommission dans le writer. Le gate conserve donc la couverture et
la fraîcheur de `rag_chunks_dgafp` et cesse de présenter la table historique
comme une surface active.

## Résilience réseau

Les lectures Grist et les appels de consultation PISTE sont rejoués au plus
quatre fois avec backoff exponentiel pour les coupures réseau et statuts HTTP
transitoires (`408`, `425`, `429`, `5xx` usuels). Le `PATCH` de writeback Grist
est également rejoué car il réapplique les mêmes champs aux mêmes IDs. La
création de lignes Grist n'est pas rejouée, afin d'éviter les doublons après
une réponse ambiguë.
