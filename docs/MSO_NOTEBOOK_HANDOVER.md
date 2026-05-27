# Handover: `scripts/extract_pdf_MSO.ipynb`

## Scope

`scripts/extract_pdf_MSO.ipynb` is the reference notebook for MSO internal
documents stored under `data/in/MSO`.

Its job is to:

1. read source files from `data/in/MSO/**`
2. extract raw text into `data/out/MSO/**`
3. detect the document structure
4. build section-aware chunks and metadata
5. generate embeddings
6. optionally upsert the result into PostgreSQL/pgvector

This notebook is meant for MSO only. It does not replace the MATTE or
Service-Public ingestion notebooks.

## Supported input formats

The notebook currently supports:

- `pdf`
- `pptx`
- `docx`

Default input patterns:

```bash
./data/in/MSO/**/*.pdf
./data/in/MSO/**/*.pptx
./data/in/MSO/**/*.docx
```

The output text files are written under:

```bash
data/out/MSO/
```

The chunk/section/document artifacts are written under:

```bash
data/out/chunked/
data/out/
```

## High-level pipeline

### 1. File extraction

The notebook dispatches extraction by extension:

- `read_pdf_to_text`
- `read_pptx_to_text`
- `read_docx_to_text`

PDF extraction follows this order:

1. `pdftotext -layout` for matrix/table-like files
2. `pypdf` text extraction
3. `pdftotext -layout` fallback when `pypdf` is too poor
4. OCR fallback with `pdftoppm` + `tesseract`

This makes the notebook robust for:

- native text PDFs
- layout-sensitive matrix documents
- scanned PDFs with weak embedded text

### 2. Document mode detection

`detect_document_mode()` routes each text into one of these modes:

- `guide`
- `faq`
- `process`
- `table_matrix`
- `fallback`

Detection strategy:

- `table_matrix` if the text looks like a matrix of acts / entities
- `faq` if the text looks like a numbered FAQ
- `process` if the document looks like a process or flow
- `guide` otherwise
- `fallback` only if no parser returns any section but raw text exists

### 3. Parsing strategy by mode

#### `guide`

Used for structured documents with headings.

Main signals:

- explicit markers like `(Titre)` / `(Intertitre)`
- roman headings like `I-`
- alphabetical headings like `A.`
- numbered headings like `1.` or `1.a`
- bullet-like headings such as `✓`

The parser builds:

- hierarchical section paths
- pseudo-questions derived from headings
- one block per real section

#### `faq`

Used for numbered FAQ documents.

The parser:

- removes table-of-contents noise
- detects numbered question lines
- keeps a section/question hierarchy when present
- stores the answer as the chunk body

This mode was added to correctly handle PSC-like FAQ documents.

#### `process`

Used for process maps, logigrams and step-driven documents.

The parser looks for:

- explicit process wording in the source name or content
- actor labels
- branch labels
- short action lines that behave like steps

Each step becomes a chunk with a pseudo-question that stays close to user
queries.

#### `table_matrix`

Used for matrix-like documents where layout matters more than prose order.

The parser is triggered by `looks_like_table_matrix_text()` when it detects
signals such as:

- `type d'actes`
- `entite de gestion`
- many `n degre` markers
- repeated entity values such as `deconcentre`, `DRH-BPECO`, `CBCM`

For those files, `pdftotext -layout` is preferred because plain PDF extraction
usually destroys the matrix semantics.

### 4. Chunk dataset construction

`build_chunks_dataset()` creates three datasets:

- documents
- sections
- chunks

Key properties:

- stable IDs are derived from deterministic UUID/sha1 helpers
- `publisher` is set to `MSO`
- `parse_version` is currently `extract_pdf_MSO_v3`
- `quality_flags` stores the source format and parse mode

### 5. Embeddings

The notebook generates:

- local `BAAI/bge-m3` embeddings (`embedding_m3`, dim `1024`)
- Scaleway embeddings (`embedding_bge_scw`, dim `3584`) when enabled

This produces:

- `*.jsonl`
- `*.parquet`
- `*.npy`

### 6. Database upsert

If `MSO_UPSERT_TO_DB=1`, the notebook upserts into:

- `public.rag_documents`
- `public.rag_sections`
- `public.documents`
- `public.rag_chunks_mso` by default

If `MSO_REPLACE_EXISTING_DOCS=1`, existing rows for the same `doc_id` are
deleted before reinsertion. This is the safe mode when reprocessing the same
document set.

## Main environment variables

### Inputs / outputs

- `MSO_BASE_IN`
- `MSO_BASE_OUT`
- `MSO_INPUT_FILES`
- `MSO_INPUT_PATTERNS`
- `MSO_TXT_PATTERNS`
- `MSO_CHUNKS_JSONL`
- `MSO_DOCS_JSONL`
- `MSO_SECTIONS_JSONL`
- `MSO_EMB_OUT_JSONL`
- `MSO_EMB_OUT_PARQUET`
- `MSO_EMB_OUT_NPY`

### Embeddings

- `EMBEDDING_MODEL`
- `MSO_EMBED_BATCH_SIZE`
- `MSO_GENERATE_BGE_SCW`
- `MSO_BGE_BATCH_SIZE`
- `MSO_BGE_TIMEOUT`
- `SCALEWAY_API_KEY`
- `SCW_ACCESS_KEY`
- `SCW_SECRET_KEY`

### OCR

- `MSO_OCR_FALLBACK`
- `MSO_OCR_LANG`
- `MSO_OCR_DPI`
- `MSO_OCR_MIN_CHARS`
- `MSO_OCR_MIN_ALPHA_CHARS`

### Database target

- `MSO_DB_TARGET`
- `MSO_TABLE`
- `PGSCHEMA`
- `MSO_UPSERT_TO_DB`
- `MSO_REPLACE_EXISTING_DOCS`

`resolve_target_dsn()` supports these targets:

- `scalingo`
- `scaleway`
- `scaleway_prod`
- `prod`
- `scaleway_staging`
- `staging`
- `custom`
- `local`

## Typical runs

### Dry run on local files only

Use this mode to validate extraction/chunking without touching the database.

```bash
MSO_INPUT_PATTERNS="./data/in/MSO/**/*.pdf,./data/in/MSO/**/*.pptx,./data/in/MSO/**/*.docx" \
MSO_UPSERT_TO_DB=0 \
jupyter nbconvert --to notebook --execute scripts/extract_pdf_MSO.ipynb
```

### Target a specific MSO folder

Example for PSC:

```bash
MSO_INPUT_PATTERNS="./data/in/MSO/psc/**/*.pdf,./data/in/MSO/psc/**/*.pptx,./data/in/MSO/psc/**/*.docx" \
MSO_TXT_PATTERNS="./data/out/MSO/psc/**/*.txt" \
MSO_CHUNKS_JSONL="./data/out/chunked/mso_psc_chunks_qna.jsonl" \
MSO_DOCS_JSONL="./data/out/chunked/mso_psc_documents.jsonl" \
MSO_SECTIONS_JSONL="./data/out/chunked/mso_psc_sections.jsonl" \
MSO_EMB_OUT_JSONL="./data/out/mso_psc_chunks_with_emb.jsonl" \
MSO_UPSERT_TO_DB=0 \
jupyter nbconvert --to notebook --execute scripts/extract_pdf_MSO.ipynb
```

### Upsert to Scalingo

This assumes the Scalingo tunnel is already up and the local environment is
configured accordingly.

```bash
MSO_DB_TARGET=scalingo \
MSO_UPSERT_TO_DB=1 \
MSO_REPLACE_EXISTING_DOCS=1 \
jupyter nbconvert --to notebook --execute scripts/extract_pdf_MSO.ipynb
```

### Upsert to Scaleway staging or prod

```bash
MSO_DB_TARGET=scaleway_staging \
MSO_UPSERT_TO_DB=1 \
MSO_REPLACE_EXISTING_DOCS=1 \
jupyter nbconvert --to notebook --execute scripts/extract_pdf_MSO.ipynb
```

```bash
MSO_DB_TARGET=scaleway_prod \
MSO_UPSERT_TO_DB=1 \
MSO_REPLACE_EXISTING_DOCS=1 \
jupyter nbconvert --to notebook --execute scripts/extract_pdf_MSO.ipynb
```

## Operational checks

Before inserting into a database, verify:

1. the input folder really contains the intended MSO documents
2. the output `txt` files look sane
3. the generated chunk JSONL is not empty
4. embeddings are not missing
5. the target DSN matches the intended environment

Minimum checks after a run:

- `mso_*_documents.jsonl` row count
- `mso_*_sections.jsonl` row count
- `mso_*_chunks_qna.jsonl` row count
- `mso_*_chunks_with_emb.jsonl` has no missing `embedding_m3`
- if Scaleway embeddings are enabled, no missing `embedding_bge_scw`

## Known strengths

- handles mixed MSO subfolders instead of one single document family
- preserves section semantics better than flat chunking
- supports matrix-like documents that standard text extraction breaks
- supports scanned PDFs through OCR fallback
- supports DOCX and PPTX in addition to PDF
- produces deterministic IDs useful for reingestion and diffing

## Known limitations

- still notebook-based, not yet packaged as a production CLI
- OCR quality depends on `tesseract` and scan quality
- matrix detection is heuristic, not schema-driven
- process detection is heuristic and can still miss edge cases
- very noisy documents may still fall back to a single section
- notebook outputs can contain run artifacts if not stripped before commit

## Practical handover guidance

If a new MSO subfolder arrives:

1. start with a dry run on that folder only
2. inspect the generated `.txt`
3. inspect the parse mode chosen for each document
4. validate chunk counts and section paths
5. only then enable DB upsert

If a document is badly parsed, first determine which layer is failing:

- extraction layer: bad raw text
- routing layer: wrong `detect_document_mode()`
- parser layer: right mode but wrong section boundaries
- ingestion layer: chunks are correct locally but not inserted correctly

In practice, most fixes happen in one of these functions:

- `read_pdf_to_text`
- `looks_like_table_matrix_text`
- `detect_document_mode`
- `parse_guide_blocks`
- `parse_faq_blocks`
- `parse_process_blocks`
- `build_chunks_dataset`
- `upsert_to_db`

## Files produced by the notebook

For a given run, expect at least:

- extracted text files under `data/out/MSO/**`
- chunk JSONL
- document JSONL
- section JSONL
- embedding JSONL
- optional Parquet and NPY embedding artifacts

These files are local processing artifacts. They should not be pushed blindly
without checking repository policy and file size.
