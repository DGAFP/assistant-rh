#!/usr/bin/env python3
"""Strip execution outputs and counts from Jupyter notebooks.

This keeps exploratory notebooks safe to commit by removing rendered output,
including accidental logs of local secrets or database URLs.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Iterable

DEFAULT_ROOTS = (Path("notebooks"), Path("scripts"))


def iter_notebooks(paths: Iterable[str]) -> list[Path]:
    roots = [Path(path) for path in paths] or list(DEFAULT_ROOTS)
    notebooks: set[Path] = set()

    for path in roots:
        if path.is_dir():
            notebooks.update(path.rglob("*.ipynb"))
        elif path.suffix == ".ipynb" and path.exists():
            notebooks.add(path)

    return sorted(notebooks)


def strip_notebook(path: Path, *, fix: bool) -> bool:
    original_text = path.read_text(encoding="utf-8")
    try:
        data = json.loads(original_text)
    except json.JSONDecodeError:
        return False

    for cell in data.get("cells") or []:
        if cell.get("cell_type") != "code":
            continue
        cell["outputs"] = []
        cell["execution_count"] = None

    new_text = json.dumps(data, ensure_ascii=False, indent=1) + "\n"
    changed = new_text != original_text
    if changed and fix:
        path.write_text(new_text, encoding="utf-8")

    return changed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fix", action="store_true", help="Rewrite notebooks in place")
    parser.add_argument("paths", nargs="*", help="Notebook paths to check; defaults to notebooks/ and scripts/")
    args = parser.parse_args()

    dirty = []
    for path in iter_notebooks(args.paths):
        if strip_notebook(path, fix=args.fix):
            dirty.append(path)

    if not dirty:
        return 0

    action = "Stripped" if args.fix else "Need stripping"
    for path in dirty:
        print(f"{action}: {path}")
    return 0 if args.fix else 1


if __name__ == "__main__":
    raise SystemExit(main())
