-- Manual rollback of 20261007130000_api_runtime_roles (#599), run by the migrating admin.
-- Restores the D1 guard and the previous ingest grants, detaches runtime logins and drops
-- the runtime roles. Switch Streamlit/API back to the admin DSN first. Supabase keeps the
-- migration in its history: re-applying it later is idempotent.
DO $rollback$
DECLARE
    role_name TEXT;
    member_name TEXT;
BEGIN
    CREATE OR REPLACE FUNCTION public.api_feedback_individual_guard() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog AS $function$
    DECLARE author UUID;
    BEGIN
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

    IF EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'assistant_rh_ingest') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON public.chat_runs, public.chat_feedbacks TO assistant_rh_ingest;
        IF to_regclass('public.chat_reviews') IS NOT NULL THEN
            GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON public.chat_reviews TO assistant_rh_ingest;
        END IF;
    END IF;

    DROP FUNCTION IF EXISTS public.api_attach_runtime_user(TEXT, TEXT);
    FOREACH role_name IN ARRAY ARRAY['arh_api', 'arh_streamlit', 'arh_analysis'] LOOP
        CONTINUE WHEN NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = role_name);
        FOR member_name IN
            SELECT m.rolname FROM pg_auth_members a JOIN pg_roles r ON r.oid = a.roleid JOIN pg_roles m ON m.oid = a.member
            WHERE r.rolname = role_name AND m.rolname <> current_user
        LOOP
            EXECUTE format('REVOKE %I FROM %I', role_name, member_name);
        END LOOP;
        EXECUTE format('REVOKE ALL ON ALL TABLES IN SCHEMA public FROM %I', role_name);
        EXECUTE format('REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM %I', role_name);
        EXECUTE format('ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM %I', role_name);
        EXECUTE format('DROP ROLE %I', role_name);
    END LOOP;
END;
$rollback$;
