-- Operator-only rollback: stop the B4 API first. Prefer keeping the additive
-- schema when rolling back the application. No effect on Streamlit passwords.
-- Revoke sessions before dropping revision checks; they must never resurrect.
UPDATE public.api_sessions SET revoked_at = COALESCE(revoked_at, clock_timestamp());
DROP TRIGGER IF EXISTS api_group_credential_revision ON public.user_groups;
DROP FUNCTION IF EXISTS public.api_group_credential_revision();
ALTER TABLE public.api_sessions DROP COLUMN IF EXISTS credential_revision;
ALTER TABLE public.user_groups DROP COLUMN IF EXISTS credential_revision;
DROP TABLE IF EXISTS public.api_auth_limits;
