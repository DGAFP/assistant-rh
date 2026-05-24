from __future__ import annotations

import sys
from pathlib import Path

cwd = Path.cwd().resolve()
REPO_ROOT = cwd.parent if cwd.name == "scripts" else cwd
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.backfill_db_embeddings import main as run_backfill


if __name__ == "__main__":
    if "--config" not in sys.argv:
        sys.argv.extend(["--config", "config/legifrance_embedding_tables.json"])
    raise SystemExit(run_backfill())
