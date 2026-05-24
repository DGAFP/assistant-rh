#!/usr/bin/env python3
"""Upload golden beta judge CSVs to a private Hugging Face dataset repo."""
from __future__ import annotations

import argparse
import os
from pathlib import Path

DEFAULT_REPO_ID = "DGAFP/assistant-rh-private-data"
DEFAULT_SUBDIR = "golden_beta"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-id", default=os.getenv("ASSISTANT_RH_PRIVATE_DATASET_REPO", DEFAULT_REPO_ID))
    parser.add_argument("--source-dir", type=Path, default=Path("data/golden_beta"))
    parser.add_argument("--subdir", default=os.getenv("ASSISTANT_RH_GOLDEN_BETA_SUBDIR", DEFAULT_SUBDIR))
    parser.add_argument("--token", default=os.getenv("HF_TOKEN") or os.getenv("HUGGINGFACE_HUB_TOKEN"))
    parser.add_argument("--create-repo", action="store_true", help="Create the private dataset repo if it does not exist.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not args.token:
        raise SystemExit("HF_TOKEN or HUGGINGFACE_HUB_TOKEN is required.")
    if not args.source_dir.exists():
        raise SystemExit(f"Source directory not found: {args.source_dir}")

    from huggingface_hub import HfApi

    api = HfApi(token=args.token)
    if args.create_repo:
        api.create_repo(repo_id=args.repo_id, repo_type="dataset", private=True, exist_ok=True)

    csv_paths = sorted(args.source_dir.glob("golden_beta_judge*.csv"))
    if not csv_paths:
        raise SystemExit(f"No golden_beta_judge*.csv files found in {args.source_dir}")

    path_in_repo = args.subdir.strip("/")
    api.upload_folder(
        folder_path=args.source_dir,
        path_in_repo=path_in_repo,
        repo_id=args.repo_id,
        repo_type="dataset",
        allow_patterns=["golden_beta_judge*.csv"],
        commit_message="Upload golden beta judge CSVs",
    )

    hf_path = f"hf://datasets/{args.repo_id}"
    if path_in_repo:
        hf_path = f"{hf_path}/{path_in_repo}"
    print(f"uploaded {len(csv_paths)} files -> {hf_path}")


if __name__ == "__main__":
    main()
