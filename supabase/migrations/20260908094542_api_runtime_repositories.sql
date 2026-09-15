-- B2. Additive API persistence; run against the provisioned runtime schema.
-- The deployment runner wraps migrations in a transaction. Explicit locking
-- prevents legacy inserts racing the feedback archival/uniqueness transition.
ALTER TABLE public.chat_runs ALTER COLUMN turn_id TYPE TEXT;
ALTER TABLE public.chat_feedbacks ALTER COLUMN turn_id TYPE TEXT;
ALTER TABLE public.chat_runs ALTER COLUMN user_group TYPE TEXT;
DO $$ BEGIN
    IF to_regclass('public.chat_reviews') IS NOT NULL THEN
        ALTER TABLE public.chat_reviews ALTER COLUMN turn_id TYPE TEXT;
    END IF;
END $$;
ALTER TABLE public.chat_runs ADD COLUMN IF NOT EXISTS api_session_hash TEXT;
ALTER TABLE public.chat_runs ADD COLUMN IF NOT EXISTS api_record JSONB;
ALTER TABLE public.chat_feedbacks ADD COLUMN IF NOT EXISTS api_session_hash TEXT;
ALTER TABLE public.chat_feedbacks ADD COLUMN IF NOT EXISTS api_group_slug TEXT;
ALTER TABLE public.chat_feedbacks ADD COLUMN IF NOT EXISTS api_revision BIGINT NOT NULL DEFAULT 0;
ALTER TABLE public.chat_feedbacks ADD COLUMN IF NOT EXISTS api_submission_id BIGINT;

CREATE TABLE IF NOT EXISTS public.api_sessions (
    token_hash TEXT PRIMARY KEY CHECK (token_hash ~ '^[0-9a-f]{64}$'),
    group_slug VARCHAR(64) NOT NULL REFERENCES public.user_groups(slug) ON DELETE CASCADE,
    credential_hash TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ,
    CHECK (expires_at > created_at)
);
CREATE INDEX IF NOT EXISTS api_sessions_group_idx ON public.api_sessions(group_slug);

CREATE TABLE IF NOT EXISTS public.chat_run_sources (
    turn_id TEXT NOT NULL REFERENCES public.chat_runs(turn_id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    doc_ref TEXT NOT NULL,
    document_id TEXT,
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    PRIMARY KEY (turn_id, ordinal),
    UNIQUE (turn_id, doc_ref)
);

CREATE TABLE IF NOT EXISTS public.chat_feedback_audit (
    audit_id BIGSERIAL PRIMARY KEY,
    turn_id TEXT,
    feedback_id BIGINT NOT NULL,
    archived_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    group_slug TEXT,
    audit_session_hash TEXT,
    reason TEXT NOT NULL,
    record JSONB NOT NULL
);
ALTER TABLE public.chat_feedback_audit ADD COLUMN IF NOT EXISTS group_slug TEXT;
ALTER TABLE public.chat_feedback_audit ADD COLUMN IF NOT EXISTS audit_session_hash TEXT;
CREATE INDEX IF NOT EXISTS chat_feedback_audit_turn_idx ON public.chat_feedback_audit(turn_id, audit_id);

LOCK TABLE public.chat_feedbacks IN SHARE ROW EXCLUSIVE MODE;
WITH ranked AS (
    SELECT id, row_number() OVER (PARTITION BY turn_id ORDER BY ts DESC NULLS LAST, id DESC) AS ordinal
    FROM public.chat_feedbacks WHERE turn_id IS NOT NULL
), archived AS (
    INSERT INTO public.chat_feedback_audit (turn_id, feedback_id, reason, record, group_slug, audit_session_hash)
    SELECT f.turn_id, f.id, 'B2 duplicate', to_jsonb(f), f.api_group_slug, f.api_session_hash
    FROM public.chat_feedbacks f JOIN ranked r ON r.id = f.id WHERE r.ordinal > 1
    RETURNING feedback_id
)
DELETE FROM public.chat_feedbacks WHERE id IN (SELECT feedback_id FROM archived);
CREATE UNIQUE INDEX IF NOT EXISTS chat_feedbacks_turn_unique ON public.chat_feedbacks(turn_id);

-- Streamlit still issues plain INSERTs. Keep them successful for repeated
-- feedback while maintaining one current row and preserving every old version.
-- API replacements use an explicit transaction and do not pass through here.
CREATE OR REPLACE FUNCTION public.api_legacy_feedback_insert() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
DECLARE previous public.chat_feedbacks%ROWTYPE;
BEGIN
    IF NEW.turn_id IS NULL THEN RETURN NEW; END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended(NEW.turn_id, 454));
    SELECT * INTO previous FROM public.chat_feedbacks WHERE turn_id = NEW.turn_id FOR UPDATE;
    IF NOT FOUND OR NEW.api_session_hash IS NOT NULL THEN RETURN NEW; END IF;
    IF (NEW.ts IS NULL AND previous.ts IS NOT NULL)
       OR (NEW.ts IS NOT DISTINCT FROM previous.ts AND NEW.id < COALESCE(previous.api_submission_id, previous.id))
       OR (NEW.ts IS NOT NULL AND previous.ts IS NOT NULL AND NEW.ts < previous.ts) THEN
        INSERT INTO public.chat_feedback_audit(turn_id, feedback_id, reason, record, group_slug)
        VALUES (NEW.turn_id, NEW.id, 'legacy older submission', to_jsonb(NEW),
                (SELECT user_group FROM public.chat_runs WHERE turn_id = NEW.turn_id));
        RETURN NULL;
    END IF;
    INSERT INTO public.chat_feedback_audit(turn_id, feedback_id, reason, record, group_slug)
    VALUES (previous.turn_id, previous.id, 'legacy replacement', to_jsonb(previous),
            (SELECT user_group FROM public.chat_runs WHERE turn_id = NEW.turn_id));
    UPDATE public.chat_feedbacks SET
        ts = NEW.ts, turn_idx = NEW.turn_idx, helpful = NEW.helpful, reasons = NEW.reasons,
        comment = NEW.comment, stars = NEW.stars, reasons_positive = NEW.reasons_positive,
        reasons_negative = NEW.reasons_negative, session_id = NEW.session_id,
        question = NEW.question, answer = NEW.answer,
        error_category = NULL, ai_reason = NULL, ai_analyzed_at = NULL,
        api_session_hash = NULL, api_group_slug = NULL, api_revision = api_revision + 1, api_submission_id = NEW.id
    WHERE id = previous.id;
    RETURN NULL;
END $$;
CREATE OR REPLACE TRIGGER api_legacy_feedback_insert
BEFORE INSERT ON public.chat_feedbacks FOR EACH ROW EXECUTE FUNCTION public.api_legacy_feedback_insert();
