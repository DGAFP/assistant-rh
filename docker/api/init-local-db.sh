#!/bin/sh
# Sourced by the Postgres image on an empty, dedicated local volume.
set -eu
bootstrap_root=${API_BOOTSTRAP_ROOT:-/bootstrap}
set --
while IFS= read -r file; do
    set -- "$@" --file="$bootstrap_root/$file"
done < "$bootstrap_root/docker/api/bootstrap-local.txt"
psql --no-psqlrc --set=ON_ERROR_STOP=1 --single-transaction \
    --username="$POSTGRES_USER" --dbname="$POSTGRES_DB" "$@"
