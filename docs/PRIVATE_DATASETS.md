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
