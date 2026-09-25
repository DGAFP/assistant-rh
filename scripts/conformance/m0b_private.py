"""Fetch the unchanged M0b evidence from the private dataset, outside Git."""

from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path

from scripts.conformance.m0b_values import ROOT, digest, require

DATASET = "DGAFP/assistant-rh-private-data"
ARCHIVE_NAME = "m0b-full-companion.tar.gz"
DATASET_PATH = "conformance/m0b-full-20260917/" + ARCHIVE_NAME
CHECKSUMS = ROOT / "tests/conformance/companions/m0b-full-20260917/SHA256SUMS"


def private_directory(directory: Path) -> Path:
    """Resolve links and refuse captures/downloads inside any Git checkout."""
    directory = directory.resolve()
    require(not directory.is_relative_to(ROOT), "Private evidence must stay outside the Git checkout")
    existing = directory
    while not existing.exists():
        existing = existing.parent
    result = subprocess.run(["git", "-C", str(existing), "rev-parse", "--show-toplevel"], capture_output=True)
    require(result.returncode != 0, "Private evidence must stay outside every Git checkout")
    if directory.exists():
        require(directory.is_dir() and directory.stat().st_mode & 0o077 == 0, "Existing evidence directory must be private (0700)")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    return directory


def verify_archive(archive: Path) -> None:
    expected = {line.split()[1]: line.split()[0] for line in CHECKSUMS.read_text().splitlines()}[ARCHIVE_NAME]
    require(digest(archive) == expected, "Private archive checksum mismatch")


def download(directory: Path) -> Path:
    from huggingface_hub import HfApi, hf_hub_download

    token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_HUB_TOKEN")
    require(bool(token), "HF_TOKEN is required to read the private conformance dataset")
    destination = private_directory(directory)
    info = HfApi(token=token).dataset_info(DATASET)
    require(info.private is True, "Conformance dataset must be private")
    archive = Path(
        hf_hub_download(
            repo_id=DATASET,
            repo_type="dataset",
            filename=DATASET_PATH,
            revision=info.sha,
            token=token,
            local_dir=destination,
            cache_dir=destination / ".cache",
        )
    )
    archive.chmod(0o600)
    verify_archive(archive)
    return archive


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    try:
        archive = download(args.directory)
    except Exception:
        # Provider exceptions may include signed download URLs; keep CI logs safe.
        raise SystemExit("Private conformance download failed; check dataset access and archive checksum") from None
    print(archive)


if __name__ == "__main__":
    main()
