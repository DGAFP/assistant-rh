# Contrat API v1

> Référence : [00-overview.md](00-overview.md) (décisions D1, D2, D6, D11). Diagrammes : [03-sequence-diagrams.md](03-sequence-diagrams.md).

## Conventions générales

- Base : `https://<host>` ; toutes les réponses en JSON UTF-8.
- **Auth publique** : `Authorization: Bearer <token de groupe>`. Le token (PBKDF2, colonne `user_groups.api_token_hash` — migration incluse au chantier) identifie un groupe → `allowed_ministries` + `default_ministry`.
- **Auth admin** : `Authorization: Bearer <ADMIN_TOKEN>` (variable d'env de l'API, v1). Un token de groupe n'accède jamais à `/admin/*` ; l'`ADMIN_TOKEN` n'est pas accepté sur `/v1/*` (sauf `/v1/models` : non — séparation stricte).
- **Bascule Streamlit** : l'API peut être déployée dark sans que Streamlit détienne ces tokens. Avant d'activer le client HTTP, l'étape D1 livre et teste le mécanisme de provisioning/rotation des tokens de groupe côté serveur Streamlit ; aucun token n'est exposé au navigateur.
- **Erreurs** : format OpenAI sur `/v1/*` :

```json
{ "error": { "message": "Invalid API key", "type": "invalid_request_error", "code": "invalid_api_key" } }
```

| HTTP | Cas |
|---|---|
| 401 | token absent/invalide |
| 403 | modèle demandé hors `allowed_ministries` du token |
| 404 | modèle inconnu, `completion_id`/ressource inexistante |
| 422 | body invalide (validation pydantic) |
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
- Les messages `system` du client sont **ignorés** (les prompts système sont la propriété du pipeline — c'est la boucle qualité). Documenté, pas une erreur.
- Le serveur retire de l'historique les blocs de sources qu'il a lui-même ajoutés aux réponses précédentes, grâce à un marqueur interne stable, avant de passer l'historique au core.
- `temperature`, `top_p`, `max_tokens`, `n`, `user` : acceptés et ignorés en v1 (la config générateur vient de `rag_config`). `n > 1` → 422.
- Champ d'extension optionnel `metadata.conversation_id` (corrélation côté client, logué dans `chat_runs` comme aujourd'hui).
- Des limites numériques, arrêtées par A2 avant C2, s'appliquent à la taille HTTP totale, au nombre de messages et à la taille de chaque `content`. Elles sont alignées entre FastAPI, le proxy Scaleway et les clients, puis ajoutées à ce contrat ; dépassement → 422/413 avant démarrage du stream.

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
- Les **sources** sont livrées deux fois : bloc markdown en fin de `content` (rendu par tout client OpenAI, dont `conversations`) et champ d'extension `x_assistant_rh.sources` (clients qui veulent les structurer). Les clients stricts ignorent les champs inconnus.
- `usage` : renseigné si le gateway LLM le fournit, sinon zéros (v1).
- Question hors périmètre (gate du query processor) : réponse 200 normale dont le contenu est le message de refus du pipeline — jamais une erreur HTTP.

**Réponse 200 (stream, SSE `text/event-stream`)**

Chunks conformes OpenAI ; keep-alive SSE `: ping` toutes les ~10 s pendant le retrieval, puis deltas de génération et bloc sources. Le `chat_run` et ses traces sont finalisés **avant** le chunk terminal et `[DONE]` :

```text
data: {"id":"chatcmpl-<turn_id>","object":"chat.completion.chunk","created":…,"model":"assistant-rh-matte","choices":[{"index":0,"delta":{"role":"assistant","content":""},"finish_reason":null}]}

data: {"id":"chatcmpl-<turn_id>", …, "choices":[{"index":0,"delta":{"content":"Réponse"},"finish_reason":null}]}

data: {"id":"chatcmpl-<turn_id>", …, "choices":[{"index":0,"delta":{"content":"\n\n---\n**Sources :**\n1. …"},"finish_reason":null}]}

data: {"id":"chatcmpl-<turn_id>", …, "choices":[{"index":0,"delta":{},"finish_reason":"stop"}], "x_assistant_rh":{…}}

data: [DONE]
```

Cycle de vie et erreurs :

- le pipeline synchrone tourne dans un worker borné ; le générateur SSE async reste disponible pour les pings et la détection de déconnexion ;
- si une erreur survient après le démarrage du SSE, le serveur émet l'événement d'erreur validé par le spike A2, ferme sans `[DONE]` et persiste le run `failed` ; il ne prétend pas pouvoir changer le statut HTTP en 500 ;
- une déconnexion client finalise un run `cancelled`/partiel et annule les appels encore annulables ;
- la persistance d'un succès précède `[DONE]`, de sorte qu'un feedback envoyé immédiatement après la completion ne rencontre pas un run encore absent.

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
- `stars` : entier **1–5** ou `null`. ⚠️ Le widget Streamlit historique produit 0–4 ; la conversion **+1 est à la charge du client** — l'API stocke l'échelle 1–5 (voir mémoire `satisfaction-baselines`).
- `reasons_positive` / `reasons_negative` : listes optionnelles de libellés issus du catalogue produit, conservées pour la parité du dashboard et de l'analyse des feedbacks.
- `comment` : optionnel.

**Réponse 204.** 404 si le `turn_id` est inconnu **ou n'appartient pas au groupe identifié par le bearer**. Un second POST autorisé sur le même run remplace le feedback courant selon une règle explicite et auditée. L'écriture déclenche le même enrichissement goldset et le même pipeline d'analyse que le chemin Streamlit historique, directement ou via un job durable.

### `GET /healthz`

Sans auth (probe). **200** `{ "status": "ok", "db": "ok", "config_loaded": true }` — 503 si la DB est injoignable.

---

## Surface admin (`ADMIN_TOKEN`)

### `GET /admin/rag-config` · `PUT /admin/rag-config`

- `GET` → l'objet de config runtime complet (clés `v3_*` : `v3_initial_top_k`, `v3_rerank_top_k`, `v3_rerank_input_k`, gates, prompts actifs, …) + métadonnées (`updated_at`, version).
- `PUT` avec un objet **partiel** → merge et validation par le schéma `assistant_rh_rag_core.config` ; 422 si clé inconnue ou valeur invalide (protège du piège des clés legacy v1/v2 mortes — mémoire `rag-config-legacy-keys-trap`).

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

La phase A produit une matrice de décision pour DB Explorer, Goldset Explorer, les pages d'éval et `_PDF_Viewer`. Une page conservée reçoit un endpoint étroit (par exemple détail document/PDF ou opérations goldset) ; aucune API SQL générique n'est exposée. Une page abandonnée est archivée seulement après validation produit.

---

## Hors périmètre v1 (explicite)

- ProConnect / OIDC (temps 2, dans le front).
- Rate limiting au-delà de l'auth (suivi des coûts via `chat_runs`).
- Import de sources (reste Streamlit → Grist + S3, domaine ingestion).
- API SQL générique et endpoints d'éval/debug non décidés par la matrice A7. Les outils conservés reçoivent uniquement des endpoints métier étroits.
- MCP.
