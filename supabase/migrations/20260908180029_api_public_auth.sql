-- B4 public eight-hour sessions (D6/A3), not permanent admin API keys.
ALTER TABLE public.user_groups ADD COLUMN IF NOT EXISTS credential_revision BIGINT NOT NULL DEFAULT 0;
ALTER TABLE public.api_sessions ADD COLUMN IF NOT EXISTS credential_revision BIGINT NOT NULL DEFAULT 0;

-- Covers legacy Streamlit UPDATEs without changing their SQL or password format.
-- A -> B -> A never revives an old session. Policy/eligibility changes also revoke.
CREATE OR REPLACE FUNCTION public.api_group_credential_revision() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
    NEW.credential_revision := OLD.credential_revision;
    IF ROW(NEW.password_hash, NEW.allowed_ministries, NEW.default_ministry, NEW.visible, NEW.is_admin)
       IS DISTINCT FROM ROW(OLD.password_hash, OLD.allowed_ministries, OLD.default_ministry, OLD.visible, OLD.is_admin) THEN
        NEW.credential_revision := OLD.credential_revision + 1;
    END IF;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS api_group_credential_revision ON public.user_groups;
CREATE TRIGGER api_group_credential_revision BEFORE UPDATE ON public.user_groups
FOR EACH ROW EXECUTE FUNCTION public.api_group_credential_revision();

-- Shared across API processes/replicas, never a persistent group lockout.
-- Subject is a SHA-256 digest; no password, bearer, slug, or address is stored.
CREATE TABLE IF NOT EXISTS public.api_auth_limits (
    scope TEXT NOT NULL CHECK (scope IN ('global', 'source', 'slug')),
    subject TEXT NOT NULL CHECK (subject ~ '^[0-9a-f]{64}$'),
    expires_at TIMESTAMPTZ NOT NULL,
    attempts INTEGER NOT NULL CHECK (attempts >= 0),
    PRIMARY KEY (scope, subject)
);
CREATE INDEX IF NOT EXISTS api_auth_limits_expiry_idx ON public.api_auth_limits(expires_at);
REVOKE ALL ON public.api_auth_limits FROM PUBLIC;

-- Bounded retention scans expire/revoke time, not the growing live-session set.
CREATE INDEX IF NOT EXISTS api_sessions_retention_idx ON public.api_sessions
    (LEAST(expires_at, COALESCE(revoked_at, expires_at)), token_hash);
