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
--   - rag_chunks_mso: 1262 chunks, IVFFLAT présent (idx_rag_chunks_mso_embedding_m3);
--   - castabilité vérifiée: hash_id <= 40 chars, short_id peuplé à 100 %,
--     source_document_id 100 % format uuid ou NULL, references_juridiques
--     MATTE (text) vides.
--
-- BASCULE SANS TROU DE RETRIEVAL (backfill-before-cutover):
--   Après la recréation, les lignes legacy sont COPIÉES dans la nouvelle table
--   (intersection de colonnes nom+cast, ci-dessous) AVANT la création des
--   index — le retriever ne voit jamais un corpus vide, et les index IVFFLAT
--   sont entraînés sur des données réelles au lieu d'une table vide.
--   Le premier run corpus complet `--ingest` remplace ensuite les documents du
--   manifest ET balaye automatiquement les copies legacy (réconciliation:
--   cascade des documents orphelins + delete_chunks_not_in_short_ids sur les
--   short_ids notebooks absents du manifest). Aucune étape manuelle.
--   Conseillé après le rebuild complet: REINDEX INDEX
--   public.idx_rag_chunks_{matte,mso}_embedding_m3; (ré-entraîne les listes
--   IVFFLAT sur le corpus reconstruit).
--
-- ROLLBACK (à exécuter manuellement si besoin, dans la release de garde):
--   DROP TABLE public.rag_chunks_matte;  -- table reconstruite
--   ALTER TABLE public.rag_chunks_matte_legacy_20260705 RENAME TO rag_chunks_matte;
--   (les index legacy, renommés avec le suffixe _lgcy0705, restent fonctionnels
--    sous leur nouveau nom — les re-renommer est optionnel), idem mso.
--
-- Idempotence: chaque étape est gardée (IF EXISTS / IF NOT EXISTS + garde sur
-- l'existence de la table legacy; backfill uniquement si la nouvelle table est
-- vide) — rejouable sans effet si déjà appliquée.

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

-- Backfill legacy -> nouvelle table (bascule sans trou de retrieval).
-- Copie par intersection de colonnes: même nom des deux côtés, colonnes
-- générées exclues (text_tsv), cast vers le type cible quand il diverge
-- (matte legacy: source_document_id varchar(50) -> uuid, hash_id text ->
-- varchar(64)); text/varchar -> jsonb passe par to_jsonb (les
-- references_juridiques legacy MATTE sont du texte libre, pas du JSON).
-- Ne tourne que si la nouvelle table est vide (rejouabilité: pas de doublon
-- après le premier run de rebuild).
DO $$
DECLARE
    corpus TEXT;
    legacy_table TEXT;
    new_table TEXT;
    col_list TEXT;
    select_list TEXT;
    existing BIGINT;
BEGIN
    FOREACH corpus IN ARRAY ARRAY['matte', 'mso'] LOOP
        legacy_table := format('rag_chunks_%s_legacy_20260705', corpus);
        new_table := format('rag_chunks_%s', corpus);
        IF to_regclass('public.' || legacy_table) IS NULL THEN
            CONTINUE;  -- environnement sans legacy (neuf): rien à retenir
        END IF;
        EXECUTE format('SELECT count(*) FROM public.%I', new_table) INTO existing;
        IF existing > 0 THEN
            CONTINUE;  -- déjà backfillée ou déjà reconstruite
        END IF;

        SELECT
            string_agg(quote_ident(tgt.attname), ', ' ORDER BY tgt.attnum),
            string_agg(
                CASE
                    WHEN format_type(src.atttypid, src.atttypmod) = format_type(tgt.atttypid, tgt.atttypmod)
                        THEN format('l.%I', tgt.attname)
                    WHEN tgt.atttypid = 'jsonb'::regtype
                         AND src.atttypid IN ('text'::regtype, 'varchar'::regtype)
                        THEN format('to_jsonb(NULLIF(TRIM(l.%I), ''''))', tgt.attname)
                    ELSE format('l.%I::%s', tgt.attname, format_type(tgt.atttypid, tgt.atttypmod))
                END, ', ' ORDER BY tgt.attnum)
        INTO col_list, select_list
        FROM pg_attribute tgt
        JOIN pg_attribute src
          ON src.attrelid = ('public.' || legacy_table)::regclass
         AND src.attname = tgt.attname
         AND src.attnum > 0
         AND NOT src.attisdropped
         AND src.attgenerated = ''
        WHERE tgt.attrelid = ('public.' || new_table)::regclass
          AND tgt.attnum > 0
          AND NOT tgt.attisdropped
          AND tgt.attgenerated = '';

        IF col_list IS NULL THEN
            RAISE EXCEPTION 'Backfill %: aucune colonne commune avec % — schéma legacy inattendu', new_table, legacy_table;
        END IF;

        EXECUTE format(
            'INSERT INTO public.%I (%s) SELECT %s FROM public.%I l',
            new_table, col_list, select_list, legacy_table
        );
        GET DIAGNOSTICS existing = ROW_COUNT;
        RAISE NOTICE 'Backfill %: % lignes legacy copiées depuis %', new_table, existing, legacy_table;
    END LOOP;
END $$;

-- Index créés APRÈS le backfill: les listes IVFFLAT sont entraînées sur les
-- données legacy copiées, pas sur une table vide (recall dégradé sinon).
CREATE INDEX IF NOT EXISTS idx_rag_chunks_matte_short_id
    ON public.rag_chunks_matte (short_id);
CREATE INDEX IF NOT EXISTS idx_rag_chunks_matte_tsv
    ON public.rag_chunks_matte USING GIN (text_tsv);
CREATE INDEX IF NOT EXISTS idx_rag_chunks_matte_embedding_m3
    ON public.rag_chunks_matte USING ivfflat (embedding_m3 vector_cosine_ops) WITH (lists = 100);

CREATE INDEX IF NOT EXISTS idx_rag_chunks_mso_short_id
    ON public.rag_chunks_mso (short_id);
CREATE INDEX IF NOT EXISTS idx_rag_chunks_mso_tsv
    ON public.rag_chunks_mso USING GIN (text_tsv);
CREATE INDEX IF NOT EXISTS idx_rag_chunks_mso_embedding_m3
    ON public.rag_chunks_mso USING ivfflat (embedding_m3 vector_cosine_ops) WITH (lists = 100);
