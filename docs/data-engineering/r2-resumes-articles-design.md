# R2 — Résumés d'article en langage métier RH : design de stockage & pipeline

**Date** : 2026-07-21 · **Branche** : `feat/r2-article-summaries` (worktree `/tmp/wt-r2-summaries`)
**Référence stratégique** : `docs/evals/revue-strategies-qualite-rag.md` §2.3 (R2 = vaisseau amiral, sonde du 17/07 : q194 rang 122→25, q229 183→24, 5/7 misses profonds projetés récupérés, contrôle différentiel PASS).
**Principe non négociable** : *le résumé TROUVE, il ne DIT jamais* — le générateur de réponses ne reçoit que le texte juridique authentique.

---

## 1. État des lieux (mesuré en base + code)

- `rag_chunks_dgafp` (staging) : **4 207 lignes = 4 207 cids distincts** → `single_chunk_per_article=True` (GoldConfig) est la réalité du corpus : 1 article = 1 chunk `{cid}_0`.
- Schéma (créé/évolué par `LegifranceDbWriter._ensure_table` + `LEGACY_TARGET_COLUMNS`, **pas** par les migrations supabase — aucune migration `rag_chunks_dgafp` dans `supabase/migrations/`) : PK `id BIGSERIAL`, `chunk_id VARCHAR(64) UNIQUE`, `cid`, `chunk_text`, `text`, méta (title, full_title, number, category, url, subtitles, section_parent_*, liens…), `chunk_text_tsv` **GENERATED ALWAYS AS to_tsvector('french', chunk_text)**, `embedding_m3 vector(1024)`, `embedding_bge_scw`, `embedding_qwen3`.
- Retriever (`retriever.py`) : sélectionne `t.chunk_id, t.chunk_text, 1 - (t.embedding_m3 <=> query)` — **l'embedding sert à trouver, chunk_text sert à servir**. dgafp `has_sections=False` → pas de heading-search, chaque chunk est *standalone* à l'agrégation.
- Agrégation (`section_aggregator.py`) : clé de groupe standalone = `_standalone_{chunk_id}` → deux chunks du même article deviennent **deux sections concurrentes** (slots reranker `_MAX_RERANK_INPUT=20`).
- Ingestion delta (`legifrance/db.py::_ingest_bundle_tx`) : à la ré-ingestion d'un article (checksum changé), **tous les chunks du `cid` absents des new_ids sont supprimés** → une ligne-résumé rattachée au même `cid` est purgée automatiquement quand l'article change. C'est le mécanisme delta gratuit.
- Backfill embeddings (`jobs/embeddings_backfill.py`) : remplit `embedding_m3` **depuis chunk_text** pour les lignes `WHERE embedding_m3 IS NULL` → ⚠️ piège identifié : une ligne-résumé insérée sans embedding serait ré-embeddée sur le texte authentique (perte du levier). Parade : les lignes R2 sont TOUJOURS insérées avec leur embedding (et l'upsert `preserve_on_null_cols` protège l'existant).
- Modèle d'embedding requête : Albert `openweight-embeddings` (1024 dims, `ALBERT_EMBED_MODEL`) — les vecteurs R2 doivent vivre dans le même espace.

## 2. Options évaluées

### Option A (retenue) — lignes additionnelles dans `rag_chunks_dgafp`

Une ligne par article : `chunk_id = {cid}_r2s` (24 chars, tient dans VARCHAR(64)),
- `embedding_m3` = **embedding du RÉSUMÉ** (Albert `openweight-embeddings`) ;
- `chunk_text` = **texte AUTHENTIQUE** de l'article (identique à `{cid}_0`) → le pipeline sert le texte source, *aucune modification runtime du serving* ;
- `text` = le résumé lui-même (trace/audit de ce que le vecteur encode — même convention que la paire text/chunk_text des chunks normaux : `text` = matière brute, `chunk_text` = ce qui est servi) ;
- `index_variant` (colonne **nouvelle**, TEXT, NULL pour les lignes normales) = `r2_summary/{version-génération}+embed-{modèle d'embedding}/{sha16(texte source)}` → marqueur + clé de fraîcheur ;
- méta copiées de la ligne `{cid}_0` (title, number, url, cid…) → pills sources identiques.

**Pourquoi elle gagne** :
- **Additif/réversible** : `DELETE WHERE index_variant LIKE 'r2_summary/%'` restaure l'état antérieur ; zéro régression structurelle possible (constat sonde).
- **Zéro changement du chemin de recherche/serving** : les lignes passent dans les requêtes SQL existantes (semantic, hybrid, lexical) sans modification ; le texte servi est le texte juridique authentique → le principe « trouve, ne dit jamais » est **structurel**, pas contractuel.
- **A/B trivial** : insérer/supprimer les lignes suffit (pas de déploiement de code retrieval pour tester).
- **Delta gratuit** : la ré-ingestion d'un article purge sa ligne `_r2s` (delete par cid) ; le job R2 en re-planifie la génération par comparaison `index_variant` attendu (version + checksum du texte courant) vs stocké.
- **Migration** : ajout de `index_variant` à `LEGACY_TARGET_COLUMNS` → `_ensure_table` fait l'`ALTER TABLE ADD COLUMN` au premier run (mécanisme natif de cette table ; pas de migration supabase, cohérent avec l'existant).

**Coûts/implications assumés** :
1. **Dédup à l'agrégation** (seul changement runtime, LOW risk mesuré via impact GitNexus : 1 dépendant direct, module Tests) : la clé standalone devient `cid` quand la méta le porte (`_standalone_cid_{table}:{cid}`), sinon `chunk_id`. La ligne `_r2s` et la ligne `_0` du même article **fusionnent** en une section (leurs chunk_text sont identiques → markdown inchangé) au lieu de consommer 2 des 20 slots du reranker. Deux hits du même article = signal `chunk_count` légitime pour le score d'agrégat. NB : avec 1 chunk/article en base, la fusion par cid est sans perte par construction ; garde ceinture-bretelles : on ne fusionne par cid que des chunks du même table_source.
2. **tsv dupliqué** : `chunk_text_tsv` étant généré depuis chunk_text (authentique), la recherche lexicale/hybride retournera les deux lignes — dédupliquées par (1). Pas de pollution lexicale par le vocabulaire du résumé (le résumé n'est PAS dans le tsv — choix délibéré : R2 est un levier d'espace *sémantique* ; l'enrichissement lexical est une stratégie séparée du catalogue).
3. **Lookup références juridiques** (`legal_refs`, ContextBuilder) par number/cid : renverra 2 lignes aux valeurs identiques (cid, url, chunk_text) → inoffensif ; les consommateurs prennent la première.
4. **Index ivfflat** : +4,2k lignes sur `embedding_m3` (~×2) — dans les marges de l'index existant ; recall à re-mesurer par l'A/B goldset de toute façon.
5. **`embedding_bge_scw`/`embedding_qwen3` restent NULL** sur les lignes R2 : le chemin bge_scaleway (fallback embedder) ne voit pas les résumés — acceptable v1 (le primaire prod est Albert), documenté pour l'A/B.

### Option B — table dédiée `rag_chunks_dgafp_r2` fusionnée comme source
Rejetée : exige de brancher une table dans `CHUNK_TABLES` + la liste `tables` de la config runtime (v3), crée une **source RRF supplémentaire** dans `_merge_cross_source_ranks` (le résumé et l'article ne fusionnent jamais par clé `(table_source, chunk_id)` → biais de rang cross-source, publisher/pills à dupliquer), et le delta ingestion ne cascade pas (la purge par cid ne touche que la table legacy). Plus de surface runtime, plus de drift, pour zéro bénéfice fonctionnel.

### Option C — colonne `embedding_summary vector(1024)` sur la ligne existante
Rejetée : modification du **hot path** SQL du retriever (UNION ou double ORDER BY par table), nouvel index ivfflat obligatoire, logique par-table à conditionner (seule dgafp l'aurait), et A/B impossible sans déploiement de code. Élégant sur le papier (dédup gratuite), mais casse la propriété « additif = zéro changement runtime » qui a justifié R2 contre R5.

## 3. Pipeline de génération (pattern `page_vision.py`)

Module `utils/article_summary.py` (corpus-agnostique — v1 dgafp, réutilisable PDF ministères) :
- `R2_LOGIC_VERSION = "r2s1"` — version de la LOGIQUE (prompt-contrat + garde de fidélité). À incrémenter à chaque évolution.
- `AlbertArticleSummarizer` : `/chat/completions` Albert, `temperature=0`, `max_tokens=700` ; `version = {R2_LOGIC_VERSION}-{model}-p{sha1(prompt)[:8]}` → toute évolution du modèle OU du prompt invalide le cache (même piège que page_vision).
- **Garde anti-invention de valeurs** `unsourced_numbers(summary, source)` : tout token numérique du résumé absent du texte source (comparaison sur chiffres normalisés) ⇒ résumé **rejeté** (l'article reste sans ligne R2 ; déterministe, pas de retry). Filet grossier assumé — le résumé n'étant jamais servi, le risque résiduel est un biais de retrieval, pas une hallucination utilisateur.
- Résumé trop court/vide ou tronqué (`finish_reason=length`) ⇒ rejeté.
- **Cache versionné** : `{cache_root}/article_summaries/{name}/{version}/{cid}/{sha256(source)}.json` (payload : cid, checksum, résumé, tokens in/out, modèle). Local d'abord ; le répertoire est synchronisable tel quel vers le bucket bronze via `ScalewayObjectStorageSync._sync_dir` (même convention de clés que `pdf_store.page_vision_cache_key`). Reprise idempotente : hit → zéro appel LLM.
- **Throttle** : `MAX_SUMMARY_WORKERS = 2` (contrainte API partagée), échec transitoire (réseau/429/5xx) → `failed` (retenté au run suivant), rejet du garde → `rejected` (pas de retry en boucle).

Intégration gold `legifrance/summary_rows.py` :
- `plan_missing_summaries(rows, version)` : compare `index_variant` attendu (`r2_summary/{version}+embed-{modèle}/{sha16(chunk_text)}`) vs stocké → liste des articles à (re)générer. Idempotent, delta par checksum.
- `build_summary_chunk_row(article_row, summary, embedding)` : ligne additive complète (`_targets=["legacy"]`, embedding_m3 **toujours renseigné** — cf. piège backfill).
- Job CLI `jobs/r2_article_summaries.py` : `--dry-run` par défaut (plan JSON), `--out` JSONL (lot pilote), `--apply` requis pour écrire en base via `upsert_legacy_chunks` (upsert sur chunk_id = idempotent). **Non exécuté avec --apply dans cette phase** (gate revue humaine).

## 4. Résultats du lot pilote (21/07, 101 articles, staging lecture seule)

Lot : 8 golds des misses profonds (q194×2, q212, q213, q217, q221, q229, q657) + 93 voisins de Titre (`SCRATCH/r2_pilote/`). Modèle `openweight-medium`, version `r2s1-openweight-medium-pfc72d953`.

- **101/101 acceptés** après ajout de deux raffinements découverts par le premier passage (6/101 rejetés, dont 2 golds — tous des faux positifs ou récupérables) :
  1. **numéraux français** : la source qui écrit « pendant trois mois » autorise le chiffre `3` dans le résumé (décret 86-83 art. 14 : « un/deux/trois mois » en toutes lettres) — sens source→chiffre uniquement ;
  2. **passe de correction unique** : à température 0, re-soumettre la proposition + ses valeurs fautives avec consigne de rester vague convertit les résumés fidèles-mais-chiffrés (art. 12 : « 90 %/la moitié » → « barème progressif, taux plein puis réduit ») et supprime les vraies inventions. Un rejet résiduel = pas de ligne R2 (baseline).
- **Fidélité (revue manuelle de 10, dont les 8 golds)** : aucune valeur normée inventée ; vocabulaire métier présent (« garde-t-il son salaire » ↔ « maintien du traitement », CDD/CDI, cumul d'activités…) ; 2 extrapolations bénignes de vocabulaire (q221 « CDD ou CDI », q229 « autorisation préalable ») — acceptables pour un texte jamais servi.
- **Coût réel mesuré** : 632,5 tokens in / 142,1 tokens out par article (surcoût de correction inclus, ~6 % des articles). **Extrapolation corpus (4 207 articles) : ~2,66 M tokens in / ~0,60 M out**, ~3 h de mur à 2 workers — environ la moitié de l'estimation initiale de la revue (5,1 M/1,5 M).

## 5. Ce qui reste avant l'A/B goldset

1. Revue humaine du lot pilote (fidélité, vocabulaire) + de cette PR.
2. Génération corpus complet (~4 207 articles ; coût mesuré au pilote, extrapolation dans le rapport).
3. Insertion staging via `--apply` (gate humaine) + `ANALYZE`/vérif index ivfflat.
4. Run éval goldset vs baseline 118 (`run-rag-eval`), journalisation obligatoire.
5. Décision : étendre aux PDF ministères / calibrer (R5 en réserve).


## Amendements revue #332

- **Clé de fraîcheur** : le modèle d'embedding entre dans `index_variant` — changer d'espace vectoriel invalide toutes les lignes R2 au prochain plan.
- **Apply** : revalidation existence+checksum `FOR UPDATE` dans la transaction d'upsert (une ingestion concurrente bloque derrière le verrou ; article supprimé/modifié → ignoré + rapporté `stale_skipped`). Testé en interleaving PostgreSQL réel (tests/test_r2_pg_interleaving.py, gated R2_PG_TEST_DSN).
- **Dédup précoce** : le retriever sur-échantillonne (x2) la table dgafp et fusionne la paire `{cid}_0`/`{cid}_r2s` AVANT troncature à top_k (la fusion de l'aggregator reste en filet). 
- **Backfill/audit embeddings** : les lignes R2 (`index_variant` renseigné) sont exclues des deux côtés — jamais de vecteur calculé depuis `chunk_text` pour une ligne R2 ; `embedding_bge_scw` y reste NULL par design.
- **Comptage corpus** : `list_legifrance_corpus` exclut les lignes R2 de `nb_chunks` (sinon double comptage et delta faussé).
- **Mode plan sans clé** : `ALBERT_API_KEY` n'est exigée qu'à la génération.
- Annexes pilote complètes : `r2-pilote/pilot_summaries.jsonl` (101 sorties brutes, tokens/statuts) + `pilot_stats.json`.
