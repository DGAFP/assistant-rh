# Scalingo Buildpack Compatibility for Moonrepo Monorepo

## Problem Statement

After the Moonrepo migration, the Streamlit UI will move from root to `apps/streamlit-ui/`. Scalingo's Python buildpack (fork of Heroku's) has specific expectations:

**Buildpack Requirements:**
1. `uv.lock` (or `requirements.txt`, `Pipfile.lock`) at repository root ✅
2. `.python-version` at root ✅
3. App code accessible from root ❓

**Current Structure:**
```
/
├── Home.py          # Entry point
├── pages/           # Streamlit pages
├── Procfile         # web: streamlit run Home.py
├── pyproject.toml
├── uv.lock
└── .python-version
```

**After Migration:**
```
/
├── apps/
│   └── streamlit-ui/
│       ├── Home.py
│       ├── pages/
│       └── pyproject.toml
├── packages/
│   ├── rag-pipeline/
│   └── shared-config/
├── pyproject.toml    # Workspace root
├── uv.lock           # Workspace lock (contains all deps)
└── .python-version
```

## Solution Options

### Option 1: Procfile Path Change (Recommended)

Simply change the Procfile to point to the subdirectory:

```diff
- web: streamlit run Home.py --server.port $PORT --server.address 0.0.0.0
+ web: streamlit run apps/streamlit-ui/Home.py --server.port $PORT --server.address 0.0.0.0
```

**Pros:**
- Simple, no code changes
- Buildpack finds `uv.lock` at root (workspace lock)
- Streamlit runs from subdirectory

**Cons:**
- Need to verify Streamlit's page discovery works (`pages/` relative to Home.py)
- Working directory will be root, not `apps/streamlit-ui/`

**Verification needed:**
- Test that Streamlit finds `pages/` relative to `Home.py` location
- Test that imports from workspace packages work

### Option 2: Root Stub with Path Manipulation

Keep a minimal `Home.py` at root that delegates to the actual app:

```python
# /Home.py (root stub for Scalingo)
"""
Stub entry point for Scalingo deployment.
Delegates to the actual Streamlit UI in apps/streamlit-ui/.
"""
import sys
import os

# Add workspace packages to path
workspace_root = os.path.dirname(os.path.abspath(__file__))
ui_path = os.path.join(workspace_root, "apps", "streamlit-ui", "src")
sys.path.insert(0, ui_path)

# Import and run the actual app
exec(open("apps/streamlit-ui/Home.py").read())
```

**Pros:**
- Buildpack sees standard structure
- Can control path setup

**Cons:**
- Complex, fragile
- `exec()` may cause issues with Streamlit's module system
- Page discovery may still break

### Option 3: Symlink Strategy

Create symlinks at root pointing to subdirectory:

```bash
ln -s apps/streamlit-ui/Home.py Home.py
ln -s apps/streamlit-ui/pages pages
ln -s apps/streamlit-ui/.streamlit .streamlit
```

**Pros:**
- Transparent to buildpack
- No code changes

**Cons:**
- Symlinks may not work on Windows (if team develops there)
- Git may not preserve symlinks correctly
- Confusing to have two paths to same files

### Option 4: Multi-buildpack with Monorepo Support

Use a monorepo-aware buildpack before the Python buildpack:

```diff
  # .buildpacks
+ https://github.com/lstoll/heroku-buildpack-monorepo.git
  https://github.com/Scalingo/apt-buildpack.git
  https://github.com/Scalingo/python-buildpack.git
```

Set `APP_BASE=apps/streamlit-ui` environment variable.

**Pros:**
- Designed for monorepos
- Clean separation

**Cons:**
- Not officially supported by Scalingo
- May require testing and debugging
- Adds complexity

## Recommendation

**Primary: Option 1 (Procfile Path Change)**

This is the simplest approach and should work because:
1. `uv.lock` remains at root (workspace lock contains all dependencies)
2. Streamlit's `--server.address` already handles the port binding
3. Streamlit looks for `pages/` relative to the entry point file

**Implementation:**

```diff
# Procfile
- web: streamlit run Home.py --server.port $PORT --server.address 0.0.0.0
+ web: streamlit run apps/streamlit-ui/Home.py --server.port $PORT --server.address 0.0.0.0
```

**Fallback: Option 2 (Root Stub)**

If Option 1 doesn't work (e.g., page discovery fails), create a minimal stub:

```python
# /Home.py (root stub)
"""
Deployment stub for Scalingo.
The actual UI is in apps/streamlit-ui/.
"""
import os
import sys

# Change to the UI directory so Streamlit finds pages/
os.chdir("apps/streamlit-ui")

# Re-exec with the actual Home.py
sys.argv[0] = "Home.py"
exec(compile(open("Home.py").read(), "Home.py", "exec"))
```

## Testing Strategy

1. **Local test:**
   ```bash
   streamlit run apps/streamlit-ui/Home.py
   ```

2. **Verify page discovery:**
   - Check that `pages/01_Chatbot.py` is found
   - Verify imports from workspace packages work

3. **Scalingo test:**
   - Deploy to a staging app
   - Verify build succeeds
   - Check runtime logs

## Files to Modify

| File | Change |
|------|--------|
| `Procfile` | Update path to `apps/streamlit-ui/Home.py` |
| `apps/streamlit-ui/.streamlit/config.toml` | May need to adjust for relative paths |
| `apps/streamlit-ui/Home.py` | May need path adjustments for page imports |

## Rollback Plan

If deployment fails:
1. Revert Procfile to original
2. Keep `Home.py` and `pages/` at root temporarily
3. Investigate failure cause

---

*Document created: 2026-04-06*
*Branch: feat/moonrepo-migration*
