# Findings - Human Review RAG Runs 2026-06-24

## Scope

This document summarizes the findings from the 34 human-reviewed chatbot runs dated 2026-06-24.

Input reviewed:

- 34 rows from the pasted TSV export.
- Matched 34/34 rows against staging `chat_runs` for 2026-06-24.
- Read-only staging inspection of `chat_runs`, `rag_documents`, `rag_sections`, and `rag_chunks_service_public`.

No staging data was modified during the analysis.

## Executive Summary

The dataset is mostly a Service-Public and MATTE source-quality test set, not a Legifrance/DGAFP test set.

- 13 rows are marked `OK`.
- 21 rows have a `KO` or partial `KO`.
- All 34 matched staging runs have `v3_needs_legal_final = false`.
- No reviewed run actually routed to DGAFP/Legifrance retrieval.

The most obvious actionable problems are:

1. Service-Public `F32513` was recently reingested, but its key SFT intro paragraph is missing from staging markdown/chunks.
2. Several good Service-Public sections are retrieved before the selector, then dropped from final context.
3. MATTE generic sources sometimes pollute the final context despite more precise Service-Public sections being available.
4. Follow-up questions are not being rewritten into retrieval queries even when the intent classifier understands the reference.
5. The reviewed lot does not validate the Legifrance issue because legal retrieval is never activated.

## Verified Staging Evidence

### F32513 Reingestion Was Recent

Staging has a fresh `F32513` document:

- `short_id`: `F32513`
- title: `Supplément familial de traitement (SFT) dans la fonction publique`
- `last_updated_date`: `2026-05-22`
- `created_at`: `2026-06-20 10:55:39+00`
- chunk rows: 97 rows in `rag_chunks_service_public`

The live Service-Public page includes the clear SFT definition:

> Le SFT est un complément de rémunération versé à tout agent public ... qui a au moins 1 enfant de moins de 20 ans à charge.

However staging `doc_markdown` and `rag_chunks_service_public` do not contain:

- `moins de 20`
- `au moins 1 enfant`
- `complément de rémunération`

The staging markdown starts with a weaker intro:

> Vous êtes fonctionnaire ou contractuel et vous avez un ou plusieurs enfants à charge? Vous avez droit au supplément familial de traitement (SFT) sous certaines conditions.

Then it jumps to `## 2 parents agents publics`.

### Probable Parser Cause

The Service-Public XML parser currently extracts a single introduction text node:

- [xml_parser.py](../packages/data-engineering/src/assistant_rh_data_engineering/service_public/xml_parser.py)
- relevant code: `root.find('.//Introduction/Texte')`

If the Service-Public XML contains multiple `Introduction/Texte` nodes, only the first one is kept. This explains why a fresh reingestion can still miss the second introductory paragraph.

## Findings by Source Issue

### Source SP

These are directly tied to Service-Public source recall, parsing, or selector behavior.

| Case(s) | Topic | Expected Source | Observed Problem | Likely Cause |
|---|---|---|---|---|
| 1, 3, 17, 18 | SFT eligibility and age limit | `F32513` | The answer misses the under-20 child condition. | Parser/content loss in Service-Public intro. |
| 6 | Transport subscription reimbursement procedure | `F12163` | Final answer says no relevant information. | Retrieval focuses on "demarche" and pulls change-of-residence sections; selector rejects all. |
| 10 | Part-time worker transport reimbursement | `F12163` | Correct transport section is present pre-selector, but final source is part-time work. | Selector chose a less direct section and dropped the transport source. |
| 22 | Ordinary sick leave rights | `F491` | Final context is a MATTE annual-leave section, not the SP sick-leave fiche. | Retrieval/selector misses the precise SP source. |
| 26 | Reimbursement for travel/meals to take exams | `F527` | Final answer says no relevant information. | F527 exists in staging but is not retrieved for this phrasing. |
| 28 | Pregnancy/AMP authorizations | `F34536` | Dedicated SP fiche exists in staging but does not appear in final context. | Retrieval recall miss. |
| 30 | Contractual agent part-time conditions | `F18029` | The SP part-time fiche is present before selector but final context is MATTE-only. | Selector over-selects MATTE and drops a more precise SP source. |
| 31 | Therapeutic part-time | `F12391` | Main SP source is present, but irrelevant MATTE source is also kept. | MATTE pollution in selector output. |

### Source MATTE

These are tied to generic or irrelevant MATTE sections entering final context.

| Case(s) | Observed MATTE Issue | Notes |
|---|---|---|
| 11 | MATTE deontology/contractual document used for a question about civil-servant rights and duties. | The question is about `fonctionnaires`; the selected MATTE source is about contractuels. |
| 22 | MATTE annual-leave/time-working section used for ordinary sick leave. | The reviewer notes the source does not cover sick leave. |
| 30 | MATTE `temps partiel` and `quel contrat choisir` retained while the dedicated SP fiche is dropped. | The answer may be partly usable, but source selection is weak. |
| 31 | MATTE `quel contrat choisir` retained alongside the correct SP therapeutic part-time source. | Reviewer flagged the MATTE source as irrelevant. |

The selector prompt already says MATTE should only be used as a tie-breaker when relevance is equivalent. The observed runs show that this instruction is not strong enough in practice.

### Source Legifrance / DGAFP

The reviewed lot does not meaningfully exercise this issue.

Evidence:

- 34/34 matched rows have `v3_needs_legal_final = false`.
- DGAFP/Legifrance sources are therefore not requested by the pipeline in these runs.
- The orientation-sexuality/discrimination case could be a legal-routing candidate, but it was not routed as legal.

The local pipeline code also excludes DGAFP when `needs_legal_search` is false:

- [pipeline.py](../packages/rag-pipeline/src/assistant_rh_rag_pipeline/pipeline.py)
- observed logic: `active_tables = [t for t in configured_tables if t != "dgafp"]`

So this test set should not be used as proof that the Legifrance/DGAFP source issue is fixed or broken.

## Findings Not Primarily Related to Source Issues

### Persona / Answer Framing

Cases 4, 9, and 23 are mostly form issues.

The content is often correct, but the answer addresses the agent directly instead of a gestionnaire RH. This should be handled at prompt/product-policy level rather than as retrieval.

### Contractuel vs Agent Public vs Fonctionnaire Scope

Cases 11, 12, 20, and 21 show scope ambiguity.

Examples:

- A question about `fonctionnaire` receives a contractuel answer.
- A question about `agent public` receives a broad answer including fonctionnaires and contractuels, while the product may need to focus on contractuels FPE.

This requires a product rule:

- either answer broadly when the user says `agent public`,
- or default to contractuels FPE and explicitly state that narrower scope.

### Follow-Up Rewriting

Case 2 is a follow-up failure.

The classifier reasoning correctly understood that `le complément` referred to the previous SFT question, but `query_for_retrieval` remained:

```text
quelles sont les conditions pour obtenir le complément ?
```

That query retrieved the Service-Public prevoyance/complementary-protection fiche instead of SFT.

The likely fix is not in retrieval alone: the intent/rewrite step must emit a concrete `query_for_retrieval` for follow-ups.

## Recommended Fixes

### P0 - Fix Service-Public Introduction Parsing

Change the Service-Public parser to collect all introduction text blocks, not only the first one.

Expected implementation direction:

- replace `root.find('.//Introduction/Texte')` with iteration over all relevant `Introduction/Texte` nodes;
- concatenate the extracted markdown in source order;
- store full intro text in both `doc_markdown` and `metadata["introduction"]`.

Add a targeted unit test with a synthetic XML containing two introduction text nodes:

- first paragraph: generic intro;
- second paragraph: operational definition with an age condition;
- assert both appear in parsed `doc_markdown`.

After deployment/reingestion, verify that staging `F32513` contains:

- `complément de rémunération`
- `au moins 1 enfant`
- `moins de 20 ans`

### P1 - Add Source-Recall Fixtures for SP Misses

Add regression fixtures for the exact phrasing failures:

- `demander le remboursement de son abonnement de transport` -> `F12163`
- `remboursements trajets repas pour passer des concours` -> `F527`
- `contractuelle enceinte assistance médicale à la procréation` -> `F34536`
- `contractuel temps partiel conditions` -> `F18029`

These should check retrieved/aggregated/final context, not only final answer text.

### P1 - Harden Selector Against MATTE Pollution

The selector should avoid keeping generic MATTE documents when a precise SP section exists.

Potential guardrails:

- keep at least one high-scoring precise SP section when its score is close to or above MATTE;
- penalize MATTE documents with generic titles like `Quel contrat choisir` unless the query is actually about contract choice;
- add selector tests for cases 30 and 31.

### P2 - Improve Follow-Up Retrieval Rewriting

When the classifier reasoning identifies the referent of a follow-up, `query_for_retrieval` must be rewritten accordingly.

Example:

```text
quelles sont les conditions pour obtenir le complément ?
```

should become something like:

```text
conditions pour obtenir le supplément familial de traitement SFT
```

### P2 - Decide Product Scope for Fonctionnaire / Agent Public

Before fixing cases 11, 12, 20, and 21, decide the expected behavior:

- answer all `agent public` questions broadly;
- answer only contractuels FPE;
- or answer broadly but clearly separate fonctionnaire vs contractuel.

Without this rule, review labels will remain inconsistent.

## Suggested Issue Split

1. `Source SP - parser intro Service-Public drops SFT eligibility paragraph`
2. `Source SP - recall and selector regressions on transport, AMP, exams, sick leave`
3. `Source MATTE - generic MATTE sections pollute final context`
4. `Follow-up rewriting - classifier understands referent but query_for_retrieval remains ambiguous`
5. `Source Legifrance - add separate legal-routing replay; current human-review lot does not test DGAFP`
