# Moonrepo Monorepo Migration Plan

> **Statut historique.** Ce plan préparatoire est remplacé par la configuration
> Moon actuelle. Les références au candidat TypeScript retiré par
> [#440](https://github.com/DGAFP/assistant-rh/issues/440) sont conservées comme trace.

## Executive Summary

This document outlines a comprehensive migration strategy to restructure the assistant-rh repository into a moonrepo-managed monorepo. The migration aims to:

1. **Separate concerns**: Extract the Streamlit UI as a standalone application
2. **Create reusable packages**: Modularize the RAG pipeline, data engineering, and shared utilities
3. **Prepare for Mastra integration**: Establish a TypeScript project structure for the upcoming Mastra RAG pipeline

**Recommendation**: Proceed with a phased migration that maintains backward compatibility while establishing the monorepo structure. The migration should be completed before significant Mastra development begins.

---

## Current State Analysis

### Repository Structure

```
assistant-rh/
├── Home.py                    # Streamlit entry point
├── pages/                      # Streamlit pages (11 files, ~460KB)
│   ├── 01_Chatbot.py          # Main chatbot interface
│   ├── 02_Chat_Logs.py        # Chat history viewer
│   ├── 03_Feedback_Dashboard.py
│   ├── 04_Admin_Config.py     # Admin configuration
│   ├── 05_DB_Explorer.py
│   ├── 06_Goldset_Explorer.py
│   ├── 07_Eval_Comparison.py
│   ├── 08_Chunking_Evaluation.py
│   ├── 09_Pipeline_Evaluation.py
│   ├── 10_Intent_Gater_Evaluation.py
│   └── 11_Golden_Beta_Analysis.py
├── src/
│   ├── rag_v3_clean/          # RAG pipeline (19 modules, ~200KB)
│   │   ├── pipeline.py        # Orchestrator
│   │   ├── query_processor.py # Intent classification
│   │   ├── retriever.py       # Vector search
│   │   ├── section_aggregator.py
│   │   ├── context_selector.py
│   │   ├── context_builder.py
│   │   ├── generator.py       # LLM answer generation
│   │   ├── embedder.py
│   │   ├── reranker.py
│   │   ├── llm_client.py
│   │   ├── chat_logger.py
│   │   ├── feedback_analyzer.py
│   │   ├── admin.py
│   │   ├── db_helpers.py
│   │   ├── citation_extractor.py
│   │   ├── config.py
│   │   ├── models.py
│   │   └── prompts/
│   ├── ui/                    # Streamlit UI components (12 modules)
│   │   ├── chatbot_llm.py
│   │   ├── chatbot_logging.py
│   │   ├── chatbot_sources.py
│   │   ├── chatbot_styles.py
│   │   ├── chatbot_feedback.py
│   │   ├── admin_auth.py
│   │   ├── llm_selector.py
│   │   ├── document_url_helper.py
│   │   ├── citation_deduplicator.py
│   │   ├── db_utils.py
│   │   └── feedback_logs.py
│   ├── data_engineering/      # Data ingestion pipeline
│   │   └── service_public/
│   │       ├── bronze.py
│   │       ├── silver.py
│   │       ├── gold.py
│   │       ├── qna_chunking.py
│   │       ├── pipeline.py
│   │       ├── db.py
│   │       └── config.py
│   └── goldset/               # Gold set management
├── scripts/                   # Deployment & utility scripts
├── notebooks/                 # Jupyter evaluation notebooks
├── tests/                     # Test suite
├── config/                    # Configuration files
└── docs/                      # Documentation
```

### Code Statistics

| Component | Files | Lines of Code | Purpose |
|-----------|-------|---------------|---------|
| `pages/` | 11 | ~8,500 | Streamlit UI pages |
| `src/rag_v3_clean/` | 19 | ~5,200 | RAG pipeline core |
| `src/ui/` | 12 | ~3,500 | Streamlit UI components |
| `src/data_engineering/` | 8 | ~1,200 | Data ingestion |
| `scripts/` | 10 | ~800 | Deployment scripts |
| `tests/` | 4 | ~400 | Test suite |
| **Total** | **64** | **~20,000** | |

### Current Dependencies

```toml
# pyproject.toml (simplified)
[project]
dependencies = [
    "streamlit>=1.55.0",
    "pandas>=2.3.3",
    "psycopg[binary]>=3.3.3",
    "openai>=2.28.0",
    "plotly>=6.6.0",
    # ... more deps
]

[dependency-groups]
dev = [
    "pytest",
    "ruff",
    "black",
    "mlflow",  # evaluation only
    # ... more dev deps
]
```

### Current Pain Points

1. **Tight coupling**: UI components directly import from `rag_v3_clean`
2. **No clear boundaries**: Streamlit pages mix business logic with presentation
3. **Deployment complexity**: Single deployment bundles UI and pipeline together
4. **Testing isolation**: Difficult to test pipeline independently from UI
5. **No TypeScript support**: Current structure doesn't accommodate Mastra integration

---

## Moonrepo Overview

### What is Moonrepo?

Moonrepo is a build system and monorepo management tool written in Rust. It provides:

- **Project graph**: Automatic dependency detection between projects
- **Task orchestration**: Parallel task execution with dependency-aware scheduling
- **Smart caching**: Content-addressable caching with remote support
- **Toolchain management**: Automatic version management for Python, Node.js, etc.
- **Task inheritance**: DRY task configuration across projects

### Language Support

| Language | Tier | Status |
|----------|------|--------|
| Python | 2-3 | Full support (partial toolchain) |
| TypeScript | 3 | Full support (Bun/Deno/Node) |
| JavaScript | 3 | Full support |
| Rust | 3 | Full support |
| Go | 3 | Full support |

### Key Configuration Files

```
.moon/
├── workspace.yml      # Project discovery, VCS, caching
├── toolchain.yml      # Language toolchain settings
└── tasks/
    ├── python.yml     # Inherited Python tasks
    └── typescript.yml # Inherited TypeScript tasks

<project>/
└── moon.yml           # Project-level configuration
```

### Why Moonrepo for assistant-rh?

| Requirement | Moonrepo Solution |
|-------------|-------------------|
| Separate UI from pipeline | Distinct projects with explicit dependencies |
| TypeScript integration | First-class TypeScript support |
| Shared packages | Project dependencies with `dependsOn` |
| Parallel builds | Task graph with parallel execution |
| CI/CD optimization | Affected-project detection |
| Version consistency | Toolchain management |

---

## Proposed Monorepo Structure

### Directory Layout

```
assistant-rh/
├── .moon/
│   ├── workspace.yml          # Workspace configuration
│   ├── toolchain.yml          # Python/Node toolchains
│   └── tasks/
│       ├── python.yml         # Shared Python tasks
│       └── typescript.yml     # Shared TypeScript tasks
├── apps/
│   ├── streamlit-ui/          # Streamlit application
│   │   ├── moon.yml
│   │   ├── pyproject.toml
│   │   ├── Home.py
│   │   ├── pages/
│   │   └── src/
│   │       └── ui/            # UI-specific components
│   └── mastra-api/            # Mastra TypeScript API (future)
│       ├── moon.yml
│       ├── package.json
│       ├── tsconfig.json
│       └── src/
│           ├── workflows/
│           └── agents/
├── tsconfig.options.json      # Base TypeScript config (shared)
├── packages/
│   ├── rag-pipeline/          # Python RAG pipeline
│   │   ├── moon.yml
│   │   ├── pyproject.toml
│   │   └── src/
│   │       └── rag_v3_clean/
│   ├── data-engineering/      # Data ingestion
│   │   ├── moon.yml
│   │   ├── pyproject.toml
│   │   └── src/
│   │       └── data_engineering/
│   ├── shared-config/         # Shared configuration
│   │   ├── moon.yml
│   │   ├── pyproject.toml
│   │   └── src/
│   │       └── config/
│   └── mastra-client/         # TypeScript client (future)
│       ├── moon.yml
│       ├── package.json
│       └── src/
├── scripts/                   # Workspace-level scripts
│   └── service_public/
├── notebooks/                 # Evaluation notebooks
├── tests/                     # Integration tests
└── docs/                      # Documentation
```

### Project Definitions

#### `.moon/workspace.yml`

```yaml
# Workspace configuration
$v: 2

projects:
  # Explicit project mapping
  sources:
    # Applications
    streamlit-ui: 'apps/streamlit-ui'
    mastra-api: 'apps/mastra-api'

    # Packages
    rag-pipeline: 'packages/rag-pipeline'
    data-engineering: 'packages/data-engineering'
    shared-config: 'packages/shared-config'
    mastra-client: 'packages/mastra-client'

  # Glob patterns for discovery
  globs:
    - 'apps/*'
    - 'packages/*'

# Default project for local development
defaultProject: 'streamlit-ui'

# VCS configuration
vcs:
  provider: 'github'
  defaultBranch: 'main'
  hooks:
    pre-commit:
      - 'moon run :lint :format --affected --status=staged --no-bail'

# Pipeline configuration
pipeline:
  cacheLifetime: '7 days'
  autoCleanCache: true
  installDependencies: true

# Code owners
codeowners:
  sync: true
  globalPaths:
    '*': ['@luis']
```

#### `.moon/toolchain.yml`

```yaml
# Python toolchain
python:
  version: '3.12'
  packageManager: 'uv'
  syncProjectReferences: false

# TypeScript/Node toolchain
typescript:
  version: '5.0'
  packageManager: 'pnpm'
  syncProjectReferences: true
```

#### `.moon/tasks/python.yml`

```yaml
# Inherited Python tasks
tasks:
  lint:
    command: 'ruff check'
    inputs:
      - 'src/**/*.py'
      - 'tests/**/*.py'
      - 'pyproject.toml'

  format:
    command: 'ruff format'
    inputs:
      - 'src/**/*.py'
      - 'tests/**/*.py'

  format-check:
    command: 'ruff format --check'
    inputs:
      - 'src/**/*.py'
      - 'tests/**/*.py'

  test:
    command: 'pytest'
    inputs:
      - 'src/**/*.py'
      - 'tests/**/*.py'
    options:
      cache: false

  typecheck:
    command: 'pyright'
    inputs:
      - 'src/**/*.py'
      - 'pyproject.toml'
```

#### `.moon/tasks/typescript.yml`

```yaml
# Inherited TypeScript tasks
tasks:
  lint:
    command: 'biome check'
    inputs:
      - 'src/**/*.ts'
      - 'src/**/*.tsx'

  format:
    command: 'biome format --write'
    inputs:
      - 'src/**/*.ts'
      - 'src/**/*.tsx'

  typecheck:
    command: 'tsc --noEmit'
    inputs:
      - 'src/**/*.ts'
      - 'src/**/*.tsx'
      - 'tsconfig.json'

  build:
    command: 'tsup'
    inputs:
      - 'src/**/*.ts'
      - 'package.json'
    outputs:
      - 'dist/'
```

### Project Configurations

#### `apps/streamlit-ui/moon.yml`

```yaml
language: 'python'
layer: 'application'
stack: 'frontend'

project:
  title: 'Streamlit UI'
  description: 'Streamlit web interface for assistant-rh'

dependsOn:
  - 'rag-pipeline'
  - 'shared-config'

tasks:
  dev:
    command: 'streamlit run Home.py'
    options:
      persistent: true
      cache: false

  build:
    command: 'echo "No build step for Streamlit"'
    options:
      cache: false
```

#### `packages/rag-pipeline/moon.yml`

```yaml
language: 'python'
layer: 'library'
stack: 'backend'

project:
  title: 'RAG Pipeline'
  description: 'Core RAG pipeline for assistant-rh'

dependsOn:
  - 'shared-config'

tasks:
  test:
    command: 'pytest tests/'
    options:
      cache: false
```

#### `apps/mastra-api/moon.yml`

```yaml
language: 'typescript'
layer: 'application'
stack: 'backend'

project:
  title: 'Mastra API'
  description: 'Mastra TypeScript RAG pipeline'

dependsOn:
  - 'mastra-client'

tasks:
  dev:
    command: 'mastra dev'
    options:
      persistent: true
      cache: false

  build:
    command: 'mastra build'
    inputs:
      - 'src/**/*.ts'
    outputs:
      - 'dist/'
```

---

## Migration Strategy

### Phase 0: Preparation (1 week)

**Objectives:**
- Install moonrepo tooling
- Create initial workspace configuration
- Validate toolchain compatibility

**Tasks:**
1. Install moon and proto:
   ```bash
   proto install moon
   ```

2. Create `.moon/` directory structure

3. Create initial `workspace.yml` with current structure as single project

4. Validate Python toolchain:
   ```bash
   moon run streamlit-ui:lint
   ```

**Deliverable:** Working moonrepo setup with existing codebase

### Phase 1: Extract Packages (2 weeks)

**Objectives:**
- Extract `rag_v3_clean` as standalone package
- Extract `data_engineering` as standalone package
- Create `shared-config` package
- Establish inter-package dependencies

**Tasks:**

1. **Create `packages/rag-pipeline/`**:
   ```bash
   mkdir -p packages/rag-pipeline/src
   mv src/rag_v3_clean packages/rag-pipeline/src/
   ```

2. **Create package `pyproject.toml`**:
   ```toml
   [project]
   name = "assistant-rh-rag-pipeline"
   version = "0.1.0"

   dependencies = [
       "psycopg[binary]>=3.3.3",
       "openai>=2.28.0",
       # ... pipeline-specific deps
   ]
   ```

3. **Create `packages/data-engineering/`**:
   ```bash
   mkdir -p packages/data-engineering/src
   mv src/data_engineering packages/data-engineering/src/
   ```

4. **Create `packages/shared-config/`**:
   - Extract database configuration
   - Extract environment utilities
   - Share across Python packages

5. **Update imports**:
   ```python
   # Before
   from src.rag_v3_clean import pipeline

   # After (hyphens in package name become underscores in imports)
   from assistant_rh_rag_pipeline import pipeline
   ```

   > **Note:** Python package names use hyphens in `pyproject.toml` (`assistant-rh-rag-pipeline`) but underscores in imports (`assistant_rh_rag_pipeline`).

**Deliverable:** Three packages with proper dependencies

### Phase 2: Extract Streamlit UI (1 week)

**Objectives:**
- Move Streamlit to `apps/streamlit-ui/`
- Establish dependency on `rag-pipeline` package
- Validate UI functionality

**Tasks:**

1. **Create `apps/streamlit-ui/`**:
   ```bash
   mkdir -p apps/streamlit-ui/pages
   mv Home.py apps/streamlit-ui/
   mv pages/*.py apps/streamlit-ui/pages/
   mv src/ui apps/streamlit-ui/src/
   ```

2. **Create app `pyproject.toml`**:
   ```toml
   [project]
   name = "assistant-rh-streamlit-ui"
   version = "0.1.0"

   dependencies = [
       "streamlit>=1.55.0",
       "assistant-rh-rag-pipeline",  # Local package
       "plotly>=6.6.0",
   ]
   ```

3. **Update imports in UI**:
   ```python
   # Before
   from src.rag_v3_clean import Pipeline

   # After (hyphens in package name become underscores in imports)
   from assistant_rh_rag_pipeline import Pipeline
   ```

**Deliverable:** Standalone Streamlit application

### Phase 3: Mastra Integration Preparation (1 week)

**Objectives:**
- Create `apps/mastra-api/` structure
- Configure TypeScript toolchain
- Validate cross-language project graph

**Tasks:**

1. **Create `apps/mastra-api/`**:
   ```bash
   mkdir -p apps/mastra-api/src
   ```

2. **Initialize TypeScript project**:
   ```bash
   cd apps/mastra-api
   pnpm init
   pnpm add mastra @mastra/rag
   ```

3. **Create `tsconfig.json`**:
   ```json
   {
     "extends": "../../tsconfig.options.json",
     "compilerOptions": {
       "outDir": "dist",
       "rootDir": "src"
     },
     "include": ["src/**/*"]
   }
   ```

   The base `tsconfig.options.json` at workspace root should contain:
   ```json
   {
     "compilerOptions": {
       "target": "ES2022",
       "module": "ESNext",
       "moduleResolution": "bundler",
       "strict": true,
       "esModuleInterop": true,
       "skipLibCheck": true,
       "declaration": true,
       "declarationMap": true,
       "sourceMap": true
     }
   }
   ```

4. **Validate project graph**:
   ```bash
   moon project mastra-api
   ```

**Deliverable:** Mastra project structure ready for development

### Phase 4: Cleanup and Documentation (1 week)

**Objectives:**
- Remove archived code
- Update documentation
- Validate CI/CD integration

**Tasks:**

1. Remove the archived legacy source tree ✅ done during public-prep cleanup

2. Update `README.md` with monorepo structure

3. Create `CONTRIBUTING.md` with moonrepo commands

4. Update GitHub Actions workflow:
   ```yaml
   - name: Run affected tests
     run: moon run :test --affected
   ```

**Deliverable:** Clean, documented monorepo

---

## Critical Analysis

### Advantages of Moonrepo Migration

| Benefit | Impact | Risk Reduction |
|---------|--------|----------------|
| **Clear project boundaries** | High | Prevents accidental coupling |
| **Independent testing** | High | Faster test cycles |
| **Parallel builds** | Medium | CI/CD efficiency |
| **TypeScript integration** | High | Enables Mastra development |
| **Affected-project detection** | Medium | CI optimization |
| **Toolchain consistency** | Medium | Environment reproducibility |

### Disadvantages and Risks

| Risk | Severity | Mitigation |
|------|----------|------------|
| **Learning curve** | Medium | Gradual migration, documentation |
| **Import path changes** | High | Use `UV` workspace support |
| **Deployment complexity** | Medium | Maintain single deployment initially |
| **Moonrepo maturity** | Low | Tier 2-3 Python support is stable |
| **Breaking changes** | Medium | Comprehensive test coverage |

### Alternative Approaches

#### Alternative 1: Nx Monorepo

**Pros:**
- Mature ecosystem
- Strong CI/CD integration
- Python plugin available

**Cons:**
- Python support is Tier 2 (less native)
- Heavier tooling
- Less Rust-native performance

**Recommendation:** Not recommended. Moonrepo's Python support is more native.

#### Alternative 2: uv Workspaces (No Build Tool)

**Pros:**
- Simpler tooling
- Native Python focus
- Fast dependency resolution

**Cons:**
- No task orchestration
- No cross-language support
- No caching/affected detection

**Recommendation:** Consider for Python-only projects. Not suitable for Mastra integration.

#### Alternative 3: Turborepo

**Pros:**
- Strong caching
- Good CI/CD integration

**Cons:**
- TypeScript/JavaScript focused
- No native Python support

**Recommendation:** Not recommended. Would require significant tooling for Python.

### Key Decision Points

1. **Should we migrate before Mastra development?**
   - **Yes.** Migrating after Mastra development would require restructuring TypeScript imports and dependencies.

2. **Should we maintain backward compatibility?**
   - **Yes, initially.** Keep the root `pyproject.toml` as a compatibility layer and update it with `[tool.uv.workspace]` to support the monorepo structure.

   ```toml
   # Root pyproject.toml (compatibility layer)
   [project]
   name = "assistant-rh"
   version = "0.1.0"
   # ... existing deps ...

   [tool.uv.workspace]
   members = [
       "apps/streamlit-ui",
       "packages/rag-pipeline",
       "packages/data-engineering",
       "packages/shared-config",
   ]
   ```

3. **Should we use glob-based or explicit project mapping?**
   - **Explicit for apps, glob for packages.** Apps need stable identifiers; packages can be discovered.

4. **Should we use uv or pip for Python package management?**
   - **uv.** Already in use, and moonrepo has better uv integration.

---

## Dependency Graph

### Current State

```
┌─────────────────────────────────────────────────┐
│                  assistant-rh                    │
│                                                  │
│  ┌─────────────┐  ┌─────────────┐              │
│  │  Streamlit  │──│ rag_v3_clean│              │
│  │    Pages    │  │   Pipeline  │              │
│  └─────────────┘  └──────┬──────┘              │
│                          │                      │
│                   ┌──────▼──────┐              │
│                   │data_engineer│              │
│                   └─────────────┘              │
└─────────────────────────────────────────────────┘
```

### Proposed State

```
┌─────────────────────────────────────────────────────────────┐
│                     assistant-rh (moonrepo)                  │
│                                                              │
│  apps/                         packages/                     │
│  ┌─────────────────┐          ┌─────────────────┐           │
│  │  streamlit-ui   │──────────│  rag-pipeline    │           │
│  │  (Python app)   │          │  (Python pkg)    │           │
│  └─────────────────┘          └────────┬────────┘           │
│                                        │                     │
│  ┌─────────────────┐          ┌────────▼────────┐           │
│  │   mastra-api    │          │ shared-config    │           │
│  │ (TypeScript app)│          │  (Python pkg)    │           │
│  └────────┬────────┘          └─────────────────┘           │
│           │                                                  │
│           │ dependsOn                                        │
│           ▼                                                  │
│  ┌─────────────────┐          ┌─────────────────┐           │
│  │  mastra-client  │──future──│ data-engineering│           │
│  │  (TS pkg)       │          │  (Python pkg)   │           │
│  └─────────────────┘          └─────────────────┘           │
└─────────────────────────────────────────────────────────────┘
```

---

## Task Workflow Examples

### Local Development

```bash
# Run Streamlit UI with hot reload
moon run streamlit-ui:dev

# Run Mastra API with hot reload
moon run mastra-api:dev

# Run all tests
moon run :test

# Run affected tests only
moon run :test --affected
```

### CI/CD Pipeline

```bash
# Install dependencies
moon sync

# Run linting on affected projects
moon run :lint --affected

# Run tests on affected projects
moon run :test --affected

# Build affected projects
moon run :build --affected
```

### Cross-Project Tasks

```bash
# Build all UI dependencies before running
moon run streamlit-ui:dev --deps

# Run data engineering pipeline
moon run data-engineering:ingest
```

---

## Implementation Timeline

| Week | Phase | Deliverables | Dependencies |
|------|-------|--------------|--------------|
| 1 | Preparation | Moonrepo installed, workspace config | moon binary |
| 2-3 | Extract Packages | `rag-pipeline`, `data-engineering`, `shared-config` | Phase 0 |
| 4 | Extract UI | `streamlit-ui` app | Phase 1 |
| 5 | Mastra Prep | `mastra-api` structure | Phase 2 |
| 6 | Cleanup | Documentation, CI/CD | Phase 1-3 |

**Total Duration:** 6 weeks (part-time), 3 weeks (full-time)

---

## Risk Assessment

### High Risk

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| Import path breakage | High | High | Use `uv` workspace, automated imports |
| Deployment pipeline changes | Medium | High | Maintain single deployment initially |

### Medium Risk

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| Team learning curve | Medium | Medium | Documentation, gradual migration |
| CI/CD integration | Low | Medium | GitHub Actions moonrepo plugin |

### Low Risk

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| Moonrepo bugs | Low | Low | Tier 2-3 Python is stable |
| Performance issues | Low | Low | Rust-native performance |

---

## Recommendations

### Immediate Actions

1. **Install moonrepo** in the development environment
2. **Create workspace configuration** with explicit project mapping
3. **Validate Python toolchain** with existing codebase

### Short-term (Phase 1-2)

1. **Extract packages** in order: `shared-config` → `rag-pipeline` → `data-engineering`
2. **Update imports** using automated refactoring
3. **Add comprehensive tests** for package boundaries

### Medium-term (Phase 3-4)

1. **Create Mastra project structure** before significant development
2. **Update CI/CD** to use moonrepo's affected-project detection
3. **Document workflow** for future contributors

### Long-term

1. **Consider separate deployments** for UI and API
2. **Evaluate moonrepo cloud** for remote caching
3. **Add more TypeScript projects** as needed

---

## Open Questions

1. **Should `goldset` be a separate package or part of `rag-pipeline`?**
   - Recommendation: Include in `rag-pipeline` initially, extract if needed.

2. **Should notebooks be in a separate project?**
   - Recommendation: Keep at workspace root, not a moon project.

3. **Should we use moonrepo's Docker integration?**
   - Recommendation: Evaluate after Phase 2, not immediately required.

4. **How to handle shared `.env` file?**
   - Recommendation: Use moonrepo's `envFile` task option, or symlink.

---

## Appendix: Command Reference

### Moonrepo Commands

```bash
# Project graph
moon project <name>           # Show project details
moon graph                    # Show full project graph

# Task execution
moon run <project>:<task>     # Run specific task
moon run :<task>             # Run task in all projects
moon run :<task> --affected  # Run on affected projects only

# CI/CD
moon ci                       # Run CI pipeline (affected only)
moon run :test --affected     # Test affected projects

# Sync
moon sync                     # Sync workspace
moon sync projects           # Sync project references

# Docker
moon docker scaffold          # Generate Docker scaffolding
moon docker prune             # Clean Docker environment
```

### UV Workspace Commands

```bash
# Install dependencies
uv sync                       # Sync all workspace deps

# Add dependency to specific project
uv add --package rag-pipeline <dep>

# Run command in project context
uv run --package streamlit-ui streamlit run Home.py
```

---

## References

- [Moonrepo Documentation](https://moonrepo.dev/docs)
- [Moonrepo Python Support](https://moonrepo.dev/docs/guides/examples/python)
- [Moonrepo TypeScript Support](https://moonrepo.dev/docs/guides/examples/typescript)
- [UV Workspace Documentation](https://docs.astral.sh/uv/workspaces/)
- [Mastra Documentation](https://mastra.ai/docs)

---

*Document prepared for assistant-rh monorepo migration planning. Last updated: 2026-04-03.*
