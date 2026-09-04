# Architecture cible M4 — core interne à `apps/api`

> Référence : [décisions D3, D4, D5 et D17](06-decisions.md).

## Vue d'ensemble

```mermaid
flowchart TB
    ST[Streamlit public<br/>client HTTP pur]
    ADMIN[Streamlit admin/ops<br/>exception DB directe]
    EV[éval goldset<br/>src/goldset — driver direct-core]

    subgraph api ["apps/api — application déployable"]
        H[handlers/ — FastAPI<br/>· auth/models <br/>· chat_completions <br/>· feedback/documents <br/>· health]

        subgraph core ["assistant_rh_api/core — logique métier pure"]
            CS[ChatService<br/>orchestration requête]
            MS[ModelService]
            P[pipeline & steps<br/>· query_processor <br>· retrieval <br/> · section aggregator  <br/> · context builder/selector <br/> · generator]
            PP[composition du prompt ministère]
            PO[ports :<br/>· SearchPort <br/>· ContentStorePort<br/>· PromptStorePort <br/>· AcronymStorePort<br/>· RerankerPort <br/>· LLMPort <br/>· EmbeddingPort<br/>· ChatRunStorePort <br/>· ConfigStorePort<br/>· FeedbackStorePort<br/>· UserGroupStorePort<br/>· AuthSessionStorePort<br/>· DocumentCapabilityPort<br/>· ClockPort<br/>· IdGeneratorPort<br/>· TraceSinkPort]


            FS[FeedbackService]

            DS[DocumentService]

            AU[AuthService]

        end

        DB[db/ — psycopg<br/>· recherche <br> · documents/sections <br/> · prompts/acronymes<br/> · chat_runs <br> · rag_config <br/> · user_groups/sessions <br> · feedback]
        GW[gateways/ — httpx/crypto<br/>Albert/Scaleway LLM/embeddings · reranker · capability documentaire]
    end

    ST -->|HTTP| H
    H --> CS
    EV --> CS
    CS --> P
    P --> PP
    H --> MS
    MS --> PO
    P --> PO
    PP --> PO
    PO --> DB
    PO --> GW
    DB --> PG[(Postgres pgvector)]
    ADMIN -->|accès allowlisté + require_admin| PG
    GW --> ALB[Albert API]
    GW --> SCW[Scaleway API]
    H --> FS
    FS --> PO
    H --> DS
    DS --> PO
    H --> AU
    AU --> PO
```

## Arborescence cible

```text
apps/api/
├── pyproject.toml            
├── moon.yml                  
├── src/assistant_rh_api/
│   ├── __init__.py           
│   ├── core/                 
│   │   ├── chat_service.py   
│   │   ├── feedback_service.py 
│   │   ├── document_service.py
│   │   ├── model_service.py 
│   │   ├── auth_service.py 
│   │   ├── pipeline/
│   │   │   ├── __init__.py
│   │   │   ├── orchestration.py  
│   │   │   └── steps/
│   │   │       ├── query_processor.py
│   │   │       ├── retrieval.py
│   │   │       ├── aggregation.py
│   │   │       ├── context_selector.py
│   │   │       ├── context_builder.py
│   │   │       └── generator.py
│   │   ├── ports.py
│   │   ├── models.py         
│   │   ├── config.py         
│   │   ├── run_context.py
│   │   ├── ministry_scope.py 
│   │   └── prompt_policy.py  
│   ├── handlers/             
│   │   ├── app.py            
│   │   ├── chat_completions.py  
│   │   ├── models.py          
│   │   ├── feedback.py          
│   │   ├── documents.py
│   │   ├── health.py         
│   │   └── auth.py           
│   ├── db/                   
│   │   ├── search.py         
│   │   ├── content_store.py  
│   │   ├── chat_run_store.py 
│   │   ├── config_store.py   
│   │   ├── prompt_store.py
│   │   ├── acronym_store.py
│   │   ├── user_groups.py    
│   │   ├── auth_sessions.py
│   │   ├── feedback_store.py 
│   │   └── dsn.py            
│   └── gateways/             
│       ├── albert.py         
│       ├── scaleway.py         
│       ├── reranker.py
│       └── document_access.py
└── tests/
    ├── core/                 
    ├── db/                   
    ├── handlers/             
    └── integration/          
```

## État par requête

`ChatService` crée un `RunContext` isolé pour chaque requête. Il porte les identifiants, le scope ministère, les snapshots et révisions de config/prompts/acronymes, les résultats intermédiaires, les outcomes providers, les timings et les événements de trace. Les pools, clients, secrets et caches process restent dans les adaptateurs et n'y figurent jamais.

Les ports et adaptateurs partagés ne stockent aucun résultat de requête. Le détail des champs, des états historiques à supprimer et des contrats de concurrence est tenu dans l'[audit d'isolation A5](07-runtime-isolation-audit.md#runcontext-minimal).
