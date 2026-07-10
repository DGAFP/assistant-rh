# Python Retrieval Improvement Strategy

## Current Assessment

The main retrieval weakness is the fusion architecture, not simply the
embedding model.

- Production retrieves `top_k=10` independently from each table and heading
  channel, then applies unweighted cross-source RRF. Because documents from
  different tables rarely overlap, rank 1 from generic MATTE can effectively
  tie rank 1 from precise Service-Public. Raw relevance magnitude is discarded.
  See [`retriever.py`](../packages/rag-pipeline/src/assistant_rh_rag_pipeline/retriever.py#L322).
- Heading search gives MATTE and Service-Public a second retrieval channel,
  increasing their voting power relative to DGAFP and RGRH.
- Only the first 20 fused sections reach the cross-encoder, and production
  keeps five by default. Relevant material eliminated by fusion cannot be
  recovered. See
  [`section_aggregator.py`](../packages/rag-pipeline/src/assistant_rh_rag_pipeline/section_aggregator.py#L209).
- DGAFP is now always searched and forced to hybrid mode when configured.
  `needs_legal_search` is only diagnostic, despite older documentation
  describing conditional routing.
- Follow-up rewriting remains a recall bottleneck: an understood reference can
  still produce an ambiguous retrieval query.
- Source extraction quality is part of retrieval quality. The existing human
  review found missing Service-Public content and several cases where a correct
  SP section was retrieved but later discarded. See
  [`RAG_FINDINGS_HUMAN_REVIEW_2026-06-24.md`](RAG_FINDINGS_HUMAN_REVIEW_2026-06-24.md).
- The evaluation page is not reliable enough for choosing a production
  configuration:
  - Its default `top_k=50` differs from production's 10.
  - `extra_de_tables`, embedding column selection, and chunk-reranker controls
    are displayed but not actually wired.
  - Chunk reranking is explicitly unimplemented in the production pipeline.
  - Gold matching supports one exact document ID and does not measure
    section-level relevance or multiple valid sources.

## Recommended Experiments

### 1. Repair Evaluation Before Tuning

- Build a production-faithful offline runner using the actual query processor,
  retriever, aggregator, selector, and context builder.
- Record document recall at 5/10/20, section recall, MRR, selector retention,
  final-context precision, source distribution, and p50/p95 latency at every
  stage.
- Support multiple acceptable documents and section IDs per question.
- Create fixed regression slices for follow-ups, acronyms, exact legal
  references, numeric/table questions, and the known SP misses `F12163`,
  `F527`, `F34536`, and `F18029`.

### 2. Replace Round-Robin-Like Source Fusion

- Retrieve a wider candidate pool per channel, initially 30 and 50 rather than
  10.
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
- Replace the current broad OR query with weighted lexical fields: document
  title at weight A, section heading/path at B, body at D.
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

### 7. Improve Corpus and Chunk Quality

- Fix missing or malformed Service-Public extraction before interpreting
  retrieval failures.
- Preserve tables as coherent atomic chunks with their headings and units.
- Add small overlapping section chunks for recall, while retaining full
  sections as context units.
- Audit section-ID coverage because unresolved sections become standalone
  chunks and lose useful hierarchy.
- Add ingestion health checks for empty introductions, missing headings,
  embedding coverage, duplicate chunks, and orphaned section IDs.

## Suggested Order

1. Fix and freeze the evaluation harness and regression set.
2. Test wider candidate pools plus global section reranking.
3. Test hybrid candidate generation and weighted lexical fields.
4. Test contextual Service-Public embeddings.
5. Add original-plus-rewritten multi-query retrieval.
6. Tune source routing, diversity, and source priors.
7. Consider embedding fine-tuning or a custom reranker only after collecting
   hard negatives from these experiments.

The highest-probability near-term improvement is **30-50 candidates per source,
hybrid retrieval, deduplication to sections, then one global cross-encoder
rerank over 30-40 sections**. This directly addresses the current source-fusion
problem without requiring a new embedding model.

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
- Recommendations are based on repository inspection and the checked-in human
  review report, without querying staging.
- Source accuracy and anti-hallucination behavior remain mandatory; recall
  improvements must not be obtained by indiscriminately increasing final
  context size.
