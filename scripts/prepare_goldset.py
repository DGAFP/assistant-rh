#!/usr/bin/env python3
"""Prepare a private goldset CSV and relink sources to corpus IDs."""

# ruff: noqa: E402,I001
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.goldset.prepare import main


if __name__ == "__main__":
    raise SystemExit(main())
