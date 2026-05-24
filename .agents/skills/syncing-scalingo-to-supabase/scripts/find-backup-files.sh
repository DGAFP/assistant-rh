#!/bin/bash
# find-backup-files.sh - Identify most recent backup files in current directory
# Usage: find-backup-files.sh [--json]

set -euo pipefail

JSON_OUTPUT=false

if [ "${1:-}" = "--json" ]; then
    JSON_OUTPUT=true
fi

# Find most recent .tar.gz file
TAR_GZ_FILE=""
TAR_GZ_COUNT=0

for f in *.tar.gz 2>/dev/null; do
    if [ -f "$f" ]; then
        TAR_GZ_COUNT=$((TAR_GZ_COUNT + 1))
        if [ -z "$TAR_GZ_FILE" ] || [ "$f" -nt "$TAR_GZ_FILE" ]; then
            TAR_GZ_FILE="$f"
        fi
    fi
done

# Find most recent .pgsql file
PGSQL_FILE=""
PGSQL_COUNT=0

for f in *.pgsql 2>/dev/null; do
    if [ -f "$f" ]; then
        PGSQL_COUNT=$((PGSQL_COUNT + 1))
        if [ -z "$PGSQL_FILE" ] || [ "$f" -nt "$PGSQL_FILE" ]; then
            PGSQL_FILE="$f"
        fi
    fi
done

# Output results
if [ "$JSON_OUTPUT" = true ]; then
    echo "{"
    echo "  \"tar_gz\": {"
    echo "    \"most_recent\": \"$TAR_GZ_FILE\","
    echo "    \"count\": $TAR_GZ_COUNT"
    echo "  },"
    echo "  \"pgsql\": {"
    echo "    \"most_recent\": \"$PGSQL_FILE\","
    echo "    \"count\": $PGSQL_COUNT"
    echo "  }"
    echo "}"
else
    echo "Backup files in current directory:"
    echo ""
    echo ".tar.gz files found: $TAR_GZ_COUNT"
    if [ -n "$TAR_GZ_FILE" ]; then
        echo "  Most recent: $TAR_GZ_FILE"
    fi
    echo ""
    echo ".pgsql files found: $PGSQL_COUNT"
    if [ -n "$PGSQL_FILE" ]; then
        echo "  Most recent: $PGSQL_FILE"
    fi
    
    if [ -n "$PGSQL_FILE" ]; then
        echo ""
        echo "Ready to restore command:"
        echo "  ./scripts/restore-supabase.sh <TARGET_DB_URI> $PGSQL_FILE"
    fi
fi
