# Architecture cible — core interne à `apps/api`

> Référence : [décisions D3, D4 et D5](06-decisions.md).

## Vue d'ensemble

```mermaid
flowchart TB
    ST[Streamlit<br/>client HTTP pur]
    EV[éval goldset<br/>src/goldset — driver direct-core]

    subgraph api ["apps/api — application déployable"]
        H[handlers/ — FastAPI<br/>· chat_completions <br/> · admin <br/>· feedback <br/> · health]

        subgraph core ["assistant_rh_api/core — logique métier pure"]
            CS[ChatService<br/>orchestration requête]
            MS[ModelService]
            P[pipeline & steps<br/>· query_processor <br>· retrieval <br/> · section aggregator  <br/> · context builder/selector <br/> · generator]
            PP[composition du prompt ministère]
            PO[ports :<br/>· SearchPort <br/>· ContentStorePort<br/>· PromptStorePort <br/>· AcronymStorePort<br/>· RerankerPort <br/>· LLMPort <br/>· EmbeddingPort<br/>· ChatRunStorePort <br/>· ConfigStorePort<br/>· FeedbackStorePort<br/>· UserGroupStorePort]


            FS[FeedbackService]

            AS[AdminService]
            AU[AuthService]

        end

        DB[db/ — psycopg<br/>· recherche <br> · documents/sections <br> · prompts/acronymes<br/> · chat_runs <br> · rag_config <br> · user_groups <br> · feedback]
        GW[gateways/ — httpx<br/>Albert/Scaleway LLM/embeddings · reranker]
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
    GW --> ALB[Albert API]
    GW --> SCW[Scaleway API]
    H --> FS
    FS --> PO
    H --> AS
    AS --> PO
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
│   │   ├── admin_service.py 
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
│   │   ├── ministry_scope.py 
│   │   └── prompt_policy.py  
│   ├── handlers/             
│   │   ├── app.py            
│   │   ├── chat_completions.py  
│   │   ├── models.py          
│   │   ├── feedback.py          
│   │   ├── admin.py          
│   │   ├── health.py         
│   │   └── auth.py           
│   ├── db/                   
│   │   ├── search.py         
│   │   ├── content_store.py  
│   │   ├── chat_run_store.py 
│   │   ├── config_store.py   
│   │   ├── user_groups.py    
│   │   ├── feedback_store.py 
│   │   └── dsn.py            
│   └── gateways/             
│       ├── albert.py         
│       ├── scaleway.py         
│       └── reranker.py
└── tests/
    ├── core/                 
    ├── db/                   
    ├── handlers/             
    └── integration/          
```
