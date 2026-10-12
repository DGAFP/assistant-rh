#!/bin/sh
set -eu
# Only runs inside this stack's new, dedicated Postgres volume.
psql --no-psqlrc -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d postgres <<'SQL'
CREATE DATABASE conversations;
CREATE ROLE dev_api LOGIN PASSWORD 'local-dev-only';
SQL
while IFS= read -r file; do
    psql --no-psqlrc -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d assistant_rh -f "/workspace/$file"
done < /workspace/docker/api/bootstrap-local.txt
psql --no-psqlrc -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d assistant_rh <<'SQL'
GRANT USAGE ON SCHEMA public TO dev_api;
SELECT public.api_attach_runtime_user('dev_api', 'arh_api');
UPDATE public.rag_config SET config = '{}'::jsonb, updated_by = 'local-dev';
SQL
