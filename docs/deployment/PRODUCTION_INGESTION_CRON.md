# Cron quotidien d’ingestion en production

Le workflow `Data Engineering — cron delta (production)` traite Service-Public,
Légifrance, MI, MASA, MATTE et MSO à **21:17 UTC chaque jour**. Deux chaînes au
maximum tournent en parallèle ; chaque chaîne attend la fin du médaillon, de
l’ingestion et du backfill. R2 et les bulk dumps sont exclus. Le verrou
`data-promote-prod` évite un chevauchement avec la promotion manuelle.

## Activation

Le workflow doit être publié sur `main`. Il n’exécute rien depuis une autre
branche, y compris lors d’un déclenchement manuel.

1. Publier et valider le correctif Grist en suivant le flux dev → staging → main.
2. Construire et valider les six images `service-public-pipeline`,
   `service-public-ingestion`, `legifrance-pipeline`, `legifrance-ingestion`,
   `pdf-sources-pipeline` et `embeddings-job` depuis le même commit de production.
   Conserver le tag immuable `prod-<SHA complet>` ; ne pas utiliser `prod-latest`.
3. Définir la variable GitHub **du dépôt** `DATA_PROD_CRON_IMAGE_TAG` avec ce tag.
   Le cron ne reconstruit pas les images et ne suit pas les futurs tags latest.
4. Vérifier en lecture seule le manifest Grist et le plan de suppression contre
   la base production, sauvegarder les données concernées avant le premier apply.
   Un `--dry-run` du dispatcher Scaleway ne vérifie que les commandes : il ne
   constitue pas un plan de diff du corpus.
5. Déclencher manuellement le workflow sur `main`, puis vérifier les six résultats,
   les rapports de qualité, les documents/chunks et le writeback Grist.
6. Définir la variable GitHub **du dépôt** `DATA_PROD_CRON_ENABLED=true`.
   Elle doit être au niveau dépôt (ou organisation), car la condition de job est
   évaluée avant l’entrée dans l’environnement `scaleway-production`.

Pour arrêter les prochains passages automatiques, mettre cette variable à
`false`. Cela n’arrête pas une exécution déjà démarrée. Le lancement manuel reste
possible pour un rattrapage. Chaque changement d’images exige une nouvelle
validation et une mise à jour explicite du tag épinglé.

## Demandes de suppression Grist

Pour les quatre ministères PDF, `statut=a_supprimer` est reconnu même si
`statut_ingestion=ok`. Les valeurs historiques `a_supprimer` et `supprime` de
`statut_ingestion` restent reconnues. Une ligne inactive ne doit pas être
réingérée. Pour une réactivation, retirer les statuts de suppression dans les
deux colonnes et le drapeau `abroge` le cas échéant.

Après la suppression effective en production, le pipeline écrit
`statut=supprime`, `statut_ingestion=supprime`,
`statut_ingestion_reelle=non_trouve` et `ingere_prod=false`. En staging, seul
`ingere_staging` change. Une ligne rejetée à la validation reste protégée : son
document n’est pas supprimé et son intention de suppression n’est pas écrasée.

Les contrôles structurels SP/Légifrance et la couverture des embeddings sont
bloquants. Le contrôle des embeddings s’exécute aussi si le contrôle structurel
échoue après une chaîne réussie. Les rapports sont conservés comme artifacts.

Suivi : #261, #250, #288, #294. Ce cron ne remplace pas le matcher indépendant de
réalité Grist (#294), ni les métriques et panels de drift à ajouter dans Grafana.
