"""Git revision guards shared by parity evidence recorders."""

from __future__ import annotations

import subprocess
from pathlib import Path

RUNTIME_PATHS = (
    "packages/rag-pipeline/src",
    "packages/shared-config/src",
    "uv.lock",
)


def get_git_sha(repo_root: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def assert_runtime_revision_compatible(repo_root: Path, runtime_git_sha: str, recorder_git_sha: str) -> None:
    dirty = subprocess.run(
        ["git", "status", "--porcelain", "--", *RUNTIME_PATHS],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if dirty:
        raise RuntimeError("Runtime paths have uncommitted changes; parity recording requires a clean runtime checkout")

    if runtime_git_sha == recorder_git_sha:
        return

    comparison = subprocess.run(
        ["git", "diff", "--quiet", runtime_git_sha, recorder_git_sha, "--", *RUNTIME_PATHS],
        cwd=repo_root,
        check=False,
    )
    if comparison.returncode == 1:
        raise RuntimeError(
            f"Recorder revision {recorder_git_sha} changes runtime paths relative to declared runtime {runtime_git_sha}"
        )
    if comparison.returncode != 0:
        raise RuntimeError(f"Cannot compare recorder revision {recorder_git_sha} with runtime {runtime_git_sha}")
