CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS unaccent;
CREATE EXTENSION IF NOT EXISTS btree_gin;

CREATE TABLE IF NOT EXISTS rag_chunks_service_public (
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

CREATE INDEX IF NOT EXISTS idx_rag_chunks_sp_short_id
    ON rag_chunks_service_public (short_id);

CREATE INDEX IF NOT EXISTS idx_rag_chunks_sp_section_id
    ON rag_chunks_service_public (section_id);

CREATE INDEX IF NOT EXISTS idx_rag_chunks_sp_source_document_id
    ON rag_chunks_service_public (source_document_id);

CREATE INDEX IF NOT EXISTS idx_rag_chunks_service_public_tsv
    ON rag_chunks_service_public USING GIN (text_tsv);

CREATE INDEX IF NOT EXISTS idx_rag_chunks_service_public_embedding_m3
    ON rag_chunks_service_public USING ivfflat (embedding_m3 vector_cosine_ops) WITH (lists = 100);

CREATE TABLE IF NOT EXISTS rag_chunks_legifrance (
    hash_id VARCHAR(64) PRIMARY KEY,
    qa_id TEXT,
    parent_qa_id TEXT,
    source_name TEXT,
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
    chunk_id VARCHAR(64),
    chunk_number INTEGER,
    title TEXT,
    full_title TEXT,
    number TEXT,
    category TEXT,
    status TEXT,
    subtitles TEXT,
    nota TEXT,
    ministry TEXT,
    url TEXT,
    cid TEXT,
    section_parent_cid TEXT,
    section_parent_titre TEXT,
    lien_citations JSONB,
    lien_citations_count INTEGER,
    lien_modifications JSONB,
    lien_modifications_count INTEGER,
    lien_concordes JSONB,
    lien_concordes_count INTEGER,
    comporte_liens_sp BOOLEAN,
    start_date DATE,
    end_date DATE,
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    embedding_m3 vector(1024),
    embedding_bge_scw vector(3584),
    text_tsv tsvector GENERATED ALWAYS AS (
        to_tsvector('french', coalesce(section_path, '') || ' ' || coalesce(chunk_text, ''))
    ) STORED
);

CREATE INDEX IF NOT EXISTS idx_rag_chunks_legifrance_short_id
    ON rag_chunks_legifrance (short_id);

CREATE INDEX IF NOT EXISTS idx_rag_chunks_legifrance_cid
    ON rag_chunks_legifrance (cid);

CREATE INDEX IF NOT EXISTS idx_rag_chunks_legifrance_section_id
    ON rag_chunks_legifrance (section_id);

CREATE INDEX IF NOT EXISTS idx_rag_chunks_legifrance_tsv
    ON rag_chunks_legifrance USING GIN (text_tsv);

-- Tables chunks des corpus PDF ministériels (pipeline manifest Grist, #246).
-- Référence: la migration supabase/migrations/20260704120000_pdf_sources_ministries.sql
-- fait foi; ce fichier reflète le schéma attendu côté Scaleway.

CREATE TABLE IF NOT EXISTS rag_chunks_mi (
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
    ON rag_chunks_mi (short_id);

CREATE INDEX IF NOT EXISTS idx_rag_chunks_mi_tsv
    ON rag_chunks_mi USING GIN (text_tsv);

CREATE INDEX IF NOT EXISTS idx_rag_chunks_mi_embedding_m3
    ON rag_chunks_mi USING ivfflat (embedding_m3 vector_cosine_ops) WITH (lists = 100);

CREATE TABLE IF NOT EXISTS rag_chunks_masa (
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
    ON rag_chunks_masa (short_id);

CREATE INDEX IF NOT EXISTS idx_rag_chunks_masa_tsv
    ON rag_chunks_masa USING GIN (text_tsv);

CREATE INDEX IF NOT EXISTS idx_rag_chunks_masa_embedding_m3
    ON rag_chunks_masa USING ivfflat (embedding_m3 vector_cosine_ops) WITH (lists = 100);

CREATE TABLE IF NOT EXISTS rag_ingestion_runs (
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
    ON rag_ingestion_runs (ministere, started_at DESC);
