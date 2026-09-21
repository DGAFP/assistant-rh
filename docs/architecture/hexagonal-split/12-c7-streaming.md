# C7 — Chat Completions SSE résilient

17 septembre 2026 · [#464](https://github.com/DGAFP/assistant-rh/issues/464)
· base `67fe730` · [contrat C1](09-chat-completions-contract.md)
· [carte A5](07-runtime-isolation-audit.md#carte-dentrée-c7--streaming-2026-09-17-67fe730).

## Exécution et bornes

`POST /v1/chat/completions` accepte désormais `stream=true`. La même validation
C1 et les mêmes résolutions auth/modèle précèdent l'admission et les headers.
L'attente de retrieval/configuration/génération ne bloque pas les pings SSE.

C6 a déjà extrait un pipeline **async**, avec HTTPX et PostgreSQL async. Le
« worker synchrone » prévu avant cette extraction devient donc un worker
`asyncio` borné, sur la boucle propriétaire des pools. Aucun runtime legacy,
thread supplémentaire ou client lié à une autre boucle n'est introduit.

| Borne par processus API | Valeur par défaut | Comportement |
|---|---|---|
| Workers / réponses actives | `API_STREAM_WORKERS=8` | Admission immédiate ; saturation → 503 `service_unavailable` avant headers, sans exécution ni file d'attente de requêtes |
| Événements en file par réponse | `API_STREAM_QUEUE_SIZE=32` | `await queue.put` jusqu'au provider ; événements de toutes les étapes C6, sans tâche de publication détachée |
| Fragment de texte | 4 096 caractères | Gros deltas découpés avant mise en file ; nombre **et** taille des éléments bornés |
| Ping | 10 secondes | Commentaire `: ping`, indépendant des deltas et événements d'étape |
| Envoi ASGI | 30 secondes par envoi | Client bloqué → annulation et finalisation ; aucun worker abandonné |
| Finalisation DB | 10 secondes par tentative | Transaction protégée contre la déconnexion et les annulations répétées ; erreur contrôlée à l'expiration |
| Arrêt Uvicorn | 5 secondes d'attente des requêtes | Annulation des requêtes encore ouvertes, puis attente du nettoyage dans le lifespan |

Les bornes existantes B3 sur les réponses providers et les délais HTTP/DB
restent applicables. La limite de workers est par processus ; un déploiement
multiprocessus doit tenir compte du produit workers × processus.

## Succès, erreur et fermeture

La réponse envoie le rôle assistant, les deltas, puis les sources finales en
markdown. La transaction atomique run/sources/traces **précède** le bloc sources,
le terminal `finish_reason=stop` avec `x_assistant_rh`, l'usage optionnel et
l'unique `[DONE]`. Les gateways demandent désormais l'usage au provider ;
l'absence de compteurs reste représentée par les zéros contractuels.

Un échec après headers émet l'enveloppe `stream_error`, ferme le flux et ne
produit ni terminal de succès ni `[DONE]`. Le run `failed` conserve le texte
partiel, les étapes et les diagnostics de tentatives, sans source autorisante.
Si la persistance échoue aussi, le log contrôlé contient le `turn_id`, sans
exception brute, DSN ou bearer ; aucune persistance n'est alors promise.

Une déconnexion annule le travail encore annulable et ferme le stream provider.
La finalisation du run `cancelled` est attendue, même sous annulations répétées.
Si le commit de succès avait déjà commencé, il se termine : un run déjà commité
n'est ni réécrit ni doublé par un run annulé. Cela ne promet pas que le client
absent reçoive la fin du flux. L'arrêt applicatif joint également le transport,
même si l'envoi était bloqué, avant la fermeture des ressources DB/provider.

Uvicorn attend les requêtes **avant** d'appeler le shutdown du lifespan. Le point
d'entrée `assistant-rh-api` borne cette attente à cinq secondes pour atteindre
le nettoyage même avec un client SSE toujours connecté. Aucun gestionnaire de
signal ni serveur personnalisé n'est ajouté. Compose laisse 150 secondes avant
SIGKILL : attente Uvicorn, fermeture provider pouvant durer 120 secondes,
finalisation DB et marge. Un lancement Uvicorn externe doit également fixer
`--timeout-graceful-shutdown 5` et laisser ce budget au processus.

Les tests TCP utilisent la configuration du point d'entrée et demandent l'arrêt
du serveur pendant que le client continue de lire. Ils vérifient l'annulation
en génération et la préservation d'un commit déjà commencé, avec une
persistance retenue volontairement jusqu'à la vérification de l'attente d'arrêt.

## Historique : arbitrage C1 / C5

Le générateur C5 conserve sa capacité autonome de recevoir un historique en
stream, héritée du runtime Streamlit. Pour **l'API**, C1 impose les mêmes entrées
aux deux transports : C7 passe donc au générateur les mêmes entrées que C6,
sans ajouter un historique uniquement en mode stream. Le query processor
continue de recevoir les cinq couples complets validés par C1. Un test compare
les requêtes de toutes les étapes, le texte final et les sources dans les deux
modes avec des ports déterministes. Le comportement du runtime Streamlit reste
inchangé ; aucune égalité de réponses stochastiques live n'est inférée.

La validation C6 qui refusait temporairement le booléen `stream=true` est
remplacée par les tests positifs SDK/transport. Les refus de types invalides,
les limites et les autres erreurs C1 restent vérifiés.

## Preuves reproductibles

### Validation initiale du 17 septembre 2026

Exécution locale sur la VM homelab `assistant-rh.discus-iguana.ts.net` : tests
ASGI en mémoire, transactions PostgreSQL synthétiques, sockets Uvicorn sur
`127.0.0.1`, puis instance Django `conversations`. Il ne s'agit pas d'une seconde
machine indépendante. Aucun provider live, base distante ou déploiement.

- SDK dépôt **OpenAI 2.38.0**, FastAPI **0.136.3**, Uvicorn **0.48.0**.
- Client A2 épinglé : `conversations` **0.0.22**, checkout
  `1bba2f0e444ae9c2ddb3eae68c665b63ee4a195e`, Pydantic-AI **2.22.0**,
  OpenAI **2.52.0** ; image `conversations:backend-development` existante.
- Suite API : **956 passed, aucun ignoré** ; tests de stream, usage,
  pings en retrieval, admission, backpressure, erreur post-headers, panne de
  commit, double panne, annulation répétée, déconnexion et arrêt avec envoi
  bloqué. PostgreSQL vérifie la lisibilité du run **dans le callback d'envoi du
  terminal**, le rollback et l'absence de droits documentaires des runs partiels.
- Suite historique : **1 548 passed, 46 skipped**.
- Instance `conversations` : **2 passed** ; le client affiche et persiste la
  réponse et ses sources, conserve le `chatcmpl-*`, n'expose pas le bearer et
  propage `openai.APIError(code="stream_error")` sur erreur post-headers.
- Ruff, mypy et les quatre contrats d'import sont vérifiés.

### Revue et validation complémentaire du 21 septembre 2026

Code validé : `21a424586d8fefac67460cd531021e7f8bb134ff`. Le transport sépare
maintenant la lecture des événements, la terminaison réussie et l'envoi d'une
erreur. L'attente protégée du nettoyage est mutualisée et la propriété des
tâches est documentée. La saturation et l'arrêt utilisent `StreamUnavailable`,
une erreur de transport, au lieu d'une erreur d'accès à la base. Les garanties
d'annulation et de persistance restent vérifiées.

- **958 tests API distincts validés**, sur une base PostgreSQL synthétique
  locale séparée du corpus. Première exécution : 953 réussis, un échec et
  quatre erreurs dus au DSN libpq refusé par SQLAlchemy. Après correction de
  la configuration locale en URI PostgreSQL, les six tests du fichier
  `test_legacy_feedback_coexistence.py` passent, dont un déjà réussi.
- **1 548 tests historiques réussis, 46 ignorés**, et **2 tests Conversations
  réussis**, avec le même checkout A2 épinglé. La sonde confirme les runs
  `completed`/`failed` finalisés et l'absence de worker de stream actif.
- **178 tests ciblés** passent après la simplification du transport ; Ruff,
  mypy (87 fichiers), les quatre contrats d'import et les CI Tests/CodeQL du
  commit passent aussi.

Un snapshot cohérent du seul corpus RAG de staging a ensuite été restauré sur
PostgreSQL local : **4 882 documents, 10 397 sections, 25 890 chunks**, avec
embeddings, configuration, prompts et acronymes. Les comptes, conversations,
feedbacks et logs de staging sont exclus. Les douze comptages du corpus sont
inchangés après les tests. Archive, configuration et lanceur local restent
hors Git ; le conteneur RAG et son volume sont conservés pour les essais suivants.

Avec les appels Albert/Scaleway autorisés, un test réel de question fictive
traverse les **sept étapes** du pipeline et le transport TCP/SSE : réponse de
842 caractères, une source, deux pings, **27,7 secondes**. Le run `completed`
et ses sources sont lisibles en base locale avant `[DONE]` ; le texte persisté
correspond au texte reçu. La session de test est révoquée à la fin.

Ce test vérifie l'intégration. Il ne remplace pas la comparaison goldset M1 :
le pgvector local est en **0.8.6**, staging en **0.8.2**, et les index ANN ont
été reconstruits. Aucune identité des plans ou classements n'est déduite.
Le proxy Scaleway reste à valider en D4. La baseline M0
([#439](https://github.com/DGAFP/assistant-rh/issues/439), close) fournit la
référence ; le GO de parité relève de M1
([#465](https://github.com/DGAFP/assistant-rh/issues/465)).

### Reproduire les tests synthétiques

Après `uv sync --all-packages --group dev`, lancer un PostgreSQL synthétique
local pour les tests API :

```bash
docker run -d --rm --name assistant-rh-c7-postgres \
  -p 127.0.0.1:55464:5432 \
  -e POSTGRES_USER=assistant_rh_api -e POSTGRES_PASSWORD=assistant_rh_api \
  -e POSTGRES_DB=assistant_rh_api_test pgvector/pgvector:pg17
API_SYNTHETIC_POSTGRES_DSN='postgresql://assistant_rh_api:assistant_rh_api@127.0.0.1:55464/assistant_rh_api_test?sslmode=disable' \
  uv run --no-sync python -m pytest apps/api/tests -q
uv run --no-sync python -m pytest tests --ignore=tests/archive -q
```

Pour l'instance Django, les settings `Test` épinglés imposent `dinum/pass` et la
base `conversations`. Utiliser un conteneur distinct, entièrement jetable :

```bash
docker run -d --rm --name assistant-rh-c7-conversations-postgres \
  -p 127.0.0.1:55465:5432 \
  -e POSTGRES_USER=dinum -e POSTGRES_PASSWORD=pass \
  -e POSTGRES_DB=conversations postgres:16
PYTHONPATH=. uv run --no-sync python scripts/probe_c7_conversations.py \
  --checkout /tmp/conversations-a2 --db-port 55465
docker stop assistant-rh-c7-postgres assistant-rh-c7-conversations-postgres
```

Le script lance la vraie API et ses étapes C2–C7, avec ports synthétiques
mémoire, et un bearer éphémère généré par B4 ; ce n'est pas le replay A2.
La preuve transactionnelle avec la vraie DB est séparée dans
`apps/api/tests/db/test_chat_stream_persistence.py`. L'image client doit avoir
été construite selon le [runbook A2](07-openai-client-spike.md).
Les avertissements Django de teardown/cache du client épinglé ne changent pas
les assertions ; les conteneurs DB sont supprimés après preuve.

Le buffering/proxy de la plateforme reste D4 ; le GO M1 et la qualité goldset
restent #465. L'adaptation du frontend `conversations` pour afficher l'erreur
OpenAI sous forme d'erreur UI reste au fork, comme en A2.
