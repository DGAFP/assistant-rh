-- Synthetic API runtime fixture. This file must never contain a staging dump,
-- user conversation, feedback, authentication material, or personal data.
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS public.rag_config (
    id INTEGER PRIMARY KEY DEFAULT 1,
    config JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_by VARCHAR(100) NOT NULL DEFAULT 'synthetic-fixture',
    CONSTRAINT rag_config_single_row CHECK (id = 1)
);

INSERT INTO public.rag_config (id, config, updated_by)
VALUES (
    1,
    '{"v3_generator_model":"synthetic-generator","v3_selector_model":"synthetic-selector","v3_system_prompt_name":"synthetic-system-prompt"}'::jsonb,
    'synthetic-fixture'
)
ON CONFLICT (id) DO UPDATE
SET config = EXCLUDED.config,
    updated_at = CURRENT_TIMESTAMP,
    updated_by = EXCLUDED.updated_by;
