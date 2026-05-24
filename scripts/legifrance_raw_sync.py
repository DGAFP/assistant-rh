from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

cwd = Path.cwd().resolve()
REPO_ROOT = cwd.parent if cwd.name == "scripts" else cwd
PYTHONPATH_ENTRIES = [
    REPO_ROOT,
    REPO_ROOT / "packages/data-engineering/src",
    REPO_ROOT / "packages/shared-config/src",
]
for entry in reversed(PYTHONPATH_ENTRIES):
    entry_str = str(entry)
    if entry_str not in sys.path:
        sys.path.insert(0, entry_str)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Synchronise uniquement le bronze/raw Légifrance depuis l'Object Storage."
    )
    parser.add_argument(
        "--lake-root",
        default="data/lake/legifrance",
        help="Racine locale du data lake.",
    )
    parser.add_argument(
        "--target-env",
        choices=["staging", "prod"],
        default="prod",
        help="Préfixe Object Storage cible.",
    )
    return parser


def main() -> int:
    from dotenv import load_dotenv

    from assistant_rh_data_engineering.utils.object_storage import ObjectStorageConfig, ScalewayObjectStorageSync

    load_dotenv(REPO_ROOT / ".env")
    args = build_parser().parse_args()
    lake_root = Path(args.lake_root)

    syncer = ScalewayObjectStorageSync(ObjectStorageConfig.from_env())
    destinations = syncer.download_medallion_root(
        lake_root,
        args.target_env,
        source_name="legifrance",
        include_layers=("bronze",),
    )

    print(
        json.dumps(
            {
                "status": "ok",
                "lake_root": str(lake_root),
                "target_env": args.target_env,
                "downloaded": destinations,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
