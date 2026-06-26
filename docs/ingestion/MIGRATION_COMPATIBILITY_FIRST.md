# Migration Strategy: Compatibility First

## Goal

The current migration objective is not to improve the functional behavior of the Service-Public pipeline.

The objective is:
- keep the final user-facing behavior as close as possible to the MVP
- replace fragile internals with an industrial pipeline
- avoid unvalidated functional drift during migration


## What Can Change

Accepted changes during migration:
- source format: `PDF -> XML`
- storage architecture: local files -> medallion layout + Object Storage
- execution mode: manual notebooks -> batch pipeline
- packaging and deployment model

Not accepted without validation:
- strong changes in chunk counts
- strong changes in chunk boundaries
- changes in retrieval behavior visible to end users
- changes in answer quality caused by different chunk granularity


## Current Rule

During migration, the pipeline must optimize for:
- compatibility with the MVP output
- compatibility with current downstream tables and retrieval behavior

It must not optimize first for:
- richer XML structure
- more granular sections
- more exhaustive chunk coverage
- better internal purity of the architecture


## Service-Public Specific Rule

For Service-Public, the XML source is kept because it is the correct industrial source.

But the transformation logic must aim to mimic the previous MVP behavior:
- keep `FPE` filtering when relevant
- preserve notebook-style chunk roles:
  - `Q_ONLY`
  - `QA_COMPOSITE`
  - `A_ATOMIC`
  - `TABLE`
- preserve output schema expected by downstream ingestion
- reduce drift against the current DB content


## Acceptance Criteria

The migration can be considered acceptable only if:
- chunk schema remains compatible
- the generated content stays close enough to the current MVP output
- retrieval quality is not degraded for end users
- differences are measured and explained

Important:
- a structurally cleaner output is not automatically a better migration result
- if the client experiences behavior drift, the migration is not finished


## Immediate Technical Direction

Priority order:

1. keep XML as source of truth
2. keep notebook-style chunking logic
3. add a `legacy_compat` normalization layer before chunking
4. compare generated output against the current MVP/DB output
5. only after parity is acceptable, stabilize infra and operations


## What Legacy Compatibility Means Here

The `legacy_compat` objective is to make XML input behave more like the previous extracted PDF text before chunking.

Typical examples:
- flatten some XML headings
- reduce structure that creates too many synthetic Q/A blocks
- keep the effective `FPE` view only when that matches the old source
- preserve notebook-like text layout as much as possible before chunk splitting


## Working Rule For The Team

If there is a tradeoff between:
- cleaner industrial structure
- closer functional compatibility with the MVP

choose MVP compatibility first during the migration phase.


## Next Phase

After the migration is validated, the project can move to a second phase focused on industrial improvements.

That phase is documented separately here:
- [SERVICE_PUBLIC_INDUSTRIAL_IMPROVEMENTS_LATER.md](SERVICE_PUBLIC_INDUSTRIAL_IMPROVEMENTS_LATER.md)
