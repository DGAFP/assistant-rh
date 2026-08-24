CREATE TABLE IF NOT EXISTS public.rag_quality_eval_runs (
    id BIGSERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    status TEXT NOT NULL DEFAULT 'started',
    goldset_name TEXT NOT NULL,
    tag_filter TEXT[] NOT NULL DEFAULT '{}',
    git_sha TEXT,
    run_label TEXT,
    config_fingerprint TEXT NOT NULL,
    config JSONB NOT NULL DEFAULT '{}'::jsonb,
    judge_provider TEXT,
    judge_model TEXT,
    ragas_status TEXT,
    aggregate JSONB NOT NULL DEFAULT '{}'::jsonb,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    error TEXT
);

CREATE INDEX IF NOT EXISTS idx_rag_quality_eval_runs_lookup
    ON public.rag_quality_eval_runs (goldset_name, config_fingerprint, status, created_at DESC);

CREATE TABLE IF NOT EXISTS public.rag_quality_eval_items (
    id BIGSERIAL PRIMARY KEY,
    run_id BIGINT NOT NULL REFERENCES public.rag_quality_eval_runs(id) ON DELETE CASCADE,
    question_id BIGINT,
    question TEXT NOT NULL,
    gold_answer TEXT,
    gold_sources TEXT[] NOT NULL DEFAULT '{}',
    answer TEXT,
    contexts JSONB NOT NULL DEFAULT '[]'::jsonb,
    sources JSONB NOT NULL DEFAULT '[]'::jsonb,
    deterministic_metrics JSONB NOT NULL DEFAULT '{}'::jsonb,
    ragas_metrics JSONB NOT NULL DEFAULT '{}'::jsonb,
    judge_result JSONB NOT NULL DEFAULT '{}'::jsonb,
    timing JSONB NOT NULL DEFAULT '{}'::jsonb,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    error TEXT
);

CREATE INDEX IF NOT EXISTS idx_rag_quality_eval_items_run_id
    ON public.rag_quality_eval_items (run_id);

CREATE INDEX IF NOT EXISTS idx_rag_quality_eval_items_question_id
    ON public.rag_quality_eval_items (question_id);
