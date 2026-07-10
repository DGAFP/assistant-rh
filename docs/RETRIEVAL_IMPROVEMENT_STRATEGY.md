# Python Retrieval Improvement Strategy

State verified on 2026-07-10 against `dev@eb0de6c`, the shared `rag_config`
runtime row (staging and dev, aligned 2026-07-06, `align-candidate_v2-rerank20`),
and the traced replay of the `suivi-tests-20260708` staging campaign. Numeric
values below (top-k, rerank depth) drift with tuning; re-check them against
`RetrievalConfig` / `rag_config` before acting on this document.

## Current Assessment

The fusion architecture is a structural retrieval weakness, but the measured
end-to-end losses concentrate after retrieval (selector) and before it
(extraction/chunking). Both views are detailed below; the embedding model is
not the primary suspect in either.

- Production retrieves `v3_initial_top_k=30` independently from each table and
  heading channel, then applies unweighted cross-source RRF. Because documents
  from different tables rarely overlap, rank 1 from generic MATTE can
  effectively tie rank 1 from precise Service-Public. Raw relevance magnitude
  is discarded. See `_merge_cross_source_ranks` in
  [`retriever.py`](../packages/rag-pipeline/src/assistant_rh_rag_pipeline/retriever.py).
  Caution: the runtime `rag_config` row still carries the legacy v1/v2 keys
  `top_k=10` and `rerank_top_k=5` next to the effective v3 keys
  (`v3_initial_top_k=30`, `v3_rerank_top_k=20`); only the `v3_*` keys are read
  by the v3 pipeline (`runtime_config_to_rag_config` in `admin.py`).
- Heading search gives section-backed tables (ministry table and
  Service-Public) a second retrieval channel, increasing their voting power
  relative to DGAFP and RGRH.
- Only the first 20 fused sections reach the cross-encoder
  (`_MAX_RERANK_INPUT` in
  [`section_aggregator.py`](../packages/rag-pipeline/src/assistant_rh_rag_pipeline/section_aggregator.py)),
  and production keeps `v3_rerank_top_k=20` — so the reranker no longer
  truncates, but relevant material eliminated by fusion before the 20-section
  input cap cannot be recovered.
- Measured loss distribution (traced replay of the 2026-07-08 staging
  campaign, 74 runs, 34 with expected documents): 21 hits, 9 lost at the
  selector although the correct source was in its 20-item input, 3 lost at the
  section rerank, 1 never retrieved. The single biggest observed loss stage is
  the LLM selector (anti-redundancy discarding the ministry doc when
  Service-Public says the same thing — issue #299), and the root cause of most
  rerank/retrieval losses is broken extraction/chunking of five key documents
  (emptied table cells, wrong headings, TOC-only chunks — issue #302), not
  ranking itself.
- DGAFP is now always searched and forced to hybrid mode when configured.
  `needs_legal_search` is only diagnostic, despite older documentation
  describing conditional routing.
- Follow-up rewriting remains a recall bottleneck: an understood reference can
  still produce an ambiguous retrieval query.
- Source extraction quality is part of retrieval quality. The existing human
  review found missing Service-Public content and several cases where a correct
  SP section was retrieved but later discarded. See
  [`RAG_FINDINGS_HUMAN_REVIEW_2026-06-24.md`](quality/RAG_FINDINGS_HUMAN_REVIEW_2026-06-24.md).
- The evaluation page is not reliable enough for choosing a production
  configuration:
  - Its default `top_k=50` differs from production's 30.
  - `extra_de_tables` and embedding column selection are displayed but not
    actually wired (all Albert column variants map to the same
    `EmbeddingModel.ALBERT`; extra tables never reach the retrieval call).
  - The chunk-reranker toggle is wired into the config but chunk reranking is
    explicitly unimplemented in the production pipeline
    (`"not_implemented"` in `pipeline.py`), so the toggle has no effect.
  - Gold matching accepts multiple valid document IDs (list, JSON or
    comma-separated) but only matches exact `doc_short_id`s and does not
    measure section-level relevance.

## Recommended Experiments

### 1. Repair Evaluation Before Tuning

- Build a production-faithful offline runner using the actual query processor,
  retriever, aggregator, selector, and context builder. Starting point: the
  campaign replay harness from issue #298 (PR #304,
  `scripts/suivi_tests_replay.py` + `src/suivi_tests/`) already replays a
  Grist question set through the real pipeline and produces a per-stage
  diagnosis (retrieved / lost-at-rerank / discarded-by-selector) offline.
- Record document recall at 5/10/20, section recall, MRR, selector retention,
  final-context precision, source distribution, and p50/p95 latency at every
  stage.
- Support multiple acceptable documents and section IDs per question (the
  replay harness's `expected_docs` format already supports AND lists and
  `A|B` alternatives at document level; section IDs remain to be added).
- Create fixed regression slices for follow-ups, acronyms, exact legal
  references, numeric/table questions, and the known SP misses `F12163`,
  `F527`, `F34536`, and `F18029`.

### 2. Replace Round-Robin-Like Source Fusion

- Retrieve a wider candidate pool per channel: 50 and 100 versus the current
  30. Note: the admin UI validation caps `v3_initial_top_k` at 30
  (`admin.py`); raise that bound as part of the experiment.
- Deduplicate by chunk/section, aggregate to sections, then globally
  cross-encoder-rerank 30-40 sections.
- Keep RRF only for combining semantic and lexical rankings within a source.
  Do not use unweighted RRF as the final ranking across disjoint source tables.
- Compare global reranking against empirical per-source score calibration and
  weighted fusion. Avoid permanent source boosts until evaluation demonstrates
  a consistent benefit.

### 3. Make Hybrid Retrieval the Primary Candidate Generator

- Test semantic versus hybrid with semantic weights `0.25`, `0.5`, and `0.75`.
- Separate branch depth from final result count: retrieve 50 semantic and 50
  lexical candidates, fuse them, then keep 30 per source.
- Replace the current broad OR query with weighted lexical fields using
  Postgres `setweight` classes: document title at `A`, section heading/path at
  `B`, body at `D`.
- Preserve exact identifiers, article numbers, acronyms, amounts, and dates as
  high-value lexical terms.

### 4. Use Contextual Embeddings

- Embed `document title + heading path + chunk text`, while continuing to
  display only the original chunk text.
- Compare raw and contextual embeddings on the same chunks and embedding model.
- This should especially help short procedural chunks whose subject exists only
  in their heading.
- Reindex one source first, preferably Service-Public, before committing to a
  full-corpus backfill.

### 5. Improve Query Construction

- Require follow-up reformulation to resolve pronouns and elliptical phrases
  using conversation history.
- Retrieve with both the normalized original query and the standalone rewrite,
  then fuse their candidate lists.
- Keep acronym expansion, but preserve both acronym and expanded form.
- Add conservative exact-term extraction for legal citations, document
  numbers, monetary amounts, dates, and named schemes such as SFT.

### 6. Introduce Source-Aware Routing and Precision Controls

- Use theme and query signals to decide candidate budgets, not hard exclusion
  initially.
- Give precise Service-Public fiches preference over generic MATTE sections only
  when the global reranker scores them comparably.
- Cap repeated sections from the same document after reranking, then use MMR or
  a per-document cap to improve context diversity.
- Evaluate conditional DGAFP budgets for legal and non-legal queries; the
  current always-on behavior may add noise and latency.
- Treat the LLM selector as part of the precision chain: it is the largest
  measured loss stage (9 of 13 diagnosed misses in the 2026-07-08 campaign),
  discarding correct ministry sources present in its input as "redundant"
  with Service-Public. Retrieval-side gains are invisible end-to-end until the
  selector cascade is fixed (issue #299); measure selector retention alongside
  every retrieval experiment.

### 7. Improve Corpus and Chunk Quality

- Fix missing or malformed extraction before interpreting retrieval failures.
  Five broken ministry documents are already identified and traced to
  concrete failure modes (emptied table cells, duplicated/truncated headings,
  TOC-only chunks) — see issue #302; on Service-Public, `F32513` lost its key
  SFT intro paragraph at parsing.
- Preserve tables as coherent atomic chunks with their headings and units.
- Add small overlapping section chunks for recall, while retaining full
  sections as context units.
- Audit section-ID coverage because unresolved sections become standalone
  chunks and lose useful hierarchy.
- Add ingestion health checks for empty introductions, missing headings,
  embedding coverage, duplicate chunks, and orphaned section IDs.

## Suggested Order

1. Fix and freeze the evaluation harness and regression set (extend the #298
   replay harness).
2. Re-extract the five broken documents (#302) and fix the selector cascade
   (#299) — both are measured root causes and cheap relative to fusion work;
   without them, retrieval experiments are evaluated against a corpus and a
   downstream stage that mask their effect.
3. Test wider candidate pools plus global section reranking.
4. Test hybrid candidate generation and weighted lexical fields.
5. Test contextual Service-Public embeddings.
6. Add original-plus-rewritten multi-query retrieval.
7. Tune source routing, diversity, and source priors.
8. Consider embedding fine-tuning or a custom reranker only after collecting
   hard negatives from these experiments.

The highest-probability near-term improvement for the retrieval stage itself
is **50-100 candidates per source, hybrid retrieval, deduplication to
sections, then one global cross-encoder rerank over 30-40 sections**. This
directly addresses the source-fusion problem without requiring a new embedding
model. End to end, however, the campaign evidence says extraction (#302) and
selector (#299) fixes pay out first.

## Interfaces and Tests

- Split retrieval configuration into `candidate_top_k`, `fused_top_k`, and
  `rerank_top_k`; the current single `initial_top_k` conflates distinct stages.
- Add configurable semantic/lexical branch depths and source budgets.
- Extend retrieval traces with channel rank, raw score, fused score, rerank
  score, query variant, and rejection reason.
- Use the following acceptance criteria:
  - Improve document Recall@10 and section Recall@10 on the frozen set.
  - Reduce cases where a gold section exists before the selector but disappears
    afterward.
  - Introduce no regression on legal-reference and numeric/table slices.
  - Keep p95 retrieval plus reranking latency within an agreed production
    budget.
  - Preserve deterministic ordering for tied scores.

## Assumptions

- This analysis covers only the production Python pipeline; Mastra is excluded.
- Recommendations are based on repository inspection (`dev@eb0de6c`), the
  checked-in human review report, a read-only inspection of the shared
  `rag_config` runtime row, and the traced replay of the 2026-07-08 staging
  campaign (`chat_runs` / `rag_trace_events`, read-only).
- Source accuracy and anti-hallucination behavior remain mandatory; recall
  improvements must not be obtained by indiscriminately increasing final
  context size.
