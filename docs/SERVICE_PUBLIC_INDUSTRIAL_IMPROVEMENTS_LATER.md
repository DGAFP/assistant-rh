# Service-Public Industrial Improvements Later

This document is intentionally for after the migration has been validated.

It must not drive the current migration decisions.


## Activation Condition

Start this phase only when:
- MVP compatibility is accepted
- client-facing behavior is stable
- the migration is considered complete


## Later Objectives

Possible post-migration improvements:
- exploit the richer XML structure more deeply
- improve section quality and semantic segmentation
- revisit chunk granularity for better retrieval
- add better metadata enrichment
- re-evaluate embedding strategy
- introduce incremental update logic with stronger manifests and hashing
- split generation, validation and DB load into more explicit jobs


## Examples Of Improvements To Revisit Later

### Better Structural Use Of XML

- more faithful handling of XML sections
- better distinction between titles, lists, tables and references
- cleaner reconstruction of situation-specific branches


### Better Chunking

- chunking optimized for retrieval quality rather than MVP parity
- revised chunk sizes and overlap strategy
- richer table handling
- stronger normalization rules for long answers


### Better Serving Architecture

- dedicated validation stage before DB load
- stronger object lifecycle rules
- more explicit versioning of chunking logic and embedding versions
- separate jobs for generation, compare and ingestion


### Better Reliability

- stronger incremental sync logic
- better run manifests and audit trail
- stricter retry and idempotency controls
- stronger observability on batch runs


## Important Rule

These ideas are improvements, not migration requirements.

If one of these improvements creates behavior drift, it must be treated as a product change, not as a migration detail.
