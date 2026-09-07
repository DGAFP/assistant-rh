from __future__ import annotations

import subprocess
import sys


def test_package_and_core_imports_do_not_wire_io() -> None:
    script = """
import sys
import assistant_rh_api
from assistant_rh_api.core import health

forbidden = {
    "fastapi",
    "psycopg",
    "httpx",
    "assistant_rh_api.handlers.app",
    "assistant_rh_api.db.health",
}
loaded = sorted(forbidden.intersection(sys.modules))
if loaded:
    raise SystemExit(f"side-effect imports: {loaded}")
assert health.HealthReport(db="ok", config_loaded=True).is_healthy
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
