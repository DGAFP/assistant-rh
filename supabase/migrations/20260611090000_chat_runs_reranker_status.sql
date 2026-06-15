-- Issue #87: persist the per-run section reranker status (completed / failed)
-- so rerank failures are measurable instead of being a silent fallback.
-- chat_runs is not created by in-repo migrations; guard for fresh databases.
DO $$
BEGIN
    IF to_regclass('public.chat_runs') IS NOT NULL THEN
        ALTER TABLE public.chat_runs ADD COLUMN IF NOT EXISTS v3_reranker_status TEXT;
        ALTER TABLE public.chat_runs ADD COLUMN IF NOT EXISTS v3_reranker_error TEXT;
    END IF;
END
$$;
