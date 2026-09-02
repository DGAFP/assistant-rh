# Spike A2 — compatibilité SDK OpenAI et `conversations`

> Date : 2026-09-02 · issue [#443](https://github.com/DGAFP/assistant-rh/issues/443) · contrat amendé dans [02-api-contract.md](02-api-contract.md).

## Verdict

Le transport retenu fonctionne avec le SDK OpenAI en non-stream et stream, et avec la couche provider réellement utilisée par `suitenumerique/conversations`. Les sources markdown sont conservées par les deux clients. Le champ `x_assistant_rh` ne casse aucun client : le SDK le rend accessible dans `model_extra`, tandis que Pydantic-AI le tolère puis l'abandonne.

Trois écarts importants ont été trouvés dans le contrat initial de la PR #418 :

1. `conversations` ne découvre pas les modèles du provider via `GET /v1/models`; son catalogue est statique dans `LLM_CONFIGURATION_FILE_PATH` ;
2. il envoie toujours ses instructions `system`, un outil `self_documentation`, les champs associés aux tools et `stream_options.include_usage`, même lorsque le provider est déjà un assistant RAG terminal.
3. un message texte peut arriver sous forme de liste de parts OpenAI `{type: "text", text: "…"}` plutôt que comme une simple chaîne.

Le contrat accepte désormais ces champs et les ignore explicitement. Il conserve la propriété des prompts, ne renvoie aucun `tool_call`, et ajoute le chunk d'usage attendu avant `[DONE]`.

Le spike local de la couche provider est vert. L'instance Django réelle, construite depuis la révision épinglée, passe également le test monté [conversations-instance-test.py](../../../tests/openai-contract/conversations-instance-test.py) : son endpoint authentifié liste les modèles, son endpoint de conversation consomme le SSE, restitue et persiste les sources markdown, et conserve l'id provider `chatcmpl-*`. Le test vérifie explicitement que le bearer backend est absent du catalogue renvoyé au navigateur, du flux SSE et des messages persistés. Il verrouille aussi la propagation actuelle de `openai.APIError` après headers.

Le même replay SDK et le test de l'instance ont été rejoués avec succès le 2026-09-02 directement sur la VM homelab `assistant-rh.discus-iguana.ts.net` (`13 passed` et `2 passed`). Aucun reverse proxy API n'existe encore à ce stade du chantier : buffering et borne HTTP du proxy restent donc, comme prévu, une validation D4 au premier déploiement dark.

## Versions éprouvées

| Composant | Version / révision | Raison |
|---|---|---|
| Assistant RH | `dev` `013a236` | baseline finale de l'issue, après squelette et stack locale API |
| SDK OpenAI du dépôt | `openai==2.38.0` | version verrouillée dans `uv.lock` |
| `suitenumerique/conversations` | `1bba2f0e444ae9c2ddb3eae68c665b63ee4a195e` (`0.0.22`) | tête de `main` testée le 2026-09-01 |
| Pydantic-AI de `conversations` | `pydantic-ai-slim==2.22.0` | version verrouillée par le client |
| SDK OpenAI de `conversations` | `openai==2.52.0` | version verrouillée par le client |

La référence de forme des chunks et de `stream_options.include_usage` est la [documentation officielle OpenAI Chat Completions](https://developers.openai.com/api/reference/cli/resources/chat/subresources/completions).

## Matrice de résultats

| Cas | SDK OpenAI | Provider `conversations` | Décision |
|---|---|---|---|
| `GET /v1/models` | Consommé, objets supplémentaires tolérés | Non appelé | garder la route pour le SDK ; config statique contrôlée au déploiement de `conversations` |
| Completion non-stream | Consommée | Consommée par la couche provider lorsque `supports_streaming=false` | enveloppe Chat Completions standard |
| Completion stream | Consommée | Consommée par l'instance Django | chunks `chat.completion.chunk`, puis usage optionnel et `[DONE]` |
| Ping `: ping` | Ignoré | Ignoré | commentaire SSE toutes les ~10 s pendant le retrieval |
| `x_assistant_rh` inconnu | Disponible dans `model_extra` | Toléré, non exposé à l'UI | extension conservée pour clients dédiés |
| Sources markdown | Conservées | Conservées et rendables | chemin universel v1 |
| Erreur JSON avant headers | Exceptions typées par statut | Pydantic-AI sait mapper les erreurs HTTP usuelles | format OpenAI `{error:{...}}` |
| Erreur après headers | `openai.APIError`, code `stream_error` | même `openai.APIError` remonte de Pydantic-AI | événement OpenAI retenu ; adaptation UI requise dans le fork |
| Déconnexion | `Stream.close()` ferme la socket ; replay la détecte | annulation du flux async disponible | C7 doit persister `cancelled` et annuler les appels encore annulables |
| Feedback | l'id `chatcmpl-*` est disponible | l'id provider est persisté, mais le bouton actuel note un id UI `trace-*` dans Langfuse | association UI → completion et appel serveur à `/v1/feedback` à ajouter au fork |

## Requêtes réellement émises par `conversations`

Sur un tour texte sans pièce jointe, Pydantic-AI envoie :

- le modèle statique configuré, par exemple `assistant-rh-matte` ;
- une ou plusieurs instructions `system` et le dernier message `user` ;
- `stream=true` et `stream_options.include_usage=true` sur le chemin normal ;
- `tools`, `tool_choice` et `parallel_tool_calls` pour l'outil `self_documentation` ajouté par le client.

L'API accepte ces champs mais n'exécute pas les tools du client. Les contenus v1 restent du texte, sous forme de chaîne ou de liste de parts `text` concaténées ; il faut configurer `supports_image=false` dans `conversations`. Les rôles `tool` et parts image/audio sont rejetés en 422.

L'historique côté `conversations` peut dépasser cinq tours et contient ses propres instructions. L'API reste stateless, retire `system`/`developer`, supprime le bloc de sources de ses anciennes réponses, puis garde les cinq derniers couples user/assistant complets.

## Limites arrêtées pour C1

| Limite | Valeur | Réponse |
|---|---:|---|
| Body HTTP total | 1 048 576 octets (1 Mio) | 413 `request_too_large` |
| Nombre de messages | 32 | 422 `too_many_messages` |
| `content` par message | 65 536 octets UTF-8 (64 Kio) | 422 `content_too_large` |
| Historique transmis au core | 5 tours complets précédents | messages plus anciens ignorés |

Les bornes couvrent la fenêtre métier, les instructions et la définition d'outil ajoutées par `conversations`. D4 devra confirmer que le proxy Scaleway autorise au moins 1 Mio et ne bufferise pas les pings ; si sa borne est inférieure, le contrat et FastAPI seront abaissés ensemble.

## Erreurs SSE retenues

Avant headers, le statut HTTP et le body OpenAI portent l'erreur. Après un premier octet SSE, le statut ne peut plus changer :

```text
data: {"error":{"message":"Service momentanément indisponible","type":"server_error","code":"stream_error"}}

```

Le serveur ferme ensuite le flux sans `[DONE]`. Il ne met dans `message` ni exception interne, ni DSN, ni prompt, ni bearer. Le SDK OpenAI lève `APIError(code="stream_error")`.

À la révision testée, le wrapper de `conversations` intercepte les `ModelHTTPError` et `ModelAPIError`, mais Pydantic-AI laisse ici remonter `openai.APIError`. Le fork devra intercepter cette classe et produire son `ErrorPart(error="model_connection_error")`; sans ce patch, l'UI reçoit une fin de flux générique. Ce défaut client n'impose pas un événement SSE propriétaire à l'API.

## Modèles, bearer et feedback

`conversations` lit le bearer depuis l'environnement de son backend. Le fichier de configuration reproductible [conversations-llm.issue443.json](../../../tests/openai-contract/conversations-llm.issue443.json) ne contient qu'une référence `environ.ASSISTANT_RH_CONVERSATIONS_API_KEY`; aucune valeur n'est versionnée ou affichée dans le rapport. Le test de l'instance utilise un sentinel éphémère et affirme son absence dans la réponse du catalogue destinée au navigateur, le flux SSE, le message UI persisté et l'historique provider persisté.

Le catalogue `conversations` étant statique, son déploiement doit valider au démarrage que chaque `model_name` actif figure dans `/v1/models` pour le bearer. Une réponse 403 reste nécessaire si un modèle ministériel connu est hors `allowed_ministries`; 404 est réservé à un modèle inconnu.

Pour le feedback futur, l'id fournisseur `chatcmpl-<turn_id>` doit être associé au message UI au moment de finaliser le stream. Le bouton envoie ensuite `rating=up|down` au backend `conversations`; ce backend appelle `/v1/feedback` avec son bearer. La clé n'est jamais confiée au code frontend.

## Reproduction locale

Le replay ne contient aucun RAG et ne journalise ni headers, ni contenus complets. Dans un premier terminal :

```bash
export ASSISTANT_RH_CONTRACT_API_KEY="$(openssl rand -hex 24)"
uv run python scripts/openai_contract_probe.py serve
```

Dans un deuxième terminal, avec la même variable :

```bash
uv run python scripts/openai_contract_probe.py probe \
  --base-url http://127.0.0.1:8765/v1 \
  --replay-errors \
  --output tests/conformance/reports/openai-contract-sdk.json
```

Pour la couche provider exacte du client, sans modifier les dépendances de production :

```bash
uv venv --python 3.12 /tmp/assistant-rh-a2-client
uv pip install --python /tmp/assistant-rh-a2-client/bin/python \
  'pydantic-ai-slim[openai]==2.22.0' 'openai==2.52.0'
PYTHONPATH=. /tmp/assistant-rh-a2-client/bin/python \
  scripts/openai_contract_probe.py probe \
  --base-url http://127.0.0.1:8765/v1 \
  --replay-errors \
  --conversations-client \
  --output tests/conformance/reports/openai-contract-conversations-provider.json
```

Les rapports sont gitignorés. Ils ne contiennent jamais la variable de bearer.

Le test automatisé du SDK et du replay est :

```bash
uv run python -m pytest tests/test_openai_contract_probe.py -v
```

Résultat local du 2026-09-02 : `13 passed`. La preuve couvre notamment le framing SSE brut (pings, chunk d'usage, `[DONE]` uniquement en succès), en plus de la consommation stream/non-stream par le SDK et des gardes contre les réponses ou historiques mal formés.

## Instance `conversations` locale — preuve validée

Cloner la révision épinglée puis monter le JSON fourni dans le backend :

```bash
git clone https://github.com/suitenumerique/conversations.git /tmp/conversations-a2
git -C /tmp/conversations-a2 checkout 1bba2f0e444ae9c2ddb3eae68c665b63ee4a195e
```

Variables backend à injecter, jamais côté frontend :

```text
LLM_CONFIGURATION_FILE_PATH=/run/assistant-rh/conversations-llm.issue443.json
LLM_DEFAULT_MODEL_HRID=assistant-rh-matte
LLM_SUMMARIZATION_MODEL_HRID=assistant-rh-summarization
ASSISTANT_RH_CONVERSATIONS_BASE_URL=http://host.docker.internal:8765/v1
ASSISTANT_RH_CONVERSATIONS_API_KEY=<même secret que le replay>
```

Pour un replay sur l'hôte consommé depuis Docker, lancer `serve --host 0.0.0.0` et ne publier le port que sur la machine de test. Construire l'image épinglée puis monter les deux fichiers du dépôt Assistant RH :

```bash
docker build --network host --target backend-development \
  --build-arg DOCKER_USER="$(id -u)" \
  -t conversations:backend-development /tmp/conversations-a2

make -C /tmp/conversations-a2 pre-bootstrap
docker compose -f /tmp/conversations-a2/compose.yml up -d postgresql

docker run --rm --network host --entrypoint python \
  --env-file /tmp/conversations-a2/env.d/test \
  -e DJANGO_CONFIGURATION=Test \
  -e DJANGO_SETTINGS_MODULE=conversations.settings \
  -e DB_HOST=127.0.0.1 -e DB_PORT=15432 \
  -e LLM_CONFIGURATION_FILE_PATH=/run/assistant-rh/conversations-llm.issue443.json \
  -e LLM_DEFAULT_MODEL_HRID=assistant-rh-matte \
  -e LLM_SUMMARIZATION_MODEL_HRID=assistant-rh-summarization \
  -e ASSISTANT_RH_CONVERSATIONS_BASE_URL=http://127.0.0.1:8765/v1 \
  -e ASSISTANT_RH_CONVERSATIONS_API_KEY \
  -v "$PWD/tests/openai-contract/conversations-llm.issue443.json:/run/assistant-rh/conversations-llm.issue443.json:ro" \
  -v "$PWD/tests/openai-contract/conversations-instance-test.py:/app/chat/tests/views/chat/conversations/test_issue443_live.py:ro" \
  conversations:backend-development \
  -m pytest /app/chat/tests/views/chat/conversations/test_issue443_live.py -q --no-cov
```

Résultat local du 2026-09-02 : `2 passed`. Le premier test couvre catalogue, stream, sources, id de completion et non-exposition du bearer dans les réponses ou messages persistés ; le second prouve que l'erreur post-headers remonte encore sous forme `openai.APIError`, résultat attendu tant que le catch du fork n'est pas livré.

## Homelab — preuve validée

L'environnement d'exécution du 2026-09-02 était la VM elle-même (`hostname -f` → `assistant-rh.discus-iguana.ts.net`) ; la tentative SSH vers l'alias local n'était donc pas nécessaire. Le replay a été exposé uniquement sur le port de test de la VM avec un bearer aléatoire éphémère, puis consommé par le SDK du dépôt et par l'image `conversations:backend-development` épinglée.

Résultats : matrice SDK/replay complète verte (`13 passed`), framing SSE brut conforme, instance Django `2 passed`. Le test de l'instance confirme que son bearer sentinel reste absent du catalogue navigateur, du SSE et des messages persistés. Le conteneur PostgreSQL et le réseau Docker éphémères ont été supprimés après le test. Aucun `.env`, header, log debug HTTP ou bearer n'a été archivé. La conversion de `openai.APIError` en `model_connection_error` reste un changement du fork avant son intégration finale ; elle n'altère pas le contrat serveur validé par A2.
