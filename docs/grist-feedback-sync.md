# Synchronisation du dashboard Feedback vers Grist

Issue [#561](https://github.com/DGAFP/assistant-rh/issues/561). Périmètre :
Streamlit et `assistant-rh-rag-pipeline`. Aucun composant de l’API Assistant RH.

## Configuration

Configurer dans l’environnement du serveur Streamlit :

| Variable | Valeur attendue |
| --- | --- |
| `GRIST_API_BASE_URL` | URL HTTPS de l’instance, par exemple `https://grist.numerique.gouv.fr` |
| `GRIST_API_KEY` | Clé existante, avec accès en écriture au document cible |
| `GRIST_FEEDBACK_DOC_ID` | ID d’un document Grist **déjà créé** et accessible à cette clé |
| `GRIST_FEEDBACK_TABLE_ID` | ID de la nouvelle table dédiée, par exemple `Feedbacks` |
| `APP_SCALEWAY_ENV` | Environnement de la **base source** : `staging`, `production` ou `local` |

Les deux variables `GRIST_FEEDBACK_*` sont obligatoires pour activer le bouton.
Elles ne reprennent jamais implicitement `GRIST_DOC_ID` / `GRIST_TABLE_ID`, qui
servent au référentiel de sources. Le même document est possible avec une table
distincte. Une destination égale à celle du référentiel configuré est refusée.

### Destination retenue pour #561

Le document de revue choisi et son identifiant sont conservés dans la
configuration locale et les variables de déploiement de l’environnement retenu :

```dotenv
GRIST_FEEDBACK_DOC_ID=<identifiant-du-document-de-revue>
GRIST_FEEDBACK_TABLE_ID=Feedbacks
```

La table `Feedbacks` a été créée et son schéma vérifié le 15 septembre 2026 :
29 colonnes, dont les trois annotations `Bool` sans formule. Aucun feedback
n’a été transféré lors de cette préparation. La reprise doit être déclenchée
depuis le dashboard sur l’environnement et la période retenus.

### Déploiement et identité de la source

En déploiement, définir `GRIST_FEEDBACK_DOC_ID` et `GRIST_FEEDBACK_TABLE_ID` comme
variables GitHub de l’environnement concerné. Les workflows et le script de
déploiement Streamlit les transmettent au conteneur. La clé reste le secret
`GRIST_API_KEY`. Aucune nouvelle dépendance ni migration PostgreSQL.

`APP_SCALEWAY_ENV` est prioritaire sur `APP_ENV` ; ce dernier sert de repli.
Avec un tunnel local, renseigner l’environnement de la base distante, et vérifier
qu’il correspond au DSN utilisé par le dashboard. Aucun choix dans l’interface
ne change la base ou cette identité. Staging et production peuvent alimenter la
même table, leurs identifiants restent distincts.

## Reprise et mises à jour

1. Ouvrir **Feedback Dashboard** avec les droits administrateur.
2. Cliquer **Rafraîchir** pour relire les données et les analyses récentes.
3. Ouvrir **Synchroniser les feedbacks vers Grist** (section export).
4. Choisir le périmètre :
   - **Filtres actuels du dashboard** : groupes, période, thème et exclusions
     qualité effectivement appliqués ; c’est le choix initial.
   - **Tout l’historique** : toutes les dates et tous les groupes de la base
     courante, y compris les groupes masqués et les feedbacks sans run.
5. Vérifier le nombre de feedbacks, l’environnement et la destination affichés,
   puis cliquer **Synchroniser vers Grist**. La table est créée si absente.

Le déclenchement est **manuel**, sans planification ni envoi à l’ouverture de la
page. Répéter cette opération après de nouveaux feedbacks ou une analyse
automatique. Chaque relance renvoie l’ensemble du périmètre choisi, ce qui reprend
aussi les corrections anciennes. La fréquence dépend des séances de revue ;
aucun curseur incrémental ne peut masquer une mise à jour.

Le résultat affiche le nombre de feedbacks confirmés par Grist, la destination,
le périmètre et l’heure de la tentative. Il persiste pendant la session Streamlit.
Une erreur affiche le statut HTTP ou un message réseau sans réponse brute ni
secret. Un lot non confirmé peut avoir été reçu : relancer le même périmètre
est prévu et conserve les annotations. Après une coupure pendant la création,
la relance recherche à nouveau la table avant de la créer.

## Contrat des données et des annotations

- Une ligne par couple `(environment, feedback_id)`, avec `turn_id` conservé.
  Deux feedbacks sur un même tour restent deux lignes.
- Dates en texte ISO UTC ; note convertie du stockage 0–4 vers **1–5**, comme
  l’export Excel #470 ; utilité **Y/N** ou vide si absente.
- Question et réponse complètes du feedback, avec repli sur `chat_runs` si
  vides. Jointure gauche : l’absence de run ou d’analyse ne bloque pas l’envoi.
- Raisons historiques/positives/négatives, commentaire, groupe, ministère
  (identifiant et libellé), thème, `beta_scope`, `error_category`, `ai_reason`,
  date d’analyse, session/rang, version RAG, mode de sélection, répartition des
  sources et durée du run sont conservés. Les valeurs absentes restent vides.
- **Hors-champs** (`hors_champs`), **missing_document** (`missing_document`) et
  **Traité** (`traite`) sont des colonnes Grist **Bool**, éditables indépendamment
  sous forme de cases à cocher, sans formule. La valeur native initiale est
  `False` (N). Filtrer `traite = False` pour préparer la revue restante.
- Cocher **Traité** est exclusivement une décision humaine. Une hypothèse
  automatique ou l’une des deux autres cases ne le valide jamais.
- Aucun envoi ne contient les trois colonnes humaines, même à la création d’une
  ligne. Une case cochée **ou décochée** est conservée. Les colonnes sources sont
  actualisées ; les qualifications restent dans Grist, sans réécriture en base.

Le client utilise l’[upsert natif de Grist](https://support.getgrist.com/api/#tag/records)
(`PUT …/records`, `require` sur environnement et ID, `fields` sur les seules
données sources), par lots de 100. Aucun `POST` de lignes ni suppression.

Ne pas modifier les identifiants de lignes/colonnes techniques, dupliquer
manuellement des lignes ou ajouter de formules aux colonnes synchronisées et
humaines. Le client vérifie leur type et l’absence de formule avant l’envoi et
refuse un schéma incompatible sans le modifier. Une colonne supplémentaire
personnelle est laissée intacte. Les suppressions de feedbacks en base ne sont
pas répercutées dans Grist ; cette première version conserve la revue collective.

## Validation

```bash
uv sync --group dev
uv run python -m pytest tests/test_feedback_grist.py tests/test_feedback_grist_ui.py tests/test_feedback_dashboard.py -q
```

Les tests utilisent des données synthétiques et un double HTTP avec état :
mapping complet, historique sans run/analyse, identité par environnement,
relances, toutes les combinaisons de cases cochées/décochées, HTTP refusé et
timeout **après** écriture, création/reprise de table, contrat de colonnes et
déclenchement Streamlit. La création et la validation du schéma ont aussi été
exécutées sur le document retenu ; la synchronisation des lignes réelles reste
à déclencher après configuration et déploiement de Streamlit.
