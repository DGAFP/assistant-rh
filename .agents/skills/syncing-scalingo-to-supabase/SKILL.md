---
name: syncing-scalingo-to-supabase
description: Syncs a PostgreSQL backup from Scalingo secure environment into Supabase by running backup/download/extract/restore sequentially while bypassing cloud-specific role/privilege restoration. Use when asked to sync database, pull Scalingo data, refresh local db, or restore staging/production db to Supabase.
---

# Syncing Scalingo to Supabase

Sequential workflow to pull a PostgreSQL backup from Scalingo and restore it into Supabase.

## When to Use

- "sync the database"
- "pull Scalingo data"
- "refresh local db from production"
- "restore staging db to Supabase"
- "update Supabase from Scalingo backup"

## Required Context

| Field | Description |
|-------|-------------|
| `APP_NAME` | Scalingo application name |
| `REGION` | Scalingo region (e.g., `osc-secnum-fr1`) |
| `TARGET_DB_URI` | Full PostgreSQL connection string for Supabase |

## Execution Protocol

**CRITICAL**: Run each step sequentially. Inspect output before proceeding. Do NOT chain commands.

### Step 1: Create Backup

```bash
scalingo --region <REGION> --app <APP_NAME> --addon postgresql backups-create
```

**Validate**: Look for `Backup successfully finished` in output. Confirm backup ID appears. Note the backup ID for next step.

### Step 2: Download Backup

```bash
scalingo --region <REGION> --app <APP_NAME> --addon postgresql backups-download
```

**Validate**: Confirm `.tar.gz` file downloaded to current directory.

### Step 3: Extract Archive

```bash
tar -xzf <DOWNLOADED_FILE>.tar.gz
```

**Validate**: Confirm `.pgsql` dump file extracted.

### Step 4: Restore to Supabase

```bash
pg_restore -d "<TARGET_DB_URI>" -O -x -1 <EXTRACTED_FILE>.pgsql
```

**Flag meanings**:
- `-O` — Skip ownership restoration (Supabase manages roles)
- `-x` — Skip ACL/privilege restoration (avoid permission conflicts)
- `-1` — Run in single transaction (all-or-nothing, cleaner failure mode)

**Validate**: Exit code 0 confirms successful restore.

## Cleanup Rule

Only delete `.tar.gz` and `.pgsql` files **after** restore exits with code 0:

```bash
rm -f *.tar.gz *.pgsql
```

## Helper Scripts

The `scripts/` directory contains:

- `restore-supabase.sh` — Enforces mandatory flags for pg_restore
- `find-backup-files.sh` — Identifies most recent backup files in current directory
