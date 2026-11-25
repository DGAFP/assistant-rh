-- Advance the old seeded default while preserving other configured values.
DO $migration$
BEGIN
    IF to_regclass('public.rag_config') IS NOT NULL THEN
        UPDATE public.rag_config
        SET config = jsonb_set(config, '{v3_rerank_input_k}', '40'::jsonb, true)
        WHERE id = 1
          AND (NOT (config ? 'v3_rerank_input_k') OR config -> 'v3_rerank_input_k' = '20'::jsonb);
    END IF;
END
$migration$;
