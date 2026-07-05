-- Pipeline PDF ministères — Phase D (#248): reconstruction MATTE & MSO.
--
-- Les tables legacy (ingérées par notebooks one-shot: champ source erroné,
-- 17/44 docs MATTE à zéro chunk, pas d'IVFFLAT sur matte — audit #103) sont
-- RENOMMÉES en *_legacy_20260705 et conservées une release pour rollback,
-- puis recréées sous le même nom avec le schéma moderne (aligné
-- rag_chunks_mi/masa de la Phase B) et les index complets. Le retriever et
-- l'exporter rag-health, qui adressent les tables par nom, sont inchangés.
--
-- Pré-vérification du schéma live (2026-07-05, staging + prod identiques):
--   - rag_chunks_matte: 959 chunks, m3+bge_scw 100 %, PAS d'index IVFFLAT;
--   - rag_chunks_mso: 1262 chunks, IVFFLAT présent (idx_rag_chunks_mso_embedding_m3).
--
-- ROLLBACK (à exécuter manuellement si besoin, dans la release de garde):
--   DROP TABLE public.rag_chunks_matte;  -- table reconstruite
--   ALTER TABLE public.rag_chunks_matte_legacy_20260705 RENAME TO rag_chunks_matte;
--   (les index legacy, renommés avec le suffixe _lgcy0705, restent fonctionnels
--    sous leur nouveau nom — les re-renommer est optionnel), idem mso.
--
-- Idempotence: chaque étape est gardée (IF EXISTS / IF NOT EXISTS + garde sur
-- l'existence de la table legacy) — rejouable sans effet si déjà appliquée.

CREATE EXTENSION IF NOT EXISTS vector;

DO $$
DECLARE
    corpus TEXT;
    index_record RECORD;
BEGIN
    FOREACH corpus IN ARRAY ARRAY['matte', 'mso'] LOOP
        -- Ne renomme qu'une table legacy non encore migrée (rejouabilité).
        IF to_regclass(format('public.rag_chunks_%s', corpus)) IS NOT NULL
           AND to_regclass(format('public.rag_chunks_%s_legacy_20260705', corpus)) IS NULL THEN
            EXECUTE format('ALTER TABLE public.rag_chunks_%s RENAME TO rag_chunks_%s_legacy_20260705', corpus, corpus);
            -- Les index/contraintes gardent leur nom au RENAME: on les suffixe
            -- pour libérer les noms au profit de la table reconstruite.
            FOR index_record IN
                SELECT indexname FROM pg_indexes
                WHERE schemaname = 'public' AND tablename = format('rag_chunks_%s_legacy_20260705', corpus)
            LOOP
                EXECUTE format('ALTER INDEX public.%I RENAME TO %I',
                               index_record.indexname,
                               left(index_record.indexname, 50) || '_lgcy0705');
            END LOOP;
        END IF;
    END LOOP;
END $$;

CREATE TABLE IF NOT EXISTS public.rag_chunks_matte (
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

CREATE INDEX IF NOT EXISTS idx_rag_chunks_matte_short_id
    ON public.rag_chunks_matte (short_id);
CREATE INDEX IF NOT EXISTS idx_rag_chunks_matte_tsv
    ON public.rag_chunks_matte USING GIN (text_tsv);
CREATE INDEX IF NOT EXISTS idx_rag_chunks_matte_embedding_m3
    ON public.rag_chunks_matte USING ivfflat (embedding_m3 vector_cosine_ops) WITH (lists = 100);

CREATE TABLE IF NOT EXISTS public.rag_chunks_mso (
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

CREATE INDEX IF NOT EXISTS idx_rag_chunks_mso_short_id
    ON public.rag_chunks_mso (short_id);
CREATE INDEX IF NOT EXISTS idx_rag_chunks_mso_tsv
    ON public.rag_chunks_mso USING GIN (text_tsv);
CREATE INDEX IF NOT EXISTS idx_rag_chunks_mso_embedding_m3
    ON public.rag_chunks_mso USING ivfflat (embedding_m3 vector_cosine_ops) WITH (lists = 100);
