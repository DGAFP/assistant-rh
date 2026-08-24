-- Keep legacy chat_runs logging columns wide enough for restored RAG metadata.
-- Some existing environments created these columns as varchar(30), and
-- `ADD COLUMN IF NOT EXISTS ... TEXT` does not widen an existing column.
DO $$
BEGIN
    IF to_regclass('public.chat_runs') IS NULL THEN
        RETURN;
    END IF;

    IF EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'chat_runs'
          AND column_name = 'trace_id'
    ) THEN
        ALTER TABLE public.chat_runs ALTER COLUMN trace_id TYPE TEXT;
    END IF;

    IF EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'chat_runs'
          AND column_name = 'table'
    ) THEN
        ALTER TABLE public.chat_runs ALTER COLUMN "table" TYPE TEXT;
    END IF;

    IF EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'chat_runs'
          AND column_name = 'cascade_source'
    ) THEN
        ALTER TABLE public.chat_runs ALTER COLUMN cascade_source TYPE TEXT;
    END IF;
END
$$;
