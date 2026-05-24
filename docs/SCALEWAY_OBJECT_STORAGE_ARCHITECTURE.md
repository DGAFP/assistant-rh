# Scaleway Object Storage Architecture

## Current State

Region:
- `fr-par`

Project target:
- `assistant-rh`

Buckets created:
- `assistant-rh-bronze`
- `assistant-rh-silver`
- `assistant-rh-gold`

The current target is direct production operation with a monthly batch pipeline.


## Target Prefix Layout

Primary production prefixes:
- `prod/bronze/service_public/...`
- `prod/silver/service_public/...`
- `prod/gold/service_public/...`

Future optional prefixes if a staging lane is added later:
- `staging/bronze/service_public/...`
- `staging/silver/service_public/...`
- `staging/gold/service_public/...`


## Medallion Mapping

### Bronze

Role:
- raw source snapshots

Contents:
- DILA ZIP snapshots
- extracted XML files
- run manifests

Local equivalent:
- `data/lake/service_public/bronze/`

Object Storage target:
- `prod/bronze/service_public/...`


### Silver

Role:
- normalized, structured documents

Contents:
- parsed document JSON
- parsed sections JSONL
- manifests

Local equivalent:
- `data/lake/service_public/silver/`

Object Storage target:
- `prod/silver/service_public/...`


### Gold

Role:
- retrieval-ready chunks and optional embedding artifacts

Contents:
- chunk JSONL
- parquet exports
- embedding matrices
- manifests

Local equivalent:
- `data/lake/service_public/gold/`

Object Storage target:
- `prod/gold/service_public/...`


## Recommendation

Keep the three buckets already created and use them in direct production mode.

Reasoning:
- no extra bucket sprawl
- simple monthly operations
- easy mapping from the local medallion lake
- ready for more sources later with the same `env/layer/source` convention


## Suggested Env Vars

```env
SCW_DEFAULT_REGION=fr-par

SCW_BUCKET_BRONZE=assistant-rh-bronze
SCW_BUCKET_SILVER=assistant-rh-silver
SCW_BUCKET_GOLD=assistant-rh-gold

SCW_PREFIX_PROD=prod
SCW_PREFIX_STAGING=staging
```

Production sync example:

```bash
python3 scripts/service_public_medallion_pipeline.py \
  --fiche-id F12391 \
  --situation FPE \
  --target-env prod \
  --sync-object-storage \
  --no-embed
```


## Data Flow

1. Fetch official XML snapshot
2. Write raw ZIP/XML to bronze bucket
3. Parse and normalize into silver bucket
4. Chunk and optionally embed into gold bucket
5. Compare generated output with the current DB when needed
6. Load serving data into PostgreSQL / pgvector only in a separate explicit step


## Lifecycle Guidance

Bronze:
- keep longer retention
- rebuild source of truth

Silver:
- keep medium retention
- rebuilt from bronze if needed

Gold:
- keep current and recent versions
- tied to chunking and embedding versions


## Operational Notes

- Bucket creation itself has no meaningful fixed monthly cost.
- Main costs come from stored GB and egress.
- Running compute near Scaleway Object Storage reduces egress risk.
- If you later add staging or stricter deployment controls, the prefix structure is already ready.
