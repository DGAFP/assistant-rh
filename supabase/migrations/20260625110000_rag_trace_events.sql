-- Issue #143: persist RAG trace events separately from chat_runs so one turn can
-- be reconstructed stage by stage without widening the chat_runs summary table.
DO $$
BEGIN
    IF to_regclass('public.chat_runs') IS NOT NULL THEN
        ALTER TABLE public.chat_runs ADD COLUMN IF NOT EXISTS trace_id TEXT;
    END IF;
END
$$;

CREATE TABLE IF NOT EXISTS public.rag_trace_events (
    id BIGSERIAL PRIMARY KEY,
    turn_id TEXT NOT NULL,
    trace_id TEXT NOT NULL,
    env TEXT NOT NULL DEFAULT '',
    event_index INTEGER NOT NULL DEFAULT 0,
    stage TEXT NOT NULL,
    attempt_name TEXT NOT NULL DEFAULT '',
    duration_ms INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'ok',
    input_ref JSONB NOT NULL DEFAULT '{}',
    output_ref JSONB NOT NULL DEFAULT '{}',
    metrics JSONB NOT NULL DEFAULT '{}',
    error_type TEXT NOT NULL DEFAULT '',
    error_message TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (turn_id, event_index)
);

CREATE INDEX IF NOT EXISTS idx_rag_trace_events_trace_id ON public.rag_trace_events (trace_id);
CREATE INDEX IF NOT EXISTS idx_rag_trace_events_turn_id ON public.rag_trace_events (turn_id);
CREATE INDEX IF NOT EXISTS idx_rag_trace_events_stage ON public.rag_trace_events (stage);
CREATE INDEX IF NOT EXISTS idx_rag_trace_events_env_created ON public.rag_trace_events (env, created_at DESC);
