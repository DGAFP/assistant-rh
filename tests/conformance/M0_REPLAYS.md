# M0 deterministic parity replays

The M0b bundle freezes the legacy Python runtime immediately before the
hexagonal API extraction. It is a behavioral contract, not a quality-tuning
dataset. Its inputs are synthetic/public HR questions and contain no personal
data.

## Versioned reference

- Queries: `tests/conformance/queries.m0-api-parity.jsonl`
- Baseline: `tests/conformance/baselines/m0-api-parity-dev-9bf1cf0/`
- Source revision: `9bf1cf0cb92420e9e551f811edadb1d7129244b1`
- Recorder revision: `0bd3799b74eca9260e55ac89d6ca3c189b84f73b`
- Related live reference: M0a run `m0a_api_parity_dev_20260901_rerun1`
  (run #240; the failed partial attempt #238 is recorded in the experiment
  journal)
- Live snapshot: `docs/evals/evidence/m0a_api_parity_dev_20260901.json`
- Eval-run config fingerprint: `51d6256bace3d6c3c36b26ea0dee66b79ecc214f78e4b67dc6b76525e1bbf1ce`
- Normalized pipeline config fingerprint: `87ab8ddbb104703787c3d9dfe00ddff103350c3ef7691736f98d8ce6ebc74920`
- Replay fingerprint: `f5e9ffefe588248a352d7ac18a556df7bff6270a2879e7ddd32acc128733e02b`

The bundle contains `00_input.json`, one exact JSON input/output contract for
each pipeline stage (`01` through `06`), and `07_pipeline_result.json` for each
fixture. Runtime-only timing, turn IDs, trace IDs, and timestamped trace events
are deliberately excluded from the structured result. Configuration, active
prompt hashes, models/providers, artifact hashes, observed branch coverage, and
the overall replay fingerprint are stored in `manifest.json`.

## Record on the frozen live runtime

This command requires the staging DB and provider credentials. The recorder may
live in a later tooling-only commit, but it verifies that the versioned RAG
runtime paths are unchanged from the declared source revision. It also fails
when an observed branch differs from the fixture's declared expectation. Record
into a fresh temporary directory so the committed reference is never
overwritten implicitly. Run it from the recorder revision pinned above, not
from a later `dev` checkout whose runtime may have changed.

```bash
M0B_OUTPUT_DIR="$(mktemp -d)"
uv run python scripts/dump_stage_baselines.py \
  --queries-file tests/conformance/queries.m0-api-parity.jsonl \
  --output-dir "$M0B_OUTPUT_DIR" \
  --runtime-git-sha 9bf1cf0cb92420e9e551f811edadb1d7129244b1 \
  --reference-run-id 240 \
  --source-environment scaleway-staging

uv run python scripts/verify_stage_baselines.py \
  --baseline-dir tests/conformance/baselines/m0-api-parity-dev-9bf1cf0 \
  --actual-dir "$M0B_OUTPUT_DIR"
```

Refreshing the reference is an explicit milestone decision. Do not overwrite
it to make a later implementation pass.

## Verify or compare offline

Self-check the committed bundle with no DB, model, or network dependency:

```bash
uv run python scripts/verify_stage_baselines.py \
  --baseline-dir tests/conformance/baselines/m0-api-parity-dev-9bf1cf0
```

After a candidate runtime exports the same file layout, compare every stage and
the structured result using exact JSON equality:

```bash
uv run python scripts/verify_stage_baselines.py \
  --baseline-dir tests/conformance/baselines/m0-api-parity-dev-9bf1cf0 \
  --actual-dir /path/to/candidate-export
```

The candidate conformance runner must replay the recorded provider/search
outputs at its ports rather than call live models or live retrieval. Exact
equality is the extraction contract; live quality remains the separate M0a
gate.

The verifier also checks JSON Schemas, artifact SHA-256 hashes, bundle
fingerprint integrity, fixture completeness, the candidate's exact artifact
inventory and JSON value types, declared branch coverage, safe relative paths,
and the input personal-data guard.

## Branch matrix

| Fixture | Contract branch |
|---|---|
| `rag-acronym-contract` | normal RAG path, acronym expansion, MATTE request scope |
| `rag-legal-dgafp` | legal-search gate and DGAFP retrieval |
| `rag-conversation-followup` | conversation-history/follow-up processing |
| `rag-ministry-mso` | request-scoped MSO + shared-source retrieval |
| `short-circuit-chit-chat` | direct-response chit-chat short circuit |
| `short-circuit-document-request` | document-request refusal short circuit |
| `short-circuit-out-of-scope` | out-of-scope refusal short circuit |

The manifest is authoritative for observed values. A selector rejection/retry
fixture may be added only when the frozen runtime produces that branch reliably
without changing pipeline settings.

## C2 query processor extraction (#459)

Run the API stage comparison and the differential/error/concurrency checks:

```bash
uv sync --all-packages --group dev --frozen
uv run --no-sync python -m pytest \
  apps/api/tests/core/test_query_processor_m0b.py \
  apps/api/tests/core/test_query_processor.py -q
```

All seven original `01_query_processor.json` files remain unchanged. The test
compares the complete input/output JSON with exact value types, the recorded
direct answers, and the available confidence/acronym metadata.

**Evidence limit:** the original recorder saved stage outputs, not raw intent
model replies, DB prompt content or the acronym dictionary. The separate
`apps/api/tests/fixtures/query_processor_m0b_ports.json` explicitly reconstructs
observable LLM response fields from `01_query_processor.json` and
`07_pipeline_result.json`. It is synthetic port input, not a recording of the
original provider response. Fields absent from M0b (raw text/reasoning and
short-circuit confidence) are not certified by that comparison. Empty acronym
snapshots reproduce the observed empty expansion metadata; they do not establish
the historical dictionary's content. Prompt generation, acronym behavior,
parsing, errors, fallbacks and ordering are separately compared to the retained
legacy implementation under identical injected inputs. The packaged fallback
prompt is byte-identical to the current legacy resource.

The authorized #545 hardening preserves expected-failure fallback values but
replaces exception text with a safe cause and explicit degraded diagnostics.
Configuration errors, bugs and cancellation propagate; their tests assert this
intentional deviation rather than full historical equality. See the C2 review
entry in `docs/architecture/hexagonal-split/LEDGER.md`.

This C2 evidence covers the extracted stage, not C6 engine wiring, whole-pipeline
M1 parity or live quality. No baseline refresh or live provider call is needed.

## C3 retrieval extraction: missing port inputs

The C3 audit (#460, 2026-09-10) found that the current bundle records the final
retrieval projections, **not** the embeddings or raw vector/lexical/heading
lanes needed to execute the candidate at `SearchPort`. Its integrity check is
therefore not a candidate parity comparison (`exact_comparison` remains null).
Reconstructing raw lanes from expected final outputs would be a circular test.

The supplemental C3 tests execute the historical retriever and the API core on
the same guarded synthetic PostgreSQL corpus, comparing all chunk fields,
full-precision scores and order across three modes and four ministry scopes.
These differential tests do **not** fulfill the M0b replay gate.

To close that gate, an explicitly versioned companion recording must capture the
embedding outcome, every raw lane including ranks and empty/failed lanes, source
catalogue, effective request configuration and the historical output together.
It must identify the runtime/corpus revision and be replayed without live I/O.
An existing recording of those inputs would also suffice. A new live query on
today's database cannot retroactively recover the frozen M0b search inputs.
Keep the original bundle untouched; #460 remains open pending this proof.

## C4 aggregation and context extraction: differential evidence (#461)

Run the extracted core against the retained runtime with the same synthetic
content and reranker inputs:

```bash
PYTHON_DOTENV_DISABLED=1 uv run --no-sync python -m pytest \
  apps/api/tests/core/test_context_steps.py apps/api/tests/core/test_imports.py -q
```

The comparisons cover exact section/chunk fields, scores, trace projections,
context metadata, resolved references and final prompt bytes. Cases include R2
pairs, stable ties, rerank input/output caps and fallback, missing sections and
documents, full documents, standard/wide modes, exhausted budgets, triangulation,
legal-reference representations, safe error diagnostics and request isolation.

**This is not historical M0b parity.** The frozen aggregation input contains
retrieved IDs, not complete chunks and section/document responses. Its outputs
are projections, not the raw reranker response. The context stage records item
references and a rendered prompt, but lacks the full selected sections and the
content/reference-store responses needed to execute the candidate independently.
Do not reconstruct port inputs from these expected outputs.

Closing the C4 gate requires a versioned companion recording of complete ordered
retrieved chunks (including metadata and unrounded scores), effective configs,
section/document reads (including missing/error outcomes), reranker request and
raw outcome, selector output, reference-store rows in observed order, and legacy
outputs captured together. Record runtime/corpus revisions and artifact hashes.
Replay the same inputs through the retained and candidate implementations offline.
The original 7-fixture/56-artifact bundle stays unchanged; its integrity self-check
is separate evidence and does not satisfy the candidate comparison.

C4 corrects B2 normalization of nullable source fields (title, URL, publisher,
token count, section document ID and heading path): raw NULL values now cross the
port intact. `apps/api/tests/db/test_context_conformance.py` compares real SQL
reads on the guarded synthetic database against the legacy runtime, including
nullable metadata and orphan sections. B2 still orders ambiguous reference
matches; legacy SQL does not order colliding numbers, so these historical
collisions cannot be certified without a recorded row order. Unexpected bugs/configuration errors and cancellation propagate in
C4; expected typed failures retain the legacy fallback values with safe codes.

### C4 companion accepted on 2026-09-14

The [versioned companion](companions/c4-staging-20260914/README.md) now supplies
complete C4 inputs captured from the retained staging-branch runtime against
read-only staging data. Offline replay matches the extracted candidate exactly
on all four RAG scenarios: sections, scores, order, metadata and formatted context.
Three short-circuit scenarios do not reach C4 and are marked `not_exercised`.

Together with synthetic differential coverage, this companion satisfies the C4
acceptance evidence for #461. It is a new recording, not reconstruction of the
original historical calls. The original seven-fixture/56-artifact bundle remains
unchanged. Reference resolution, triangulation and ambiguous reference-row order
were not exercised by this capture; synthetic coverage and the documented
collision limitation remain explicit. This does not close C2/C3/C5/C6 or M1 gates.

The archive replays a pinned candidate; future changes to C4 require rerunning
the comparison with the changed candidate. The publishing commit changes only
documentation and evidence, preserving the validated runtime source.

## Full-engine companion recorded on 2026-09-17 (#560)

The [M0b full-engine companion](companions/m0b-full-20260917/README.md) captures
fresh retained-runtime outputs and raw search/content/provider inputs together
on one read-only staging snapshot. The current production bootstrap, ChatService
and pipeline replay all seven scenarios offline: 27 executed stage outputs and
seven business results match exactly. Five negative controls detect changes to
inputs, expected outputs and port-call inventory. The private CI job replays the
unchanged recording against trusted dev/staging/main revisions, including
optimized Python validation. The full-corpus archive is held in the private
Hugging Face dataset; public PR jobs use only synthetic data.

This fulfills the C6 assembly evidence through the explicitly versioned
companion route. It does not recover the missing September 1 calls or refresh
the historical reference. The original 7/56 integrity check remains separate
with `exact_comparison: null`; the companion comparison is true. The known B2
resolution difference for ambiguous sections is outside this engine-at-ports
comparison. Live goldset quality, M1 and SSE/C7 remain separate gates.

The recording also exposed and reproduced a semantic SQL plan regression with
IVFFlat. Candidate numbering now occurs after the retained selection query;
the hybrid lane ranking is unchanged. A guarded synthetic PostgreSQL test
fails with the old query and passes with the correction.
