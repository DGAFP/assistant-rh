# Travail sur main et livraisons par tags

`main` est la branche de travail permanente. Les branches courtes ouvrent leurs
PR vers `main` ; une fusion exécute la CI et ne déploie rien. Les règles de main
imposent les PR, signatures et contrôles de sécurité. Utiliser une fusion sans
squash, conformément aux méthodes autorisées par le dépôt.

| Événement | Résultat pour l’API |
| --- | --- |
| PR / merge vers `main` | Tests, sécurité, construction sans publication |
| `vX.Y.Z-rc.N` | Construction scannée, livraison et recette staging |
| `vX.Y.Z` | Promotion des digests de la RC validée de même version et même SHA |

Préparer versions, changelog et lockfiles avant la première RC. Toute correction
après recette nécessite une nouvelle RC. Les versions de l’API et de Conversations
restent indépendantes. Aucun tag n’est créé par une fusion ou par Release Please.

Pour un hotfix, partir du tag production, ouvrir une PR vers main et conserver
le commit testé en fusionnant sans squash. Taguer ce commit intégré avec une RC,
puis promouvoir le même SHA après recette. La CI tourne aussi sur le tag : le
résultat fusionné d’une PR ne suffit pas à valider son SHA source.

La [procédure de livraison](../deploy/README.md) décrit les preuves Flux/Helm,
la recette authentifiée, les reprises et les prérequis de production.

## Transition

Le socle déployé de staging `2043a66b9da9b88a7e44c0ce1efdcc3896a36353`
est repris dans main avec son historique et les métadonnées de version de main.
Les nouvelles PR ciblent main. Les PR encore basées sur dev doivent être
resynchronisées avec main avant intégration ; dev et staging ne déclenchent plus
la livraison Kapsule. Les branches historiques restent disponibles.

L’ancien workflow Release Please, ses promotions staging → main et les
back-merges automatiques sont retirés. La promotion en attente #610 devient
obsolète. Les migrations ne suivent plus une exécution Release Please et ne
lancent plus Streamlit en chaîne. Les opérations manuelles historiques restent
accessibles explicitement ; aucune migration n’appartient à la livraison API.
Les ingestions et leurs planifications restent des opérations distinctes.

Décision commune : [ADR 0001](https://github.com/DGAFP/assistant-rh-infra/blob/docs/adr-tagged-releases/docs/adr/0001-livraisons-par-tags.md).
