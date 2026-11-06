#!/usr/bin/env bash
# Create a non-owner PostgreSQL login for one runtime (#599) and store its DSN as a
# GitHub environment secret. The password is generated locally, sent only to Scaleway
# and to `gh secret set` through stdin, and never printed.
#
#   scripts/create_scaleway_db_runtime_user.sh <staging|production> <streamlit|api>
#
# Prerequisites: `scw` and `gh` authenticated; migration 20261007130000_api_runtime_roles
# applied to the target. Afterwards, attach the login as the admin (see the runbook).
set -euo pipefail

target=${1:?usage: $0 <staging|production> <streamlit|api>}
runtime=${2:?usage: $0 <staging|production> <streamlit|api>}
case "$target" in
  staging) instance_name=assistant-rh-stag ;;
  production) instance_name=assistant-rh-prod ;;
  *) echo "unknown target: $target" >&2; exit 2 ;;
esac
case "$runtime" in
  streamlit) login=assistant_rh_streamlit; secret=STREAMLIT_POSTGRES_DSN; role=arh_streamlit ;;
  api) login=assistant_rh_api; secret=API_POSTGRES_DSN; role=arh_api ;;
  *) echo "unknown runtime: $runtime" >&2; exit 2 ;;
esac
region=${SCW_DEFAULT_REGION:-fr-par}
repo=${GITHUB_REPOSITORY:-DGAFP/assistant-rh}
database=${DATABASE_NAME:-assistant_rh}

instance=$(scw rdb instance list name="$instance_name" region="$region" -o json)
instance_id=$(jq -r --arg n "$instance_name" '[.[] | select(.name == $n)] | if length == 1 then .[0].id else error("expected one instance") end' <<<"$instance")
endpoint=$(jq -r '.[0].endpoints[0] | "\(.ip):\(.port)"' <<<"$instance")
if scw rdb user list instance-id="$instance_id" region="$region" -o json | jq -e --arg u "$login" 'any(.[]; .name == $u)' >/dev/null; then
  echo "$login already exists on $instance_name; rotate its password from the console instead." >&2
  exit 1
fi

# Scaleway policy: 8-128 chars with upper, lower, digit and special characters.
password=$(python3 -c 'import secrets, string
alphabet = string.ascii_letters + string.digits + "-_.~"
while True:
    value = "".join(secrets.choice(alphabet) for _ in range(40))
    if all(any(c in group for c in value) for group in (string.ascii_lowercase, string.ascii_uppercase, string.digits, "-_.~")):
        print(value)
        break')

scw rdb user create instance-id="$instance_id" region="$region" name="$login" password="$password" is-admin=false -o json >/dev/null
# Read-only: schema usage, reads and extension functions. Writes come from $role only.
scw rdb privilege set instance-id="$instance_id" region="$region" database-name="$database" user-name="$login" permission=readonly -o json >/dev/null
printf 'postgresql://%s:%s@%s/%s?sslmode=require' "$login" "$password" "$endpoint" "$database" \
  | gh secret set "$secret" --env "scaleway-$target" --repo "$repo"
unset password

echo "Created $login on $instance_name and stored $secret in scaleway-$target."
echo "Next, as the migration admin on $target:"
echo "  SELECT public.api_attach_runtime_user('$login', '$role');"
