#!/bin/sh
# Apply the versioned API bootstrap to one database of this stack, in a single transaction.
# Usage: bootstrap-api.sh <database> empty|imported
# Demo credentials (local_auth.sql) are never applied. The synthetic runtime.sql only creates
# rag_config on an empty database; an imported staging rag_config is kept.
set -eu
database=$1
mode=$2
case $mode in
    empty | imported) ;;
    *) echo "mode inconnu : $mode" >&2; exit 2 ;;
esac
set --
while IFS= read -r file; do
    case $file in
        */local_auth.sql) continue ;;
        */runtime.sql) [ "$mode" = imported ] && continue ;;
    esac
    set -- "$@" --file="/workspace/$file"
done < /workspace/docker/api/bootstrap-local.txt
if [ "$mode" = empty ]; then
    set -- "$@" --command="UPDATE public.rag_config SET config = '{}'::jsonb, updated_by = 'local-dev'"
fi
psql --no-psqlrc --set=ON_ERROR_STOP=1 --single-transaction --quiet \
    --username="$POSTGRES_USER" --dbname="$database" "$@" \
    --command="GRANT USAGE ON SCHEMA public TO dev_api" \
    --command="SELECT public.api_attach_runtime_user('dev_api', 'arh_api')"
