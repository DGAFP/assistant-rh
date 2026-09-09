-- Synthetic baseline matching the historical runtime before B2. No real data.
CREATE TABLE IF NOT EXISTS public.user_groups (
    slug VARCHAR(64) PRIMARY KEY, label VARCHAR(128) NOT NULL, priority INTEGER NOT NULL DEFAULT 0,
    visible BOOLEAN NOT NULL DEFAULT TRUE, is_admin BOOLEAN NOT NULL DEFAULT FALSE, password_hash TEXT,
    allowed_ministries JSONB NOT NULL DEFAULT '["matte"]', default_ministry TEXT NOT NULL DEFAULT 'matte'
);
ALTER TABLE public.user_groups ADD COLUMN IF NOT EXISTS icon VARCHAR(16) NOT NULL DEFAULT '';
ALTER TABLE public.user_groups ADD COLUMN IF NOT EXISTS color VARCHAR(16) NOT NULL DEFAULT '';
CREATE TABLE IF NOT EXISTS public.system_prompts (
    name VARCHAR(100) PRIMARY KEY, content TEXT NOT NULL, is_active BOOLEAN DEFAULT TRUE
);
CREATE TABLE IF NOT EXISTS public.acronyms (
    id SERIAL PRIMARY KEY, acronym TEXT UNIQUE NOT NULL, expansion TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS public.rag_documents (
    doc_id UUID PRIMARY KEY, short_id TEXT, title TEXT, source_url TEXT, publisher TEXT, doc_markdown TEXT, token_count INTEGER
);
CREATE TABLE IF NOT EXISTS public.rag_sections (
    section_id UUID PRIMARY KEY, doc_id UUID REFERENCES public.rag_documents(doc_id),
    heading TEXT, heading_path TEXT, section_markdown TEXT, references_juridiques JSONB
);
CREATE TABLE IF NOT EXISTS public.rag_chunks_service_public (
    hash_id TEXT PRIMARY KEY, chunk_text TEXT, short_id TEXT, section_path TEXT,
    embedding_m3 VECTOR(3), embedding_bge_scw VECTOR(3),
    text_tsv TSVECTOR GENERATED ALWAYS AS (to_tsvector('french', chunk_text)) STORED
);
CREATE TABLE IF NOT EXISTS public.rag_chunks_matte (
    hash_id TEXT PRIMARY KEY, chunk_text TEXT, section_id UUID,
    embedding_m3 VECTOR(3), embedding_bge_scw VECTOR(3),
    text_tsv TSVECTOR GENERATED ALWAYS AS (to_tsvector('french', chunk_text)) STORED
);
CREATE TABLE IF NOT EXISTS public.rag_chunks_dgafp (
    chunk_id TEXT PRIMARY KEY, chunk_text TEXT, number TEXT, cid TEXT, url TEXT, full_title TEXT,
    embedding_m3 VECTOR(3), embedding_bge_scw VECTOR(3),
    chunk_text_tsv TSVECTOR GENERATED ALWAYS AS (to_tsvector('french', chunk_text)) STORED
);
CREATE TABLE IF NOT EXISTS public.rag_chunks_mso (LIKE public.rag_chunks_matte INCLUDING ALL);
CREATE TABLE IF NOT EXISTS public.rag_chunks_mi (LIKE public.rag_chunks_matte INCLUDING ALL);
CREATE TABLE IF NOT EXISTS public.rag_chunks_masa (LIKE public.rag_chunks_matte INCLUDING ALL);
CREATE TABLE IF NOT EXISTS public.rag_chunks_rgrh (LIKE public.rag_chunks_matte INCLUDING ALL);
CREATE TABLE IF NOT EXISTS public.chat_runs (
    turn_id VARCHAR(8) PRIMARY KEY, trace_id TEXT, ts TIMESTAMP, user_group TEXT,
    session_id TEXT, conversation_id TEXT, question TEXT, answer TEXT, selected_ministry TEXT, model TEXT,
    retrieved JSONB, v3_full_prompt TEXT, chunks_sent_to_selector JSONB,
    v3_source_distribution JSONB, v3_sections_count INTEGER, v3_context_items_count INTEGER,
    v3_context_tokens INTEGER, v3_context_mode TEXT, llm_selector_reasoning TEXT, llm_selector_response TEXT,
    v3_selector_confidence DOUBLE PRECISION, v3_selector_selected_count INTEGER, v3_selector_decisions JSONB,
    llm_selector_model TEXT, v3_chunks_raw JSONB
);
CREATE TABLE IF NOT EXISTS public.chat_feedbacks (
    id BIGSERIAL PRIMARY KEY, turn_id VARCHAR(8), ts TIMESTAMP, turn_idx INTEGER,
    helpful BOOLEAN, reasons TEXT, comment TEXT, stars INTEGER CHECK (stars BETWEEN 0 AND 4),
    reasons_positive TEXT, reasons_negative TEXT, session_id TEXT, question TEXT, answer TEXT,
    error_category TEXT, ai_reason TEXT, ai_analyzed_at TIMESTAMPTZ, beta_scope TEXT, theme TEXT
);
ALTER TABLE public.chat_runs ALTER COLUMN ts DROP NOT NULL;
ALTER TABLE public.chat_feedbacks ALTER COLUMN ts DROP NOT NULL;
