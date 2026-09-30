-- D1 groundwork. NULL marks collective B4 history; never infer a person from it.
-- #596 must provide the verified internal UUID and the user registry before rollout.
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
