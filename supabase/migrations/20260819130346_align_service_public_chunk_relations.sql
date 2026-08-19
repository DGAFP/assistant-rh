-- Aligne la table historique Service-Public sur le contrat commun des chunks.
-- Les colonnes sont nullables pour permettre une bascule en ligne; le writer
-- remplit les relations à chaque réingestion de fiche.
ALTER TABLE public.rag_chunks_service_public
    ADD COLUMN IF NOT EXISTS references_juridiques JSONB,
    ADD COLUMN IF NOT EXISTS section_id UUID,
    ADD COLUMN IF NOT EXISTS source_document_id UUID;

-- Les cascades d'ingestion et les jointures document/section filtrent ces
-- colonnes. PostgreSQL ne crée pas automatiquement d'index sur les relations.
CREATE INDEX IF NOT EXISTS idx_rag_chunks_sp_section_id
    ON public.rag_chunks_service_public (section_id);

CREATE INDEX IF NOT EXISTS idx_rag_chunks_sp_source_document_id
    ON public.rag_chunks_service_public (source_document_id);

-- Rattache les chunks existants à leur document quand le short_id identifie
-- sans ambiguïté un document Service-Public. Les cas ambigus restent NULL et
-- sont résolus par le fallback runtime jusqu'à leur prochaine réingestion.
WITH unique_documents AS (
    SELECT
        UPPER(TRIM(short_id)) AS normalized_short_id,
        MIN(doc_id::text)::uuid AS doc_id
    FROM public.rag_documents
    WHERE short_id IS NOT NULL
      AND LOWER(TRIM(source)) = 'service_public'
    GROUP BY UPPER(TRIM(short_id))
    HAVING COUNT(*) = 1
)
UPDATE public.rag_chunks_service_public AS chunks
SET source_document_id = documents.doc_id
FROM unique_documents AS documents
WHERE chunks.source_document_id IS NULL
  AND chunks.short_id IS NOT NULL
  AND UPPER(TRIM(chunks.short_id)) = documents.normalized_short_id;

-- Rejoue le même choix déterministe que le résolveur legacy du retriever:
-- heading_path exact en priorité, puis dernier heading du chemin.
WITH resolved_sections AS (
    SELECT
        chunks.hash_id,
        (
            SELECT sections.section_id
            FROM public.rag_documents AS documents
            JOIN public.rag_sections AS sections
              ON sections.doc_id = documents.doc_id
            WHERE LOWER(TRIM(documents.source)) = 'service_public'
              AND UPPER(TRIM(documents.short_id)) = UPPER(TRIM(chunks.short_id))
              AND (
                  sections.heading_path = chunks.section_path
                  OR sections.heading = BTRIM(REGEXP_REPLACE(chunks.section_path, '^.*>\s*', ''))
              )
            ORDER BY
                CASE WHEN sections.heading_path = chunks.section_path THEN 0 ELSE 1 END,
                sections.section_index NULLS LAST,
                sections.section_id
            LIMIT 1
        ) AS section_id
    FROM public.rag_chunks_service_public AS chunks
    WHERE chunks.section_id IS NULL
      AND chunks.short_id IS NOT NULL
      AND chunks.section_path IS NOT NULL
)
UPDATE public.rag_chunks_service_public AS chunks
SET section_id = resolved.section_id
FROM resolved_sections AS resolved
WHERE chunks.hash_id = resolved.hash_id
  AND resolved.section_id IS NOT NULL;
