# Legifrance DGAFP Automation Plan

## Goal

Automate the refresh of `rag_chunks_dgafp` from official Legifrance / DILA sources while staying as close as possible to the historical Scalingo export/baseline.

This historical compatibility note captures the diagnosis after the reverse-engineering work. Scalingo is no longer an active runtime target; references below describe the historical DGAFP baseline/export used to validate the Scaleway projection.

## Current Findings

### 1. DILA is a viable source for the DGAFP table

The current bronze lake built from the DILA / Legifrance bulk contains `3992` article payloads:

- `bronze/raw/articles/*.json`: `3992`
- sample article IDs present in current bronze and current bulk snapshot:
  - `LEGIARTI000006207978`
  - `LEGIARTI000006211065`
  - `LEGIARTI000042560147`
  - `LEGIARTI000050595674`

These article IDs include rows that were present in historical `rag_chunks_dgafp` on Scalingo but missing from the latest Scaleway ingestion result.

Conclusion:

- the gap is not explained by the official source disappearing
- the DILA / Legifrance source is sufficient to rebuild a DGAFP-like table

### 2. The current Scaleway DGAFP table is behind the current bronze corpus

Observed counts:

- historical Scalingo `rag_chunks_dgafp`: `3992`
- current bronze DILA article payloads: `3992`
- current Scaleway `rag_chunks_dgafp`: `3860`

This means the latest Scaleway DGAFP table is not a source limitation problem. It is a pipeline state / projection problem.

### 3. Scalingo is a strict superset of the recreated Scaleway DGAFP table

Comparison on `chunk_id`:

- common `chunk_id`: `3860`
- only in Scaleway: `0`
- only in Scalingo: `132`

Conclusion:

- Scaleway did not invent incompatible rows
- Scalingo currently contains a larger DGAFP state than the latest recreated Scaleway table

### 4. The remaining differences are mostly transformation differences, not trivial formatting

On shared `chunk_id`, most differences are not caused by insignificant URL variants.

Meaningful differences were observed on:

- `chunk_text`
- `section_parent_cid`
- `title`
- `full_title`
- `subtitles`
- `status`

Tiny URL-only differences were negligible.

This indicates that the main gap is in the DGAFP compatibility projection, not in the availability of the source data.

## What This Means

We can target the following architecture:

```text
DILA LEGI bulk
-> bronze normalization
-> DGAFP-compatible projection
-> rag_chunks_dgafp
```

The current reverse-engineering work is therefore useful:

- it gave us the target schema
- it showed that DILA covers the expected article population
- it narrowed the missing work to transformation and validation

## Required Changes

### 1. Rebuild the medallion output from the current bronze corpus

Before tuning the DGAFP projection, we need a clean run whose silver/gold output is aligned with the current bronze snapshot.

Expected result:

- silver article documents should return to a population close to `3992`
- gold DGAFP-compatible chunks should return to a population close to the historical table

### 2. Make DGAFP compatibility an explicit projection target

Keep two separate outputs:

- `rag_chunks_dgafp`: compatibility-oriented projection
- `rag_chunks_legifrance`: modern canonical projection

The modern table can stay richer and more normalized. The DGAFP table should optimize for historical compatibility.

### 3. Tune the DGAFP-compatible builder

The current DGAFP projection in `packages/data-engineering/src/assistant_rh_data_engineering/legifrance/gold.py`
should be aligned with the historical table on these fields:

- `title`
- `full_title`
- `chunk_text`
- `subtitles`
- `section_parent_cid`
- `section_parent_titre`
- `status`

Expected direction:

- reduce over-enrichment in `chunk_text`
- keep titles closer to historical wording
- preserve stable legal hierarchy identifiers where historical DGAFP expects them

### 4. Keep the current technical upsert key

The current DGAFP upsert key is correct and should be preserved:

- table: `rag_chunks_dgafp`
- conflict key: `chunk_id`
- chunk identity: `<article_id>_<chunk_index>`

This is more robust than using the article number alone.

## Validation Protocol

Any DGAFP compatibility iteration should be validated against the historical Scalingo export/baseline, not by reintroducing Scalingo as an active runtime dependency.

### Comparison rules

Ignore insignificant differences:

- trailing slash in URLs
- repeated whitespace
- timestamps and ingestion metadata

Treat these as significant:

- `chunk_id`
- `cid`
- `number`
- `chunk_text`
- `title`
- `full_title`
- `subtitles`
- `section_parent_cid`
- `section_parent_titre`
- `status`

### Success criteria

For `rag_chunks_dgafp`, each iteration should report:

- total row count
- overlap on `chunk_id`
- rows only in generated output
- rows only in historical output
- field-level diffs on common `chunk_id`

The goal is not strict byte-for-byte equality on every non-business field. The goal is:

- same article coverage
- same chunk identity
- semantically equivalent chunk text
- close historical compatibility for retrieval and legal reference resolution

## Existing Repo Support

Useful scripts already present:

- `scripts/compare_legifrance_db_vs_pipeline.py`
- `scripts/compare_legifrance_targets.py`

These should be reused and, if needed, extended rather than replaced.

## Execution Plan

### Phase 1: Refresh the baseline

1. Run a full `dump -> medallion` on the current DILA bronze corpus.
2. Verify that silver/gold repopulate the full article population.
3. Recreate `rag_chunks_dgafp` from that fresh output.

### Phase 2: Align the DGAFP projection

1. Compare Scaleway DGAFP output with the historical Scalingo DGAFP export/baseline.
2. Identify the largest field deltas by volume.
3. Adjust `gold.py` compatibility projection.
4. Repeat until row count and chunk coverage are close to the historical baseline.

### Phase 3: Industrialize the automation

1. Keep DILA bulk as the official source of truth.
2. Run the pipeline on a schedule.
3. Add a comparison report as a post-run validation artifact.
4. Alert when compatibility drifts beyond an agreed threshold.

## Short-Term Priority

The next useful engineering step is:

1. regenerate silver/gold from the current bronze snapshot
2. compare the regenerated DGAFP output with the historical Scalingo export/baseline
3. then tune the DGAFP projection

Until this is done, we should not conclude that the official DILA source is insufficient.
