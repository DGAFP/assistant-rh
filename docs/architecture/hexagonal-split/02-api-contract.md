# Contrat API v1

> Référence : [décisions D1, D2, D6, D11 et D15 à D18](06-decisions.md). Diagrammes : [03-sequence-diagrams.md](03-sequence-diagrams.md).

## Conventions générales

- Base : `https://<host>` ; toutes les réponses en JSON UTF-8.
- **Auth unique** : `Authorization: Bearer <token de groupe>`. Le format `arh_<env>_<token_id>.<secret>` permet une sélection indexée par `token_id`, puis une vérification PBKDF2 en temps constant du secret. La table `user_group_api_tokens` autorise plusieurs tokens actifs par groupe et les relie à `allowed_ministries`, `default_ministry` et `is_admin` via le groupe.
- **Autorisation admin** : `/admin/*` utilise le même resolver de bearer puis exige `is_admin=true`. Il n'existe pas d'`ADMIN_TOKEN` statique séparé. Une commande de bootstrap DB crée le premier token admin et affiche son clair une seule fois.
- **Bascule Streamlit** : l'API peut être déployée dark sans que Streamlit détienne ces tokens. E1 injecte `STREAMLIT_API_BEARERS_JSON` comme secret serveur, selon le [protocole A3](08-streamlit-api-parity.md#provisioning-serveur-des-bearers-streamlit) ; aucun token n'est exposé au navigateur.
- **Erreurs** : format OpenAI sur `/v1/*` :

```json
{ "error": { "message": "Invalid API key", "type": "invalid_request_error", "code": "invalid_api_key" } }
```

| HTTP | Cas |
|---|---|
| 401 | token absent/invalide |
| 403 | modèle demandé hors `allowed_ministries` ou route admin appelée sans rôle `is_admin` |
| 404 | modèle inconnu, `completion_id`/ressource inexistante |
| 409 | révision obsolète lors d'une mutation admin |
| 422 | body invalide (validation pydantic) |
| 429 | tentatives de vérification de mot de passe trop nombreuses |
| 500 | erreur survenue avant le démarrage d'une réponse non-stream ou SSE |

Sur `/admin/*`, format simple : `{ "detail": "..." }` (statuts identiques).

- **Modèles exposés** : `assistant-rh-<ministère>` pour chaque id du catalogue (`matte`, `mso`, `mi`, `masa`) présent dans `allowed_ministries` du token. Le nom générique `assistant-rh` est accepté en entrée et résolu sur `default_ministry`.
- **Compatibilité assumée** : le sous-ensemble exact de Chat Completions supporté est figé par des tests contre le SDK OpenAI et une instance de `conversations` pendant le spike A2. Toute extension non documentée reste rejetée ou ignorée explicitement.

---

## Surface publique

### Authentification du produit Streamlit

- `GET /v1/auth/groups` liste uniquement les métadonnées d'affichage des groupes visibles, non-admin et utilisables pour le login.
- `POST /v1/auth/verify` vérifie `{ "slug": "…", "password": "…" }` et retourne le slug, le rôle, la politique ministère et `credential_revision`, jamais un bearer. L'erreur 401 est identique pour un groupe absent et un mot de passe faux. La route est rate-limitée par IP + slug et n'enregistre jamais le mot de passe.
- `GET /v1/auth/me`, avec bearer, retourne la même identité non secrète et sert à valider le mapping serveur de Streamlit.

Ces routes sont appelées côté serveur Streamlit. Elles ne créent pas de session navigateur et CORS n'est pas ouvert. Le registre de données et permissions est figé par [A3](08-streamlit-api-parity.md#registre-figé-des-endpoints-publics-et-documentaires).

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
- Des limites numériques, arrêtées par A2 avant C1, s'appliquent à la taille HTTP totale, au nombre de messages et à la taille de chaque `content`. Elles sont alignées entre FastAPI, le proxy Scaleway et les clients, puis ajoutées à ce contrat ; dépassement → 422/413 avant démarrage du stream.

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
- `stars` : entier **1–5** ou `null`. Pendant la coexistence avec le runtime historique, l'adaptateur convertit en **0–4 au stockage** et les lectures API reconvertissent en 1–5. Cela évite de mélanger deux encodages dans `chat_feedbacks`. Une migration atomique du stockage n'est permise qu'après retrait de tous les consommateurs 0–4.
- `reasons_positive` / `reasons_negative` : listes optionnelles de libellés issus du catalogue produit, conservées pour la parité du dashboard et de l'analyse des feedbacks.
- `comment` : optionnel.

**Réponse 204.** 404 si le `turn_id` est inconnu **ou n'appartient pas au groupe identifié par le bearer**. Un second POST autorisé sur le même run remplace le feedback courant selon une règle explicite et auditée. L'écriture déclenche le même enrichissement goldset et le même pipeline d'analyse que le chemin Streamlit historique, directement ou via un job durable.

### `GET /healthz`

Sans auth (probe). **200** `{ "status": "ok", "db": "ok", "config_loaded": true }` — 503 si la DB est injoignable.

---

## Surface admin (bearer d'un groupe `is_admin`)

Le registre exhaustif, les propriétaires et les données sensibles sont dans l'[arbitrage A3](08-streamlit-api-parity.md#registre-figé-des-endpoints-admin-d2). Toute mutation utilise une révision attendue et produit un audit sans secret.

### `GET /admin/rag-config` · `PUT /admin/rag-config`

- `GET` → l'objet de config runtime complet (clés `v3_*` : `v3_initial_top_k`, `v3_rerank_top_k`, `v3_rerank_input_k`, gates, prompts actifs, …) + métadonnées (`updated_at`, version).
- `PUT` avec un objet **partiel** → merge et validation par `assistant_rh_api.core.config` ; 422 si clé inconnue ou valeur invalide, notamment les anciennes clés v1/v2.
- `POST /admin/rag-config/reset` restaure les défauts avec confirmation et révision explicites.

### Prompts et acronymes

- System prompts : `GET/POST /admin/system-prompts`, `GET/PUT/DELETE /admin/system-prompts/{name}` et `POST /admin/system-prompts/{name}/duplicate`.
- Acronymes : `GET/POST /admin/acronyms`, `PUT/DELETE /admin/acronyms/{acronym}`, `GET /admin/acronyms/missing` et `POST /admin/acronyms/missing/{acronym}/resolve`.
- Toute mutation incrémente une révision de configuration. Les instances API rechargent prompts/acronymes sans conserver indéfiniment une valeur dans un objet pipeline partagé.

### `GET /admin/user-groups` · `POST /admin/user-groups` · `PATCH/DELETE /admin/user-groups/{slug}`

CRUD des groupes : `slug`, `label`, `icon`, `color`, `priority`, `is_admin`, `visible`, `allowed_ministries`, `default_ministry`, `chart_color`, `chart_label`. Les hash (`password_hash`, `secret_hash`) ne sont **jamais** renvoyés.

`POST /admin/user-groups/{slug}/reset-password` conserve la gestion des mots de passe du sélecteur Streamlit tant que ce mécanisme existe. Les groupes structurels restent non supprimables.

Le mot de passe est write-only. Le reset incrémente `credential_revision`. `DELETE` refuse les groupes structurels, le dernier admin et une révision obsolète, puis révoque les tokens dans la même transaction.

### `GET/POST /admin/user-groups/{slug}/tokens` · `DELETE /admin/user-groups/{slug}/tokens/{token_id}`

`GET` ne retourne que les identifiants, labels et dates. `POST` génère un nouveau token API pour le groupe, stocke son hash, conserve les tokens existants pendant la rotation et retourne le nouveau token **en clair une seule fois** :

```json
{ "slug": "dgafp-beta", "token_id": "…", "token": "arh_prod_….…", "created_at": "2026-09-02T10:00:00Z" }
```

`DELETE` révoque explicitement l'ancien token après mise à jour et smoke du client. Le [runbook E1](08-streamlit-api-parity.md#provisioning-serveur-des-bearers-streamlit) définit la rotation sans interruption.

### `GET /admin/chat-runs` · `GET /admin/chat-runs/stats` · `GET /admin/chat-runs/{turn_id}` · `GET /admin/chat-runs/{turn_id}/trace`

- Liste paginée : filtres `from`, `to`, `group`, `ministry`, `source`, `has_feedback`, `limit` (≤ 200), `offset`. Champs résumés (turn_id, ts, groupe, ministère, question tronquée, note).
- Stats : agrégats bornés nécessaires aux métriques pipeline et usage du dashboard, avec les mêmes filtres.
- Détail : le run complet (question, réponse, sources, timings, feedback, config utilisée).
- Trace : événements `rag_trace_events` ordonnés pour la page Pipeline Timeline.

### `GET /admin/feedback` · `GET /admin/feedback/stats` · `POST /admin/feedback/analyze` · `GET /admin/feedback/analyze/{job_id}`

- Liste paginée détaillée : étoiles, helpful, raisons positives/négatives, commentaire, groupe/ministère, question/réponse, thème et résultat d'analyse IA ; filtres équivalents au dashboard actuel.
- Stats : volumes, répartition up/down, moyenne d'étoiles (échelle 1–5), par période/groupe/ministère.
- Analyse : déclenchement borné/idempotent de l'analyse des feedbacks négatifs non analysés puis suivi du job ; le travail long ne dépend pas du rerun d'une page Streamlit.

## Accès documentaire

`GET /v1/documents/{doc_ref}/content` résout un PDF legacy, un objet de dropzone ou une URL externe. Il accepte soit un bearer dont le groupe est autorisé sur le corpus, soit une capability opaque portée par une source de completion. Il n'existe aucune route publique de listing.

Les URLs externes canoniques sont retournées directement. Pour un document interne, la capability est signée sur `doc_ref + turn_id`, vérifiée contre les sources persistées du run, limitée à un document et révocable. Bearers, clés S3, UUID legacy et chemins de stockage ne figurent jamais dans l'URL. Les règles complètes sont dans [A3](08-streamlit-api-parity.md#accès-documentaire-étroit).

## Pages DB, goldset et évaluation

DB Explorer, Goldset Explorer et les pages d'évaluation deviennent un [outil RAG-ops séparé](08-streamlit-api-parity.md#gate-de-retrait-des-pages-rag-ops). Ils ne créent aucun endpoint v1/admin. Leur accès DB utilise des rôles dédiés et leurs tables sont créées par migration, jamais par une page.

---

## Hors périmètre v1 (explicite)

- ProConnect / OIDC (temps 2, dans le front).
- Rate limiting général au-delà de l'auth et de la protection obligatoire de `POST /v1/auth/verify` (suivi des coûts via `chat_runs`).
- Import de sources (reste Streamlit → Grist + S3, domaine ingestion).
- API SQL générique, endpoints corpus/goldset et endpoints d'éval/debug : l'arbitrage A3 les affecte à RAG-ops.
- MCP.
