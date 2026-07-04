# Plan — Ingestion PDF par ministère (MI, MASA + rebuild MATTE & MSO + page d'import admin)

> Design arrêté le 2026-07-03. Découpage en issues : [#245](https://github.com/DGAFP/assistant-rh/issues/245) (Phase A — infra partagée) → [#246](https://github.com/DGAFP/assistant-rh/issues/246) (Phase B — MI tracer bullet) → [#247](https://github.com/DGAFP/assistant-rh/issues/247) (Phase C — MASA) / [#248](https://github.com/DGAFP/assistant-rh/issues/248) (Phase D — MATTE & MSO) / [#249](https://github.com/DGAFP/assistant-rh/issues/249) (Phase E — page d'import) / [#250](https://github.com/DGAFP/assistant-rh/issues/250) (Phase F — drift, cron, prod).
> Supersède l'approche « manifest fichier manuel » de #225/#226/#231.

## Context

The Assistant RH corpus must grow to two new ministries (MI — Intérieur, MASA — Agriculture) whose sources are **lists of PDFs** referenced in a Grist doc (La Suite numérique) — not automatable like Service-Public (XML feed) or Légifrance (DILA bulk dumps). MATTE and MSO were ingested the same PDF way but via one-off notebooks (`scripts/amelioration_matte.ipynb`, `scripts/extract_pdf_MSO.ipynb`): no OCR fallback (17/44 MATTE docs have zero chunks), wrong `source` field, no vector index, not reproducible (audit issue #103). This work builds a proper medallion (bronze/silver/gold) ingestion path for PDF-list ministries, rebuilds MATTE and MSO through it, and adds an admin import page so corpus entry can never drift from the manifest.

**⚠️ Base branch**: branch from **`dev`** — the ministry-scope machinery (PR #218: `packages/rag-pipeline/src/assistant_rh_rag_pipeline/ministry_scope.py` with `MINISTRY_CATALOG` {matte, mso}, `SHARED_TABLE_KEYS=("service_public","dgafp")`, live group-driven selectbox in `01_Chatbot.py`; PR #220 removed `rag_chunks_test`) only exists on dev/staging.

## Decisions locked (grilling session 2026-07-03)

1. **Grist = manifest / source of truth** — one row per PDF, we own the table, columns addable. Pipeline validates a required-columns contract at bronze: missing column ⇒ hard fail; invalid row ⇒ rejected + writeback `erreur`, run continues.
2. **PDF binaries in a Scaleway dropzone bucket**, fed by a new **admin-only Streamlit import page** (bucket upload + Grist row created atomically — no console-drift). Grist row carries `cle_bucket`.
3. **Scheduled recurring ingestion** on the existing host (Scaleway serverless jobs, GitHub Actions cron weekly + `workflow_dispatch`), idempotent, delta-aware via PDF sha256.
4. **Per-ministry modules** `mi/`, `masa/`, `matte/`, `mso/` (bronze/silver/gold each, matching the `service_public/`/`legifrance/` convention). **Shared infra** in utils: Grist client, OCR provider, PDF/bucket store, DB writer. Parsing/sectioning is per-ministry and may diverge.
5. **OCR = Albert API `/ocr`** behind an `OcrProvider` interface (`AlbertOcrProvider`; LightOnOCR-1B / Mistral addable later). Bronze caches raw PDF + raw OCR output keyed by sha256 → re-runs never re-pay OCR; providers A/B-able.
6. **Embeddings at ingestion via Albert API** (BGE-M3 → `embedding_m3 vector(1024)`, same vectors as query time; kills sentence-transformers from the job image). Backup column `embedding_bge_scw vector(3584)` populated via the existing Scaleway API path — schema-identical to existing tables.
7. **New tables** `rag_chunks_mi`, `rag_chunks_masa` + clean rebuilds of `rag_chunks_matte`, `rag_chunks_mso` (rename legacy → recreate same name). IVFFLAT + tsv GIN + btree(short_id) indexes from day one. Registration = `CHUNK_TABLES` + `MINISTRY_CATALOG` + group policies.
8. **Reconcile-and-delete** each run: docs absent from manifest or `statut=abrogé` ⇒ chunks+sections+documents deleted (one txn), logged; bronze cache retained for cheap restore. `--dry-run` supported.
9. **MSO included** as 4th ministry (same notebook anti-pattern as MATTE; marginal cost low).
10. **Drift/run reporting: both** — `rag_ingestion_runs` table + `rag_health_exporter` per-ministry metrics AND per-row writeback to Grist (`statut_ingestion`, `derniere_ingestion`, `nb_chunks`, `hash_contenu`, `erreur_ingestion`).
11. **Admin import page** also accepts Légifrance/SP ids (`LEGIARTI…`/`FXXXX`) → whitelist rows in Grist; the **existing** legi/SP medallion jobs read the whitelist and widen their filter at next run (no new per-document fetch client).
12. Ministry routing already live on dev: no retriever redesign — new corpora become catalog entries.

## Verified mechanisms to reuse

- `apps/data-ingestion-cli/.../main.py`: `COMMANDS[(domain, job)] → module.main()` with `default_args` — add entries only.
- `jobs/service_public_medallion.py` + `service_public/pipeline.py`: template for orchestrator (bronze→silver→gold, `--ingest`, `--sync-object-storage`, lake at `data/lake/<source>/`).
- `utils/object_storage.py` (`ScalewayObjectStorageSync`): aws-CLI wrapper, buckets `assistant-rh-{bronze,silver,gold}`, `medallion_prefix()` accepts `pdf_sources/mi` — reuse as-is for cache/sync; `list_objects/download_object/read_text_object` for dropzone ops.
- `service_public/db.py`: generic column-introspecting upserts (`ON CONFLICT`), `replace_chunks_by_short_ids()` (delete+upsert one txn), stable uuid5 doc/section ids, `remap_existing_document_ids`. ~90% source-agnostic → extract.
- `utils/gold.py`: `build_embedders()`, embedder interfaces (pattern for `OcrProvider`).
- CI/CD: `.github/data-engineering-jobs.json` (job registry), `.github/scripts/data_engineering_plan.py` (path-based plan; add `pdf_sources` domain), `data-engineering-preview-staging.yml`, migrations via `supabase/migrations/**` + `db-migrations-scaleway.yml` (`supabase db push --db-url $SCW_POSTGRES_DSN`).
- Gotcha to NOT copy: `service_public/gold.py` hardcodes `source: "SERVICE PUBLIC"` — the bug that leaked into MATTE. Each ministry's gold sets its own `SOURCE`.

## Implementation

### New shared infra — `packages/data-engineering/src/assistant_rh_data_engineering/utils/`
- `grist.py` — `GristClient` (GET/PATCH `/api/docs/{doc}/tables/{table}/records`; env `GRIST_API_BASE_URL`, `GRIST_API_KEY`, `GRIST_DOC_ID`), `ManifestRow`, `validate_manifest_contract()`. Also reads/writes the legi/SP whitelist table.
- `ocr.py` — `OcrProvider` protocol (`name`, `version`, `ocr_pdf(bytes) → OcrResult{markdown, pages, raw_json}`), `AlbertOcrProvider` (Albert `/ocr`), `build_ocr_provider()` factory.
- `pdf_store.py` — `PdfSourceStore` over `ScalewayObjectStorageSync`: dropzone fetch by `cle_bucket`, content-addressed bronze cache get/put (PDF + OCR by sha256). Used by both pipeline and the admin upload page.
- `db.py` — `RagDbWriter`: mechanical extraction of generic `ServicePublicDbWriter` parts + `list_short_ids_with_checksum(source)`, `delete_documents_cascade(short_ids, chunk_table)`, `insert_ingestion_run(summary)`. `ServicePublicDbWriter` becomes thin subclass (zero SP behavior change).

### Per-ministry packages `mi/`, `masa/`, `matte/`, `mso/`
Each: `config.py` (SOURCE/PUBLISHER, chunk table, lake paths, uuid5 namespace, required Grist columns) · `bronze.py` (manifest fetch+validate, dropzone download, sha256, OCR w/ cache-hit) · `silver.py` (OCR markdown → document record with `checksum`=sha256 + heading-based sectioning — **the divergeable part**, seeded from SP's section splitter) · `gold.py` (chunks, sha1 `hash_id` like SP, correct `source`, Albert embeddings) · `pipeline.py` (mirrors `ServicePublicPipeline` + `reconcile()`).

### Job / CLI / infra
- `jobs/pdf_sources_medallion.py` — one orchestrator, `--ministere {mi,masa,matte,mso}` + SP-style flags + `--doc-id`, `--dry-run`, `--skip-grist-writeback`, `--ocr-provider`, `--force-reocr`. Medallion **and** `--ingest` in one job (small volumes).
- CLI: `("mi"|"masa"|"matte"|"mso", "medallion")` → `jobs.pdf_sources_medallion` with `default_args=("--ministere", …)`; embedding backfill manifests `config/{mi,masa,mso}_embedding_tables.json` (matte exists).
- `Dockerfile.pdf_sources_pipeline` (no sentence-transformers → light image), build workflow, 4 entries in `.github/data-engineering-jobs.json`, `pdf_sources` domain in `data_engineering_plan.py`, `config/scaleway_serverless_job_pdf_sources_*.json`, cron workflow `.github/workflows/data-engineering-pdf-sources-cron.yml` (weekly `0 4 * * 1` + dispatch ministere/env/dry_run).
- Legi/SP additions: existing `jobs/{legifrance,service_public}_medallion.py` gain an optional read of the Grist referential (rows with `source_corpus` legi/SP) that widens their document filter (ids `LEGIARTI…`/`FXXXX`).

### Grist contract — révisé 2026-07-03 (Phase A, vérifié sur le doc réel)
**Table unique : le référentiel existant EST le manifest.** Il couvre déjà tous les corpus via `source_corpus` (MI, MASA, MATTE, MSO, RGRH, Service-public, Interministériel/Légifrance) avec un `uid` stable par ligne — pas de table manifest ni de table whitelist séparées. Implémenté dans `utils/grist.py`.
Read (REQUIRED_MANIFEST_COLUMNS) : `source_corpus` (discriminant, filtrage par corpus insensible à la casse) · `uid` (unique → `short_id`) · `titre_document` · `cle_bucket` (colonne ajoutée — lien vers la dropzone ; ligne sans `cle_bucket` ⇒ rejetée ⇒ c'est la liste des PDF à déposer) · `abroge` (`oui` → abrogé ; vide/`non` → en vigueur). `date_publication` ajoutée mais **optionnelle** (jamais bloquante). Les colonnes du suivi manuel (`cle_matching`, `statut_cible`, `statut_ingestion_reelle`, …) sont ignorées par le pipeline.
Writeback — **colonne de statut UNIQUE, partagée opérateurs/jobs (révisé 2026-07-04)** : `statut_ingestion` = (vide)=à ingérer · `ok` (job: présent et à jour, inchangé compris — le détail ingéré/inchangé vit dans `rag_ingestion_runs`) · `erreur` (job, retentée) · `a_supprimer` (**opérateur**: suppression cascade au prochain run) · `supprime` (job après cascade; ligne inactive, ré-activation en vidant la cellule). Plus `derniere_ingestion` · `nb_chunks` · `hash_contenu` · `erreur_ingestion`. `abroge=oui` reste le drapeau juridique et déclenche aussi la suppression. Les colonnes du suivi manuel historique (`statut_ingestion_reelle`, `statut_cible`, `cle_matching`) sont périmées — retrait au profit de `statut_ingestion` (Phase F).
Ajouts unitaires legi/SP (Phase E) : lignes du même référentiel (`source_corpus` Légifrance/Service-public, ids dans `id_extraction`/`legitext`/`jorftext`) — pas de table à part.

### Buckets / bronze layout
- New dropzone bucket `assistant-rh-sources-pdf` (private, versioned): `mi/…`, `masa/…`, `matte/…`, `mso/…` — written only by the admin page.
- **Formats acceptés (révisé 2026-07-04)** : PDF + .doc/.docx/.xls/.xlsx (flux récurrent de bureautique dans les sources ministérielles). La page d'import vérifie la signature par format ; `cle_bucket` garde l'extension d'origine. **Le bronze du pipeline (#246) convertit les non-PDF en PDF (LibreOffice headless dans l'image du job) avant OCR** ; le cache OCR reste indexé par sha256 du fichier d'origine.
- Bronze cache in existing `assistant-rh-bronze`:
  `{env}/bronze/pdf_sources/{ministere}/pdfs/{sha256}.pdf` · `…/ocr/{provider}/{version}/{sha256}.{json,md}` · `…/manifests/manifest_{run_id}.json` (Grist snapshot). Silver/gold via `sync_medallion_root(source_name=f"pdf_sources/{ministere}")`.

### Reconcile algorithm (per ministry run)
1. Fetch manifest → contract check → valid/rejected split (rejected ⇒ writeback `erreur`).
2. `expected = {uid: row | statut=en_vigueur}`; `current = list_short_ids_with_checksum(source)`.
3. Per expected: download → sha256; unchanged (hash match ∧ chunks>0) ⇒ `ignore_inchange`; else OCR (cache-hit) → silver → gold → upsert docs/sections + `replace_chunks_by_short_ids` atomically ⇒ writeback `ok`. Per-doc exceptions caught ⇒ `erreur`, run continues, non-zero exit at end.
4. `orphans = current − expected` ⇒ `delete_documents_cascade`, writeback `supprime` on abrogé rows, all logged. `--dry-run` prints the full plan without writing.
5. Insert `rag_ingestion_runs` summary row.

### SQL migration — `supabase/migrations/<ts>_pdf_sources_ministries.sql`
- `rag_chunks_mi`, `rag_chunks_masa`: modeled on `rag_chunks_service_public` (+ `references_juridiques JSONB`, `section_id`, `source_document_id`, generated `text_tsv`, `embedding_m3 vector(1024)`, `embedding_bge_scw vector(3584)`); indexes: btree(short_id), GIN(text_tsv), IVFFLAT(embedding_m3, lists=100).
- MATTE & MSO: `ALTER TABLE … RENAME TO rag_chunks_{matte,mso}_legacy_<date>` + recreate clean same-name tables (retriever/exporter untouched; legacy kept one release for rollback).
- `rag_ingestion_runs` (run_id, ministere, env, timestamps, expected/ingested/skipped/failed/deleted counts, ocr_provider, details jsonb).
- Pre-flight the renames against live schema (no rag_documents/sections DDL in repo). Mirror into `config/sql/scaleway_postgres_core_schema.sql` (reference).

### Registration (on dev's machinery)
- `rag-pipeline/config.py`: `ChunkTable` entries for `mi`, `masa` (matte/mso exist).
- `ministry_scope.py`: `MINISTRY_CATALOG` entries for `mi` (label "Intérieur"), `masa` (label "Agriculture et Souveraineté alimentaire").
- Group policies / admin group management: grant MI/MASA groups their ministry.
- `jobs/rag_health_exporter.py`: add tables + `rag_ingestion_runs` + drift metrics `assistant_rh_rag_ingestion_{expected,ingested,skipped,failed,deleted}_total{ministere=…}` + last-run timestamp.

### Admin import page — `apps/streamlit-ui/pages/NN_Admin_Import.py` (admin-gated like existing admin pages)
- **Source document path**: upload PDF/.doc/.docx/.xls/.xlsx + ministère/thème/sous-thème → sha256 du fichier d'origine → dropzone PUT (`{ministere}/{uid}_{nom-fichier}.{ext}` avec extension préservée) → Grist row (auto `uid`, `source_corpus`, `cle_bucket`, `abroge` vide) → "ingéré au prochain run planifié". Bucket+Grist atomic at entry; les non-PDF sont convertis en PDF par le bronze avant OCR.
- **Legi/SP path**: input `LEGIARTI…`/`FXXXX` (+ thème) → format validation → row in the same referential (`source_corpus` legi/SP + colonnes id existantes). Existing pipelines pick it up at next run.
- Reuses `GristClient` + `PdfSourceStore`; S3 + Grist credentials on the UI host.

## Phases (reviewable PRs)

- **Phase A — shared infra** ([#245](https://github.com/DGAFP/assistant-rh/issues/245)): `utils/{grist,ocr,pdf_store,db}.py` + SP delegation refactor + unit tests (mocked HTTP/S3; contract & reconcile as pure functions); create dropzone bucket; add Grist columns + whitelist table. Verify: pytest green; one SP staging preview run green (proves db refactor inert); read-only contract check against the real Grist doc; one-page OCR smoke against Albert `/ocr`.
- **Phase B — MI end-to-end staging** ([#246](https://github.com/DGAFP/assistant-rh/issues/246)): `mi/` package, `jobs/pdf_sources_medallion.py`, CLI, migration (mi+masa+runs), Dockerfile/workflows/jobs.json/plan.py, registrations. Verify on staging: 3–5 real MI PDFs via the manual path; run job; check chunk counts, embedding coverage (`observability rag-health --once`), Grist writeback; re-run ⇒ all `ignore_inchange`; remove a row ⇒ delete logged; retrieval smoke in staging Streamlit with an MI-scoped group.
- **Phase C — MASA** ([#247](https://github.com/DGAFP/assistant-rh/issues/247)): template copy of B minus infra; same verification script — proves the pattern.
- **Phase D — MATTE & MSO rebuilds** ([#248](https://github.com/DGAFP/assistant-rh/issues/248)): `matte/` + `mso/` packages; rename-and-recreate migration; recover full PDF sets (MATTE 44 docs, MSO notebook set) into dropzone + Grist; staging→prod; retire `scripts/amelioration_matte.ipynb` + `scripts/extract_pdf_MSO.ipynb`, supersede audit tooling, close #103. Verify: 0 zero-chunk docs, correct `source`, IVFFLAT present, no regression on the conformance goldset for MATTE/MSO questions.
- **Phase E — admin import page + whitelist consumption** ([#249](https://github.com/DGAFP/assistant-rh/issues/249)): the Streamlit page; legi/SP whitelist read in their medallion jobs. Verify: upload a PDF on staging → row+object created → next run ingests; submit a LEGIARTI id → appears after next legi run.
- **Phase F — drift, cron, prod** ([#250](https://github.com/DGAFP/assistant-rh/issues/250)): exporter metrics + Grafana panels, cron workflow, prod job configs + secrets, runbook `docs/PDF_SOURCES_INGESTION.md`. Verify: staging cron dry-run; panels populated; prod first run `--dry-run` then real, per checklist.

## Risks / open items
1. **Grist**: service-account token with editor rights, doc/table ids, rate limits — provision before Phase A; nothing exists in repo.
2. **Albert `/ocr`**: confirm endpoint contract (input format, page limits, output markdown quality on old scans) in the Phase A smoke; `OcrProvider` interface is the hedge (LightOn local / Mistral fallback).
3. **Albert embeddings at ingestion**: confirm the embedding endpoint yields vectors identical/compatible with query-time BGE-M3 (same model+normalization); else fall back to local embedder for gold only.
4. **Bucket** creation rights + S3/Grist credentials on the streamlit-ui host for the import page.
5. **Legacy id mapping** (MATTE/MSO → new `MATTE-xxxx`/`MSO-xxxx`): old chat-log citations dangle — accepted (cosmetic).
6. **OCR-markdown sectioning quality**: headings may be noisy on old circulaires; iterate per-ministry silver via the Chunking Evaluation page — that's exactly why parsing is per-ministry.
7. **Prod exposure is safe-by-default**: new catalog/table keys are inert until group policies include them; gate enablement on content sign-off.
