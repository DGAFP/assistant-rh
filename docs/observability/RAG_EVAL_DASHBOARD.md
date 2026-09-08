# Evaluation dashboard

`config/grafana/rag-eval-dashboard.json` is the portable Grafana dashboard
**Assistant RH - Évaluations RAG** (`ef5g8p2`). It uses the existing RAG Health
Prometheus datasource and defaults to staging. The UID was assigned through native
creation in managed Grafana; keep it when importing updates.

## Recorded results and interpretation

Choose an environment, goldset, run and retrieval attempt. The question filter
applies to the detail tables, while the funnel always summarizes the whole run.
Initial and selector retry attempts remain separate. The context KPIs use only
`context_builder_output` for the selected attempt. They are not a claim about the
final generator input if another attempt superseded it.

The historical global hit/recall metrics combine document IDs from multiple
pipeline stages, including the raw pool. A source can be found and then discarded
while still contributing to those global scores. These remain visible in a clearly
labelled diagnostic panel; they are not the final-context KPIs or a new quality gate.

The question/source table shows recorded excerpts. A compact matrix shows judge
verdict, gold presence at five stages and context recall, followed by a table of
recall at every stage and judge score. **Found** means at least one gold
source, not all sources. The stage recall is the stored historical metric and can
count identifier aliases. Expected sources are the stored human labels, not an
assertion that those labels were individually matched at every stage. No current
corpus lookup, alias reconstruction, evaluation replay or judge call is performed.

The funnel is recomputed from the stored per-question stage metrics, with separate
hit and recall denominators. Missing stages, invalid measurements and explicitly
zero gold counts do not become retrieval failures. Null, boolean, non-finite or
out-of-range ratios are excluded. A genuine measured zero remains zero.
Transitions count the same items with valid measurements at both endpoints:
retained, lost (1 to 0), or gained (0 to 1). **Top-12 is an alternative diagnostic
cut**, not an extra step between top-20 and the selector. Partial recall losses
remain visible in the question recall table.

Baseline verdicts and deltas remain those recorded by the automatic evaluator,
not an independent validation of experimental comparability. The experiment
journal can reclassify an automatic comparison as diagnostic (notably #240
against #226); that qualification takes precedence over its stored pass flag. Deltas are
exported only for an explicitly comparable stored result. A scope fingerprint,
shared judge or similar sample size alone does not prove comparability.

## Collection boundaries

The collector reads the last 50 `rag_quality_eval_runs` rows by primary key. The
window is global per environment; a goldset may have fewer runs or none. This is
not a complete archive. A second query uses the item `run_id` index and a lateral
lookup with `LIMIT 201` for each selected run. Runs above 200 items are marked
oversized and omitted entirely from detail and derived averages. No partial
sample is presented as a full run. Empty and partially recorded runs are explicit.

At most 10,000 items can be exported across the current run window. Numeric item
metrics use the stored item ID, avoiding collisions when question IDs repeat.
Only the bounded `item_info` series carries a question and expected-source excerpt
(each at most 500 characters; at most 20 expected-source entries). This is intended
for the curated evaluation goldsets, not live user conversations. Answers, gold
answers, contexts, prompts, judge explanations and arbitrary metadata are not
selected or exported. The query projects only stage diagnostics and scalar judge
fields. No new service, datasource, database account or migration is required.

Historical series follow Prometheus retention after they leave the export window.
Dashboard variables query current samples, not retained label values. The item and run metrics are scraped separately every 120 seconds at
`/metrics/evals`; health remains at 60 seconds at `/metrics/health`. The combined
`/metrics` endpoint stays available for compatibility. The two configured scrape
paths are disjoint, avoiding duplicate samples; 120 seconds preserves a margin
below the usual five-minute instant-query lookback. All panels
use instant queries: creation dates are evaluation dates, not scrape timestamps.
Only the history date column gets date formatting; identifiers stay textual.

## Deployment and verification

Deploy RAG Health, then import the JSON into the existing dashboard and choose its
Prometheus datasource. The optional run/item schemas publish availability flags;
an available empty run table publishes zero runs. The item availability panel
explains empty or oversized runs; absent metrics remain non-available.

Verify a known run's item count, judge score and stage numerators/denominators.
Run #240 (`baseline_v1`) has 98 recorded items: initial pool 79/96 hits, top-20
65/96, top-12 62/96, selector 51/94 and context 51/96. Its question 1 has gold in the
pool and top-20, then loses it at the selector and context despite global recall
being 1.0. Use this as a diagnostic regression check without replaying the RAG.
Check retry separation, empty data, untraced items and oversized-run behavior in
tests. Inspect the matrix and history in Grafana after import.

Metric and dimension definitions appear in panel help, table field descriptions,
and the expandable explanation blocks below the charts. Their reference is
[RAG_EVAL_METRICS.md](RAG_EVAL_METRICS.md). These are explanations only; queries,
scoring and collection cadence are unchanged.
