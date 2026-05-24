#!/usr/bin/env python3
"""Check that required private Hugging Face datasets are accessible."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.ui.private_datasets import DEFAULT_PRIVATE_DATASET_REPO, PrivateDatasetError, resolve_golden_beta_files

DEFAULT_SUBDIR = "golden_beta"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-id", default=os.getenv("ASSISTANT_RH_PRIVATE_DATASET_REPO", DEFAULT_PRIVATE_DATASET_REPO))
    parser.add_argument("--subdir", default=os.getenv("ASSISTANT_RH_GOLDEN_BETA_SUBDIR", DEFAULT_SUBDIR))
    parser.add_argument("--revision", default=os.getenv("ASSISTANT_RH_PRIVATE_DATASET_REVISION") or None)
    default_cache_dir = os.getenv(
        "ASSISTANT_RH_PRIVATE_DATASET_CACHE_DIR",
        ".cache/assistant-rh/private-datasets",
    )
    parser.add_argument("--cache-dir", type=Path, default=Path(default_cache_dir))
    parser.add_argument("--token", default=os.getenv("HF_TOKEN") or os.getenv("HUGGINGFACE_HUB_TOKEN"))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    try:
        files = resolve_golden_beta_files(
            local_dir=Path("/nonexistent/assistant-rh-golden-beta-local-cache"),
            cache_dir=args.cache_dir,
            source="hf",
            repo_id=args.repo_id,
            subdir=args.subdir,
            revision=args.revision,
            token=args.token,
        )
    except PrivateDatasetError as exc:
        raise SystemExit(str(exc)) from exc

    print(f"ok: {files.source}")
    print(f"judge1: {files.judge1_path.name} ({files.judge1_path.stat().st_size} bytes)")
    print(f"judge2: {files.judge2_path.name} ({files.judge2_path.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
