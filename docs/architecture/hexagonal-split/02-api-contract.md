# Contrat API v1

> Référence : [décisions D1, D2, D6, D11 et D15 à D18](06-decisions.md). Périmètre Streamlit : [arbitrage A3](08-streamlit-api-parity.md). Diagrammes : [03-sequence-diagrams.md](03-sequence-diagrams.md).

## Conventions générales

- Base : `https://<host>` ; toutes les réponses en JSON UTF-8.
- **Auth publique** : `Authorization: Bearer <session opaque>`. `POST /v1/auth/session` émet après vérification du mot de passe une session bornée au groupe, à sa politique ministère et à huit heures. La route de rédemption documentaire `GET /v1/documents/access/{capability}` utilise sa capability courte à la place du bearer afin de permettre une navigation directe du navigateur.
- **Conservation du secret** : le bearer de session reste côté serveur du frontend. Il n'entre ni dans le cookie Streamlit, ni dans une URL, ni dans un log. Il n'existe pas de bundle statique de bearers par groupe.
- **Admin hors v1** : les fonctions admin restent dans Streamlit avec accès DB direct sous l'exception A3. Aucun endpoint `/admin/*` n'est requis par ce contrat.
- **Erreurs** : format OpenAI sur `/v1/*` :

```json
{ "error": { "message": "Invalid API key", "type": "invalid_request_error", "code": "invalid_api_key" } }
```

| HTTP | Cas |
|---|---|
| 401 | token absent/invalide |
| 403 | modèle demandé hors `allowed_ministries` |
| 404 | modèle inconnu, `completion_id`/ressource inexistante |
| 422 | body invalide (validation pydantic) |
| 413 | body HTTP supérieur à 1 Mio, rejeté avant lecture et avant démarrage du stream |
| 429 | tentatives de vérification de mot de passe trop nombreuses |
| 500 | erreur survenue avant le démarrage d'une réponse non-stream ou SSE |

- **Modèles exposés** : `assistant-rh-<ministère>` pour chaque id du catalogue (`matte`, `mso`, `mi`, `masa`) présent dans `allowed_ministries` du token. Le nom générique `assistant-rh` est accepté en entrée et résolu sur `default_ministry`.
- **Compatibilité assumée** : le sous-ensemble exact de Chat Completions supporté est figé par des tests contre le SDK OpenAI et une instance de `conversations` pendant le spike A2. Toute extension non documentée reste rejetée ou ignorée explicitement.

---

## Surface publique

### Authentification du produit Streamlit

`GET /v1/auth/groups` liste uniquement les métadonnées d'affichage des groupes visibles, non-admin et utilisables pour le login, ordonnées par priorité puis slug :

```json
{
  "data": [
    { "slug": "dgafp-beta", "label": "DGAFP Beta", "icon": "🏛️", "color": "#0053b3" }
  ]
}
```

`POST /v1/auth/session` vérifie le mot de passe :

```json
{ "slug": "dgafp-beta", "password": "…" }
```

La réponse 200 crée une session non renouvelable de huit heures :

```json
{
  "access_token": "<opaque>",
  "token_type": "bearer",
  "expires_in": 28800,
  "expires_at": "2026-09-04T22:00:00Z",
  "group": {
    "slug": "dgafp-beta",
    "allowed_ministries": ["matte"],
    "default_ministry": "matte",
    "credential_revision": 3
  }
}
```

L'erreur 401 est identique pour un groupe absent et un mot de passe faux. La route est rate-limitée par IP + slug et n'enregistre jamais le mot de passe.

`GET /v1/auth/me`, avec bearer, retourne l'objet `group`, `expires_at` et le nombre de secondes restantes, jamais le token lui-même.

Le bearer retourné est conservé uniquement côté serveur Streamlit. Le cookie navigateur ne contient que les données non secrètes nécessaires au parcours produit. La session n'est pas renouvelée silencieusement : son expiration ou un reset de mot de passe impose une nouvelle authentification. Le registre de données et permissions est figé par [A3](08-streamlit-api-parity.md#registre-des-endpoints-publics).

### `GET /v1/models`

Liste les modèles accessibles au token.

**Réponse 200**

```json
{
  "object": "list",
  "data": [
    { "id": "assistant-rh-matte", "object": "model", "created": 1755734400, "owned_by": "assistant-rh" },
    { "id": "assistant-rh-mso",   "object": "model", "created": 1755734400, "owned_by": "assistant-rh" }
  ]
}
```

### `POST /v1/chat/completions`

Une réponse RAG complète (retrieval + génération) sur le corpus du ministère routé par `model`.

**Requête**

```json
{
  "model": "assistant-rh-matte",
  "messages": [
    { "role": "user", "content": "Comment poser un congé de formation ?" },
    { "role": "assistant", "content": "…réponse précédente…" },
    { "role": "user", "content": "Et pour un agent contractuel ?" }
  ],
  "stream": true
}
```

Règles de mapping :

- Le **dernier message `user`** est la question. Le serveur conserve au maximum les **5 derniers tours complets** précédents (10 messages user/assistant), comme le Streamlit historique ; les messages plus anciens sont ignorés de façon déterministe. Contrat **stateless** : le client peut renvoyer tout l'historique à chaque appel, mais la politique de fenêtre appartient au serveur.
- Les messages `system` et `developer` du client sont **acceptés et ignorés** (les prompts système sont la propriété du pipeline — c'est la boucle qualité). `conversations` en envoie plusieurs à chaque tour. `content` accepte une chaîne ou une liste OpenAI composée uniquement de parts `{ "type": "text", "text": "…" }`, concaténées dans l'ordre ; `conversations` utilise cette seconde forme même pour certains tours texte. Les rôles `tool` et les parts image/audio ne font pas partie de la v1 et produisent une 422.
- Le serveur retire de l'historique les blocs de sources qu'il a lui-même ajoutés aux réponses précédentes, grâce à un marqueur interne stable, avant de passer l'historique au core.
- `temperature`, `top_p`, `max_tokens`, `n`, `user` : acceptés et ignorés en v1 (la config générateur vient de `rag_config`). `n > 1` → 422.
- `tools`, `tool_choice` et `parallel_tool_calls` sont acceptés et ignorés en v1. C'est nécessaire parce que `conversations` 0.0.22 déclare toujours son outil `self_documentation`; l'API Assistant RH reste pourtant une completion RAG terminale et ne renvoie jamais de `tool_calls`.
- `stream_options.include_usage` est accepté pour `stream=true`. Si sa valeur est vraie, un chunk final `choices: []` porte `usage`, conformément au SDK OpenAI. Les autres options de stream sont rejetées en v1.
- Champ d'extension optionnel `metadata.conversation_id` (corrélation côté client, logué dans `chat_runs` comme aujourd'hui).
- Limites arrêtées par A2 : body HTTP ≤ **1 Mio** (`Content-Length`, dépassement → 413), ≤ **32 messages**, chaque `content` texte ≤ **64 Kio UTF-8** (dépassement → 422). Elles laissent de la marge aux 10 messages d'historique et aux instructions/outils ajoutés par `conversations`, tout en restant sous les limites usuelles de proxy. C1 les applique avant tout démarrage de stream ; D4 revalide que le proxy Scaleway n'impose pas une borne inférieure.

**Réponse 200 (non-stream, `stream` absent ou `false`)**

```json
{
  "id": "chatcmpl-<turn_id>",
  "object": "chat.completion",
  "created": 1755734400,
  "model": "assistant-rh-matte",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "Réponse rédigée…\n\n---\n**Sources :**\n1. Titre du document interne — MATTE\n2. [Titre public](https://…) — Service-Public"
      },
      "finish_reason": "stop"
    }
  ],
  "usage": { "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0 },
  "x_assistant_rh": {
    "turn_id": "<turn_id>",
    "ministry": "matte",
    "sources": [
      { "title": "…", "url": null, "publisher": "MATTE", "doc_ref": "…", "access": "authenticated" }
    ]
  }
}
```

- `id` : `chatcmpl-` + le `turn_id` du `chat_run` (clé de corrélation feedback et logs).
- Les **sources** sont livrées deux fois : bloc markdown en fin de `content` et champ d'extension `x_assistant_rh.sources`. Le SDK OpenAI conserve l'extension dans `model_extra`, mais `conversations`/Pydantic-AI ne la transforme pas encore en panneau de sources. Une source publique garde donc son URL canonique dans le markdown ; une source interne reste sans URL durable et le fork doit utiliser l'extension pour demander une URL courte au clic. Un client générique peut afficher sa référence sans la rendre cliquable.
- `usage` : renseigné si le gateway LLM le fournit, sinon zéros (v1).
- Question hors périmètre (gate du query processor) : réponse 200 normale dont le contenu est le message de refus du pipeline — jamais une erreur HTTP.

**Réponse 200 (stream, SSE `text/event-stream`)**

Chunks conformes OpenAI ; keep-alive SSE `: ping` toutes les ~10 s pendant le retrieval, puis deltas de génération et bloc sources. Le `chat_run` et ses traces sont finalisés **avant** le chunk terminal et `[DONE]` :

```text
data: {"id":"chatcmpl-<turn_id>","object":"chat.completion.chunk","created":…,"model":"assistant-rh-matte","choices":[{"index":0,"delta":{"role":"assistant","content":""},"finish_reason":null}]}

data: {"id":"chatcmpl-<turn_id>", …, "choices":[{"index":0,"delta":{"content":"Réponse"},"finish_reason":null}]}

data: {"id":"chatcmpl-<turn_id>", …, "choices":[{"index":0,"delta":{"content":"\n\n---\n**Sources :**\n1. …"},"finish_reason":null}]}

data: {"id":"chatcmpl-<turn_id>", …, "choices":[{"index":0,"delta":{},"finish_reason":"stop"}], "x_assistant_rh":{…}}

data: {"id":"chatcmpl-<turn_id>", …, "choices":[], "usage":{"prompt_tokens":0,"completion_tokens":0,"total_tokens":0}}

data: [DONE]
```

Le chunk `usage` vide n'est émis que si le client demande `stream_options.include_usage=true`. Les pings sont des commentaires SSE et ne portent jamais de JSON applicatif.

Cycle de vie et erreurs :

- le pipeline synchrone tourne dans un worker borné ; le générateur SSE async reste disponible pour les pings et la détection de déconnexion ;
- si une erreur survient après le démarrage du SSE, le serveur émet `data: {"error":{"message":"…","type":"server_error","code":"stream_error"}}`, ferme sans `[DONE]` et persiste le run `failed` ; il ne prétend pas pouvoir changer le statut HTTP en 500. Le SDK OpenAI lève `APIError(code="stream_error")`. `conversations` 0.0.22 laisse actuellement remonter cette exception après headers : le fork devra la convertir en son événement UI `model_connection_error` ;
- une déconnexion client finalise un run `cancelled`/partiel et annule les appels encore annulables ;
- la persistance d'un succès précède `[DONE]`, de sorte qu'un feedback envoyé immédiatement après la completion ne rencontre pas un run encore absent.

### Notes client `conversations`

- Le client ne découvre pas les modèles via `GET /v1/models` : ils sont déclarés statiquement dans son fichier `LLM_CONFIGURATION_FILE_PATH`. Le modèle configuré doit donc être recoupé au déploiement avec la liste visible par le bearer ; le SDK reste la preuve de `/v1/models`.
- Le provider actuel lit une clé statique côté backend et ne l'envoie pas au navigateur. La session interactive de huit heures est le contrat du Streamlit temps 1, pas encore le mécanisme machine-to-machine du fork : A2 a validé le transport, tandis que l'auth du temps 2 reste à décider sans réintroduire un bundle Streamlit par groupe.
- L'id fournisseur `chatcmpl-<turn_id>` est conservé dans `pydantic_messages`, mais les boutons actuels utilisent un id UI `trace-*` et écrivent dans Langfuse. Le fork doit conserver l'association message UI → completion, fournir le widget étoiles/raisons/commentaire puis appeler `POST /v1/feedback` côté serveur ; aucun token ne passe au navigateur.

### `POST /v1/feedback`

Hors spec OpenAI. Rattache une note utilisateur au run identifié par l'id de completion.

**Requête**

```json
{
  "completion_id": "chatcmpl-<turn_id>",
  "stars": 2,
  "reasons_positive": [],
  "reasons_negative": ["Sources manquantes"],
  "comment": "La réponse ne cite pas le décret applicable."
}
```

- `completion_id` : accepté avec ou sans préfixe `chatcmpl-`.
- `stars` : entier **1–5 obligatoire**. Pendant la coexistence avec le runtime historique, l'adaptateur persiste `stars - 1` sur l'échelle 0–4.
- `reasons_positive` / `reasons_negative` : listes de libellés issus du catalogue produit actif : positif `Clair`, `Utile`, `Pertinent`, `Complet`, `Précis` ; négatif `Confus`, `Éléments faux`, `Non pertinent`, `Incomplet`, `Sources manquantes`.
- `comment` : chaîne optionnelle ; au moins une raison ou un commentaire non vide est requis.
- `helpful` est dérivé par le serveur : 1–2 → `false`, 3–5 → `true`. Le champ historique `rating` n'appartient pas au contrat canonique.
- Les combinaisons suivent le widget : 1–2 affiche seulement les raisons négatives, 3–4 autorise les deux listes, 5 affiche seulement les raisons positives. Un libellé inconnu ou une raison dans une liste non applicable produit une 422.

**Réponse 204.** 404 si le `turn_id` est inconnu **ou n'appartient pas au groupe identifié par le bearer**. Le store normalise la charge utile — `stars`, raisons dédupliquées dans l'ordre du catalogue, commentaire nettoyé aux extrémités — puis calcule une empreinte canonique. Sous transaction et verrou du feedback courant, une charge identique est un no-op sans nouvelle ligne d'audit ; une charge différente archive la valeur précédente puis remplace la valeur courante atomiquement. Une contrainte garantit au plus un feedback courant par `turn_id`. L'enrichissement goldset historique reste conservé ; l'analyse admin reste dans Streamlit.

### `GET /healthz`

Sans auth (probe). **200** `{ "status": "ok", "db": "ok", "config_loaded": true }` — 503 si la DB est injoignable.

---

## Accès documentaire

`POST /v1/documents/{doc_ref}/access-url`, avec `{ "completion_id": "chatcmpl-<turn_id>" }`, vérifie que le document figure dans les sources persistées du run et que la session appartient au groupe autorisé. La réponse contient une URL opaque de rédemption valable quinze minutes et son expiration :

```json
{ "url": "https://…", "expires_at": "2026-09-04T14:15:00Z" }
```

Seuls `doc_ref` et `turn_id` sont persistés avec le run. L'URL signée est créée au clic par le frontend authentifié et n'entre jamais dans le contenu ou l'historique du chat.

`GET /v1/documents/access/{capability}` consomme cette capability sans bearer de session supplémentaire, afin qu'un navigateur puisse suivre le lien. Le serveur résout alors le support courant : il streame les bytes d'un document legacy stocké en PostgreSQL, ou redirige vers une URL S3 présignée ; une source publique conserve son URL canonique et n'a normalement pas besoin de cette route. Une capability invalide, expirée ou inconnue répond 404. Elle expire après quinze minutes, n'autorise qu'un document, n'est jamais persistée et doit être masquée dans les logs d'accès. Les réponses de contenu utilisent `Cache-Control: private, no-store` et un `Content-Disposition` sûr. Il n'existe aucune route de listing. Les règles complètes sont dans [A3](08-streamlit-api-parity.md#accès-documentaire-court).

## Surface admin reportée

Chat Logs, Feedback Dashboard, Admin Config, DB/Goldset Explorer, les pages d'évaluation, Pipeline Timeline et User Groups restent dans Streamlit sous auth admin et accès DB direct allowlisté. Aucun contrat `/admin/*` ne fait partie de la v1 ; leur éventuelle migration est un chantier ultérieur.

---

## Hors périmètre v1 (explicite)

- ProConnect / OIDC (temps 2, dans le front).
- Rate limiting général au-delà de l'auth et de la protection obligatoire de `POST /v1/auth/session` (suivi des coûts via `chat_runs`).
- Import de sources (reste Streamlit → Grist + S3, domaine ingestion).
- API SQL générique, endpoints admin, corpus/goldset et endpoints d'éval/debug : les fonctions existantes restent dans Streamlit admin.
- Migration Grafana/Tempo, LangSmith ou RAG-ops des pages Chat Logs, Pipeline Timeline et qualité.
- Agentic RAG et adoption éventuelle de LangChain.
- MCP.
