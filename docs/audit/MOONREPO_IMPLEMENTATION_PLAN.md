# Moonrepo Migration Implementation Plan

> **Statut historique.** Ce plan décrit la migration initiale et ne constitue
> plus une procédure active. Les références au candidat TypeScript retiré par
> [#440](https://github.com/DGAFP/assistant-rh/issues/440) sont conservées comme trace.

## Overview

This document provides a concrete, step-by-step implementation plan for the initial moonrepo migration. It covers **Phase 0 (Preparation)** and **Phase 1 (Extract Packages)** from the main migration plan.

**Branch**: `feat/moonrepo-migration`
**Base**: `main`
**Scope**: Foundational monorepo setup with package extraction

---

## Phase 0: Preparation

### Step 0.1: Install Moonrepo Tooling

**Prerequisites:**
- `proto` toolchain manager (required by moon)
- `moon` CLI

**Commands:**
```bash
# Install proto if not present
curl -fsSL https://moonrepo.dev/install/proto.sh | bash

# Install moon via proto
proto install moon

# Verify installation
moon --version
```

**Verification:**
```bash
moon --help
```

---

### Step 0.2: Create `.moon/` Directory Structure

**Files to create:**

```
.moon/
├── workspace.yml      # Workspace configuration
├── toolchain.yml      # Python/Node toolchains
└── tasks/
    └── python.yml     # Inherited Python tasks
```

**Implementation:**

```bash
mkdir -p .moon/tasks
```

---

### Step 0.3: Create `workspace.yml`

**File:** `.moon/workspace.yml`

```yaml
$v: 2

projects:
  # Explicit project mapping for apps
  sources:
    streamlit-ui: 'apps/streamlit-ui'
    mastra-api: 'apps/mastra-api'

  # Glob patterns for package discovery
  globs:
    - 'packages/*'

# Default project for local development
defaultProject: 'streamlit-ui'

# VCS configuration
vcs:
  provider: 'github'
  defaultBranch: 'main'

# Pipeline configuration
pipeline:
  cacheLifetime: '7 days'
  autoCleanCache: true
  installDependencies: true
```

**Note:** Initially, this will reference projects that don't exist yet. We'll create them in Phase 1.

---

### Step 0.4: Create `toolchain.yml`

**File:** `.moon/toolchain.yml`

```yaml
# Python toolchain
python:
  version: '3.12'
  packageManager: 'uv'
  syncProjectReferences: false

# TypeScript/Node toolchain (for future Mastra)
typescript:
  version: '5.0'
  packageManager: 'pnpm'
  syncProjectReferences: true
```

---

### Step 0.5: Create `tasks/python.yml`

**File:** `.moon/tasks/python.yml`

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
    command: 'mypy'
    inputs:
      - 'src/**/*.py'
      - 'pyproject.toml'
```

---

### Step 0.6: Update Root `pyproject.toml` for uv Workspace

**Goal:** Transform the root `pyproject.toml` into a workspace root that references all sub-projects.

**Current state analysis needed:**
- Read current `pyproject.toml`
- Identify shared dependencies
- Plan workspace member structure

**Target structure:**

```toml
[project]
name = "assistant-rh"
version = "0.1.0"
# Root project becomes a meta-package / dev shell

[tool.uv.workspace]
members = [
    "apps/streamlit-ui",
    "packages/rag-pipeline",
    "packages/data-engineering",
    "packages/shared-config",
]

[tool.uv.sources]
# Local package references
assistant-rh-rag-pipeline = { workspace = true }
assistant-rh-data-engineering = { workspace = true }
assistant-rh-shared-config = { workspace = true }
```

---

## Phase 1: Extract Packages

### Extraction Order

**Critical:** Extract in dependency order to avoid circular references:

1. **`shared-config`** (no dependencies) → extract first
2. **`rag-pipeline`** (depends on `shared-config`) → extract second
3. **`data-engineering`** (depends on `shared-config`) → extract third
4. **`streamlit-ui`** (depends on `rag-pipeline`, `shared-config`) → extract last

---

### Step 1.1: Create `packages/shared-config/`

**Purpose:** Shared configuration, database utilities, environment handling.

**Files to extract:**
- `src/rag_v3_clean/config.py` → shared config
- `src/rag_v3_clean/db_helpers.py` → database utilities
- Environment utilities (new)

**Structure:**
```
packages/shared-config/
├── moon.yml
├── pyproject.toml
├── src/
│   └── assistant_rh_shared_config/
│       ├── __init__.py
│       ├── config.py
│       ├── db.py
│       └── env.py
└── tests/
    └── test_config.py
```

**`moon.yml`:**
```yaml
language: 'python'
layer: 'library'
stack: 'backend'

project:
  title: 'Shared Config'
  description: 'Shared configuration and utilities for assistant-rh'

tasks:
  test:
    command: 'pytest tests/'
    options:
      cache: false
```

**`pyproject.toml`:**
```toml
[project]
name = "assistant-rh-shared-config"
version = "0.1.0"
description = "Shared configuration and utilities for assistant-rh"

dependencies = [
    "pydantic>=2.12.0",
    "python-dotenv>=1.2.0",
    "psycopg[binary]>=3.3.3",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/assistant_rh_shared_config"]
```

---

### Step 1.2: Create `packages/rag-pipeline/`

**Purpose:** Core RAG pipeline (extracted from `src/rag_v3_clean/`).

**Files to move:**
- All files from `src/rag_v3_clean/` → `packages/rag-pipeline/src/assistant_rh_rag_pipeline/`

**Structure:**
```
packages/rag-pipeline/
├── moon.yml
├── pyproject.toml
├── src/
│   └── assistant_rh_rag_pipeline/
│       ├── __init__.py
│       ├── pipeline.py
│       ├── query_processor.py
│       ├── retriever.py
│       ├── section_aggregator.py
│       ├── context_selector.py
│       ├── context_builder.py
│       ├── generator.py
│       ├── embedder.py
│       ├── reranker.py
│       ├── llm_client.py
│       ├── chat_logger.py
│       ├── feedback_analyzer.py
│       ├── admin.py
│       ├── citation_extractor.py
│       ├── models.py
│       └── prompts/
│           └── ...
└── tests/
    └── ...
```

**`moon.yml`:**
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

**`pyproject.toml`:**
```toml
[project]
name = "assistant-rh-rag-pipeline"
version = "0.1.0"
description = "Core RAG pipeline for assistant-rh"

dependencies = [
    "assistant-rh-shared-config",
    "openai>=2.28.0",
    "psycopg[binary]>=3.3.3",
    "pydantic>=2.12.0",
    "pandas>=2.3.3",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/assistant_rh_rag_pipeline"]
```

---

### Step 1.3: Create `packages/data-engineering/`

**Purpose:** Data ingestion pipeline (extracted from `src/data_engineering/`).

**Files to move:**
- All files from `src/data_engineering/` → `packages/data-engineering/src/assistant_rh_data_engineering/`

**Structure:**
```
packages/data-engineering/
├── moon.yml
├── pyproject.toml
├── src/
│   └── assistant_rh_data_engineering/
│       ├── __init__.py
│       ├── service_public/
│       │   ├── bronze.py
│       │   ├── silver.py
│       │   ├── gold.py
│       │   ├── qna_chunking.py
│       │   ├── pipeline.py
│       │   ├── db.py
│       │   └── config.py
│       └── ...
└── tests/
    └── ...
```

**`moon.yml`:**
```yaml
language: 'python'
layer: 'library'
stack: 'backend'

project:
  title: 'Data Engineering'
  description: 'Data ingestion pipeline for assistant-rh'

dependsOn:
  - 'shared-config'

tasks:
  test:
    command: 'pytest tests/'
    options:
      cache: false
```

---

### Step 1.4: Create `apps/streamlit-ui/`

**Purpose:** Streamlit application (extracted from root).

**Files to move:**
- `Home.py` → `apps/streamlit-ui/Home.py`
- `pages/` → `apps/streamlit-ui/pages/`
- `src/ui/` → `apps/streamlit-ui/src/assistant_rh_ui/`

**Structure:**
```
apps/streamlit-ui/
├── moon.yml
├── pyproject.toml
├── Home.py
├── pages/
│   ├── 01_Chatbot.py
│   ├── 02_Chat_Logs.py
│   └── ...
├── src/
│   └── assistant_rh_ui/
│       ├── __init__.py
│       ├── chatbot_llm.py
│       ├── chatbot_logging.py
│       └── ...
└── .streamlit/
    └── config.toml
```

**`moon.yml`:**
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

---

## Import Path Migration

### Before
```python
from src.rag_v3_clean import pipeline
from src.rag_v3_clean.config import Config
from src.ui import chatbot_llm
```

### After
```python
from assistant_rh_rag_pipeline import pipeline
from assistant_rh_shared_config import Config
from assistant_rh_ui import chatbot_llm
```

**Automated migration script:**
```bash
# Find and replace imports
find . -name "*.py" -exec sed -i '' \
  -e 's/from src\.rag_v3_clean/from assistant_rh_rag_pipeline/g' \
  -e 's/from src\.data_engineering/from assistant_rh_data_engineering/g' \
  -e 's/from src\.ui/from assistant_rh_ui/g' \
  {} \;
```

---

## Verification Checklist

### After Phase 0

- [ ] `moon --version` works
- [ ] `.moon/workspace.yml` exists and is valid
- [ ] `.moon/toolchain.yml` exists and is valid
- [ ] `.moon/tasks/python.yml` exists and is valid
- [ ] `moon projects` lists workspace (empty initially)

### After Phase 1

- [ ] `packages/shared-config/` created with proper structure
- [ ] `packages/rag-pipeline/` created with proper structure
- [ ] `packages/data-engineering/` created with proper structure
- [ ] `apps/streamlit-ui/` created with proper structure
- [ ] All imports updated
- [ ] `uv sync` succeeds at root
- [ ] `moon run :test` passes
- [ ] `streamlit run apps/streamlit-ui/Home.py` works

---

## Risk Mitigation

### Import Path Breakage

**Risk:** High — all imports will break initially.

**Mitigation:**
1. Create packages first, then update imports atomically
2. Use automated sed/find commands
3. Run tests after each package extraction
4. Keep backup of working state

### Circular Dependencies

**Risk:** Medium — may discover hidden circular imports.

**Mitigation:**
1. Extract in dependency order (shared-config first)
2. Use `mypy` to detect import cycles
3. If cycle found, create shared utility package

### uv Workspace Issues

**Risk:** Medium — uv workspace support is relatively new.

**Mitigation:**
1. Test `uv sync` after each package addition
2. Ensure all `pyproject.toml` files use hatchling
3. Verify `[tool.uv.sources]` resolves correctly

---

## Rollback Plan

If migration fails:

1. **Reset to main:**
   ```bash
   git checkout main
   git branch -D feat/moonrepo-migration
   ```

2. **Alternative: Keep packages, revert imports:**
   - Keep `packages/` structure
   - Revert import changes
   - Add `sys.path` manipulation for backward compatibility

---

## Out of Scope for This PR

The following are deferred to future PRs:

- **Phase 2:** Mastra project structure (`apps/mastra-api/`)
- **Phase 3:** GitHub Actions CI/CD updates
- **Phase 4:** Removal of the archived legacy source tree ✅ done during public-prep cleanup
- **Phase 5:** Separate deployments

---

## Timeline

| Step | Estimated Time | Dependencies |
|------|----------------|--------------|
| 0.1 - Install moon | 15 min | proto installed |
| 0.2-0.5 - Create .moon/ | 30 min | moon installed |
| 0.6 - Update pyproject.toml | 30 min | workspace members defined |
| 1.1 - shared-config | 1 hour | Phase 0 complete |
| 1.2 - rag-pipeline | 2 hours | shared-config ready |
| 1.3 - data-engineering | 1 hour | shared-config ready |
| 1.4 - streamlit-ui | 2 hours | all packages ready |
| Import migration | 1 hour | all packages created |
| Verification | 1 hour | all changes complete |
| **Total** | **~8-10 hours** | |

---

## Next Steps After This PR

1. **Phase 2:** Create `apps/mastra-api/` structure
2. **Phase 3:** Update GitHub Actions for moonrepo
3. **Phase 4:** Clean up archived code
4. **Phase 5:** Evaluate separate deployments

---

*Document created: 2026-04-06*
*Branch: feat/moonrepo-migration*
