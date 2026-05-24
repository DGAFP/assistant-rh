#!/bin/bash
# restore-supabase.sh - Restore a PostgreSQL dump to Supabase with mandatory flags
# Usage: restore-supabase.sh <target_db_uri> <dump_file.pgsql>

set -euo pipefail

# Validate arguments
if [ $# -ne 2 ]; then
    echo "Error: Invalid arguments"
    echo "Usage: restore-supabase.sh <target_db_uri> <dump_file.pgsql>"
    echo ""
    echo "Arguments:"
    echo "  target_db_uri   Full PostgreSQL connection string for Supabase"
    echo "  dump_file.pgsql Path to the .pgsql dump file to restore"
    exit 1
fi

TARGET_DB_URI="$1"
DUMP_FILE="$2"

# Validate dump file exists
if [ ! -f "$DUMP_FILE" ]; then
    echo "Error: Dump file not found: $DUMP_FILE"
    exit 1
fi

# Validate file extension
if [[ "$DUMP_FILE" != *.pgsql ]]; then
    echo "Warning: File does not have .pgsql extension: $DUMP_FILE"
fi

echo "Restoring dump file: $DUMP_FILE"
echo "Target database: [connection string hidden for security]"
echo ""

# Run pg_restore with mandatory flags:
# -O : Skip ownership restoration (Supabase manages roles)
# -x : Skip ACL/privilege restoration (avoid permission conflicts)
# -1 : Run in single transaction (all-or-nothing, cleaner failure mode)
if pg_restore -d "$TARGET_DB_URI" -O -x -1 "$DUMP_FILE"; then
    echo ""
    echo "✓ Restore completed successfully"
else
    EXIT_CODE=$?
    echo ""
    echo "✗ Restore failed with exit code: $EXIT_CODE"
    exit $EXIT_CODE
fi
