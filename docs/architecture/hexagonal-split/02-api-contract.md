# Contrat API v1

> Référence : [décisions D1, D2, D6 et D11](06-decisions.md). Diagrammes : [03-sequence-diagrams.md](03-sequence-diagrams.md).

## Conventions générales

- Base : `https://<host>` ; toutes les réponses en JSON UTF-8.
- **Auth unique** : `Authorization: Bearer <token de groupe>`. Le token hashé (PBKDF2, colonne `user_groups.api_token_hash`) identifie un groupe avec `allowed_ministries`, `default_ministry` et `is_admin`.
- **Autorisation admin** : `/admin/*` utilise le même resolver de bearer puis exige `is_admin=true`. Il n'existe pas d'`ADMIN_TOKEN` statique séparé. Une commande de bootstrap DB crée ou réinitialise le premier groupe admin et affiche son token une seule fois.
- **Bascule Streamlit** : l'API peut être déployée dark sans que Streamlit détienne ces tokens. Avant d'activer le client HTTP, l'étape E1 livre et teste le provisioning/rotation côté serveur Streamlit ; aucun token n'est exposé au navigateur.
- **Erreurs** : format OpenAI sur `/v1/*` :

```json
{ "error": { "message": "Invalid API key", "type": "invalid_request_error", "code": "invalid_api_key" } }
```

| HTTP | Cas |
|---|---|
| 401 | token absent/invalide |
| 403 | modèle demandé hors `allowed_ministries` ou route admin appelée sans rôle `is_admin` |
| 404 | modèle inconnu, `completion_id`/ressource inexistante |
| 422 | body invalide (validation pydantic) |
| 413 | body HTTP supérieur à 1 Mio, rejeté avant lecture et avant démarrage du stream |
| 500 | erreur survenue avant le démarrage d'une réponse non-stream ou SSE |

Sur `/admin/*`, format simple : `{ "detail": "..." }` (statuts identiques).

- **Modèles exposés** : `assistant-rh-<ministère>` pour chaque id du catalogue (`matte`, `mso`, `mi`, `masa`) présent dans `allowed_ministries` du token. Le nom générique `assistant-rh` est accepté en entrée et résolu sur `default_ministry`.
- **Compatibilité assumée** : le sous-ensemble exact de Chat Completions supporté est figé par des tests contre le SDK OpenAI et une instance de `conversations` pendant le spike A2. Toute extension non documentée reste rejetée ou ignorée explicitement.

---

## Surface publique

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
        "content": "Réponse rédigée…\n\n---\n**Sources :**\n1. [Titre du document](https://…) — MATTE\n2. …"
      },
      "finish_reason": "stop"
    }
  ],
  "usage": { "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0 },
  "x_assistant_rh": {
    "turn_id": "<turn_id>",
    "ministry": "matte",
    "sources": [
      { "title": "…", "url": "https://…", "publisher": "MATTE", "doc_ref": "…" }
    ]
  }
}
```

- `id` : `chatcmpl-` + le `turn_id` du `chat_run` (clé de corrélation feedback et logs).
- Les **sources** sont livrées deux fois : bloc markdown en fin de `content` (rendu par tout client OpenAI, dont `conversations`) et champ d'extension `x_assistant_rh.sources` (clients qui veulent les structurer). Le SDK OpenAI conserve l'extension dans `model_extra`; `conversations`/Pydantic-AI la tolère mais ne la transforme pas en panneau de sources. Le markdown est donc le seul chemin interopérable en v1.
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
- Le bearer du provider est lu côté backend depuis une variable d'environnement et n'est pas envoyé au navigateur. Une configuration provider correspond à un bearer de groupe ; le routage par utilisateur/groupe demandera le fork prévu au temps 2 ou des instances séparées.
- L'id fournisseur `chatcmpl-<turn_id>` est conservé dans `pydantic_messages`, mais les boutons de feedback actuels utilisent un id UI `trace-*` et écrivent dans Langfuse. Le fork doit conserver l'association message UI → completion puis appeler `POST /v1/feedback` côté serveur avec son bearer ; aucun token ne passe au navigateur.

### `POST /v1/feedback`

Hors spec OpenAI. Rattache une note utilisateur au run identifié par l'id de completion.

**Requête**

```json
{
  "completion_id": "chatcmpl-<turn_id>",
  "rating": "down",
  "stars": null,
  "reasons_positive": [],
  "reasons_negative": ["Source juridique manquante"],
  "comment": "La réponse ne cite pas le décret applicable."
}
```

- `completion_id` : accepté avec ou sans préfixe `chatcmpl-`.
- `rating` : `"up"` | `"down"`.
- `stars` : entier **1–5** ou `null`. Le widget Streamlit historique produit 0–4 ; la conversion **+1 est à la charge du client** — l'API stocke l'échelle 1–5.
- `reasons_positive` / `reasons_negative` : listes optionnelles de libellés issus du catalogue produit, conservées pour la parité du dashboard et de l'analyse des feedbacks.
- `comment` : optionnel.

**Réponse 204.** 404 si le `turn_id` est inconnu **ou n'appartient pas au groupe identifié par le bearer**. Un second POST autorisé sur le même run remplace le feedback courant selon une règle explicite et auditée. L'écriture déclenche le même enrichissement goldset et le même pipeline d'analyse que le chemin Streamlit historique, directement ou via un job durable.

### `GET /healthz`

Sans auth (probe). **200** `{ "status": "ok", "db": "ok", "config_loaded": true }` — 503 si la DB est injoignable.

---

## Surface admin (bearer d'un groupe `is_admin`)

### `GET /admin/rag-config` · `PUT /admin/rag-config`

- `GET` → l'objet de config runtime complet (clés `v3_*` : `v3_initial_top_k`, `v3_rerank_top_k`, `v3_rerank_input_k`, gates, prompts actifs, …) + métadonnées (`updated_at`, version).
- `PUT` avec un objet **partiel** → merge et validation par `assistant_rh_api.core.config` ; 422 si clé inconnue ou valeur invalide, notamment les anciennes clés v1/v2.

### `/admin/system-prompts/*` · `/admin/acronyms/*`

- System prompts : liste, détail, création/mise à jour, duplication et suppression protégée du défaut.
- Acronymes : liste/recherche, création, mise à jour, suppression, liste des acronymes détectés manquants et marquage comme traité.
- Toute mutation incrémente une révision de configuration. Les instances API rechargent prompts/acronymes sans conserver indéfiniment une valeur dans un objet pipeline partagé.

### `GET /admin/user-groups` · `POST /admin/user-groups` · `PATCH/DELETE /admin/user-groups/{slug}`

CRUD des groupes : `slug`, `label`, `icon`, `color`, `priority`, `is_admin`, `visible`, `allowed_ministries`, `default_ministry`, `chart_color`, `chart_label`. Les hash (`password_hash`, `api_token_hash`) ne sont **jamais** renvoyés.

`POST /admin/user-groups/{slug}/reset-password` conserve la gestion des mots de passe du sélecteur Streamlit tant que ce mécanisme existe. Les groupes structurels restent non supprimables.

### `POST /admin/user-groups/{slug}/rotate-token`

Génère un nouveau token API pour le groupe, stocke son hash, retourne le token **en clair une seule fois** :

```json
{ "slug": "dgafp-beta", "token": "arh_live_…", "rotated_at": "2026-08-21T10:00:00Z" }
```

### `GET /admin/chat-runs` · `GET /admin/chat-runs/{turn_id}` · `GET /admin/chat-runs/{turn_id}/trace`

- Liste paginée : filtres `from`, `to`, `group`, `ministry`, `source`, `has_feedback`, `limit` (≤ 200), `offset`. Champs résumés (turn_id, ts, groupe, ministère, question tronquée, note).
- Détail : le run complet (question, réponse, sources, timings, feedback, config utilisée).
- Trace : événements `rag_trace_events` ordonnés pour la page Pipeline Timeline.

### `GET /admin/feedback` · `GET /admin/feedback/stats` · `POST /admin/feedback/analyze`

- Liste paginée détaillée : étoiles, helpful, raisons positives/négatives, commentaire, groupe/ministère, question/réponse, thème et résultat d'analyse IA ; filtres équivalents au dashboard actuel.
- Stats : volumes, répartition up/down, moyenne d'étoiles (échelle 1–5), par période/groupe/ministère.
- Analyse : déclenchement borné/idempotent de l'analyse des feedbacks négatifs non analysés ; le travail long ne dépend pas du rerun d'une page Streamlit.

### Documents et pages DB/éval

La matrice A3 fixe le sort de DB Explorer, Goldset Explorer, des pages d'éval et de `_PDF_Viewer` avant les endpoints concernés. Une page conservée reçoit un endpoint métier étroit, jamais une API SQL générique. L'archivage exige une validation produit.

---

## Hors périmètre v1 (explicite)

- ProConnect / OIDC (temps 2, dans le front).
- Rate limiting au-delà de l'auth (suivi des coûts via `chat_runs`).
- Import de sources (reste Streamlit → Grist + S3, domaine ingestion).
- API SQL générique et endpoints d'éval/debug non décidés par la matrice A3. Les outils conservés reçoivent uniquement des endpoints métier étroits.
- MCP.
