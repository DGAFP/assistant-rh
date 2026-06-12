# Audit architectural — code, sécurité, observabilité

> Dossier d'audit : voir [README](README.md). Note précédente : [01 — Audit qualité RAG](01_RAG_QUALITY_AUDIT_2026-06.md).
> Date : 2026-06-09. Périmètre : monorepo `assistant-rh` (Python RAG pipeline, UI Streamlit, port Mastra/TS, data-engineering, CI/CD). Constats vérifiés sur le code et la base locale (copie staging).

---

## 1. Synthèse

Le cœur du pipeline (`packages/rag-pipeline/`) est bien découpé en modules et autonome — c'est le point fort du repo. Les problèmes se concentrent sur quatre axes :

1. **Fiabilité silencieuse** : le système préfère se dégrader sans bruit plutôt qu'échouer visiblement (rerank 422 avalé, table absente avalée, fallbacks sans alerte). C'est la cause racine de la panne de qualité documentée dans la note 01.
2. **Schéma de base non gouverné** : 2 migrations versionnées seulement pour ~22 tables (bootstrap conformance + statut reranker ajouté par #88) ; la dérive local/staging/prod est déjà effective (`rag_chunks_test` absente, tables `_scalingo`/`_scw` fantômes).
3. **Le package critique n'a aucun test** : 0 test dans `packages/rag-pipeline/` ; les 142 tests racine couvrent surtout la conformance Mastra et les scripts CI. La régression du reranker était indétectable.
4. **Surface UI sur-étendue et sous-sécurisée** : 11 pages Streamlit dont des outils d'éval de 1 000–2 500 lignes embarqués en production, XSS possible via le rendu HTML des réponses LLM, SQL interpolé post-auth, conteneurs en root, pas de politique de rétention des logs conversationnels.

---

## 2. Architecture

### 2.1 Points forts

- `packages/rag-pipeline/` : modules à responsabilité claire (`query_processor` → `retriever` → `section_aggregator` → `context_selector` → `context_builder` → `generator`), dataclasses typées, zéro dépendance vers le legacy. Bonne base.
- Worktree/bare-repo discipliné, conventional commits, release-please, pre-commit hooks (ruff, Biome, scan OSV JS).
- CI riche : 19 workflows couvrant build d'images, ingestion, migrations, conformance nightly, security-audit (`pip-audit` Python + OSV JS).

### 2.2 Problèmes structurels

**A1 — Double pipeline Python/TypeScript en parallèle.** Le port Mastra (`apps/mastra-pipeline/`, ~9 850 lignes TS) duplique le pipeline Python (~30 150 lignes) avec un appareil de conformance dédié (6 contrats JSON, baselines, nightly). Coût : chaque évolution se paie deux fois, et le port est incomplet (reranker non branché — `albert.ts` : « TODO: wire embedding/reranking resolvers »). Tant que la cible (Python OU TS) n'est pas tranchée, la conformance est un impôt permanent sur chaque changement de comportement — et elle est sensible à l'ordre, ce qui décourage précisément les améliorations de ranking dont la qualité a besoin (cf. note 01).

**A2 — La logique métier vit dans les pages Streamlit.** `08_Chunking_Evaluation.py` (2 545 lignes), `09_Pipeline_Evaluation.py` (1 728), `01_Chatbot.py` (1 468) contiennent métriques d'éval, mapping de config, A/B testing de groupes, logging — du code non testable, non réutilisable, dupliqué entre pages. Le mapping `rag_config` → `RAGConfig` (config prod réelle) n'existe *que* dans `01_Chatbot.py:1226-1250` : tout autre consommateur (scripts d'éval, notebooks, Mastra) reconstruit sa propre version → c'est l'écart « config documentée vs config réelle » de la note 01.

**A3 — `rag_config` est un fourre-tout non typé.** ~70 clés JSON dont une majorité de clés v2 mortes (`enable_mmr`, `boost_*`, `dedup_threshold`, `enable_hyde`, `chunk_selection_mode`… 1 seule référence générique dans le code v3). Des valeurs contradictoires y dorment (`v3_token_budget: 8000` alors que le mode WIDE actif utilise 12 000 via getter). Personne ne peut dire quelles clés sont actives sans lire le code de la page Chatbot.

**A4 — Schéma de base non versionné.** `supabase/migrations/` contient **2 migrations** (bootstrap conformance, mai 2026 ; statut reranker `chat_runs` via [#88](https://github.com/DGAFP/assistant-rh/pull/88), juin 2026) alors que la base compte ~22 tables, dont des fantômes (`rag_chunks_dgafp_scalingo`, `rag_chunks_legifrance_scw`, `rag_chunks_mso` jamais interrogée, `goldset_runs` disparue). Conséquence directe : staging n'a pas `rag_chunks_test` alors que la config l'active, et personne ne l'a vu (note 01, addendum 1). Il n'existe aucune source de vérité du schéma attendu.

**A5 — Couches legacy entremêlées.** `src/ui/` (« helpers not yet packaged »), `src/_archive/`, `scripts/` (704 Ko de notebooks historiques), duplication des helpers DB (`src/ui/db_utils.py` vs `packages/rag-pipeline/.../db_helpers.py`), deux générations d'ingestion coexistantes (note 01). Le README annonce `src/goldset/` comme « outils d'évaluation » mais la table cible est vide.

**A6 — `chat_runs` : table de log obèse et mal ciblée.** ~125 colonnes par tour, accumulées par strates de versions (`v3_*`, doublons `use_query_rewriting`/`rewritten_query`/`reformulated_query`/`reformulation_model`…, colonnes v2 mortes). Beaucoup ne sont jamais lues. Symptômes : schéma illisible, écritures coûteuses, et surtout **mauvaise granularité pour le diagnostic** — on logge des *compteurs* et des agrégats (`v3_sections_before/after_rerank`, `v3_top1_score` souvent à 0) mais **pas les données qui permettraient une vraie observabilité du retrieval** : le jeu de chunks à chaque étape (retrieval brut par table → fusion → agrégation → rerank → selector → contexte final) n'est pas persisté de façon exploitable. Conséquence : impossible de rejouer ou d'expliquer a posteriori *pourquoi* un chunk pertinent a été perdu (cf. les cas tracés en note 01 §3, reconstitués à la main faute de trace). Le sujet est double — **rationaliser le schéma** (supprimer le mort, normaliser) et **ajouter les bons signaux** (sets de chunks par étape, scores réels, état du rerank). L'audit approfondi (note [06](06_AUDIT_CODE_ET_DB.md)) chiffre le problème : **154 colonnes réelles, 33 jamais écrites par le code — dont précisément les colonnes de diagnostic** (`v3_chunks_raw`, `v3_top1_score`, `v3_chunks_before/after_rerank`…), conçues mais jamais câblées. La note 06 couvre aussi les index vectoriels manquants, l'absence de FK, les tables fantômes et les erreurs fail-open. Voir aussi le plan d'audit D16 (note [05](05_PLAN_AUDIT_ET_COUVERTURE.md)) et l'observabilité (note [03](03_RAG_OBSERVABILITY_ROADMAP_2026-06.md)).

---

## 3. Qualité de code

| Constat | Détail | Impact |
|---|---|---|
| **0 test dans `packages/rag-pipeline/`** | Les 142 tests racine ne couvrent ni le reranker, ni le retriever (hors déterminisme), ni l'aggregator. Aucun test de contrat des payloads providers (embeddings, `/rerank`, LLM) | La rupture d'API Albert `/rerank` (panne totale, note 01) était structurellement indétectable |
| Lint minimal | ruff limité à `E,F,I`, line-length 150 ; pas de `B` (bugbear), `S` (bandit), `RUF` | Classes entières de bugs non détectées |
| mypy installé mais jamais exécuté | Présent dans dev-deps, absent de la CI et sans configuration | Le typage des dataclasses n'est pas vérifié |
| Gestion d'erreurs « avale-tout » | 20 `except Exception` dans le pipeline, 3 bare `except:`, 11 blocs silencieux (`pass`/`continue`). Motif récurrent : `try → log → continue avec résultat dégradé` | Cf. §4 — les pannes deviennent des dégradations invisibles |
| Pas de couverture mesurée | Aucun seuil de coverage en CI | Impossible d'objectiver les zones à risque |
| Fichiers monolithiques | 5 pages > 1 000 lignes ; `retriever.ts` 1 294 lignes | Coût de revue et de modification élevé |

---

## 4. Observabilité — le déficit le plus coûteux

Le paradoxe du repo : `chat_runs` logge ~125 colonnes par tour de chat, mais **aucun signal n'est exploité automatiquement** — et paradoxalement les bonnes données pour le diagnostic n'y sont pas (cf. A6 ci-dessous).

- **Aucun APM / error tracking / métrique** : pas de Sentry, Prometheus, OTel ni équivalent. Le logging est du `logging.getLogger` standard sans configuration centrale, visible uniquement dans les logs de conteneur Scaleway.
- **Les pannes provider sont des warnings de flux** : le rerank cassé loggue « Section reranking failed, keeping aggregated order » à chaque requête depuis des semaines/mois sans qu'aucune alerte n'existe. Idem pour `rag_chunks_test` absente (« Search on X failed » avalé par le ThreadPool, `retriever.py:271-272`) et pour les fallbacks embeddings/LLM.
- **Pas de health check sémantique** : rien ne vérifie périodiquement que les endpoints Albert (modèles, schémas de payload) répondent comme attendu — alors que le projet a déjà subi deux ruptures d'API silencieuses (rerank, et le schéma `/models`).
- **Colonnes de log mal conçues pour le diagnostic** : `v3_sections_before/after_rerank` sont des compteurs (impossible de savoir si le rerank a réordonné) ; `v3_top1_score` est à 0 **parce que la colonne n'est jamais écrite** (note [06](06_AUDIT_CODE_ET_DB.md) §1.1, et non parce que les scores seraient nuls) ; l'état du rerank (ok/échec) n'était pas loggé — corrigé depuis par #88 (`v3_reranker_status`), l'alerting reste à créer.
- **L'analyse automatique des feedbacks crashe** (`'NoneType' object has no attribute 'strip'`) sans supervision.

**Recommandation prioritaire** : (1) error tracking (Sentry self-hosted ou équivalent souverain) sur Streamlit + jobs ; (2) un « provider contract check » quotidien en CI (embeddings + rerank + LLM, payloads réels) ; (3) promouvoir 3 compteurs en alertes : taux d'échec rerank, taux de fallback provider, taux de no-answer.

---

## 5. Sécurité

### 5.1 Constats par sévérité

**Élevé**

- **S1 — XSS via le rendu HTML des réponses LLM.** `01_Chatbot.py:1188` : `st.markdown(t.assistant, unsafe_allow_html=True)` (commentaire : « pour supporter les `<br/>` ») + 28 autres usages d'`unsafe_allow_html`, dont des métadonnées DB interpolées (`title_str`, badges). Le LLM peut être amené — par injection indirecte via le corpus ou la question — à émettre du HTML/JS exécuté dans le navigateur de l'agent. Le viewer PDF injecte aussi `source_url` (DB) dans un `<script>window.open("{url}")</script>` (`_PDF_Viewer.py:365`). Piste : rendre en markdown strict (échapper le HTML), convertir les `<br/>` en amont, valider/échapper les URLs.

**Moyen**

- **S2 — Injection SQL post-auth dans les pages d'éval.** `09_Pipeline_Evaluation.py:517-548` : `goldset_names`/`tags` interpolés en f-string dans le SQL (`",".join(f"'{g}'")`). Les valeurs viennent aujourd'hui de la DB elle-même et la page est derrière `require_admin`, mais le motif est un piège (copié dans `11_Golden_Beta_Analysis.py`). À paramétrer systématiquement.
- **S3 — Modèle d'autorisation admin fragile.** `is_admin()` accorde l'admin sur la valeur d'un cookie chiffré (`user_group == "dgafpallianceadmin"`). La docstring d'`admin_auth.py` documente encore « set via ?group=dgafpallianceadmin URL param » ; seul `01_Chatbot.py:649` neutralise ce groupe côté URL. La sécurité repose entièrement sur la force de `COOKIES_PASSWORD` (avec un fallback local explicite `ALLOW_INSECURE_COOKIES_PASSWORD`) et sur le fait qu'aucune autre page ne posera jamais ce cookie. Pas d'expiration de session admin, pas de comparaison en temps constant du mot de passe (`password == ADMIN_PASSWORD`), pas de rate-limiting.
- **S4 — Conteneurs en root.** Aucun des 7 Dockerfiles ne déclare de `USER` non privilégié.
- **S5 — Données conversationnelles sans gouvernance.** `chat_runs` stocke questions, réponses, `session_id`, `user_group`, prompts complets (3 054 lignes depuis oct. 2025) ; les agents y collent des situations RH individuelles potentiellement identifiantes (cas réels observés dans les feedbacks). Aucune politique de rétention, purge ou anonymisation dans le code ou les migrations. Pour un service public : sujet RGPD/registre de traitement à traiter explicitement.

**Bas / hygiène**

- S6 — Secrets : bonne hygiène git (`.env` ignoré, jamais commité, GitHub Environments). Mais le `.env` local agrège les DSN **prod + staging + local** dans un seul fichier : une erreur d'export suffit à pointer un script local sur la prod (le garde-fou n'existe que dans la prudence des scripts). Suggestion : fichiers séparés + garde-fou applicatif (refus de DSN prod hors CI).
- S7 — Pas de scan de secrets (gitleaks/trufflehog) ni de SAST Python (bandit/semgrep) en CI — `pip-audit` et OSV JS couvrent seulement les dépendances.
- S8 — L'endpoint OpenAI-compatible Mastra ne montre aucune couche d'authentification dans `index.ts` (à confirmer selon le déploiement prévu).

### 5.2 Ce qui est bien

`.env` exclu de git avec historique propre ; secrets par GitHub Environments séparés staging/prod ; échec au démarrage si `COOKIES_PASSWORD` absent en staging/prod ; `pip-audit` + OSV en CI avec hook pre-push ; pas de mot de passe en dur dans le code.

---

## 6. CI/CD et data engineering

- **Workflows complets mais sans gate qualité produit** : la conformance nightly vérifie la parité Python/Mastra, pas la qualité des réponses (goldset vide — note 01, addendum 2). Un déploiement peut dégrader la qualité sans qu'aucun signal CI ne bouge.
- **Migrations** : le workflow `db-migrations-scaleway.yml` existe mais n'a presque rien à appliquer (2 migrations). Le schéma réel a été construit hors bande (notebooks, scripts) — voir A4.
- **Ingestion non idempotente de bout en bout** : les jobs (medallion, embeddings) écrivaient sections et chunks séparément sans étape de réconciliation finale — c'est l'origine des 31 fiches SP sans chunks (note 01). Chaque job devrait se terminer par un check de complétude bloquant. **Mise à jour (2026-06-12)** : confirmé et corrigé pour Service-Public par les PRs [#95](https://github.com/DGAFP/assistant-rh/pull/95)–[#98](https://github.com/DGAFP/assistant-rh/pull/98) (validation fail-fast par fiche, ingestion unique après validation complète du pipeline, préservation des IDs) ; la réconciliation finale côté base (document indexable → ≥ 1 chunk effectif) reste à mettre en CI, et MATTE/MSO ne sont pas couverts.
- Bonne pratique notée : preview staging avant promote prod pour la data-engineering.

---

## 7. Plan d'action recommandé (priorisé)

| # | Action | Axe | Effort |
|---|---|---|---|
| 1 | Tests de contrat providers (embeddings, `/rerank`, LLM) en CI quotidienne + alerte | Fiabilité | S |
| 2 | Error tracking + alertes sur taux d'échec rerank / fallback / no-answer | Observabilité | S–M |
| 3 | Snapshot du schéma DB en migration baseline + interdiction de DDL hors migration ; check de dérive staging/prod en CI | Données | M |
| 4 | Suite de tests unitaires `packages/rag-pipeline/` (reranker, retriever, aggregator, fallbacks) + coverage gate | Qualité | M |
| 5 | Échapper le HTML des réponses LLM et des métadonnées (S1) ; paramétrer le SQL des pages d'éval (S2) | Sécurité | S |
| 6 | Extraire le mapping `rag_config → RAGConfig` dans le package (une seule source de config prod) ; purger les clés v2 mortes | Architecture | M |
| 7 | `USER` non-root dans les Dockerfiles ; gitleaks + bandit en CI | Sécurité | S |
| 8 | Politique de rétention/anonymisation `chat_runs`/`chat_feedbacks` (décision DPO) + job de purge | Conformité | M |
| 9 | Trancher la cible Python vs Mastra et planifier la décommission de l'autre (ou geler le port) | Architecture | décision |
| 10 | Extraire les pages d'éval de l'app de prod (app admin séparée ou CLI) ; réduire `01_Chatbot.py` | Architecture | L |

Les actions 1–3 sont les contreparties « système » des quick wins qualité de la note 01 : elles garantissent que la prochaine panne silencieuse ne durera pas des mois.

## Sources

- Code : `packages/rag-pipeline/src/assistant_rh_rag_pipeline/`, `apps/streamlit-ui/pages/`, `src/ui/admin_auth.py`, `src/ui/cookies_security.py`, `apps/mastra-pipeline/src/`, `Dockerfile.*`, `.github/workflows/`, `supabase/migrations/`, `pyproject.toml`, `.pre-commit-config.yaml`.
- Base locale (copie staging) : inventaire des tables, `rag_config`, `chat_runs`.
- Note liée : [01_RAG_QUALITY_AUDIT_2026-06.md](01_RAG_QUALITY_AUDIT_2026-06.md) (qualité RAG, couverture d'index, dispositif d'évaluation).
