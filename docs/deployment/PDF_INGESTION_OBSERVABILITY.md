# Suivi des ingestions PDF dans Grafana

L'exporter RAG Health lit `rag_ingestion_runs` dans la base de son environnement.
Il expose, pour MI, MASA, MATTE et MSO, la disponibilité et la date du dernier
passage terminé, son succès (aucun échec ni rejet) et les nombres de documents
attendus, ingérés, inchangés, échoués, supprimés et rejetés.

Les métriques `assistant_rh_ingestion_last_run_documents` sont des jauges du
dernier passage, **pas des compteurs cumulatifs**. Le label `result` vaut
`expected`, `ingested`, `skipped`, `failed`, `deleted` ou `rejected`.

La portée est enregistrée dans `details._scope` à partir de cette version :

- `full` : manifest complet d'un ministère ;
- `document` : exécution filtrée par `--doc-id` ;
- `unknown` : historique sans portée explicite, qui reste visible mais ne
  constitue pas une preuve de traitement du manifest complet.

Le dernier passage de chaque portée est conservé séparément. Les panels de bilan
et d'écart au manifest ne prennent que `scope="full"`. Un test sur un seul
document ne remet donc pas à zéro la fraîcheur d'un passage complet. Les quatre
ministères exposent `assistant_rh_ingestion_run_available=0` pour toute portée
sans passage terminé. Une table absente ou incomplète ne produit aucun faux
résultat d'ingestion réussie ; sa présence reste surveillée par les métriques
de tables de l'exporter.

L'écart affiché est le nombre actuel de documents moins le nombre attendu au
dernier passage complet. Il peut inclure des documents rejetés protégés. Un écart
nul ne prouve pas l'égalité des identifiants, et le manifest peut avoir changé
depuis ce passage : le rapprochement indépendant Grist/corpus reste suivi en #294.

## Déploiement et vérification

1. Publier le pipeline et l'exporter via le flux habituel. Reconstruire l'image PDF
   du cron au SHA publié pour que les prochains runs consignent leur portée.
2. Exécuter `RAG Health Deploy` depuis la révision validée, d'abord pour
   `scaleway-staging`, puis pour `scaleway-production` après vérification.
3. Sauvegarder le dashboard existant avant d'importer
   `config/grafana/rag-health-dashboard.json`. Conserver la source personnalisée
   « Assistant RH RAG Health » dans la variable `datasource`.
4. Vérifier les séries dans cette source Cockpit, au-delà du seul `/metrics`
   local : `assistant_rh_rag_last_poll_success` doit valoir 1 et la dernière
   collecte réussie doit être récente pour chaque environnement.
5. Après un passage réel du pipeline, rapprocher son bilan, les documents/chunks
   en base et l'acquittement Grist. Une suppression doit apparaître dans le panel
   des suppressions et ne pas être réingérée au passage suivant.

Ne pas déduire le succès d'un cron de la seule fraîcheur de l'exporter : celui-ci
peut continuer à collecter alors que l'ingestion ne tourne plus. Inversement,
si l'API Jobs reste en attente mais que Cockpit contient un bilan d'exécution,
comparer les identifiants de run, les logs et la base avant toute relance. Une
incohérence d'état ne doit pas être transformée automatiquement en succès.

Les comptes Grafana et jetons de vérification temporaires sont révoqués après
utilisation. Cette configuration ne crée aucun contact de notification.

## Lecture du dashboard

La vue ouvre la production par défaut ; le sélecteur permet aussi staging ou
la comparaison des deux environnements. La synthèse distingue le résultat de
la dernière collecte de la fraîcheur du dernier snapshot réussi. Une collecte
réussie ne prouve pas l’exécution du cron.

Les tableaux d’ingestion affichent les valeurs actuelles, avec les périmètres
« historique », « ciblé » et « complet ». « À établir » signifie qu’aucun run
complet explicitement identifié n’est disponible. Les bilans complets, la
qualité détaillée et le diagnostic de collecte sont repliables. Les détails
d’embeddings conservent le modèle et la colonne physique.
