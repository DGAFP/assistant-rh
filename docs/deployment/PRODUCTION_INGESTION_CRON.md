# Crons quotidiens d’ingestion staging et production

Le workflow `Data Engineering — cron delta (production)` traite Service-Public,
Légifrance, MI, MASA, MATTE et MSO à **21:17 UTC chaque jour**. Deux chaînes au
maximum tournent en parallèle ; chaque chaîne attend la fin du médaillon, de
l’ingestion et du backfill. R2 et les bulk dumps sont exclus. Le verrou
`data-promote-prod` évite un chevauchement avec la promotion manuelle.

Le workflow `Data Engineering — cron delta (staging)` traite les mêmes six
corpus à **02:00 UTC chaque jour**, avec deux chaînes au maximum en parallèle.
Il utilise les images `staging-latest`. Les horaires GitHub sont indicatifs :
un passage peut démarrer en retard. Les deux crons réconcilient chacun leur
propre base ; il ne s’agit pas d’une copie de staging vers production.

## Ajout d’un PDF

1. Créer la ligne du ministère dans Grist puis joindre le fichier via **Import
   Sources**, ou créer la ligne et déposer le fichier directement depuis cette page.
2. L’upload dépose le fichier dans la dropzone et renseigne `uid`, `cle_bucket`
   et `hash_contenu`. Il ne déclenche pas de job.
3. Au prochain passage de chaque environnement, le pipeline lit le manifest,
   traite les fichiers nouveaux/modifiés, écrit documents, sections et chunks,
   puis lance le backfill des embeddings et vérifie leur couverture à 100 %.
   Un PDF inchangé n’est pas systématiquement retraité.

La ligne doit appartenir à MI, MASA, MATTE ou MSO, avec un UID unique, un titre,
une clé de fichier valide et aucun statut de suppression. Les indicateurs
`ingere_staging` et `ingere_prod` attestent la présence dans chaque corpus ; la
réussite du backfill est contrôlée séparément par le workflow.

## Activation

Le workflow de production doit être publié sur `main`. Il n’exécute rien depuis une autre
branche, y compris lors d’un déclenchement manuel.

1. Publier et valider le correctif Grist et le cron PDF staging en suivant le flux
   dev → staging → main. Les schedules GitHub utilisent le workflow de `main` :
   une PR fusionnée uniquement sur `dev` n’active pas le nouveau périmètre quotidien.
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

Deux gestes sont pris en charge pour les PDF :

- mettre `statut=a_supprimer` pour conserver la ligne et suivre la suppression ;
- supprimer physiquement la ligne Grist : le prochain passage complet détecte
  le document absent du manifest et le supprime dans chaque environnement.

La cascade supprime les chunks (embeddings compris), les sections puis le
document, dans une transaction par base et en limitant la suppression au corpus
concerné. Elle n’est pas simultanée entre les deux bases : chacune applique la
demande lors de son propre passage réussi. Les fichiers de la dropzone et les
archives bronze/silver/gold sont conservés.

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
Le statut terminal `supprime` reste inactif : le premier environnement à passer
ne neutralise pas la suppression attendue dans l’autre. Vérifier les **deux**
indicateurs `ingere_*`, pas seulement le statut global. Pour une ligne supprimée
physiquement, la trace se trouve dans `rag_ingestion_runs` et dans les tables RAG,
puisqu’il n’y a plus de ligne Grist à acquitter.

Une erreur HTTP ou une réponse Grist malformée interrompt la lecture avant toute
suppression. Une réponse explicite `records: []` est en revanche un manifest vide
valide et retire les documents du corpus lors d’une réconciliation complète.
Un fichier temporairement inaccessible ou une ligne rejetée avec UID connu ne
sont pas assimilés à une suppression de la ligne.

Les contrôles structurels SP/Légifrance et la couverture des embeddings sont
bloquants. Le contrôle des embeddings s’exécute aussi si le contrôle structurel
échoue après une chaîne réussie. Les rapports sont conservés comme artifacts.
Après une réconciliation PDF réussie, une table de chunks existante mais vide
est acceptée uniquement si aucun document du ministère ne subsiste en base
(`--allow-empty-corpus <ministère>`), notamment après le retrait du dernier PDF.
Le rapport reste explicite (`is_empty=true`) ; une table absente ou des embeddings
manquants sur des chunks restants font toujours échouer le contrôle. SP/Légifrance
conservent l’exigence d’une table non vide.

Suivi : #261, #250, #288, #294. Ce cron ne remplace pas le matcher indépendant de
réalité Grist (#294), ni les métriques et panels de drift à ajouter dans Grafana.

## Tests de cascade

`tests/test_pdf_reconciliation_postgres.py` vérifie les quatre ministères avec
les deux ordres de passage prod/staging, la suppression de ligne et les statuts
de retrait. Il utilise `API_SYNTHETIC_POSTGRES_DSN`, uniquement sur la base locale
`assistant_rh_api_test`, dans des schémas temporaires distincts. Il s’exécute dans
CI Tests avec le service PostgreSQL existant ; sans ce DSN, ces tests sont ignorés.
