-- #604: chat_runs.conversation_id is varchar(8) on staging, sized for Streamlit's
-- 8-character IDs. API clients send their own correlation ID (Conversations: a UUID),
-- which failed run finalization after the whole pipeline. varchar -> text needs no
-- table rewrite; the API bounds the value instead (handlers/chat_body.py).
ALTER TABLE public.chat_runs ALTER COLUMN conversation_id TYPE TEXT;
