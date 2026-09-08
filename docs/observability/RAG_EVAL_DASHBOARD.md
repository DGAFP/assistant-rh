# Evaluation dashboard

`config/grafana/rag-eval-dashboard.json` is the portable Grafana dashboard
**Assistant RH - Évaluations RAG** (`assistant-rh-rag-evals`). It uses the existing
RAG Health Prometheus datasource; staging is selected by default because this is
where evaluation results are normally recorded.

The RAG Health collector reads the last 50 `rag_quality_eval_runs` rows by primary
key, with a single bounded SELECT on each polling cycle. It does not read item
questions, answers, contexts or prompts. No database migration is required.
Missing or partially migrated schemas publish `schema_available=0`; an available
but empty database publishes `runs_exposed=0`. The window is global per environment,
so filtering a goldset can return fewer runs or none. This is not a complete archive.

Choose an environment, goldset and run. The dashboard displays execution status,
sample size, duration, judge pass rate, retrieval metrics, judge dimensions,
optional RAGAS metrics and the retrieval funnel. Missing metrics remain absent;
zero is never substituted for a disabled judge. Completion is an execution state,
not a quality verdict. `limit=0` means no explicit limit, not proof of a full goldset.

Run metadata includes judge model, git revision, configuration fingerprint and a
hash of recorded evaluation scope and tag filters. The hash helps identify scope
changes; it does not establish baseline comparability or source snapshot identity.
Stage sample counts are exported separately because attempts can evaluate different
subsets. Baseline deltas are emitted only for a stored comparison with
`comparable=true` and a baseline run ID. Verdicts come from the runner; Grafana does
not recompute a comparison or infer a regression from two displayed averages.

All queries use current snapshots. Historical creation dates come from the run
records; a Prometheus scrape timestamp is not presented as an evaluation date.
Per-run metric labels are bounded by the 50-row export window (older series still
follow the datasource retention policy). No per-question series are emitted.

Deploy the updated RAG Health exporter, then import the JSON with the existing
Grafana import helper or UI and select its Prometheus datasource. Unlike OTLP
excerpts, these historical aggregates already exist in the database: a deployment
collects the recent existing runs without replaying an evaluation or calling a model.
Verify `schema_available`, `runs_exposed`, one known run's sample count and scores,
and an empty environment. Keep full question-level investigations in the existing
administrative evaluation tools.
