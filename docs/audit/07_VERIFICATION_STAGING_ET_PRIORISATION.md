# Vérification sur staging réel & priorisation refondée

> Dossier d'audit : voir [README](README.md). Date : 2026-06-15.
> Objet : **contre-audit** des notes 00-06 contre (a) le **code actuel de `main`** (incluant les fix #87/#88 et #95-#98) et (b) la **base staging réelle** via `SCW_POSTGRES_DSN_STAGING` (et non plus une copie locale). But : confirmer ce qui tient, corriger les chiffres périmés, et **reprioriser sur l'état réel de staging au 15 juin**.
> Méthode : 3 passes de relecture croisée du code (reranker/retriever/scoring/fail-open, logging `chat_runs`, sécurité/archi) + 8 requêtes SQL en lecture seule sur staging (`SET default_transaction_read_only=on`). Aucune écriture.

---

## 0. Verdict

**Le dossier est solide et, fait rare, réellement reproductible : ses diagnostics centraux tiennent contre le code et contre staging.** Le cœur (reranker cassé puis réparé, scoring RRF peu discriminant, fail-open systémique, observabilité conçue mais non câblée, dispositif d'éval aveugle) est **exact et grounded**.

Mais l'audit a été produit sur une **copie locale figée** : plusieurs chiffres sont **périmés** par rapport à staging, et **deux constats doivent être recadrés** car ils changent la priorisation :

1. **Le trou de couverture Service-Public est déjà refermé sur staging** (55/55 fiches ont des chunks). Le « rejeu staging à exécuter » du dossier est **fait**. Le vrai problème de couverture vivant aujourd'hui, c'est **DGAFP, MSO et MATTE**, pas SP.
2. **DGAFP n'a pas « un index vectoriel manquant » : il a 0 embedding** (3 992/3 992 `embedding_m3` NULL). C'est un corpus entier éteint en recherche sémantique — de la même classe que le bug reranker. L'index est un faux sujet ; le vrai correctif est le **backfill des embeddings**.

Le reranker, lui, est **réparé ET déployé sur staging** : `v3_reranker_status='completed'` sur 24/25 des runs de juin (dont aujourd'hui), 1 `failed`. Il ne reste que l'alerting.

---

## 1. Ce que la vérification sur staging change (copie locale → staging live)

| Sujet | Dossier (copie locale) | **Staging réel (15/06)** | Conséquence |
|---|---|---|---|
| Couverture Service-Public | 31/53 fiches sans chunk (58 %) ; « rejeu à exécuter » | **55/55 fiches ont des chunks ; 0 trou** | ✅ **résolu sur staging** — sortir de P0, ne reste que vérif prod |
| Embeddings DGAFP | « 3 992 chunks emb m3 » (note 06) vs « NULL à confirmer » (note 01) | **0/3 992 `embedding_m3` (100 % NULL)**, idem bge_scw/qwen3 | ⚠️ **corpus invisible en sémantique** — backfill, pas index |
| Index vectoriel DGAFP | manquant (à créer) | manquant **mais rien à indexer** (0 embedding) ; l'index HNSW existe sur le fantôme `dgafp_scalingo` | index = faux sujet tant que pas d'embedding |
| Chunks SP | 1 554 | **2 782** (tous avec `embedding_m3`) | ré-ingestion SP passée sur staging |
| Sections | 5 962 | **6 100** | corpus a grossi |
| `references_juridiques` | 279/5 962 (4,7 %) | **1 136/6 100 (18,6 %)** | constat « trop peu » tient, mais base 4× plus haute |
| FK dans toute la base | « 30 FK » | **3 FK** (0 sur la chaîne rag) | constat « pas d'intégrité » **renforcé** |
| `chat_runs` colonnes | 154 | **156** ; 24 ghost cols ne sont jamais écrites | « ~33 jamais écrites » tient (156 − ~123 écrites) |
| `rag_chunks_test` | absente du seed local | **absente de staging** ET `v3_enable_chunks_test=true` | ✅ fail-open vivant à chaque requête |
| Reranker | « réparé via #88 » | **réparé + déployé** (24/25 juin `completed`) | ne reste que l'alerting |

Les chiffres **identiques** au dossier (donc validés sur staging) : helpful rate **74,1 %**, janvier **69,5 %**, `retrieval_issue` **58 %** / `missing_document` **23 %** des négatifs, no-answer **19 %** (571/3 083), matte 762 NULL bge, rgrh **146/324 (45 %)** sans m3, max section **174 337**, sections < 300 chars **32 %**, doublons SP **33 %**, `goldset_questions_v2` **vide**, `chat_reviews` **12**, tables fantômes `_scalingo`/`_scw` présentes, config prod = `semantic` / `top_k=20` / selector ON / `wide`.

---

## 2. État réel de staging au 15 juin (la « source de vérité »)

**Contenu invisible au retrieval (problème dominant, vivant) :**

| Corpus | Chunks | Embedding `m3` | Interrogé par le retriever ? | État |
|---|---|---|---|---|
| `service_public` | 2 782 | 100 % | oui (+ ivfflat) | ✅ OK |
| `matte` | 959 | 100 % | oui, **sans index vectoriel** | ◐ seq scan ; **17/44 docs sans chunk** |
| `dgafp` | 3 992 | **0 %** | oui (legal-intent only) | ❌ **éteint en sémantique** |
| `rgrh` | 324 | 55 % (146 NULL) | oui, sans index | ◐ 45 % exclus silencieusement |
| `mso` | 1 262 | 100 % (+ ivfflat) | **non** (hors `v3_tables`) | ❌ **16 docs invisibles** |
| `rag_chunks_test` | — | — | activée en config, **table absente** | ❌ **fail-open à chaque requête** |

**Config active (`rag_config`, 1 ligne)** : `v3_search_mode=semantic`, `v3_initial_top_k=20`, `v3_enable_selector=true`, `v3_context_mode=wide`, `v3_enable_reranker=true`, **`v3_enable_chunks_test=true`**, `v3_tables=[matte, service_public, dgafp, rgrh]`, `v3_token_budget=8000` (incohérent avec WIDE=12 000 via getter), `relevance_threshold=0.3`. Le blob contient encore ~30 clés v2 mortes (`enable_mmr`, `enable_hyde`, `boost_*`, `dedup_threshold`, `chunk_selection_mode`…).

**Latence réelle (runs v3 avec timing, staging)** — dimension « à mesurer » (D9) désormais mesurée :

| Étage | p50 | p95 | max |
|---|---:|---:|---:|
| query processing | **2 168 ms** | 3 263 ms | 22 s |
| retrieval | 400 ms | 5 077 ms | 18,7 s |
| selector | 2 313 ms | 4 403 ms | 16,4 s |
| generation | 2 806 ms | 7 338 ms | 23,8 s |

→ ~8 s médian bout-en-bout ; query-processing à 2,2 s p50 vs 200-500 ms « documentés ». Réel et matériel.

**Usage & feedback en extinction** : `chat_runs` tombe à **9 en mai, 60 en juin** ; **aucun feedback après avril**. Le helpful rate, métrique-phare, **devient aveugle** — relancer la collecte est un prérequis à toute mesure.

---

## 3. Vérification des constats (synthèse)

| # | Constat du dossier | Verdict staging/code | Note |
|---|---|---|---|
| 1 | Reranker `/rerank` cassé (422) | ✅ **réel, réparé, déployé** | payload `query/documents/top_n` ; 24/25 juin `completed` ; reste alerting |
| 2 | Score RRF peu discriminant (k=60, normalisé plafond) | ✅ vérifié (code) | `retriever.py:301-328` ; « 8 listes » → en réalité ~6 |
| 3 | 58 % fiches SP sans chunk | ✅ réel **mais résolu sur staging** | 55/55 ; reporter l'effort sur MATTE/MSO |
| 4 | Disparité auto-éval/expert, goldset vide | ✅ vérifié | `goldset_questions_v2`=0 ; négatifs déjà catégorisés (amorce de goldset) |
| 5 | `chat_runs` 154 col, ~33 jamais écrites | ✅ vérifié (156 ; 16 diag nommées **jamais écrites**) | « 126 écrites » → 79 par `build_log_row` |
| 6 | Index vectoriels manquants (matte, dgafp, rgrh) | ◐ **à recadrer** | matte = vrai (959 emb, seq scan) ; **dgafp = 0 emb (index moot)** ; rgrh tiny |
| 7 | Fail-open sans métrique | ✅ vérifié | sauf reranker (persiste désormais le statut) |
| 8 | Pas d'observabilité / alerting | ✅ vérifié | 0 APM, diag non câblés |
| 9 | Tests RAG dispersés ; schéma non versionné | ✅ vérifié | 0 test dans le package ; 2 migrations ; schéma `chat_runs` hors code |
| 10 | Sécurité UI (XSS, SQLi, root, RGPD) | ✅ vérifié | XSS `01_Chatbot.py:1188` ; SQLi `09:517-545` (**pas** copié en page 11) ; 7 Dockerfiles root |
| 11 | Doublons SP 33 %, chunks-titres, sections géantes | ✅ vérifié | 756 doublons (27 %), 33 % <200, max 174 337 |
| 12 | DGAFP quasi absente | ✅ **confirmé + cause établie** | 0 embedding + drop hors legal-intent |

---

## 4. Corrections à apporter au dossier avant merge (commentaires de revue)

Le dossier est mergeable comme document de cadrage, mais ces points devraient être corrigés pour ne pas figer des chiffres faux :

1. **Note 06 §2.2** : la cellule « `rag_chunks_dgafp` — 3 992 (emb m3) » est **fausse** (0 emb m3 sur staging). C'est la note 01 C5 / la revue kaaloo qui ont raison. Recadrer : DGAFP = *backfill embeddings*, pas *index manquant*.
2. **Note 01 add.1 & note 00** : marquer SP **résolu sur staging** (55/55), pas « rejeu à exécuter ». L'effort coverage restant = **MATTE 17/44 + MSO 16/16**.
3. **Note 06 §2.1** : « 30 FK dans la base » → **3** sur staging (constat renforcé, chiffre à corriger).
4. **Note 01 §2.3** : `references_juridiques` 4,7 % → **18,6 %** sur staging (revérifier le critère « exploitable »).
5. **Note 06 §1.1 / note 02 A6** : « logger écrit 126 colonnes » → **79** (`build_log_row`) ; 156 colonnes en base. Le fond (~33 jamais écrites) tient ; corriger le compte et l'attribution (`build_non_rag_row`/`_prepare_data` pour les colonnes v2 mortes, pas `build_log_row`).
6. **Note 01 §2.1** : « 8 listes RRF » → **~6** (4 chunk + 2 heading, seuls matte/SP ont des sections).
7. **Note 02 §5 S2** : le motif SQLi n'est **pas** recopié dans `11_Golden_Beta_Analysis.py` (paramétré `%s`). Retirer cette mention.
8. **Note 02 A1** : le port Mastra **a** câblé le rerank (`lib/albert.ts` `rerankWithAlbert`, appelé par le step) ; seul le *resolver gateway* est un stub (TODO). Nuancer « rerank non branché ».

---

## 5. Priorisation refondée sur l'état réel

> Principe inchangé du dossier : **mesurable et observable d'abord, retrieval/données ensuite, multi-ministère après**. Mais le **contenu de P0 change** : SP est fait ; les vraies pannes silencieuses vivantes sont DGAFP/MSO/chunks_test/index, toutes vérifiées sur staging et toutes bon marché.

### P0 — Pannes silencieuses vivantes + couverture (semaine du 16/06, risque faible, fort impact)

> Toutes mesurables sur staging *maintenant*. Même classe que le reranker : un sous-système éteint sans bruit.

| # | Action | Effort | Métrique visée |
|---|---|---|---|
| P0.1 | **DGAFP : backfill `embedding_m3` (0/3 992)** + index ivfflat. **Les embeddings existent déjà, complets, dans le fantôme `rag_chunks_dgafp_scalingo` (3 992/3 992 m3+bge_scw+qwen3, mêmes clés `chunk_id`/`cid`)** → probablement une **copie keyed** (`UPDATE … FROM …_scalingo`) plutôt qu'un re-embedding coûteux ; vérifier d'abord la parité du `chunk_text` entre les deux tables | **S** si copie keyed (sinon M re-embed) | recall requêtes juridiques |
| P0.2 | **`rag_chunks_test` : table absente + flag actif.** Créer la table **ou** `v3_enable_chunks_test=false`. Rendre « table activée absente » = erreur dure, pas warning avalé | S | recall + fin d'un fail-open par requête |
| P0.3 | **Index vectoriel MATTE** (959 emb, seq scan à chaque requête) | S | latence/coût |
| P0.4 | **Backfill `embedding_m3` RGRH (146/324)** | S | recall corpus RGRH contrôlant |
| P0.5 | **Alerting reranker** (fix vivant, 1 `failed`/25) : alerte si taux `v3_reranker_status='failed'` > 1 % | S | non-régression du fix |
| P0.6 | **Check de réconciliation index en CI** : tout doc indexable → ≥ 1 chunk embeddé dans une table *interrogée*. Aurait attrapé DGAFP/MSO/MATTE/SP | S-M | couverture, anti-récidive |
| P0.7 | **MSO (1 262 chunks, 16 docs, non interrogé)** : câbler au retriever **ou** retirer du corpus (décision) | S→M | couverture |
| P0.8 | **MATTE 17/44 docs sans chunk** : rejouer le backfill chunk/embeddings (même schéma que SP) | M | couverture |

### P1 — Rendre mesurable (bloquant : rien n'est jugeable sans ça, 16/06 → 18/07)

- **Goldset v1** (80-120 Q) depuis `chat_feedbacks` — les **197 négatifs sont déjà catégorisés + thématisés** (amorce). **Inclure** `missing_document`/no-answer (ne pas les exclure comme aujourd'hui).
- **Câbler les ~33 colonnes diag déjà présentes** (`v3_top1_score`, `v3_chunks_before/after_rerank`, scores) **ou** table de traces → observabilité retrieval sans toucher au schéma.
- **Set de calibration** auto-éval vs expert (κ ≥ 0,7) — conditionne l'usage de toute métrique auto.
- **Dashboards** (usage, helpful, latence p50/p95 par étage, échecs provider) + **3 alertes** (rerank, fallback, no-answer).
- **Relancer la collecte de feedback** (éteinte depuis avril ; 60 runs en juin).

### P1.5 — Retrieval / scoring / données (après baseline)

- **Scoring v2** : score reranker comme signal aval ; **câbler `relevance_threshold`** (0,3 est en config — vérifier qu'il est appliqué) pour l'abstention.
- **Bascule hybride** réversible, validée sur le goldset (le sémantique domine : 816 vs 237 runs).
- **Dédup SP** (756 copies) + filtrage chunks-titres (33 % < 200) à l'ingestion.
- **Étendre `references_juridiques`** (18,6 % → cible).
- **Boucle trous documentaires** (classe RIFSEEP) : no-answer/`missing_document` → backlog ingestion.

### P2 — Multi-ministère (après fondations qualité)

Scope `ministry_id` serveur (filtre SQL avant retrieval), ingestion MI/MSO/MASA/MEF, ProConnect/habilitations. **Le check de réconciliation P0.6 est un prérequis** : sans lui, on multiplie le bug « corpus invisible » par 6.

### P2.5 — Produit/UX + dette schéma

Affichage enrichi des sources ; rationalisation `chat_runs` (156 col, ~33 mortes, ghosts `_scalingo`/`_scw`, 0 FK) ; rétention/RGPD (`chat_runs` depuis oct. 2025, données RH identifiantes) ; corrigés sécu (échapper le HTML S1, paramétrer SQL S2, `USER` non-root S4).

### P3 — Exploratoire (itération 3)

Réemploi pipeline, deep-research borné, GraphRAG, upload — conditionnels aux fondations.

---

## 6. Métriques & acceptation

| Métrique | Baseline staging (15/06) | Cible itération 2 |
|---|---|---|
| Couverture index (doc indexable → chunk embeddé interrogé) | SP 100 %, **MATTE 61 %, DGAFP 0 % sém., MSO 0 %** | 100 % + check CI |
| Recall@10 (goldset) | à figer (P1) | +20 pts |
| No-answer / questions répondables | **19 %** | < 10 %, ≥ 90 % justifiés |
| Helpful rate (4 sem.) | **74,1 %** (mais collecte éteinte) | ≥ 85 % + collecte relancée |
| Accord juge↔expert (κ) | non mesuré | ≥ 0,7 |
| Échec rerank / fallback / no-answer | rerank ~4 %/mois, **non alerté** | < 1 %, alerté |
| Latence p50 bout-en-bout | **~8 s** | pas de régression, viser < 5 s |

**Acceptation** : aucune bascule config/scoring en prod sans run goldset avant/après archivé (principe du dossier, maintenu).

---

## 7. Limites / non vérifié

- **Prod jamais touchée** : tout ci-dessus est staging. À reconfirmer en prod : embeddings DGAFP, présence `rag_chunks_test`, déploiement du fix reranker, latences.
- **Cause racine DGAFP 0-embedding partiellement établie** : les embeddings (m3, bge_scw, qwen3) + l'index HNSW sont **complets dans le fantôme `rag_chunks_dgafp_scalingo` (3 992/3 992)**, vides dans la table vive `rag_chunks_dgafp` et dans `_scw` — les colonnes partagent les mêmes clés (`chunk_id`, `cid`). Hypothèse : migration des chunks faite sans report des embeddings. **Avant de ré-générer les embeddings, tester une copie par clé depuis `_scalingo`** (vérifier que le `chunk_text` est identique entre les deux).
- **`relevance_threshold=0.3`** est en config ; non confirmé qu'il soit appliqué dans le code de scoring (à vérifier en P1.5).
- Latences sur sous-ensemble de runs v3 avec timing rempli (n ≈ 180-700) — indicatif.

## Sources

- Code `main` : `packages/rag-pipeline/src/assistant_rh_rag_pipeline/` (`reranker.py`, `retriever.py`, `section_aggregator.py`, `pipeline.py`, `chat_logger.py`, `config.py`), `apps/streamlit-ui/pages/`, `src/ui/`, `apps/mastra-pipeline/`, `supabase/migrations/`, commits `0e24884`/`c797381`/`d33e32f`.
- **Staging réel** via `SCW_POSTGRES_DSN_STAGING` (PostgreSQL 17.10) : `information_schema`, `pg_indexes`, `pg_constraint`, comptages et NULL d'embeddings, `rag_config`, `chat_runs` (3 083), `chat_feedbacks` (761), couverture doc→chunk — 2026-06-15, lecture seule.
- Notes [00](00_SYNTHESE_ET_PRIORISATION.md)-[06](06_AUDIT_CODE_ET_DB.md) du dossier.
