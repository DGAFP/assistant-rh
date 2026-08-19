-- Group-scoped ministry policy for request-scoped RAG retrieval.
--
-- Existing groups keep current MATTE-only behavior. The Streamlit admin page
-- edits these values from the code-owned ministry catalog.
ALTER TABLE public.user_groups
    ADD COLUMN IF NOT EXISTS allowed_ministries JSONB NOT NULL DEFAULT '["matte"]'::jsonb;

ALTER TABLE public.user_groups
    ADD COLUMN IF NOT EXISTS default_ministry TEXT NOT NULL DEFAULT 'matte';

UPDATE public.user_groups
SET allowed_ministries = '["matte"]'::jsonb
WHERE allowed_ministries IS NULL
   OR jsonb_typeof(allowed_ministries) <> 'array'
   OR CASE
        WHEN jsonb_typeof(allowed_ministries) = 'array'
        THEN jsonb_array_length(allowed_ministries) = 0
        ELSE FALSE
      END;

UPDATE public.user_groups
SET default_ministry = 'matte'
WHERE default_ministry IS NULL
   OR btrim(default_ministry) = '';
