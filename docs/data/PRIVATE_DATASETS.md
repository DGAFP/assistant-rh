# Private datasets

Some evaluation artifacts contain restricted beta-test rows and should not live in a public Git repository. Store those files in a private Hugging Face dataset owned by the DGAFP organization, then let the app fetch them at runtime.

## Recommended Hugging Face layout

Dataset repo:

```text
DGAFP/assistant-rh-private-data
```

Files:

```text
golden_beta/
  golden_beta_judge1_20260218_0859.csv
  golden_beta_judge2_20260217_2354.csv
goldsets/
  priority_contractuels_v1/
    priority_contractuels_v1.enriched.csv
    priority_contractuels_v1.source_links.csv
```

The repo must be private. Give read access to humans and deploy tokens that need the admin evaluation pages.

## Environment variables

```env
HF_TOKEN=hf_...
ASSISTANT_RH_PRIVATE_DATASET_REPO=DGAFP/assistant-rh-private-data
ASSISTANT_RH_PRIVATE_DATASET_REVISION=
ASSISTANT_RH_PRIVATE_DATASET_CACHE_DIR=.cache/assistant-rh/private-datasets
ASSISTANT_RH_GOLDEN_BETA_SOURCE=auto
ASSISTANT_RH_GOLDEN_BETA_SUBDIR=golden_beta

ASSISTANT_RH_PRIVATE_GOLDSET_REPO=DGAFP/assistant-rh-private-data
ASSISTANT_RH_PRIVATE_GOLDSET_NAME=priority_contractuels_v1
ASSISTANT_RH_PRIVATE_GOLDSET_SUBDIR=goldsets/priority_contractuels_v1
```

`ASSISTANT_RH_GOLDEN_BETA_SOURCE=auto` keeps the current private checkout working from local `data/golden_beta/*.csv` files, and falls back to Hugging Face when those files are absent in a clean public checkout.

Use `ASSISTANT_RH_GOLDEN_BETA_SOURCE=hf` in deployments if you want to force Hugging Face access and avoid accidentally depending on local files.

## Moon tasks

The legacy Python Moon project exposes convenience tasks for private dataset workflows:

```bash
# Verify that HF_TOKEN can read the private dataset
HF_TOKEN=... moon run legacy:private-data-check

# Materialize the Golden Beta CSVs locally under data/golden_beta/ (gitignored)
HF_TOKEN=... moon run legacy:golden-beta-download

# Upload local data/golden_beta/golden_beta_judge*.csv files to the private dataset
HF_TOKEN=... moon run legacy:golden-beta-upload
```

These tasks are non-cacheable and run from the workspace root. Use a read-only token for `private-data-check`/`golden-beta-download`; use a write token only for `golden-beta-upload`.

## Uploading current files

After creating the HF organization/repo and exporting a token with write access:

```bash
HF_TOKEN=... uv run python scripts/upload_golden_beta_to_hf.py \
  --repo-id DGAFP/assistant-rh-private-data \
  --source-dir data/golden_beta \
  --subdir golden_beta \
  --create-repo
```

Do not commit restricted CSVs to Git. The Golden Beta CSVs have been uploaded to `DGAFP/assistant-rh-private-data` and are intentionally removed from the Git tree; use `HF_TOKEN` to fetch them at runtime.

## Prepared goldsets

Generic goldsets are prepared from spreadsheet-style CSV/TSV files with French
columns:

```text
Questions,Réponses,Thématique,Sources,Mots-clés,Ministère
```

Prepare and relink a private goldset against the staging corpus:

```bash
uv run python scripts/prepare_goldset.py \
  --input /path/to/raw_priority_contractuels.csv \
  --goldset-name priority_contractuels_v1 \
  --target-dsn-env SCW_POSTGRES_DSN_STAGING \
  --extra-tag iteration2 \
  --output-dir .cache/assistant-rh/goldsets/priority_contractuels_v1
```

The command writes:

```text
priority_contractuels_v1.enriched.csv
priority_contractuels_v1.source_links.csv
```

`*.enriched.csv` is one row per question. It keeps compatibility fields such as
`gold_sources` while adding JSON-list columns for source labels, resolved source
links, chunk IDs, section IDs, and warnings. `*.source_links.csv` is one row per
candidate source link and is intended for review/debugging.

Validate an enriched file:

```bash
uv run python scripts/prepare_goldset.py \
  --validate-only \
  --input .cache/assistant-rh/goldsets/priority_contractuels_v1/priority_contractuels_v1.enriched.csv
```

Upload prepared files to the private dataset:

```bash
HF_TOKEN=... uv run python scripts/prepare_goldset.py \
  --input /path/to/raw_priority_contractuels.csv \
  --goldset-name priority_contractuels_v1 \
  --target-dsn-env SCW_POSTGRES_DSN_STAGING \
  --extra-tag iteration2 \
  --output-dir .cache/assistant-rh/goldsets/priority_contractuels_v1 \
  --upload-hf
```

To also tag/upsert the prepared rows into staging `goldset_questions_v2`, add
`--upsert-db`. This is the only mode in this script that writes to the database:

```bash
uv run python scripts/prepare_goldset.py \
  --input /path/to/raw_priority_contractuels.csv \
  --goldset-name priority_contractuels_v1 \
  --target-dsn-env SCW_POSTGRES_DSN_STAGING \
  --extra-tag iteration2 \
  --output-dir .cache/assistant-rh/goldsets/priority_contractuels_v1 \
  --upsert-db
```

`--extra-tag iteration2` appends `iteration2` to the PostgreSQL `tags` array and
to the enriched CSV `tags` column. If the goldset itself should be named
`iteration2`, pass `--goldset-name iteration2` instead.

Download an already prepared private goldset:

```bash
HF_TOKEN=... uv run python scripts/prepare_goldset.py \
  --download-hf \
  --goldset-name priority_contractuels_v1 \
  --output-dir .cache/assistant-rh/goldsets/priority_contractuels_v1
```

If a source label cannot be linked confidently, the row is kept with
`link_status=partial`, `ambiguous`, or `unresolved`; review
`link_warnings` and `*.source_links.csv` before using the file as a blocking
quality gate.
