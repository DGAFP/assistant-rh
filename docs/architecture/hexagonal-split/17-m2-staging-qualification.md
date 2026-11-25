# M2 — Recette API et Conversations

## Décision au 25 novembre 2026

**NO-GO de qualification M2.** L’API staging `21aa3e3a3bb37d78456c3e846cb47d9e70e429f0` est réconciliée sur deux pods au digest `sha256:432d8d3d51cb863902a20e6eaa6c46c2f028bb92462065f9c46b502d4d18a891`. Le gate M1 passe. Le SSE à travers le proxy et l’arrêt depuis Conversations passent aussi sur la candidate. Les pings, les erreurs après envoi des en-têtes et le rollback staging restent à qualifier, ainsi que le cloisonnement documentaire et la matrice de feedback de #532. L’ACL initiale est rétablie. [Publication API](https://github.com/DGAFP/assistant-rh/actions/runs/40498924160) · [D4 #531](https://github.com/DGAFP/assistant-rh/issues/531) · [M2 #532](https://github.com/DGAFP/assistant-rh/issues/532)

La CI du correctif de dépendances Conversations #26 est verte. Le job de publication de la PR est ignoré et les pods frontend et backend utilisent encore `3fe712b7`. La publication du feedback et sa recette navigateur restent à faire en phase E. [CI Conversations #26](https://github.com/DGAFP/conversations/actions/runs/45634251796) · [Workflow de publication](https://github.com/DGAFP/conversations/actions/runs/45634251791) · [E1 #533](https://github.com/DGAFP/assistant-rh/issues/533)

| Vérification | Preuve disponible | Preuve attendue |
| --- | --- | --- |
| D3 — replay exact | 98/98 cas de la capture staging comparés exactement entre core direct et route JSON depuis la sortie du retriever. Les sept scénarios enregistrés de M0b passent également. | Retrouver l’archive privée de septembre pour rejouer le run M1 #259. |
| D3 — clients | OpenAI `3.20.0` et `pydantic-ai-slim` `2.54.0` passent le smoke avec le serveur synthétique TCP. L’erreur post-headers est un `ModelAPIError` causé par l’`APIError` `stream_error`. | Vérifier le provider dans le backend Conversations déployé et ses appels privés à l’API. |
| D3 — goldset live | 98/98 réponses JSON valides et identiques aux runs persistés. Le gate M1 sur la capture complète donne **65/98 réponses validées** et **72,51 %** de rappel documentaire. | Gate M1 dans les tolérances des trois références. |
| D4 — opérabilité | Suite API locale sur PostgreSQL 17 et pgvector, **1 524 tests réussis**. Sur le proxy HTTPS staging, un flux de **47,2 s** se termine normalement et une demande d’arrêt produit un run `cancelled`. | Qualifier les pings, les erreurs après envoi des en-têtes, les limites sous charge et le retour arrière staging. |
| M2 — feedback et documents | Scénarios synthétiques d’auteur, d’habilitations courantes, de révocation et de documents privés. | Vérifier les rôles runtime #599, corriger puis qualifier le cloisonnement recherche → hydratation → délivrance sur les corpus concernés et rejouer la matrice API sur staging. |
| E1 — parcours navigateur | Historique, chat et PDF vérifiés dans les [recettes de livraison](https://github.com/DGAFP/assistant-rh/pull/618). | Rejouer le parcours intégré sur les images actives, y compris feedback persistant et modification, puis les scénarios de droits et de session. |

## Rejeu déterministe et qualité live

Reprendre l’archive privée figée du [run M1 #259](13-m1-run-metrics.md), vérifier l’empreinte de l’archive et passer au runner le répertoire décompressé. Le rejeu exerce l’adaptateur HTTP en mémoire. Les appels SQL, providers et le réseau sont hors du périmètre de ce rejeu. Les tests TCP complètent la couverture du streaming.

```sh
uv run --no-sync python -m scripts.conformance.m1_replay \
  "$M1_FROZEN_DIR" --http --check --report "$REPLAY_REPORT"
```

Pour la collecte live, l’exporteur lit le tag `baseline_v1` dans PostgreSQL
staging, contrôle l’empreinte publiée de M1 et résout les références
documentaires avec la même logique que le banc M1. Il écrit un panel JSONL
privé au format attendu par le collecteur.

```sh
uv run --no-sync python -m scripts.conformance.m2_panel \
  --env-file "$STAGING_DB_ENV" --output "$GOLDSET_JSONL"
```

Le manifeste de déploiement contient `environment: staging`, `git_sha`, `image_digest`, `config_sha256`, `corpus_sha256` et `model_config_sha256`. Relever les empreintes avant et après la campagne. Préparer une identité et une clé réservées à la recette dans l’environnement isolé. Le programme de signature reçoit le modèle demandé en argument et produit sur stdout une délégation #596 limitée à ce modèle, avec un nouveau `jti` à chaque appel. Révoquer la clé de recette et nettoyer les runs, traces et feedbacks associés à la fin.

```sh
uv run --no-sync python -m scripts.conformance.m2_goldset_http \
  --base-url "$STAGING_API_V1" --panel "$GOLDSET_JSONL" \
  --deployment "$DEPLOYMENT_MANIFEST" --signer "$STAGING_TEST_SIGNER" \
  --report "$TRANSPORT_REPORT" --private-capture "$PRIVATE_CAPTURE"
```

Le rapport compte les réponses valides, les échecs de transport et les références attendues effectivement servies. Le rappel des sources servies est une mesure distincte du rappel de retrieval de M1. Associer à l’identifiant du run une capture autorisée et complète du contexte effectivement servi au générateur. Les traces opérationnelles peuvent être tronquées. Appliquer le protocole M1 `gold-passages-v2` à trois votes et la tolérance maximale de cinq points sur le pass rate et le rappel de retrieval contre chacune des références M1. Consigner les références de run dans le journal. Les outils RAG-ops conservent leur périmètre actuel.

### Preuve complémentaire D3 du 25 novembre

Le [compagnon M0b](../../../tests/conformance/companions/m0b-full-20260917/README.md)
est vérifié avec son SHA-256 publié. La commande ci-dessous rejoue deux fois les
sept scénarios enregistrés, avec des ports DB et fournisseurs figés. Le test
compare les projections d’étapes, la réponse, les sources publiques et
protégées et l’usage. Le runner contrôle l’enveloppe HTTP brute puis compare le
résultat structuré du traitement au core direct.

34 tests ciblés passent. Le collecteur live a aussi été exécuté sur un
serveur HTTP local avec deux requêtes synthétiques, deux délégations
distinctes et récupération des deux références attendues. Les réponses
restent dans la capture privée, avec permissions `0600`.

```sh
PYTHON_DOTENV_DISABLED=1 uv run --no-sync python -m scripts.conformance.m0b_companion \
  replay "$M0B_EVIDENCE_DIR" --http --report "$M0B_HTTP_REPORT"
```

La suite privée vérifie l’empreinte de l’archive et effectue ce rejeu à partir de
`M0B_PRIVATE_ARCHIVE`. Les 98 questions et leurs références ont été exportées
en lecture seule depuis `SCW_POSTGRES_DSN_STAGING`. L’empreinte du panel source,
`afd6cfc97a4232e9f8b9253d9d9b5b930c63ed83421f544f5c8178a42e163bdc`, correspond
à M1 et la comparaison avec le run M0a #240 confirme les identifiants,
questions, réponses attendues et références. Le JSONL de collecte couvre 94
questions MATTE et 4 MSO. Son empreinte est
`2dfa514409f67a9df87f3dbd2ce65a1798016ede984d550738fd0630bb386c41`.

Le rejeu déterministe M1 requiert aussi les traces et réponses fournisseurs
figées dans son archive. Les identifiants locaux M1 #258 et #259 correspondent
à d’autres runs sur le serveur PostgreSQL staging.

### Goldset HTTP sur l’API staging du 25 novembre

Les **98 requêtes** du panel vérifié ont abouti sur la révision `21aa3e3`
depuis le backend Conversations, avec une délégation neuve par appel et
deux requêtes simultanées au maximum. Les réponses et références retournées
correspondent aux 98 runs persistés. La latence médiane est de **12,63 s**
et le p95 de **38,55 s**. Parmi les 98 réponses, 53 contiennent au moins
une référence attendue. Le rappel moyen par question des références servies
est de **45,14 %**, avec la méthode de correspondance du collecteur HTTP.

Le PDF privé de recette est accessible par son auteur en HTTP 200, sans
redirection. Le second compte du même groupe reçoit un HTTP 404. Les
empreintes de la configuration, des modèles et du corpus restent identiques
avant et après la campagne. Le nettoyage confirme zéro compte ou groupe
de la campagne, la suppression de **98 runs et 676 traces**, et le
rétablissement de la liste ACL initiale.

Les projections de contexte et préfixes des prompts correspondent pour les
96 réponses générées. Les deux autres suivent une branche de réponse
directe. Les traces de retrieval sont tronquées sur les 96 exécutions RAG.
[Résultats agrégés](../../evals/evidence/m2_http_goldset_staging_20261125.json)

### Jugement des réponses HTTP

Le bras `m1_http_staging_judge_20261125` utilise la référence M0a #240,
le juge Scaleway `mistral-medium-3.5-128b` et trois votes selon
`gold-passages-v2`. Il valide **69 réponses sur 98 (70,41 %)** et fournit
294 votes exploitables. Les diagnostics de retrieval sont absents des
entrées du juge à cause de la troncature des traces HTTP. L’écart avec
les scores M1 est donc indicatif.

```sh
PYTHON_DOTENV_DISABLED=1 uv run --no-sync python -m scripts.conformance.m2_judge prepare \
  --campaign "$HTTP_PRIVATE_DIR" --env-file "$STAGING_DB_ENV" --output "$JUDGE_DIR"

PYTHON_DOTENV_DISABLED=1 uv run --no-sync python -m scripts.conformance.m2_judge judge \
  --env-file "$M1_JUDGE_ENV" --output "$JUDGE_DIR"
```

### Rappel M1 et rejeu sur une nouvelle capture staging

La capture complémentaire utilise le moteur du pod de staging en lecture
seule, avec les mêmes 98 questions. Elle conserve les sorties du retriever
avant la réduction des traces et les appels d’inférence en aval. Elle
alimente un rejeu de la route JSON avec une délégation synthétique neuve
par cas. Le runner vérifie l’intégralité des appels enregistrés, les étapes,
la réponse, les sources, l’usage et l’enveloppe HTTP, hors réseau.

La conformance exacte s’applique à cette nouvelle capture, à partir de la
sortie du retriever. Le rejeu historique du lot de septembre requiert
l’archive privée M1 #259. Les réponses de la collecte HTTP initiale
alimentent le jugement de qualité.

Le rapprochement des 80 cas sauvegardés et des 18 cas repris confirme
les 98 identifiants, questions et modèles attendus. Le rappel documentaire
M1 atteint **72,51 %** contre **72,91 %** sur M1 core live et legacy live
et **72,43 %** sur M0a rejugé. Les écarts respectent la tolérance de
cinq points. Au moins un document attendu est retrouvé pour **81/98**
questions. Le rejeu exact passe sur **98/98 cas**. Les deux pods, le
corpus, les réglages et les modèles concordent avec le point de reprise,
et la liste ACL initiale est rétablie. [Résultats agrégés](../../evals/evidence/m2_d3_staging_followup_20261125.json)

Exécuter les commandes depuis un environnement uv où l’API et les
outils d’évaluation sont installés.

```sh
PYTHON_DOTENV_DISABLED=1 uv run --no-sync python -m scripts.conformance.m2_frozen_api \
  "$FROZEN_JSONL" --sha256 "$FROZEN_SHA256" --count 98

PYTHON_DOTENV_DISABLED=1 uv run --no-sync python -m scripts.conformance.m2_recall \
  "$FROZEN_JSONL" --archive-sha256 "$FROZEN_SHA256" \
  --panel "$GOLDSET_JSONL" --panel-sha256 "$GOLDSET_SHA256" --env-file "$STAGING_DB_ENV"
```

Les captures privées contiennent les questions, contextes et réponses.
Le dossier de recette versionne les résultats agrégés et les empreintes
nécessaires pour vérifier les fichiers détenus par les personnes habilitées.

### Gate M1 sur la même capture

Les 98 réponses de `m1_http_frozen_recapture_20261125` sont jugées avec
leurs contextes et leurs diagnostics de retrieval. Le juge reçoit le
corps servi avant le bloc de sources ajouté par l’API. Les 294 votes
sont exploitables et **65 réponses sur 98 (66,33 %)** sont validées.
Le rappel des mêmes cas est de **72,51 %**. Le gate passe sur les deux
métriques face aux trois références, avec une tolérance de cinq points.

| Référence M1 | Écart de validation | Écart de rappel |
| --- | ---: | ---: |
| Legacy live #258 | +3,06 points | −0,41 point |
| Core live #259 | +5,10 points | −0,41 point |
| M0a rejugé #255 | +5,10 points | +0,08 point |

```sh
PYTHON_DOTENV_DISABLED=1 uv run --no-sync python -m scripts.conformance.m2_gate prepare \
  --archive "$FROZEN_JSONL" --panel "$GOLDSET_JSONL" --reference-inputs "$VERIFIED_M0A_INPUTS" \
  --env-file "$STAGING_DB_ENV" --output "$GATE_DIR"

PYTHON_DOTENV_DISABLED=1 uv run --no-sync python -m scripts.conformance.m2_judge judge \
  --env-file "$M1_JUDGE_ENV" --output "$GATE_DIR"

PYTHON_DOTENV_DISABLED=1 uv run --no-sync python -m scripts.conformance.m2_gate report --output "$GATE_DIR"
```

Le rapport vérifie que le manifeste correspond à la capture versionnée
et renvoie un code non nul si le gate échoue.
[Preuve du gate M1](../../evals/evidence/m2_m1_gate_same_capture_20261125.json)

## Vérification Scaleway et retour arrière

Sur l’API `21aa3e3` et Conversations `3fe712b7`, le smoke HTTPS
authentifié reçoit les en-têtes en **0,094 s**, le premier texte en
**21,662 s** et le marqueur terminal après **47,195 s**, avec 550
fragments de texte. Le second échange déclenche l’action d’arrêt après
le premier texte. Elle retourne 200 et le run API est persisté
`cancelled`. Le compteur `disconnect` du pod sondé passe de 81 à 82
et la jauge des flux actifs revient à zéro. Aucun ping n’a été observé
sur ces deux échanges. Le premier essai avec `Accept: text/event-stream`
est rejeté par la négociation HTTP de Conversations. Les smokes valides
utilisent la négociation du navigateur.

Le backend Conversations résout le Service privé, obtient 200 sur
`/healthz` et 401 sur le catalogue sans délégation. Le load balancer
annonce 300 s côté client et serveur. Les deux pods API ont une limite
de 1 Gi et consomment 68 et 66 Mi au relevé. Le rôle API réel peut écrire
les feedbacks, les deux triggers attendus sont actifs et ce rôle ne
détient pas les tables protégées. Les deux comptes de recette, les deux
runs et leurs 14 traces ont été nettoyés, et l’ACL initiale est rétablie.
[Preuves D4 et contrôle du rôle API](../../evals/evidence/m2_d4_staging_operability_20261125.json)

Le dernier chart déployé avant la candidate est `0.52.1+14799ef13eb0`,
associé à `4701d04`. Les deux révisions contiennent le même code, le
même verrou de dépendances et les mêmes migrations. L’historique Flux
et le ReplicaSet précédent fournissent les références pour l’essai de
retour arrière.

1. Relever les révisions de chart, les digests démarrés des deux applications et l’état Flux. Vérifier les migrations et les rôles #599 avant le trafic de recette.
2. Depuis le backend Conversations, vérifier la résolution du Service privé API et `/healthz`. Via l’entrée staging, relever les horaires SSE du premier en-tête, des pings et du premier texte sur un flux long. Relever mémoire, redémarrages et limites sur les workloads.
3. Sur des requêtes de recette, corréler succès, erreur après 200 et annulation à leur statut persisté, aux logs et à `assistant_rh_sse_streams_total` par `outcome`. Après une annulation, vérifier la descente de `assistant_rh_sse_active` et la disponibilité du service.
4. Pour éprouver le rollback, relever le dernier chart et les digests sains, avec leur compatibilité de schéma. Épingler cette version du chart dans la configuration Flux, attendre l’état `Ready` et vérifier les digests des pods. Rejouer santé, SSE, document et feedback. Restaurer ensuite la sélection de la candidate par Flux et consigner les deux reconciliations. La preuve attendue inclut les révisions réellement observées et les résultats des smokes.

## Recette de l’intégration #533

Employer deux comptes de recette du même groupe, avec droits limités au même ministère. Après connexion, vérifier le catalogue et poser deux questions successives en SSE. Ouvrir une source publique et un PDF privé, enregistrer un feedback, recharger, se reconnecter et modifier le feedback. Vérifier que chacun des messages retrouve le bon tour et les bonnes sources. Vérifier aussi un ancien message sans association et un fichier indisponible.

Pour chaque nouvelle ouverture et modification, vérifier les refus au second compte, après expiration de session et après retrait du droit. Vérifier que le rétablissement restaure l’historique attendu. Contrôler l’absence de bearer API et d’URL S3 dans les échanges navigateur et relever les en-têtes du PDF. Le contrat actuel répond au `Range` avec le document entier et `Accept-Ranges: none`. Rejouer une lecture longue interrompue, une indisponibilité et un retry. Rattacher les résultats aux révisions de l’API, du backend et du frontend effectivement testées.
