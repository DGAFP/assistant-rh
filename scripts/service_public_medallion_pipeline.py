from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
for path in (
    REPO_ROOT / "packages" / "shared-config" / "src",
    REPO_ROOT / "packages" / "data-engineering" / "src",
):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from assistant_rh_data_engineering.jobs.service_public_medallion import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
