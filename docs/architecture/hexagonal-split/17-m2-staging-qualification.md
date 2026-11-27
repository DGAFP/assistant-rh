# M2 — Recette API et Conversations

## Décision au 27 novembre 2026 — candidate `2043a66`

**GO M2 vers la phase E ; phase D terminée.** Après autorisation explicite de l’opérateur le 27 novembre, le jugement des 98 cas par Scaleway `mistral-medium-3.5-128b`, rubrique `gold-passages-v2`, donne **68/98 réponses validées (69,39 %)** et **294 votes exploitables**, sans vote en échec. Le rappel documentaire est **72,51 %**. Les deux métriques respectent les tolérances des trois références M1, avec une baisse maximale de cinq points inchangée. [Gate complet et empreintes](../../evals/evidence/m2_m1_gate_candidate_20261127.json).

La décision porte sur les révisions ci-dessous et permet d’ouvrir la phase E et son canary staging borné. Les recettes navigateur, ProConnect, canary, charge et stabilité conservent leurs critères propres. Les scores des campagnes précédentes restent historiques.

Révisions qualifiées : API `2043a66b9da9b88a7e44c0ce1efdcc3896a36353`, digest `sha256:5f0eff1de8629ca7e7c2f827062f000322968b995a35f3a3a8f6861bf160673b`, chart `0.84.1+aaf6ad46cd64` ; backend Conversations `d38bec4eeaebc911b5278d404dc8eb5879c9d1ce`, digest `sha256:374f1985a3f8425ab02a8ce8a79f58c53da37e2031cec63d92e5893333ec4663`, chart `0.73.1+d1303baae68f`. Les quatre pods sont prêts sans redémarrage à la fin de la campagne.

Le DSN opérateur TLS fourni a levé le blocage d’accès à la nouvelle base staging. Aucun changement du runtime, de configuration, d’habilitations existantes ou de routage n’a été nécessaire. Les 102 fichiers Python de l’image API correspondent au code `dev` `533bedb` ; les deux garde-fous SQL correspondent à la migration versionnée. La suite complète passe : **1 578 tests, zéro échec et zéro skip**, dont le rejeu M0b privé, les contrôles du gate sur une nouvelle candidate et le refus des cibles opérateur incorrectes. Ruff, format et les quatre contrats d’import passent.

## Recettes terminées sur la candidate

| Vérification | Résultat et preuve |
| --- | --- |
| Feedback et droits SQL #599 | **123 contrôles HTTP/SQL réussis** sur des ressources synthétiques existantes : auteur et ministère courant, pair du même groupe, session collective, reconnexion, répétition sans écriture, huit modifications concurrentes, audit, conservation des annotations, invalidation unique de l’analyse et refus d’une analyse obsolète. Une panne SQL limitée à la fixture provoque un HTTP 500 et annule le feedback ainsi que l’audit. [Preuve individuelle](../../evals/evidence/m2_individual_staging_20261126.json). |
| Rôles non API | Deux logins Scaleway temporaires non administrateurs, avec permission native `readonly` et les rôles existants `arh_streamlit` / `arh_analysis`, refusent les modifications de note, commentaire et motifs individuels, les suppressions et le retrait des triggers. Les écritures d’annotations et d’analyse autorisées passent ; le remplacement et l’audit collectifs historiques passent. Les permissions puis les logins sont supprimés. Le compte API réel reste non propriétaire. |
| Documents | PDF S3 réel et document legacy synthétique de **33 554 413 octets**, sans redirection, `private, no-store`, `nosniff`, nom sûr et empreinte stable. Les variantes Range rendent le fichier complet en HTTP 200. Deux fermetures anticipées n’empêchent pas le transfert suivant. Les substitutions de run, pairs, ministères, corpus, candidat non servi, objet absent, expiration et révocation sont refusés. [Contrat](02-api-contract.md#accès-documentaire). |
| Clients et SSE privé | Le SDK OpenAI `3.20.0` et le provider Conversations avec Pydantic AI `2.54.0` créent deux runs `completed`. Le flux SSE complet dure **77,346 s**, avec sept pings ; la coupure à **10,038 s** persiste `cancelled`. [Preuve individuelle](../../evals/evidence/m2_individual_staging_20261126.json). |
| Goldset HTTP déployé | **98/98 réponses HTTP valides**, identiques aux réponses et sources persistées, avec auteur et statut vérifiés. Deux requêtes simultanées au maximum, pendant deux collectes de capture en lecture seule ; médiane **30,99 s**, p95 **63,68 s**. Ces observations ne remplacent pas le test de charge de phase E. Les 98 runs, 676 traces et 192 sources sont supprimés. [Preuve HTTP](../../evals/evidence/m2_http_goldset_candidate_20261126.json). |
| Capture et conformance | Nouvelle capture de **98 cas** sur les images déployées et la nouvelle base staging, avec accès DB en lecture seule et runs en mémoire. Rejeu core ↔ route JSON exact **98/98**, connexions Python et DNS refusés. Le rappel documentaire est **72,51 %**, dans les trois tolérances M1. [Capture et rappel](../../evals/evidence/m2_candidate_capture_20261126.json). |
| Opérabilité D4 | Configuration, prompts, acronymes, corpus documentaire et sections inchangés avant/après ; quatre pods prêts, zéro redémarrage ; contrôle agrégé des journaux sans session, DSN ni URL de stockage privée. [Preuve finale](../../evals/evidence/m2_candidate_operability_20261126.json). |

Les anciennes preuves D4 de concurrence HTTPS, d’erreur contrôlée après en-têtes et de rollback restent applicables au périmètre transport : chat, authentification, télémétrie, dépendances et déploiement sont inchangés depuis `21aa3e3`, et l’AST de finalisation des runs est identique. Le SSE privé, la déconnexion, les SDK et le transfert documentaire ont été rejoués sur la candidate actuelle. **Les trois scénarios D4 précités n’ont pas été rejoués en HTTPS sur cette candidate.** Les mesures D4 ne constituent pas une validation de charge à 50 utilisateurs.

Le goldset et ses références M0a ont été relus uniquement sur l’ancien staging, les tables d’évaluation étant vides sur la nouvelle base. Leur empreinte correspond à M1. La carte d’alias historique figée `72b0fd8…` est identique à celle de M1 et sert au même évaluateur ; la carte du corpus courant `31b5baf…` est conservée séparément dans les empreintes avant/après. Les captures, questions complètes, réponses et extraits restent dans une archive privée locale, hors Git, avec permissions `0600` et manifeste d’intégrité. Aucune référence d’évaluation n’a été importée en staging.

Les comptes Django, groupes, sessions, marqueurs anti-rejeu, runs, feedbacks, audits, documents synthétiques, logins SQL et déclencheur de panne de recette sont nettoyés. Aucun objet SQL temporaire ne reste. Le tunnel et l’ACL préexistants fournis par l’opérateur sont préservés. La [preuve partielle antérieure](../../evals/evidence/m2_api_acceptance_partial_20261126.json) reste historique ; son blocage d’accès opérateur est résolu.

## Reproduire M2

La recette complète utilise les scripts existants et les deux scripts d’intégration suivants. Elle crée des fixtures durables isolées, deux logins SQL temporaires et un trigger de panne limité à une fixture, puis les supprime dans le nettoyage final. Elle contrôle le cluster, la base, le compte et TLS avant ces écritures.

```sh
PYTHON_DOTENV_DISABLED=1 uv run --no-sync python -m scripts.integration.m2_staging_operator \
  --operator-env-file "$STAGING_OPERATOR_ENV" --cloud-env-file "$STAGING_CLOUD_ENV" \
  --kubeconfig "$STAGING_KUBECONFIG" --private-dir "$M2_PRIVATE_DIR"
```

`scripts/integration/m2_individual_staging.py` est exécuté par cet opérateur dans un pod backend Conversations ; son état contient des sessions et reste privé. Les anciennes sondes `m2_delegation_staging.py` et `m2_runtime_roles.py` restent utilisables pour les contrôles sans opérateur ; le probe SQL sous le seul compte API signale toujours une couverture incomplète et ne vaut pas GO.

Le jugement utilise `scripts.conformance.m2_judge judge`, puis `scripts.conformance.m2_gate report --proof docs/evals/evidence/m2_candidate_capture_20261126.json`. Les entrées privées sont liées à la capture `600eb155…` et à l’empreinte `65837656…`. L’inventaire des 98 questions, la rubrique, les trois votes, les empreintes et les tolérances M1 sont contrôlés. Le résultat complet, les votes et le manifeste sont archivés hors Git en mode `0600` ; seuls les agrégats sont publiés.

| Référence M1 | Taux de référence | Écart du taux de validation | Écart du rappel | Résultat |
| --- | --- | --- | --- | --- |
| `legacy_live` | 63,27 % | 6,12 pt | -0,41 pt | Conforme |
| `core_live` | 61,22 % | 8,16 pt | -0,41 pt | Conforme |
| `m0a_rejudged` | 61,22 % | 8,16 pt | 0,08 pt | Conforme |

Aucun report de phase D ne reste ouvert. Les suites sont attribuées dans le LEDGER :

| Suite | Suivi | Responsable | Statut |
| --- | --- | --- | --- |
| Parcours navigateur, ProConnect et intégration | #533 / #595 / #596 | pcasad | Phase E |
| Canary, charge et stabilité | #535 / #536 / #537 | pcasad | Phase E |
| Préflight de production | #364 | pcasad | Avant activation en production |

## Campagnes précédentes — historique

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

Le rapport compte les réponses valides, les échecs de transport et les références attendues effectivement servies. Le rappel des sources servies est une mesure distincte du rappel de retrieval de M1. Le collecteur fournit les réponses et identifiants de run dans `PRIVATE_CAPTURE`. Le jugement des réponses HTTP exige aussi une capture privée des contextes vérifiés avant la purge des runs. La mesure conjointe du jugement et du rappel utilise la capture complète décrite plus bas.

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

Le bras historique `m1_http_staging_judge_20261125` utilise la référence M0a #240,
le juge Scaleway `mistral-medium-3.5-128b` et trois votes selon
`gold-passages-v2`. Il valide **69 réponses sur 98 (70,41 %)** et fournit
294 votes exploitables. Les diagnostics de retrieval sont absents des
entrées du juge à cause de la troncature des traces HTTP. L’écart avec
les scores M1 est donc indicatif.

Pour préparer un jugement à partir du collecteur, fournir `TRANSPORT_REPORT`,
`PRIVATE_CAPTURE` et un JSONL privé `VERIFIED_CONTEXTS` avec une ligne par
`turn_id`. Ce fichier contient `model`, `status` (`completed`), la liste
`contexts` et les empreintes `answer_sha256` et `sources_sha256` calculées
à partir du run persisté. Pour les sources, conserver l’ordre du run et la
projection HTTP `doc_ref`, `title`, `publisher`, `access` et `url`.
Une URL vide devient `null`. Pour chaque valeur, appliquer SHA-256 aux
octets de `json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")`.
Le statut `context_status` vaut
`trace_projection_and_prompt_prefix_matched` après rapprochement des extraits
avec la trace et le préfixe de génération, ou `no_generation_context`
pour une réponse directe avec une liste vide. L’opérateur vérifie les
contextes, et le préparateur du juge contrôle la concordance des réponses
et des sources avec les empreintes du run.

```sh
PYTHON_DOTENV_DISABLED=1 uv run --no-sync python -m scripts.conformance.m2_judge prepare \
  --panel "$GOLDSET_JSONL" --transport-report "$TRANSPORT_REPORT" \
  --private-capture "$PRIVATE_CAPTURE" --verified-contexts "$VERIFIED_CONTEXTS" \
  --env-file "$STAGING_DB_ENV" --output "$JUDGE_DIR"

PYTHON_DOTENV_DISABLED=1 uv run --no-sync python -m scripts.conformance.m2_judge judge \
  --env-file "$M1_JUDGE_ENV" --output "$JUDGE_DIR"
```

Le format historique s’ouvre avec `--campaign`. Son répertoire contient
`aggregate.json`, `events.jsonl` et `campaign-contexts.jsonl`, avec
`m1-http-panel.jsonl` dans le répertoire parent.

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
même verrou de dépendances et les mêmes migrations.

Le 26 novembre, Flux revient au digest OCI du chart précédent et le
déploie en révision Helm **12** avec deux pods API prêts. Le sélecteur
initial est ensuite restauré. Flux redéploie exactement le chart
`0.55.1+78870b6dd828` et le digest d’image de départ en révision
Helm **13**, avec deux pods prêts. Depuis le backend Conversations,
`/healthz` répond HTTP 200 et le catalogue sans délégation HTTP 401
aux trois états. La liste ACL initiale est rétablie.
[Preuve du rollback et de la restauration](../../evals/evidence/m2_d4_staging_rollback_20261126.json)

### Campagne SSE D4 du 26 novembre 2026

| Trajet | Observation |
| --- | --- |
| Service privé API depuis Conversations | Le flux complet dure **175,439 s**. Les 17 commentaires `: ping` arrivent à dix secondes d’intervalle, avec un premier texte à **70,024 s** et un run `completed`. |
| Coupure réseau sur le service privé | Le client ferme la connexion au premier ping à **10,038 s**, avant le marqueur terminal. Le run est persisté `cancelled` avec le motif `disconnect`. |
| Erreur SSE contrôlée | Un processus isolé de l’image candidate écoute en loopback avec une authentification synthétique. Après HTTP 200 et un ping, le flux émet `stream_error`. Le PostgreSQL staging contient un run `failed` sans sources. Le journal contient l’échec, le compteur `failed` s’incrémente et la jauge revient à zéro. |
| Proxy HTTPS Scaleway | Les trois flux se chevauchent **122,143 s**. Chaque client reçoit un `data-keepalive` de Conversations après **55,098 à 55,161 s**, puis le premier texte après **59,051 à 64,052 s**. Aucun commentaire `: ping` de l’API n’est observé sur ce trajet. Les flux se terminent en **122,143 à 148,608 s** et les trois runs sont persistés `completed`. |

Le load balancer et Traefik configurent des délais de **300 s**. Le pic
observé pendant l’essai est de **115 MiB** sur un pod API (limite
**1 GiB**) et **1 487 MiB** sur un pod Conversations (limite **2 GiB**).
Le test HTTPS couvre trois flux simultanés, avec une durée maximale de
**148,608 s**. Les valeurs de **300 s** pour les délais et de **50 flux
par pod API** pour la concurrence proviennent de la configuration. Les
pods API créés au retour de rollback atteignent l’état prêt en **6 et 7 s**. Après les essais, les deux pods
sont prêts, sans redémarrage, et sans flux actif au relevé.

Conversations obtient encore HTTP 200 sur `/healthz` et HTTP 401 sur
le catalogue sans délégation. Les comptes, runs et traces synthétiques
sont purgés et l’ACL initiale est rétablie.
[Preuves SSE #531](../../evals/evidence/m2_d4_staging_sse_20261126.json)

1. Relever les révisions de chart, les digests démarrés des deux applications et l’état Flux. Vérifier les migrations et les rôles #599 avant le trafic de recette.
2. Depuis le backend Conversations, vérifier la résolution du Service privé API et `/healthz`. Via l’entrée staging, relever les horaires SSE du premier en-tête, des pings et du premier texte sur un flux long. Relever mémoire, redémarrages et limites sur les workloads.
3. Sur des requêtes de recette, corréler succès, erreur après 200 et annulation à leur statut persisté, aux logs et à `assistant_rh_sse_streams_total` par `outcome`. Après une annulation, vérifier la descente de `assistant_rh_sse_active` et la disponibilité du service.
4. Pour éprouver le rollback, relever le dernier chart et les digests sains, avec leur compatibilité de schéma. Épingler cette version du chart dans la configuration Flux, attendre l’état `Ready` et vérifier les digests des pods. Rejouer santé, SSE, document et feedback. Restaurer ensuite la sélection de la candidate par Flux et consigner les deux reconciliations. La preuve attendue inclut les révisions réellement observées et les résultats des smokes.

## Recette de l’intégration #533

Employer deux comptes de recette du même groupe, avec droits limités au même ministère. Après connexion, vérifier le catalogue et poser deux questions successives en SSE. Ouvrir une source publique et un PDF privé, enregistrer un feedback, recharger, se reconnecter et modifier le feedback. Vérifier que chacun des messages retrouve le bon tour et les bonnes sources. Vérifier aussi un ancien message sans association et un fichier indisponible.

Pour chaque nouvelle ouverture et modification, vérifier les refus au second compte, après expiration de session et après retrait du droit. Vérifier que le rétablissement restaure l’historique attendu. Contrôler l’absence de bearer API et d’URL S3 dans les échanges navigateur et relever les en-têtes du PDF. Le contrat actuel répond au `Range` avec le document entier et `Accept-Ranges: none`. Rejouer une lecture longue interrompue, une indisponibilité et un retry. Rattacher les résultats aux révisions de l’API, du backend et du frontend effectivement testées.
