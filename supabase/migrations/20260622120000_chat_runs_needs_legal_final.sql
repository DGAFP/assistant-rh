-- PR #171: persist the post-heuristic legal-search decision separately from the
-- LLM-only signal. chat_logger now writes both v3_needs_legal_llm (LLM-only) and
-- v3_needs_legal_final (LLM ∪ deterministic guardrail). The logger builds its
-- INSERT dynamically from the row dict, so the column must exist or every write
-- fails and silently falls back to CSV.
-- chat_runs is not created by in-repo migrations; guard for fresh databases.
DO $$
BEGIN
    IF to_regclass('public.chat_runs') IS NOT NULL THEN
        ALTER TABLE public.chat_runs ADD COLUMN IF NOT EXISTS v3_needs_legal_final BOOLEAN;
    END IF;
END
$$;
