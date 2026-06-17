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

---

## Issue #102 — DGAFP/Légifrance ingestion audit (addendum, 2026-06)

This addendum documents the findings and corrective tooling from the
ingestion audit called out in issue #102, after the Scaleway staging
DB showed `rag_chunks_dgafp` with chunks but no embedding coverage
(`embedding_m3 IS NULL` on every row, 0/3992).

### Acquisition paths

The DGAFP/Légifrance acquisition is split in **two independent paths**
that should not be conflated:

```text
Automatic path (Scaleway Serverless Jobs)
  legifrance-bulk-dump    -> legifrance/bronze/raw/legi_bulk
  legifrance-medallion    -> silver/gold (NO embeddings, --no-embed)
  legifrance-ingestion    -> rag_chunks_dgafp + rag_chunks_legifrance
  embeddings-legifrance   -> rag_chunks_dgafp.embedding_* + rag_chunks_legifrance.embedding_*

Manual path (curated article numbers / CIDs)
  config/legifrance_articles.json       -> article_numbers (R.331-7, ...)
  config/legifrance_article_cids.json   -> article_cids (LEGIARTI..., generated by reference export)
  config/legifrance_legacy_texts.json  -> legacy_texts_path (txt annexes for gold)
  ingestion --reference-csv / --load-all-artifacts pulls from those configs
```

Both paths share the same bronze/silver/gold artifacts and the same
`rag_chunks_dgafp` / `rag_chunks_legifrance` targets. The Scaleway job
`legifrance-medallion` is intentionally invoked with `--no-embed`; the
embeddings are produced separately by `embeddings-legifrance` so that
the GPU/embedding step can be scheduled, cost-bounded, and re-run
independently from the medallion refresh.

### Idempotence des embeddings lors d'un rerun `--no-embed`

Risque identifié : `ServicePublicDbWriter._upsert` exécutait
`DO UPDATE SET <col> = EXCLUDED.<col>` sur **toutes** les colonnes non
conflit, y compris `embedding_m3` et `embedding_bge_scw`. Conséquence :
un rerun de l'ingestion Légifrance en mode `--no-embed` (qui produit
des chunks avec `embedding_* = NULL`) écrasait à `NULL` les vecteurs
déjà persistés.

Correction (issue #102) : un nouveau paramètre
`preserve_on_null_cols` permet d'émettre un `COALESCE(EXCLUDED.col,
<table>.col)` sur les colonnes d'embedding, afin qu'une valeur entrante
`NULL` n'écrase pas un vecteur existant.

- `LegifranceDbWriter.upsert_legacy_chunks` protège `embedding_m3`,
  `embedding_bge_scw`, `embedding_qwen3` (filtrés par introspection
  via `information_schema.columns`).
- `LegifranceDbWriter.upsert_modern_chunks` protège `embedding_m3` et
  `embedding_bge_scw` (la table moderne n'expose pas qwen3).

Cette correction est **transparente** pour les autres colonnes et
n'affecte pas `rag_chunks_service_public` (qui n'a pas de colonne
d'embedding dans le manifest courant).

### Mode audit read-only pour la couverture embeddings

`embeddings_backfill.py` accepte désormais les options :

- `--check-only` (alias `--dry-run`) : ne charge aucun modèle
  d'embedding, n'appelle aucune API Scaleway, et n'écrit rien en base.
  Imprime, par `(table, embedding_column)` du manifest :
  `total`, `non_null`, `missing_with_text`, `empty_text`,
  `coverage_pct`. Code retour `0` si la couverture est au seuil,
  `1` sinon.
- `--coverage-min-pct <float>` : seuil de couverture (0-100). Avec
  `--check-only`, un code retour `1` est émis dès qu'une colonne est
  sous le seuil. Sans `--check-only`, le seuil reste informatif
  (réservé aux évolutions futures).

Cela permet un check CI / Cockpit / Scaleway workflow_dispatch sans
risque de modification accidentelle de la base.

### Fail-fast sur extraction incomplète Légifrance

`legifrance_bulk_dump` accepte désormais :

- `--strict-articles` (implicite avec `--article-ids-json`) :
  `SystemExit` non-zéro si le manifest demande N articles LEGIARTI et
  que l'extraction (snapshot + deltas) en trouve strictement moins.
  Le payload JSON `status=error reason=incomplete_article_extraction`
  liste `requested_count`, `extracted_xml_count`, `missing_count` et
  `missing_ids_sample` pour faciliter le diagnostic.
- `--allow-partial` : opt-out pour tolérer un manifest connu
  partiellement absent (migration depuis un export de référence
  Scalingo où certains LEGIARTI n'existent plus dans le snapshot).
- Sans `--article-ids-json`, le comportement legacy reste inchangé
  (mode `extract_full_snapshot`).

### Backfill ciblé Légifrance

Le job `data-ingestion embeddings legifrance` est, par construction,
ciblé sur `config/legifrance_embedding_tables.json` qui ne référence
que `rag_chunks_dgafp` et `rag_chunks_legifrance`. La sélection peut
être resserrée via `--only-table rag_chunks_dgafp
--only-column embedding_m3`.

Exemple de commandes **read-only** d'audit (aucune écriture) :

```bash
# Couverture globale Légifrance
uv run data-ingestion embeddings legifrance \
  --check-only \
  --coverage-min-pct 95

# Couverture ciblée DGAFP / m3 uniquement
uv run data-ingestion embeddings legifrance \
  --check-only \
  --only-table rag_chunks_dgafp \
  --only-column embedding_m3 \
  --coverage-min-pct 100
```

Exemple de commande de **backfill réel** (à valider avec Paul avant
exécution sur staging) :

```bash
uv run data-ingestion embeddings legifrance \
  --dsn-env SCW_POSTGRES_DSN \
  --only-table rag_chunks_dgafp \
  --only-column embedding_m3
```

### Équivalence avec la sélection Scaleway

Le script `.github/scripts/scaleway_data_jobs.py` filtre déjà
correctement les jobs d'embeddings via `embedding_source` et la
clé `embeddings-legifrance`. Aucune modification n'a été nécessaire
de ce côté. Le job correspondant dans `data-engineering-jobs.json`
reste la source canonique :

```json
{
  "key": "embeddings-legifrance",
  "domain": "embeddings",
  "image": "embeddings-job",
  "args": ["embeddings", "legifrance", "--dsn-env", "SCW_POSTGRES_DSN"]
}
```

### Validation protocol updates

- Avant tout rerun de `legifrance-medallion --no-embed` sur staging,
  capturer une couverture d'embedding de référence via
  `--check-only` et la committer dans `docs/LEGIFRANCE_DGAFP_AUTOMATION_PLAN.md`
  (ou dans un artefact de comparaison daté).
- Après rerun, re-mesurer la couverture : un delta strictement
  positif (et jamais régressif) valide l'idempotence.
- Avant tout `legifrance bulk-dump` avec `--article-ids-json`, le
  pipeline CI doit échouer sur extraction incomplète.
