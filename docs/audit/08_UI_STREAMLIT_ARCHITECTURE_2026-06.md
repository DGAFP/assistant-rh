# Audit architectural — UI Streamlit & frontière UI ↔ pipeline

> Dossier d'audit : voir [README](README.md). Notes liées :
> - [02 — Audit architectural global](02_ARCHITECTURE_AUDIT_2026-06.md) (couches code / sécurité / observabilité — la surface UI est citée mais la frontière UI ↔ pipeline n'y est pas traitée).
> - [UI_REPLACEMENT_ANALYSIS.md](../UI_REPLACEMENT_ANALYSIS.md) (note historique d'avril 2025, ère Scalingo : compare des UI candidates, pas l'architecture interne).
>
> Date : 2026-06-17. Périmètre : `apps/streamlit-ui/`, `src/ui/`, intégration avec `packages/rag-pipeline/` et `apps/mastra-pipeline/`. Constats vérifiés sur le code de `main` au 2026-06-17 (worktree) ; recommandations croisées avec l'état de l'art open-source RAG (Onyx, Quivr, Verba, RAGFlow, Open WebUI, LibreChat) et les contraintes secteur public (DSFR, AgentConnect/FranceConnect, Albert/DINUM).

---

## 1. Synthèse

L'UI Streamlit est **opérationnellement propre pour un pilote** — auth admin double, fallbacks robustes, instrumentation très riche (~50 colonnes par tour) — mais **structurellement fragile pour un produit** et **activement en travers du port Mastra**. Les points durs ne sont pas des bugs : ils sont les motifs standards qui poussent les équipes RAG hors de Streamlit en production.

Trois constats structurants :

1. **Frontière UI ↔ pipeline absente.** L'UI importe le pipeline en process (`from assistant_rh_rag_pipeline import create_pipeline_v3_clean`) et lit directement les internes (`pipeline_v3.last_result.context_items / timing / metadata`, [01_Chatbot.py:1345-1390](../../apps/streamlit-ui/pages/01_Chatbot.py)). Tout changement de schéma `RAGResult` casse l'UI silencieusement. Ce couplage rend par ailleurs **impossible le partage d'UI** entre le pipeline Python et le port Mastra — chaque évolution se paie deux fois (cf. note 02 §A1).

2. **`st.session_state` lié au websocket = classe de panne réelle en production gouv.** Confirmé structurel par les issues Streamlit [#4297](https://github.com/streamlit/streamlit/issues/4297) et [#8901](https://github.com/streamlit/streamlit/issues/8901) : un timeout de reverse-proxy efface l'historique de chat. Derrière AgentConnect/FranceConnect (toujours via un proxy), c'est une vraie source d'incidents — d'autant que l'historique vit aujourd'hui **uniquement en mémoire de session**, pas en DB côté UI (la persistance `chat_runs` est append-only, non rechargée à la reconnexion).

3. **`01_Chatbot.py` est un god-file (1 468 lignes)** qui mélange healthcheck, init session, sidebar, orchestration, streaming, feedback, logging — non testé (0 test UI dans `tests/`, cf. note 02 §3). Les pages d'éval 08–11 (2 545 / 1 728 / 1 047 / 627 lignes) **redéclarent chacune leurs propres dataclasses** au lieu de partager un module commun. Le constat « logique métier dans les pages Streamlit » (note 02 §A2) est confirmé et localisé.

La conclusion opérationnelle ne change pas si l'on retient Streamlit ou non : **il faut tracer la frontière HTTP UI ↔ pipeline maintenant**. C'est la pré-condition de toute évolution UI (Streamlit assaini, ou remplacement) et c'est la seule mécanique qui rendrait la décision Python/Mastra (note 02 §A1) non destructive côté frontend.

---

## 2. État actuel — cartographie

### 2.1 Arborescence et flux

```
apps/streamlit-ui/
  Home.py                       35 l.  — redirige vers 01_Chatbot
  .streamlit/config.toml         — thème clair, error detail réduit
  pages/                       12 886 l. au total
    01_Chatbot.py             1 468 l.  ← god-file, UI prod
    02_Chat_Logs.py             708 l.  (admin)
    03_Feedback_Dashboard.py  1 041 l.  (admin)
    04_Admin_Config.py          949 l.  (admin — system_prompts, acronyms, rag_config)
    05_DB_Explorer.py           673 l.  (admin — SQL libre)
    06_Goldset_Explorer.py      774 l.  (éval)
    07_Eval_Comparison.py       930 l.  (éval)
    08_Chunking_Evaluation.py 2 545 l.  (éval — chunking DE vs V3)
    09_Pipeline_Evaluation.py 1 728 l.  (éval — ablation modules)
    10_Intent_Gater_Evaluation.py 1 047 l.  (éval — intent gating)
    11_Golden_Beta_Analysis.py  627 l.  (éval — cohortes)
    _PDF_Viewer.py              396 l.  (utilitaire)
src/ui/                       3 015 l.  — helpers extraits
  admin_auth.py, cookies_security.py
  chatbot_llm.py, chatbot_logging.py, chatbot_sources.py, chatbot_feedback.py
  db_utils.py (engine cached)
```

Flux d'une requête (résumé) :

```
User input
  → init session (rag_config_initialized, health_check_done, session_id, conversation_id, ...)
  → sidebar (context_mode, search_mode, top-k, rerank, temp...)
  → pipeline_v3 = create_pipeline_v3_clean(config_v3)         ← instancié par tour
  → qr = pipeline_v3.process_query(query, history)
  → si qr.should_proceed :
      stream = pipeline_v3.run_stream(qr, history, on_status=_update_status)
      st.write_stream(stream_and_filter_sources(stream))      ← parse SOURCES: en flux
  → render_sources(pipeline_v3.last_result.context_items)     ← accès direct aux internes
  → build_log_row(...) → log_run(row)                          ← Postgres, fallback CSV
```

### 2.2 Ce qui marche bien

- **Auth admin double** : cookie chiffré (`user_group = dgafpallianceadmin`) **ou** `ADMIN_PASSWORD`, fail-closed en staging/prod si `COOKIES_PASSWORD` manque ([src/ui/admin_auth.py](../../src/ui/admin_auth.py), [src/ui/cookies_security.py](../../src/ui/cookies_security.py)). Le contournement par `?group=` URL est neutralisé côté Chatbot.
- **Fallback LLM** Albert → Scaleway avec tracking TTFT, throughput, et flag `used_fallback` ([src/ui/chatbot_llm.py](../../src/ui/chatbot_llm.py)).
- **Fallback logging** Postgres → CSV (`/tmp/assistant_rh_data/` ou `apps/streamlit-ui/data/`) si DB indisponible.
- **Sidebar = console de tuning** : `context_mode`, `top_k`, reranker, sélecteur LLM, température — modifiables sans redéploiement (puisque relus depuis `rag_config`).
- **Caching utile et borné** : `@st.cache_data(ttl=300)` healthcheck, `ttl=15` `load_runtime_config()` (pour répercuter vite les changements admin), `@st.cache_resource` SQLAlchemy engine + `ChatLLM` indexé sur la config.
- **Instrumentation très riche** côté logging (note 02 §A6 décrit la dette mais la donnée brute existe : intent, refs juridiques, fallback, TTFT, A/B cohort…).

### 2.3 Smells confirmés (vérification ligne à ligne)

| # | Smell | Localisation | Conséquence |
|---|---|---|---|
| U1 | God-page Chatbot (1 468 l.) — healthcheck, init, sidebar, orchestration, streaming, feedback, logging | [`01_Chatbot.py`](../../apps/streamlit-ui/pages/01_Chatbot.py) | Non testable, merge-conflict bait, coût de modification élevé |
| U2 | Couplage direct UI → internes pipeline (`pipeline_v3.last_result.context_items / timing / metadata`) | `01_Chatbot.py:1345-1390` | Tout changement `RAGResult` casse l'UI silencieusement ; aucun adapter |
| U3 | Pipeline ré-instancié à chaque tour (`create_pipeline_v3_clean(config_v3)`) | `01_Chatbot.py:1220-1330` | Acceptable aujourd'hui, gaspillage net si retrieval s'alourdit |
| U4 | `st.session_state` = dict global non typé (~50 clés) + lié au websocket | `01_Chatbot.py:611-965` + [issue Streamlit #4297](https://github.com/streamlit/streamlit/issues/4297) | Régressions silencieuses sur typo de clé ; perte d'historique sur drop websocket (proxy idle timeout) |
| U5 | Init scattered : `rag_config_initialized` L611, `health_check_done` L176, `config_logged` L782 | `01_Chatbot.py` | Pas de point d'entrée unique — ajouter une étape d'init est risqué |
| U6 | Pages d'éval 08–11 redéfinissent chacune leurs `@dataclass` (configs pipeline, table specs) | `08_Chunking_Evaluation.py`, `09_Pipeline_Evaluation.py` | Drift entre éval et prod (rejoint note 02 §A2 — mapping `rag_config → RAGConfig` n'existe qu'en page Chatbot) |
| U7 | Fallback Scaleway en dur dans l'UI (`get_fallback_config()`) | `src/ui/chatbot_llm.py:52-60` | Logique provider dans la couche UI — exactement là où l'état de l'art dit de ne pas la mettre |
| U8 | Healthcheck caché 5 min | `01_Chatbot.py:87` (`@st.cache_data(ttl=300)`) | Une coupure DB/LLM reste invisible jusqu'à 5 min — trop long pour des services critiques |
| U9 | 0 test UI (`tests/` couvre le pipeline uniquement) | `tests/` | Refactor à l'aveugle ; rejoint note 02 §3 |
| U10 | Duplication résiduelle `src/ui/` ↔ `pages/` (ex. heuristiques de réponse négative entre `chatbot_sources.py` et `chatbot_feedback.py`) | — | Vrai effort d'extraction (3 015 l. dans `src/ui/`) mais pas terminé |
| U11 | Code commenté résiduel (lignes 1027, 1128, 1202 de `01_Chatbot.py`) | `01_Chatbot.py` | Signal de refactor incomplet ; sans gravité immédiate |

Sécurité — déjà couvert en note 02 §5 (XSS via `unsafe_allow_html` à 28 endroits, S1 ; SQLi post-auth sur pages d'éval, S2 ; rétention conversationnelle, S5). Pas de re-traitement ici, mais ces points sont **amplifiés** par U1 : tant que la page est monolithique, ajouter une couche d'échappement systématique est un chantier transverse.

---

## 3. État de l'art — ce que font les autres RAG

Sept projets open-source RAG comparables, tous récents et activement déployés :

| Projet | Backend | UI | Frontière |
|---|---|---|---|
| [Onyx (ex-Danswer)](https://github.com/onyx-dot-app/onyx) | Python / FastAPI + Celery | Next.js (`/web`) | REST/JSON, OpenAPI |
| [Quivr](https://github.com/QuivrHQ/quivr) | Python / FastAPI | Next.js | REST |
| [Verba](https://github.com/weaviate/Verba) | FastAPI | Next.js (Tailwind/DaisyUI) | REST (build SPA embarquable, runtime HTTP) |
| [RAGFlow](https://github.com/infiniflow/ragflow) | Python (Quart) + Go, Redis queue | React | REST/streaming |
| [Open WebUI](https://github.com/open-webui/open-webui) | FastAPI + LanceDB | Svelte | OpenAI-compatible |
| [LibreChat](https://www.librechat.ai/) | Node/Express | React | REST + WebSocket |
| [suitenumerique/conversations](https://github.com/suitenumerique/conversations) (DINUM) | Django REST + Pydantic AI | Next.js + Vercel AI SDK | OpenAI-compatible (`AI_BASE_URL`) |

**Consensus.** Aucun ne ship l'UI en process avec le pipeline. Tous exposent une frontière HTTP, et la majorité converge sur **`/v1/chat/completions` SSE** au format [Vercel AI SDK Data Stream Protocol](https://vercel.com/blog/ai-sdk-3-4) (tokens, tool calls, citations, finish multiplexés sur une seule réponse SSE). Le fait que la DINUM elle-même (`suitenumerique/conversations`, MIT, déployable Kubernetes, OIDC ProConnect) ait adopté ce contrat est un point d'alignement stratégique fort.

**Provider fallback** : motif gateway — [LiteLLM](https://docs.litellm.ai/docs/routing) en façade, ou directement Albert qui **est lui-même** une instance d'OpenGateLLM et parle OpenAI. Conclusion : la logique Albert → Scaleway n'a rien à faire dans `Generator` ni dans `chatbot_llm.py`. Elle appartient à la gateway.

**Surface admin / éval** : deux écoles.
- *Onyx, Quivr, Open WebUI, LibreChat* : admin et éval **dans la même SPA**, routes `/admin` gardées par rôle. Adapté quand les opérateurs ne sont pas devs.
- *Mastra Studio*, *Langfuse*, *Phoenix* : surface dev/éval **séparée** de l'UI utilisateur. [Mastra Studio](https://mastra.ai/en/docs/getting-started/studio) est désormais déployable en prod avec auth, positionnée comme workspace partagé d'équipe. Pour un setup gouv, ce split permet d'exposer Studio en interne (VPN) et l'UI sur le domaine public.

**Streamlit reste pertinent quand** : usage interne, <50 utilisateurs concurrents, pas de besoin DSFR strict, pas de citations comme composants riches, équipe Python-only. Les équipes migrent quand : multi-tenant, grand public, mobile, DSFR exigé, besoin de « stop generation » natif (impossible proprement dans le modèle de rerun Streamlit — issue [#14524](https://github.com/streamlit/streamlit/issues/14524)).

**Note sur Chainlit** : souvent cité comme « Streamlit pour chat », mais **les fondateurs se sont retirés en mai 2025**, le projet est community-maintained avec CVE connues. À écarter comme alternative long terme pour un produit gouv.

---

## 4. L'angle Mastra — pourquoi la décision UI dépend de la décision pipeline

Le port Mastra (note 02 §A1, [MASTRA_PORT_ANALYSIS.md](../MASTRA_PORT_ANALYSIS.md)) émet **nativement** dans le format AI SDK Data Stream Protocol. Mastra ship par ailleurs [Studio](https://mastra.ai/blog/agent-studio) (playground agents, mémoire, traces) et [`@mastra/ai-sdk`](https://www.npmjs.com/package/@mastra/ai-sdk) pour brancher une UI Next.js. Le pattern observé chez les équipes Mastra : **Studio comme surface interne** + **Next.js + AI SDK / [assistant-ui](https://www.assistant-ui.com/) comme UI utilisateur** — pas Studio comme UI publique.

Conséquence pour le projet :

- Tant que **l'UI Streamlit lit les internes Python**, le port Mastra **doit re-construire** sa propre UI (ou réutiliser Studio comme interim). C'est-à-dire : fork frontend permanent.
- Si **on trace une frontière HTTP OpenAI-compatible** côté Python, le port Mastra parle déjà ce dialecte → **une seule UI** peut viser les deux backends via une variable d'environnement.
- Côté DSFR : [betagouv/dsfr-kit](https://github.com/betagouv/dsfr-kit) a un binding Streamlit, mais [react-dsfr](https://github.com/codegouvfr/react-dsfr) est nettement plus complet. Si la conformité DSFR devient obligatoire (probable pour une cible grand public), la trajectoire UI tire mécaniquement vers React.

Autrement dit : **la frontière HTTP est la pré-condition technique** qui rend la décision « Python ou Mastra » (note 02 action #9) non destructive côté frontend, et qui rend possible un éventuel passage à DSFR sans tout réécrire.

---

## 5. Options & recommandation

Trois options, du plus pragmatique au plus structurant. Elles ne s'excluent pas : **B est la fondation de A et de C**.

### Option B — Sortir le pipeline derrière FastAPI, garder Streamlit comme client mince (≈ 1 semaine)

- Encapsuler le pipeline Python derrière FastAPI exposant `/v1/chat/completions` SSE au format AI SDK Data Stream.
- Streamlit devient client HTTP : `requests.post(..., stream=True)` + `st.write_stream`. L'UI ne dépend plus que d'un contrat JSON stable.
- Déplacer la sélection / fallback provider de [`src/ui/chatbot_llm.py`](../../src/ui/chatbot_llm.py) vers la gateway (LiteLLM, ou la couche FastAPI directement si Albert ne suffit pas).
- **Persister l'historique de conversation côté DB** (pgvector est déjà là) et le recharger à la reconnexion → résout U4 / le drop websocket.
- Garde la valeur opérationnelle existante (instrumentation, admin Streamlit) pendant qu'on prépare la suite.

C'est l'unique action qui rend toutes les suivantes non bloquantes.

### Option A — Une seule UI Next.js devant les deux backends (≈ 3–4 semaines, déclenche quand le pilote sort ou DSFR devient obligatoire)

- Next.js + [react-dsfr](https://github.com/codegouvfr/react-dsfr) + [assistant-ui](https://www.assistant-ui.com/) ou `useChat` (AI SDK).
- Pointe vers Python ou Mastra via env var (les deux exposent le même contrat après B).
- Mastra Studio devient la surface dev/éval interne ; les pages d'éval Streamlit 08–11 y migrent ou deviennent une CLI.
- Auth via reverse-proxy (Keycloak + plugin [InseeFr/Keycloak-FranceConnect](https://github.com/InseeFr/Keycloak-FranceConnect)) + headers de confiance — pattern beta.gouv.fr standard.

### Option C — Adopter [suitenumerique/conversations](https://github.com/suitenumerique/conversations) ou [Open WebUI](https://github.com/open-webui/open-webui) une fois la frontière HTTP en place

- `suitenumerique/conversations` (MIT, Django REST + Next.js + Vercel AI SDK, OIDC ProConnect natif) : alignement stratégique fort avec l'écosystème La Suite numérique. Encore jeune (note historique [UI_REPLACEMENT_ANALYSIS.md](../UI_REPLACEMENT_ANALYSIS.md)) mais activement développée par la DINUM.
- Open WebUI : maturité maximale (auth, persistance, citations, streaming, mobile, « stop generation »), mais contrôle DSFR limité — acceptable si l'usage est interne et le branding gouv peut être relâché.
- L'intégration suppose **la frontière HTTP de B** ; sans elle, ces options ne sont pas atteignables.

### Ce qu'il faut éviter

- **Investir davantage dans Streamlit pour admin/éval citations** : on accumule une dette que A ou C effaceront.
- **Mettre le fallback provider dans `Generator` ou dans la page** : c'est exactement ce que la note 02 §A2 décrit comme « logique métier dans Streamlit » — à sortir, pas à étendre.
- **Maintenir deux UIs en parallèle** (Streamlit pour Python + Next.js pour Mastra) : ce fork compose en permanence et annule le bénéfice du port.

---

## 6. Plan d'action priorisé

Priorisation P0 = fondation, P1 = sortie de dette, P2 = trajectoire long terme. Effort S (≤ 2 j) / M (≤ 1 sem.) / L (> 1 sem.).

| # | Action | Niveau | Effort | Lien |
|---|---|---|---|---|
| 1 | Exposer le pipeline Python via FastAPI `/v1/chat/completions` SSE (AI SDK Data Stream) | P0 | M | Note 02 §A1 (décision Python/Mastra non bloquante) |
| 2 | Persister l'historique de conversation côté DB et le recharger à la reconnexion (résout U4) | P0 | S | — |
| 3 | Sortir le fallback Albert → Scaleway de l'UI vers la gateway (LiteLLM ou couche FastAPI) ; supprimer le hardcode `chatbot_llm.py:52-60` | P0 | S | Smell U7 |
| 4 | Réduire `01_Chatbot.py` < 600 l. — extraire orchestration / sidebar / feedback dans `src/ui/` ; introduire un `SessionState` typé (TypedDict ou dataclass) | P1 | M | Smells U1, U4, U5 |
| 5 | Adapter pattern entre `RAGResult` et l'UI : un module unique de mapping (côté pipeline ou côté `src/ui/`), figeant le contrat | P1 | S | Smell U2 |
| 6 | Mutualiser les `@dataclass` des pages d'éval 08–11 dans un module partagé ; déduire le mapping `rag_config → RAGConfig` une seule fois (rejoint note 02 action #6) | P1 | M | Smell U6 |
| 7 | Tests pytest pour `src/ui/` (parser SOURCES, `is_negative_response`, `should_hide_sources`, `stream_with_fallback` en path d'erreur) — comble la zone aveugle note 02 §3 | P1 | M | Smell U9 |
| 8 | Bouton refresh manuel sur le healthcheck + raccourcir TTL à 60 s ; alerter si DB/LLM down détectés (rejoint note 03) | P1 | S | Smell U8 |
| 9 | Décider la trajectoire UI long terme (B → A, B → C `suitenumerique/conversations`, ou rester sur Streamlit assaini) — à arbitrer **après** la décision pipeline (note 02 action #9) | P2 | décision | — |
| 10 | Si A ou C : extraire les pages d'éval 08–11 de l'app de prod (CLI ou Mastra Studio) — déjà recommandé par note 02 action #10 | P2 | L | Note 02 §A2 |

Les actions 1–3 sont la pré-condition de tout le reste : sans frontière HTTP, le port Mastra paie une UI en double et chaque évolution UI coûte deux fois.

---

## Sources

- Code vérifié au 2026-06-17 : [`apps/streamlit-ui/`](../../apps/streamlit-ui/), [`src/ui/`](../../src/ui/), [`packages/rag-pipeline/`](../../packages/rag-pipeline/), [`apps/mastra-pipeline/`](../../apps/mastra-pipeline/), [`pyproject.toml`](../../pyproject.toml).
- Notes liées : [02_ARCHITECTURE_AUDIT_2026-06.md](02_ARCHITECTURE_AUDIT_2026-06.md), [UI_REPLACEMENT_ANALYSIS.md](../UI_REPLACEMENT_ANALYSIS.md) (historique avril 2025), [MASTRA_PORT_ANALYSIS.md](../MASTRA_PORT_ANALYSIS.md).
- Streamlit (limites structurelles) : issues [#4297](https://github.com/streamlit/streamlit/issues/4297) (session_state lié au websocket), [#8901](https://github.com/streamlit/streamlit/issues/8901) (reconnect), [#14524](https://github.com/streamlit/streamlit/issues/14524) (`st.write_stream` & rerun).
- État de l'art RAG : [Onyx](https://github.com/onyx-dot-app/onyx), [Quivr](https://github.com/QuivrHQ/quivr), [Verba](https://github.com/weaviate/Verba), [RAGFlow](https://github.com/infiniflow/ragflow), [Open WebUI](https://github.com/open-webui/open-webui), [LibreChat](https://www.librechat.ai/), [suitenumerique/conversations](https://github.com/suitenumerique/conversations).
- Standards / SDK : [Vercel AI SDK Data Stream Protocol](https://vercel.com/blog/ai-sdk-3-4), [assistant-ui](https://www.assistant-ui.com/), [`@mastra/ai-sdk`](https://www.npmjs.com/package/@mastra/ai-sdk), [Mastra Studio](https://mastra.ai/en/docs/getting-started/studio).
- Gateway / fallback : [LiteLLM Router](https://docs.litellm.ai/docs/router_architecture), [Albert API (DINUM)](https://ia.numerique.gouv.fr/outils-ia/albert-api) (basée OpenGateLLM).
- DSFR / auth gouv : [betagouv/dsfr-kit](https://github.com/betagouv/dsfr-kit) (binding Streamlit existant mais partiel), [react-dsfr](https://github.com/codegouvfr/react-dsfr), [InseeFr/Keycloak-FranceConnect](https://github.com/InseeFr/Keycloak-FranceConnect).
