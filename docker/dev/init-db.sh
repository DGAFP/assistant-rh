#!/bin/sh
set -eu
# Only runs inside this stack's new, dedicated Postgres volume.
psql --no-psqlrc -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d postgres <<'SQL'
CREATE DATABASE conversations;
CREATE ROLE dev_api LOGIN PASSWORD 'local-dev-only';
SQL
sh /workspace/docker/dev/bootstrap-api.sh assistant_rh empty
