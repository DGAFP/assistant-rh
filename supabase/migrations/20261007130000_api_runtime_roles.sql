-- Runtime roles (#599): only the API writes individual feedback content.
--
-- Ownership stays with the migrating admin. NOLOGIN roles carry write grants only:
--   arh_api        API runtime, the only writer of individual feedback content
--   arh_streamlit  legacy Streamlit writes on its own tables and collective chat history
--   arh_analysis   AI feedback analysis columns
-- Login users are created by Scaleway (schema usage, reads and extension functions come
-- from its read-only permission), then attached with api_attach_runtime_user(). A runtime
-- user owns nothing, so it cannot disable or drop the guards below, nor TRUNCATE.
-- See docs/deployment/SCALEWAY_DB_RUNTIME_ROLES.md.
DO $migration$
DECLARE
    role_name TEXT;
    table_name TEXT;
BEGIN
    FOREACH role_name IN ARRAY ARRAY['arh_api', 'arh_streamlit', 'arh_analysis'] LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = role_name) THEN
            EXECUTE format('CREATE ROLE %I NOLOGIN', role_name);
        END IF;
    END LOOP;

    -- Streamlit: dashboards read everything but API credentials; it writes its own tables
    -- and the collective chat history. Individual feedback is guarded below.
    GRANT SELECT ON ALL TABLES IN SCHEMA public TO arh_streamlit;
    REVOKE ALL ON public.api_sessions, public.api_auth_limits, public.api_delegation_replays FROM arh_streamlit;
    FOREACH table_name IN ARRAY ARRAY[
        'user_groups', 'rag_config', 'system_prompts', 'acronyms', 'acronyms_missing', 'chat_reviews',
        'goldset_questions_v2', 'intent_eval_goldset', 'intent_eval_experiments', 'rag_quality_eval_runs', 'rag_quality_eval_items'
    ] LOOP
        IF to_regclass('public.' || table_name) IS NOT NULL THEN
            EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON public.%I TO arh_streamlit', table_name);
        END IF;
    END LOOP;
    GRANT INSERT, UPDATE ON public.chat_runs, public.chat_feedbacks TO arh_streamlit;
    -- The legacy replacement trigger runs as the invoker and archives the previous version.
    GRANT INSERT ON public.chat_feedback_audit TO arh_streamlit;
    GRANT INSERT ON public.rag_trace_events TO arh_streamlit;
    GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO arh_streamlit;
    -- New admin-created tables stay readable by the dashboards; writes are always explicit.
    ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO arh_streamlit;

    -- API: exactly the statements of apps/api/src/assistant_rh_api/db.
    GRANT SELECT ON public.rag_documents, public.rag_sections, public.rag_config, public.system_prompts, public.acronyms TO arh_api;
    FOREACH table_name IN ARRAY ARRAY['rag_chunks_matte', 'rag_chunks_mso', 'rag_chunks_mi', 'rag_chunks_masa',
                                      'rag_chunks_service_public', 'rag_chunks_dgafp', 'rag_chunks_rgrh'] LOOP
        IF to_regclass('public.' || table_name) IS NOT NULL THEN
            EXECUTE format('GRANT SELECT ON public.%I TO arh_api', table_name);
        END IF;
    END LOOP;
    -- Row locks need UPDATE on one column: the least sensitive one, never rights or credentials.
    GRANT SELECT, UPDATE (icon) ON public.user_groups TO arh_api;
    GRANT SELECT, UPDATE (conversation_id) ON public.chat_runs TO arh_api;
    GRANT INSERT ON public.chat_runs TO arh_api;
    GRANT SELECT, INSERT, UPDATE, DELETE ON public.api_sessions, public.api_auth_limits TO arh_api;
    GRANT SELECT, INSERT ON public.chat_run_sources, public.chat_feedback_audit, public.rag_trace_events TO arh_api;
    GRANT SELECT, INSERT, UPDATE ON public.chat_feedbacks TO arh_api;
    -- The purge locks expired rows (FOR UPDATE SKIP LOCKED), which requires UPDATE.
    GRANT SELECT, INSERT, UPDATE, DELETE ON public.api_delegation_replays TO arh_api;
    GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO arh_api;

    -- AI analysis: reads feedback and runs, writes only its own columns.
    GRANT SELECT ON public.chat_feedbacks, public.chat_runs TO arh_analysis;
    GRANT UPDATE (error_category, ai_reason, ai_analyzed_at) ON public.chat_feedbacks TO arh_analysis;
    IF EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'assistant_rh_ingest') THEN
        -- Table-level REVOKE also removes the matching column privileges.
        REVOKE INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON public.chat_runs, public.chat_feedbacks FROM assistant_rh_ingest;
        IF to_regclass('public.chat_reviews') IS NOT NULL THEN
            REVOKE INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON public.chat_reviews FROM assistant_rh_ingest;
        END IF;
        GRANT arh_analysis TO assistant_rh_ingest;
    END IF;

    -- Individual feedback content is written by the API role only. Others keep annotation
    -- and analysis columns; deletion is reserved to the API and the table owner.
    CREATE OR REPLACE FUNCTION public.api_feedback_individual_guard() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog AS $function$
    DECLARE
        author UUID;
        api BOOLEAN := pg_has_role(current_user, 'arh_api', 'USAGE');
        free_columns CONSTANT TEXT[] := ARRAY['beta_scope', 'theme', 'error_category', 'ai_reason', 'ai_analyzed_at'];
    BEGIN
        -- Same row, same author: the API store, or annotation/analysis only. No parent lookup.
        -- Keep the trigger free of column dependencies so the local B2 bootstrap remains repeatable.
        IF TG_OP = 'UPDATE' AND NEW.turn_id IS NOT DISTINCT FROM OLD.turn_id
           AND NEW.api_actor_user_id IS NOT DISTINCT FROM OLD.api_actor_user_id
           AND (api OR to_jsonb(NEW) - free_columns = to_jsonb(OLD) - free_columns) THEN
            RETURN NEW;
        END IF;
        IF TG_OP IN ('UPDATE', 'DELETE') THEN
            SELECT r.author_user_id INTO author FROM public.chat_runs r WHERE r.turn_id = OLD.turn_id;
            IF author IS NOT NULL AND NOT api AND (
                TG_OP = 'UPDATE' OR NOT pg_has_role(current_user, (SELECT c.relowner FROM pg_class c WHERE c.oid = TG_RELID), 'USAGE')
            ) THEN
                RAISE EXCEPTION 'Individual feedback is written only by the API' USING ERRCODE = '42501';
            END IF;
            IF TG_OP = 'DELETE' THEN
                RETURN OLD;
            END IF;
        END IF;
        SELECT r.author_user_id INTO author FROM public.chat_runs r WHERE r.turn_id = NEW.turn_id;
        IF author IS NOT NULL AND NOT api THEN
            RAISE EXCEPTION 'Individual feedback is written only by the API' USING ERRCODE = '42501';
        END IF;
        IF author IS NOT NULL AND NEW.api_actor_user_id IS DISTINCT FROM author THEN
            RAISE EXCEPTION 'Individual feedback requires its run author' USING ERRCODE = '23514';
        END IF;
        RETURN NEW;
    END;
    $function$;
    CREATE OR REPLACE TRIGGER api_feedback_individual_guard BEFORE INSERT OR UPDATE OR DELETE ON public.chat_feedbacks
        FOR EACH ROW EXECUTE FUNCTION public.api_feedback_individual_guard();
END;
$migration$;

-- Attach a Scaleway-created login to one runtime role. Scaleway's permission applies its
-- table grants as the owner; direct writes are revoked so that every write goes through
-- the runtime role and its guards. Run by the admin, once per login and after any later
-- Scaleway permission change.
CREATE OR REPLACE FUNCTION public.api_attach_runtime_user(login TEXT, runtime_role TEXT) RETURNS void
LANGUAGE plpgsql SET search_path = pg_catalog AS $function$
BEGIN
    IF runtime_role NOT IN ('arh_api', 'arh_streamlit', 'arh_analysis') THEN
        RAISE EXCEPTION 'unknown runtime role %', runtime_role;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = login AND rolcanlogin AND NOT rolsuper) THEN
        RAISE EXCEPTION 'runtime user % must be an existing non-superuser login', login;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_class c WHERE c.relnamespace = 'public'::regnamespace AND pg_has_role(login, c.relowner, 'MEMBER')) THEN
        RAISE EXCEPTION 'runtime user % must not own or inherit ownership of public tables', login;
    END IF;
    EXECUTE format('REVOKE INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON ALL TABLES IN SCHEMA public FROM %I', login);
    IF runtime_role <> 'arh_api' THEN
        -- Session digests and quotas stay private to the API, even for read-only logins.
        EXECUTE format('REVOKE ALL ON public.api_sessions, public.api_auth_limits, public.api_delegation_replays FROM %I', login);
    END IF;
    EXECUTE format('GRANT %I TO %I', runtime_role, login);
END;
$function$;
REVOKE ALL ON FUNCTION public.api_attach_runtime_user(TEXT, TEXT) FROM PUBLIC;
