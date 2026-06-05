# Mastra Port: Executive Summary

## Context

The assistant-rh chatbot currently runs on a Python-based pipeline with a Streamlit interface. This technology stack has served its purpose but is showing its age: the code is difficult to maintain, errors are hard to diagnose, and adding new features requires significant effort.

We are investigating a migration to Mastra, a modern TypeScript framework designed specifically for AI workflows. This document summarizes our findings and outlines the integration strategy.

## Why consider a change

**Current challenges:**

- **Maintenance burden.** The pipeline mixes responsibilities across multiple files with no clear boundaries. Changes often have unexpected side effects.

- **Limited visibility.** When something goes wrong, diagnosing the problem requires digging through logs with ~120 columns of data. Finding the relevant information is slow and error-prone.

- **Static user experience.** The current system can only stream the final answer. Users see nothing until generation completes, then get the full response at once. Modern chat interfaces show progress ("searching…", "reading documents…", "generating answer…").

- **Evolving requirements.** The team needs to experiment with different prompts, models, and retrieval strategies. Each experiment currently requires code changes and redeployment.

**What Mastra offers:**

- **Structured workflows.** Each step in the pipeline is a self-contained unit with clear inputs and outputs. Type mismatches are caught before deployment.

- **Built-in observability.** Every step is automatically traced. You can see exactly how long each stage took and what data passed through it.

- **Progressive streaming.** The frontend can show users what's happening at each stage, creating a more responsive experience.

- **Interactive development.** A visual interface (Mastra Studio) lets the team test changes, experiment with prompts, and debug issues without redeploying.

- **Structured evaluation.** The team can run test cases against the pipeline and compare results across changes, building confidence before production deployment.

## What stays the same

The core intelligence of the system remains unchanged:

- **The same database** (PostgreSQL) stores all documents, sections, and configuration.

- **The same LLM providers** (Albert from DINUM, Scaleway as backup) generate answers and embeddings.

- **The same retrieval logic** — the six-stage pipeline that classifies intent, searches documents, aggregates sections, selects context, and generates answers.

- **The same administrative configuration** — RAG parameters, prompt templates, and the acronym dictionary remain in their current database tables.

## What changes

| Current | After migration |
|---------|-----------------|
| Streamlit web interface | OpenAI-compatible API + modern chat UI |
| Debugging via log files | Visual workflow inspection |
| Single-shot answer streaming | Progressive status updates |
| Code changes for experiments | Configuration-driven testing |

## Integration strategy: Parallel operation

To minimize risk, we propose running both systems simultaneously during evaluation:

```
┌─────────────────────────────────────────────────────────────┐
│                     Evaluation Period                        │
│                                                              │
│   Python Pipeline ─────► Streamlit UI                       │
│        (current)              │                              │
│                              ▼                              │
│                         Side-by-side                        │
│                         comparison                          │
│                              │                              │
│                              ▼                              │
│   Mastra Pipeline ─────► Chat UI (new)                      │
│        (new)                                                │
│                                                              │
│   ──────────────── Shared Database ────────────────         │
│         (both read from same PostgreSQL)                    │
└─────────────────────────────────────────────────────────────┘
```

**Key principle:** Both pipelines read from the same database. Configuration changes apply to both simultaneously. This allows:

- Running identical queries against both systems
- Comparing answer quality in real conditions
- Rolling back instantly if issues arise (the Python pipeline remains fully operational)
- Switching traffic gradually, not all at once

## User interface: Two options

The current Streamlit interface will be replaced. We have identified two viable paths:

### Option A: suitenumerique/conversations (Recommended)

This is the French government's official open-source chat interface, built for public sector use.

**Advantages:**
- Official French government project, designed for "La Suite numérique" ecosystem
- Native support for ProConnect (government single sign-on)
- Built for SecNumCloud compliance from the start
- French-language interface with no translation gaps
- MIT license (fully permissive for government use)

**Risks:**
- Early-stage project (breaking changes possible)
- Smaller community (47 stars, 20 contributors)
- Requires adaptation for the current Scaleway deployment target

**Why it's the recommended choice:** Strategic alignment with the French government's digital transformation. If the official instance becomes available, integration would be straightforward. Even if we self-host, we benefit from a purpose-built public sector solution.

### Option B: LibreChat (Fallback)

A mature, widely-used open-source chat interface.

**Advantages:**
- Production-ready (35,000+ stars on GitHub)
- MIT license (government-safe)
- Keycloak support (compatible with ProConnect via intermediary)
- Docker-based deployment compatible with container platforms

**Why it's the fallback:** If conversations proves too early-stage or deployment proves problematic, LibreChat is a safe, mature alternative.

**Decision point:** We recommend validating conversations first (2-4 weeks), with LibreChat as a proven backup.

## Implementation approach

The migration follows a phased approach, each phase delivering working software:

| Phase | Duration | Outcome |
|-------|----------|---------|
| Foundation | 2 weeks | Mastra project running, connected to database and LLM providers |
| Pipeline | 4 weeks | All six stages implemented, testable via Mastra Studio |
| Integration | 2 weeks | OpenAI-compatible API endpoints ready for UI connection |
| Validation | 2 weeks | Side-by-side testing with Python pipeline |

**Total estimated effort:** 10 weeks

Each phase produces working software. The Python pipeline remains operational throughout. If issues arise at any point, we can pause and the current system continues serving users.

## Risks and mitigations

| Risk | Mitigation |
|------|------------|
| Feature parity gaps | Detailed analysis complete; all critical features mapped to Mastra equivalents |
| Performance differences | Parallel operation allows direct comparison; rollback is instant |
| Team learning curve | TypeScript is mainstream; Mastra documentation is comprehensive |
| UI integration delays | UI validation starts early (Phase 1); fallback option identified |
| Breaking changes in conversations | LibreChat is a mature backup; UI work is decoupled from pipeline |

## Key decisions for stakeholders

1. **UI Direction:** Do we proceed with conversations validation, or prioritize LibreChat for faster deployment?

2. **Resource allocation:** Can the team dedicate full-time effort to the migration, or should we plan for a longer timeline with part-time allocation?

3. **Evaluation criteria:** What metrics will determine success? (Answer quality, latency, user satisfaction, maintenance effort?)

4. **Go-live authority:** Who makes the final decision to switch traffic from Python to Mastra?

## Supporting documentation

The technical details are documented in separate analysis files:

- **Pipeline Analysis** — Complete breakdown of the current Python pipeline, stage by stage
- **Implementation Plan** — Detailed architecture and code-level design for the Mastra version
- **Risk & Opportunity Analysis** — Critical review of challenges and benefits
- **Studio Config Analysis** — How administrative configuration translates to the new system
- **UI Replacement Analysis** — Full comparison of chat interface options

## Next steps

1. **Confirm UI direction** — conversations vs. LibreChat (decision needed)
2. **Validate conversations on Scaleway** — Deploy a test instance (1-2 weeks)
3. **Begin Phase 0** — Project setup and infrastructure (once UI direction confirmed)
4. **Establish evaluation criteria** — Define success metrics before starting

---

*This document is a living summary. It will be updated as the investigation progresses and decisions are made.*
