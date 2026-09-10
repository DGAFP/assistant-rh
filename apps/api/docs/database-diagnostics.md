# Diagnostics des erreurs DB

Les adaptateurs conservent un diagnostic technique sûr sur l'exception publique
après nettoyage de la transaction. Ils ne journalisent pas la transaction : seul
le point de décision sait si l'appel échoue ou continue avec un repli. Les classes,
codes et corps des réponses HTTP restent inchangés.

## Lire un événement

Chaque événement est un objet JSON dans le message du log et expose aussi les
mêmes champs comme attributs du `LogRecord` pour les collecteurs structurés :

```json
{"category":"statement","code":"database_failure","correlation_id":"9207b35d70e24126b1d889a147bc5e52","event":"database_failure","operation":"prompt.get","recovered":false,"sqlstate":"42P01"}
```

- `operation` : valeur de `DBOperation` codée dans le dépôt, jamais un nom de
  prompt, une table choisie par le client, une URL ou un contenu utilisateur.
- `category` : intégrité, sérialisation, deadlock, indisponibilité/capacité du
  pool, connexion, vérification de connexion, instruction ou erreur applicative.
- `code` : code DB public stable, même si le handler répond par un code HTTP
  générique différent. Par exemple une erreur DB peut produire `internal_error`.
- `sqlstate` : code PostgreSQL reconnu par psycopg, ou `null`. Par exemple `42P01`
  indique une relation absente, `28P01` un échec d'authentification, `23505` une
  violation d'unicité. Aucune table, contrainte, valeur ou explication du serveur
  n'est copiée. Les timeouts du pool et erreurs purement réseau n'ont souvent
  aucun SQLSTATE ; un code serveur inconnu est également omis.
- `correlation_id` : identifiant opaque généré côté serveur. Pour HTTP, il est
  retourné dans `X-Request-ID` et partagé par les diagnostics de cette requête.
  Les valeurs entrantes `X-Request-ID`, `traceparent` et les bearers ne sont pas
  reprises. Il s'agit d'une corrélation locale, pas d'une intégration OpenTelemetry.
- `event=database_recovery`, niveau `WARNING`, signifie que le point de décision
  applique un repli ou prévoit un nouvel essai ; `recovered=true` décrit ce choix,
  **pas une preuve de rétablissement de PostgreSQL**. Un échec définitif est
  `event=database_failure`, niveau `ERROR`.

Les messages d'exception, traces Python, SQL, paramètres, DSN, identifiants de
session et données personnelles ne sont jamais arguments des logs émis ici.
Le diagnostic ne donne donc volontairement pas l'objet SQL ni la valeur fautive.

## Points de décision couverts

| Chemin | Journalisation |
| --- | --- |
| Exception DB atteignant le handler HTTP | Un `ERROR`, diagnostic conservé même après un emballage applicatif |
| Repli de `RAGConfigurationService.load` | Un `WARNING`, valeurs par défaut et code de repli conservés |
| Échec du contrôle de santé | Un `ERROR` avant le rapport de santé générique |
| Nettoyage périodique des sessions | Un `WARNING` par échec, nouvelle corrélation par itération, nouvel essai à l'intervalle existant |
| Ouverture/fermeture du pool | Un `ERROR` terminal ; une frontière extérieure ne le répète pas |
| Tentative de connexion d'un worker du pool | Le warning psycopg existant est enrichi, sans second warning |
| Vérification d'une connexion avant transaction | Catégorie et SQLSTATE préservés jusqu'au point de décision, avec le nom de l'opération du dépôt |
| Erreur secondaire de rollback | Warning assaini `db.rollback`, sans remplacer l'exception initiale |
| Connexion rendue dans un état anormal | Warning assaini `db.pool.reset` ou `db.rollback` avant récupération/fermeture |

Les filtres sur `psycopg`, `psycopg.pool` et `psycopg.transaction` identifient nos
appels par leur contexte, ou nos connexions/transactions dans les arguments du
logger. Ils remplacent les warnings par des champs sûrs et suppriment les messages
DEBUG/INFO de ces appels, qui peuvent déjà contenir du texte interpolé du driver.
Les autres pools hors de ces contextes restent inchangés. Les représentations des
connexions et transactions ne sont pas copiées dans les logs.
Une tentative échouée du worker et l'expiration de l'attente d'ouverture sont
deux événements distincts : `db.connect` et `db.pool.open`. Il peut y avoir plusieurs
tentatives. Les workers héritent de la corrélation du cycle de vie qui ouvre le
pool ; un échec de reconnexion n'est pas attribué à une requête cliente.

Toutes les transactions des dépôts actuels portent une opération explicite. Les
appels directs à `Database.transaction()` gardent la valeur `db.transaction`.
Un appel sans contexte reçoit un identifiant de diagnostic autonome.

## Autres tâches et intégration future

Un nouveau job autonome doit ouvrir `diagnostic_context()` à son entrée, puis
appeler `report_database_error(exc, recovered=...)` à son point de décision.
Le helper ne journalise que les erreurs DB, ou une exception qui en conserve le
diagnostic dans sa chaîne ; il déduplique les appels concernant le même diagnostic.
Il ne doit jamais être remplacé par `logger.exception` ou un log de `str(exc)`.
Une transaction traduite seule conserve le diagnostic mais reste silencieuse.

La PR #545 introduit un warning ciblé pour le repli acronymes dans le query
processor. Cette PR ne modifie ni sa branche ni ce step : la traduction silencieuse
évite d'ajouter un deuxième log sous son warning. Quand ce step sera intégré,
remplacer son warning par `report_database_error(exc,
operation=DBOperation.ACRONYM_LOAD, recovered=True)` permettra d'y inclure aussi
opération, catégorie, SQLSTATE et corrélation, toujours une seule fois.
Les replis de chargement des prompts du même step devront aussi utiliser ce
helper à leur point de décision ; ils ne sont pas présents sur le `dev` de base.

C6 n'est pas livré ici : aucune prétention de corrélation avec son futur
`RunContext`, de trace moteur complète, ni d'instrumentation des futurs dépôts
appelés par le moteur. Son assemblage devra réutiliser le contexte de requête et
garder la journalisation des replis au point de décision.

## Vérification

`apps/api/tests/db/test_diagnostics.py` injecte des exceptions au niveau du driver
et exerce les adaptateurs, les handlers ASGI et les vrais workers du pool sans
connexion réseau. Les tests couvrent catégories et SQLSTATE, messages adverses,
corps HTTP inchangés, requêtes concurrentes, replis, déduplication et annulation.
Les chemins réels de rollback psycopg/pool sont également exercés avec des erreurs
secondaires injectées, ainsi que les logs DEBUG comportant des données adverses.
Les tests existants de transactions et de dépôts sur PostgreSQL synthétique
restent la preuve des commits, rollbacks et comportements SQL ; ces diagnostics
ne nécessitent aucune migration ni accès staging/production.
