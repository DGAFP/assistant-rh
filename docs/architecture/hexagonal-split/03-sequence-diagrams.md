# Diagrammes de séquence

> Référence : [contrat API](02-api-contract.md) · [architecture cible](01-target-architecture.md).

## 1. `POST /v1/chat/completions` (stream)

Le chemin nominal complet, avec la résolution token → groupe → modèle → ministère.

```mermaid
sequenceDiagram
    autonumber
    participant C as Client (conversations / Streamlit)
    participant H as handlers/openai_compat
    participant A as handlers/auth
    participant UG as db/user_groups
    participant CS as core/ChatService
    participant CFG as db/config_store
    participant P as core/pipeline (steps)
    participant S as db/search
    participant GW as gateways (Albert, reranker)
    participant CR as db/chat_run_store

    C->>H: POST /v1/chat/completions (bearer, model, messages, stream=true)
    H->>A: authentifier(bearer)
    A->>UG: résoudre token (PBKDF2) → groupe
    UG-->>A: groupe {allowed_ministries, default_ministry}
    A-->>H: 401 si inconnu
    H->>H: model → ministère (403 si hors allowed, fallback default si "assistant-rh")
    H->>H: messages → (question, historique) ; system ignorés
    H->>CS: handle(question, historique, ministère, conversation_id)
    CS->>CFG: config runtime (cache TTL ~15 s)
    CS->>CS: resolve_retrieval_scope(ministère) → table_keys
    CS->>P: process_query(question, historique)
    P->>GW: embeddings / reformulation (Albert)
    Note over P: gate hors-périmètre → réponse de refus (200, pas d'erreur)
    CS->>P: run_stream(qr, historique, turn_id, scope)
    P->>S: recherches vector + lexicale (table_keys, top_k)
    S-->>P: chunks scorés bruts
    P->>P: fusion, dédup, anti-redondance (core)
    P->>GW: rerank(candidats) puis gate/seuils (core)
    P->>GW: chat_stream(prompt ministère, contexte, historique)
    loop tokens
        GW-->>P: token
        P-->>H: token
        H-->>C: SSE chunk delta
    end
    P-->>H: last_result (sources, timings)
    H-->>C: chunk bloc sources + chunk final (finish_reason=stop, x_assistant_rh) + [DONE]
    H->>CR: log chat_run + trace events (turn_id)
```

Variante non-stream : identique jusqu'à l'étape 15, puis la réponse est assemblée en un seul `chat.completion` ; le log `chat_run` précède la réponse HTTP.

## 2. `GET /v1/models`

```mermaid
sequenceDiagram
    autonumber
    participant C as Client
    participant H as handlers/openai_compat
    participant A as handlers/auth
    participant UG as db/user_groups

    C->>H: GET /v1/models (bearer)
    H->>A: authentifier(bearer)
    A->>UG: token → groupe
    UG-->>A: {allowed_ministries: [matte, mso]}
    A-->>H: groupe
    H-->>C: 200 {data: [assistant-rh-matte, assistant-rh-mso]}
```

## 3. `POST /v1/feedback`

```mermaid
sequenceDiagram
    autonumber
    participant C as Client
    participant H as handlers/feedback
    participant A as handlers/auth
    participant FS as db/feedback_store

    C->>H: POST /v1/feedback {completion_id, rating, stars 1–5, comment}
    H->>A: authentifier(bearer de groupe)
    H->>H: completion_id → turn_id (strip "chatcmpl-")
    H->>FS: upsert feedback(turn_id, rating, stars, comment)
    FS-->>H: ok | run inconnu
    H-->>C: 204 | 404
```

## 4. Admin — `PUT /admin/rag-config`

```mermaid
sequenceDiagram
    autonumber
    participant ST as Streamlit admin
    participant H as handlers/admin
    participant CORE as core/config (schéma)
    participant CFG as db/config_store

    ST->>H: PUT /admin/rag-config {v3_rerank_input_k: 40} (ADMIN_TOKEN)
    H->>H: vérifier ADMIN_TOKEN (401 sinon)
    H->>CFG: lire config courante
    H->>CORE: valider merge (clé connue ? valeur valide ?)
    CORE-->>H: 422 si clé inconnue (piège clés legacy)
    H->>CFG: écrire config + updated_at
    H-->>ST: 200 config résultante
    Note over CFG: le cache TTL (~15 s) des instances API se rafraîchit seul
```

## 5. Éval via-API (test de fidélité de l'adaptateur, D9)

```mermaid
sequenceDiagram
    autonumber
    participant R as runner éval via-API
    participant API as apps/api (VM homelab, DB staging)
    participant CORE as runner direct-core (même config)
    participant J as journal d'expérimentations

    R->>CORE: goldset → réponses de référence (adaptateurs branchés, sans HTTP)
    loop questions du goldset
        R->>API: POST /v1/chat/completions (non-stream, tag source=api-vm)
        API-->>R: réponse + x_assistant_rh.sources
    end
    R->>R: comparer réponses/sources API vs direct-core (même config figée)
    Note over R: écart attendu ≈ nul — tout écart = bug d'adaptateur<br/>(mapping messages, scope, assemblage sources), pas de pipeline
    R->>J: consigner run, config, écarts (obligatoire)
```

## 6. Temps 1 → Temps 2 (vue macro)

```mermaid
sequenceDiagram
    participant U as Agent RH
    participant F as Front
    participant API as apps/api
    Note over F: Temps 1 : F = Streamlit 01_Chatbot (client SSE)
    Note over F: Temps 2 : F = fork conversations (ProConnect, feedback → /v1/feedback)
    U->>F: question
    F->>API: /v1/chat/completions (bearer du déploiement)
    API-->>F: SSE deltas + sources
    F-->>U: réponse rendue
    U->>F: note / commentaire
    F->>API: /v1/feedback
```
