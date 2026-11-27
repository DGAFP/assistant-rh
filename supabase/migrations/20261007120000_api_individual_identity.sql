-- Individual identity (#596) and transactional feedback (D1, #528). Additive only.
-- NULL author marks collective B4 history; never infer a person from it.
-- The author is the stable Conversations user ID from a verified delegation; no user registry.
-- Applying this migration activates nothing: delegation stays disabled until the API is
-- configured with CONVERSATIONS_DELEGATION_JWKS/ISSUER, after the #599 SQL restrictions.
DO $migration$
BEGIN
    ALTER TABLE public.chat_runs ADD COLUMN IF NOT EXISTS author_user_id UUID;
    ALTER TABLE public.chat_feedbacks ADD COLUMN IF NOT EXISTS api_actor_user_id UUID;
    ALTER TABLE public.chat_feedback_audit ADD COLUMN IF NOT EXISTS actor_user_id UUID;

    -- No backfill, ownership transfer or first-login claim of collective history.
    CREATE OR REPLACE FUNCTION public.api_run_author_immutable() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog AS $function$
    BEGIN
        IF NEW.author_user_id IS DISTINCT FROM OLD.author_user_id THEN
            RAISE EXCEPTION 'Run author is immutable' USING ERRCODE = '23514';
        END IF;
        RETURN NEW;
    END;
    $function$;
    CREATE OR REPLACE TRIGGER api_run_author_immutable BEFORE UPDATE ON public.chat_runs
        FOR EACH ROW EXECUTE FUNCTION public.api_run_author_immutable();

    -- Runs with an individual author cannot be overwritten by legacy INSERTs,
    -- including first submissions with no current feedback. Name sorts before
    -- api_legacy_feedback_insert, so its replacement logic cannot bypass this.
    CREATE OR REPLACE FUNCTION public.api_feedback_individual_guard() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog AS $function$
    DECLARE author UUID;
    BEGIN
        -- Skip annotation/analysis updates without a parent lookup. Keep the trigger
        -- free of column dependencies so the local B2 bootstrap remains repeatable.
        IF TG_OP = 'UPDATE' THEN
            IF NEW.turn_id IS NOT DISTINCT FROM OLD.turn_id
               AND NEW.api_actor_user_id IS NOT DISTINCT FROM OLD.api_actor_user_id THEN
                RETURN NEW;
            END IF;
        END IF;
        SELECT author_user_id INTO author FROM public.chat_runs WHERE turn_id = NEW.turn_id;
        IF author IS NOT NULL AND NEW.api_actor_user_id IS DISTINCT FROM author THEN
            RAISE EXCEPTION 'Individual feedback requires its run author' USING ERRCODE = '23514';
        END IF;
        RETURN NEW;
    END;
    $function$;
    CREATE OR REPLACE TRIGGER api_feedback_individual_guard BEFORE INSERT OR UPDATE ON public.chat_feedbacks
        FOR EACH ROW EXECUTE FUNCTION public.api_feedback_individual_guard();
END;
$migration$;

-- Signed delegation IDs already used (#596). Only verified assertions are recorded, so
-- unauthenticated callers cannot fill it; rows are purged once their assertion has expired.
CREATE TABLE IF NOT EXISTS public.api_delegation_replays (
    jti TEXT PRIMARY KEY CHECK (jti ~ '^[A-Za-z0-9_-]{16,128}$'),
    key_id TEXT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS api_delegation_replays_expires_idx ON public.api_delegation_replays(expires_at);
