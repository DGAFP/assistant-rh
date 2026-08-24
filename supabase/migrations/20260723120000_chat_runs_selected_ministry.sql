-- Issue #341: persist the exact active ministry per run so the feedback
-- dashboard can display it instead of guessing from retrieved sources
-- (shared sources like Service-Public/DGAFP cannot identify the ministry).
-- chat_runs is not created by in-repo migrations; guard for fresh databases.
DO $$
BEGIN
    IF to_regclass('public.chat_runs') IS NOT NULL THEN
        ALTER TABLE public.chat_runs ADD COLUMN IF NOT EXISTS selected_ministry TEXT;
    END IF;
END
$$;
