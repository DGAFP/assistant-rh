from __future__ import annotations

import sys
from pathlib import Path

cwd = Path.cwd().resolve()
REPO_ROOT = cwd.parent if cwd.name == "scripts" else cwd
PYTHONPATH_ENTRIES = [
    REPO_ROOT / "packages/data-engineering/src",
    REPO_ROOT / "packages/shared-config/src",
]
for entry in reversed(PYTHONPATH_ENTRIES):
    entry_str = str(entry)
    if entry_str not in sys.path:
        sys.path.insert(0, entry_str)

from assistant_rh_data_engineering.jobs.embeddings_backfill import main as run_backfill  # noqa: E402

if __name__ == "__main__":
    if "--config" not in sys.argv:
        sys.argv.extend(["--config", "config/matte_embedding_tables.json"])
    raise SystemExit(run_backfill())
