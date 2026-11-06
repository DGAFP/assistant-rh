# Observabilité du runtime API

Chaque instance de l’application possède son registre Prometheus. Le listener
est activé uniquement avec `API_METRICS_PORT` (9109 dans le chart staging), sur un
port distinct du serveur utilisateur. Le Service HTTP n’expose pas ce port et
`/metrics` retourne 404 sur le serveur API. Le collecteur découvre chaque pod ;
un scrape du Service partagé ne conviendrait pas aux compteurs par processus.
Conserver une seule instance Uvicorn par pod. La NetworkPolicy autorise le
collecteur ; les autorisations internes déjà accordées par la plateforme restent
additives, notamment pour Conversations et les pods du namespace API.

Les compteurs HTTP incluent les refus 503 avant création d’un run. Leur label
`outcome` reflète aussi le résultat SSE après l’ouverture HTTP 200. Les flux ont
un compteur distinct pour succès, erreur, refus, déconnexion, arrêt serveur et
échec d’envoi. Les annulations ne sont pas assimilées à une panne fournisseur.

`assistant_rh_sse_first_text_seconds` mesure le temps depuis l’entrée ASGI jusqu’au
premier texte de pipeline envoyé avec succès à ASGI ; le rôle initial, les pings
et les sources de fin ne déclenchent pas cette mesure.
`assistant_rh_http_duration_seconds` couvre le dernier envoi de corps réussi ou,
en l’absence de fin, le retour de l’application. Ces mesures ne prouvent pas la
réception par le navigateur : la sonde externe Conversations assure ce contrôle.

Les histogrammes par stage incluent les stages échoués ou annulés. Les tentatives
HTTP fournisseur incluent les retries et fallbacks, avec opération, fournisseur
et catégorie fixe. Les statistiques SQL exposent le pool et l’admission sans
connexion ni requête SQL pendant le scrape. Le port diagnostic du cœur est pur,
synchrone, isolé par `ContextVar` et tolère l’échec d’un observateur.

Les labels proviennent de listes fixes : aucune question, réponse, identité,
conversation, modèle configurable, URL ou exception brute. Le request_id apparaît
seulement dans les logs techniques et dans l’en-tête HTTP, jamais dans les labels
Prometheus. Le filtre Alloy doit autoriser uniquement la grammaire technique
`api_diagnostic` / `api_request_finished` avant export vers Loki.

Les règles et dashboards associés sont dans `assistant-rh-infra/monitoring`.
La livraison du code, son activation par le chart, puis l’ingestion dans Cockpit
sont trois vérifications distinctes. L’ouverture du listener seule ne prouve
pas le bon fonctionnement d’un dashboard ou des notifications.
