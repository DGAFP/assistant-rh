# AGENTS.md

Guidance for coding agents working in this repository. Keep this file short, current, and operational; use `docs/` for long-form design notes and historical context.

## Project snapshot

Assistant RH is a French government HR chatbot for contractual public employees in the Fonction Publique d'État.

- UI: Streamlit app in `apps/streamlit-ui/`.
- Core runtime: Python RAG pipeline in `packages/rag-pipeline/`.
- Data ingestion: CLI app in `apps/data-ingestion-cli/` plus data-engineering package in `packages/data-engineering/`.
- TypeScript port/API work: Mastra app in `apps/mastra-pipeline/`.
- Database: PostgreSQL with pgvector on Scaleway Managed Database.
- AI providers: Albert/DINUM primary; Scaleway fallback for LLM and embeddings.

## Repository layout

```text
apps/
  streamlit-ui/         Streamlit UI entrypoint and pages
  mastra-pipeline/      TypeScript Mastra pipeline and OpenAI-compatible API work
  data-ingestion-cli/   Canonical ingestion CLI
packages/
  rag-pipeline/         Production Python RAG pipeline
  data-engineering/     Ingestion jobs and transformations
  shared-config/        Shared configuration helpers
src/
  ui/                   Streamlit UI helpers not yet packaged
  _archive/             Historical code; do not extend unless explicitly asked
tests/                  Unit, integration, and conformance tests
docs/                   Durable project documentation and runbooks
scripts/                One-off tooling and historical notebooks/scripts
```

## Worktree and git rules

This repository is normally used as a bare/worktree workspace under `~/Code/alliance/assistant-rh`.

- Do not edit files from the workspace control-plane root. Work inside a real worktree such as `main/`, `chore-add-agents-md/`, or a new feature worktree.
- For new work, create a dedicated worktree from up-to-date `main` using Worktrunk (`wt`), not raw `git worktree` commands.
- After creating a worktree in a non-interactive agent session, run `wt step copy-ignored` from the new worktree so `.env` and other ignored local files are copied when configured.
- Keep changes scoped and reviewable. Avoid drive-by refactors.
- Use conventional commit messages such as `fix: ...`, `feat: ...`, `docs: ...`, `chore: ...`.
- Prefer creating PRs directly with `gh pr create -R DGAFP/assistant-rh --base main --head <branch>`.

## Local setup and commands

Python uses uv. Node/TypeScript uses pnpm.

```bash
# Install Python dependencies, including dev tools
uv sync --group dev

# Run Python tests
uv run python -m pytest tests/ --ignore=tests/archive -v

# Run Python lint checks
uv run ruff check src apps/streamlit-ui/pages tests --select E,F,I

# Auto-fix Python lint where appropriate
uv run ruff check --fix src apps/streamlit-ui/pages tests

# Run Streamlit locally
uv run streamlit run apps/streamlit-ui/Home.py

# Run TypeScript lint checks
pnpm lint:ts

# Mastra app
pnpm mastra:dev
pnpm mastra:build

# Data ingestion CLI help
uv run data-ingestion --help
```

Notes:

- `pyproject.toml` intentionally has `default-groups = []`; use `uv sync --group dev` for local work that needs pytest, ruff, mypy, or pre-commit.
- Do not switch uv defaults to include dev dependencies globally; production and image builds rely on runtime-only installs.
- Python code targets Python 3.12.
- Root Python line length is 150; ruff currently selects `E`, `F`, and `I`.

## Environment variables and secrets

Never commit real secrets or `.env` files.

Important runtime variables:

```bash
APP_ENV=local|staging|production
APP_DB_TARGET=scaleway
APP_SCALEWAY_ENV=staging|production
SCW_POSTGRES_DSN=postgresql://...

ALBERT_API_KEY=...
ALBERT_BASE_URL=https://albert.api.etalab.gouv.fr/v1
SCALEWAY_API_KEY=...
SCALEWAY_BASE_URL=https://api.scaleway.ai/v1

COOKIES_PASSWORD=...
ADMIN_PASSWORD=...
```

Provider naming matters:

- `ALBERT_*` is used by the Python RAG pipeline for Albert/DINUM.
- `SCALEWAY_*` is for Scaleway AI inference fallback.
- `SCW_*` is for Scaleway infrastructure such as database, registry, buckets, project, and organization.
- `SCW_POSTGRES_DSN` is the canonical Scaleway runtime DSN. GitHub environments provide different values for staging and production under the same secret name.
- `SCALINGO_POSTGRESQL_URL` is only for Scalingo-specific paths if present; do not reintroduce Scalingo as an active Scaleway runtime fallback.

Local database caution:

- Local Supabase commonly runs on `127.0.0.1:54322`.
- `.env` may point to staging or a tunnel; confirm the target before running migrations, ingestion, or destructive scripts.

## RAG pipeline architecture

The production pipeline is in `packages/rag-pipeline/` and follows this flow:

```text
Query
  -> QueryProcessor      intent, acronym expansion, reformulation, legal-search flag
  -> Retriever           parallel vector/hybrid retrieval across source tables
  -> SectionAggregator   chunk-to-section grouping and reranking
  -> ContextSelector     optional LLM filtering, may short-circuit no-answer cases
  -> ContextBuilder      token budget, full-doc inclusion, triangulation, legal refs
  -> Generator           answer generation with provider fallback
```

Core design constraints:

- Preserve source priority and anti-hallucination behavior: answer only from provided sources; say when sources are insufficient.
- Keep DGAFP/legal search conditional on intent where relevant.
- Chunks are retrieval units; sections/documents are context units.
- Preserve deterministic ordering when scores tie. Conformance tests are sensitive to ordering drift.
- Keep provider fallbacks graceful: Albert primary, Scaleway fallback.
- Do not weaken error handling around provider failures, database access, streaming, or selector no-answer cases.

## Data model landmarks

Common RAG tables:

- `rag_documents`: document metadata and full markdown.
- `rag_sections`: section markdown and hierarchy.
- `rag_chunks_matte`: MATTE guide chunks.
- `rag_chunks_service_public`: Service-Public chunks.
- `rag_chunks_dgafp`: legal/regulatory chunks.
- `rag_chunks_rgrh`: RGRH chunks.
- `rag_chunks_test`: additional/test chunks when enabled.
- `rag_config`: runtime pipeline configuration.
- `system_prompts`: editable prompt templates.
- `acronyms`: acronym expansion dictionary.

Before changing schema assumptions, search for all consumers in Python, TypeScript, tests, scripts, and workflows.

## Testing and verification expectations

Choose the smallest reliable verification for the change.

- Documentation-only changes: inspect rendered Markdown or at least verify the diff.
- Python logic changes: run targeted tests first, then broader pytest when practical.
- RAG behavior changes: run relevant unit tests and conformance checks where available.
- TypeScript/Mastra changes: run `pnpm lint:ts` and relevant Mastra build/tests.
- Deployment/workflow changes: validate YAML and use one-shot status checks; avoid long-running `--watch` commands.

Do not delete or relax tests to make a change pass unless the user explicitly approves and the rationale is documented in the PR.

## Common pitfalls

- Editing from the bare workspace root instead of a worktree.
- Forgetting `uv sync --group dev` in a new or old worktree before running tests.
- Removing exported functions without searching for imports/usages across `src/`, `packages/`, `apps/`, `tests/`, and `scripts/`.
- Confusing `SCALEWAY_*` AI variables with `SCW_*` infrastructure variables.
- Running commands against staging/production DSNs accidentally from `.env`.
- Assuming GitHub Environment secrets are available without `environment: ...` on the workflow job.
- Using `localeCompare` or locale-sensitive sorting for deterministic fingerprints in TypeScript; use stable binary string comparison instead.
- Touching `src/_archive/` or historical notebooks unless the task specifically targets them.

## Pull request guidance

PR descriptions should include:

- Summary of what changed.
- Why the change is needed.
- Testing performed, or why tests were not run.
- Related issue link when applicable.

For small documentation-only PRs, keep the description concise. For behavior changes, include enough context for reviewers to understand risk and verification.
