#!/usr/bin/env python3
"""Download Golden Beta judge CSVs from the private Hugging Face dataset."""
from __future__ import annotations

import argparse
import os
import shutil
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
    parser.add_argument("--output-dir", type=Path, default=Path("data/golden_beta"))
    parser.add_argument("--token", default=os.getenv("HF_TOKEN") or os.getenv("HUGGINGFACE_HUB_TOKEN"))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    try:
        files = resolve_golden_beta_files(
            local_dir=args.output_dir,
            cache_dir=args.cache_dir,
            source="hf",
            repo_id=args.repo_id,
            subdir=args.subdir,
            revision=args.revision,
            token=args.token,
        )
    except PrivateDatasetError as exc:
        raise SystemExit(str(exc)) from exc

    args.output_dir.mkdir(parents=True, exist_ok=True)
    copied = []
    for source_path in (files.judge1_path, files.judge2_path):
        target_path = args.output_dir / source_path.name
        shutil.copy2(source_path, target_path)
        copied.append(target_path)

    print(f"source: {files.source}")
    for path in copied:
        print(f"downloaded: {path} ({path.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
