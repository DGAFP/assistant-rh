-- Pipeline PDF ministères — Phase B (#246).
-- Tables chunks MI + MASA (schéma aligné sur rag_chunks_service_public,
-- + references_juridiques/section_id/source_document_id comme legifrance),
-- indexées dès le jour 1 (btree short_id, GIN tsv, IVFFLAT embedding_m3),
-- et table de traçage des runs d'ingestion (réconciliation manifest Grist).

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS public.rag_chunks_mi (
    hash_id VARCHAR(64) PRIMARY KEY,
    qa_id TEXT,
    parent_qa_id TEXT,
    source_name VARCHAR(255),
    section_path TEXT,
    role TEXT,
    chunk_index INTEGER,
    text TEXT,
    chunk_text TEXT,
    lang TEXT DEFAULT 'fr',
    thematique TEXT,
    short_id VARCHAR(64),
    source TEXT,
    references_juridiques JSONB,
    section_id UUID,
    source_document_id UUID,
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    embedding_m3 vector(1024),
    embedding_bge_scw vector(3584),
    text_tsv tsvector GENERATED ALWAYS AS (
        to_tsvector('french', coalesce(section_path, '') || ' ' || coalesce(chunk_text, ''))
    ) STORED
);

CREATE INDEX IF NOT EXISTS idx_rag_chunks_mi_short_id
    ON public.rag_chunks_mi (short_id);
CREATE INDEX IF NOT EXISTS idx_rag_chunks_mi_tsv
    ON public.rag_chunks_mi USING GIN (text_tsv);
CREATE INDEX IF NOT EXISTS idx_rag_chunks_mi_embedding_m3
    ON public.rag_chunks_mi USING ivfflat (embedding_m3 vector_cosine_ops) WITH (lists = 100);

CREATE TABLE IF NOT EXISTS public.rag_chunks_masa (
    hash_id VARCHAR(64) PRIMARY KEY,
    qa_id TEXT,
    parent_qa_id TEXT,
    source_name VARCHAR(255),
    section_path TEXT,
    role TEXT,
    chunk_index INTEGER,
    text TEXT,
    chunk_text TEXT,
    lang TEXT DEFAULT 'fr',
    thematique TEXT,
    short_id VARCHAR(64),
    source TEXT,
    references_juridiques JSONB,
    section_id UUID,
    source_document_id UUID,
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    embedding_m3 vector(1024),
    embedding_bge_scw vector(3584),
    text_tsv tsvector GENERATED ALWAYS AS (
        to_tsvector('french', coalesce(section_path, '') || ' ' || coalesce(chunk_text, ''))
    ) STORED
);

CREATE INDEX IF NOT EXISTS idx_rag_chunks_masa_short_id
    ON public.rag_chunks_masa (short_id);
CREATE INDEX IF NOT EXISTS idx_rag_chunks_masa_tsv
    ON public.rag_chunks_masa USING GIN (text_tsv);
CREATE INDEX IF NOT EXISTS idx_rag_chunks_masa_embedding_m3
    ON public.rag_chunks_masa USING ivfflat (embedding_m3 vector_cosine_ops) WITH (lists = 100);

-- Traçage des runs d'ingestion PDF (réconciliation manifest Grist):
-- une ligne par run et par ministère, détail par document en JSONB.
CREATE TABLE IF NOT EXISTS public.rag_ingestion_runs (
    run_id VARCHAR(128) PRIMARY KEY,
    ministere TEXT NOT NULL,
    target_env TEXT,
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    ocr_provider TEXT,
    expected_count INTEGER NOT NULL DEFAULT 0,
    ingested_count INTEGER NOT NULL DEFAULT 0,
    skipped_count INTEGER NOT NULL DEFAULT 0,
    failed_count INTEGER NOT NULL DEFAULT 0,
    deleted_count INTEGER NOT NULL DEFAULT 0,
    rejected_count INTEGER NOT NULL DEFAULT 0,
    details JSONB,
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_rag_ingestion_runs_ministere_started
    ON public.rag_ingestion_runs (ministere, started_at DESC);
