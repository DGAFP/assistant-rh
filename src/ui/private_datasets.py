"""Helpers for restricted datasets stored outside the public Git tree."""
from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Literal

DEFAULT_PRIVATE_DATASET_REPO = "DGAFP/assistant-rh-private-data"
DEFAULT_GOLDEN_BETA_SUBDIR = "golden_beta"
DEFAULT_CACHE_DIR = Path(".cache") / "assistant-rh" / "private-datasets"

SourceMode = Literal["auto", "local", "hf"]


class PrivateDatasetError(RuntimeError):
    """Raised when a restricted dataset cannot be resolved."""


@dataclass(frozen=True)
class GoldenBetaFiles:
    judge1_path: Path
    judge2_path: Path
    source: str


def _latest_matching(paths: Iterable[Path], pattern: str) -> Path | None:
    matches = sorted((path for path in paths if fnmatch.fnmatch(path.name, pattern)), reverse=True)
    return matches[0] if matches else None


def _resolve_local_golden_beta_files(local_dir: Path) -> GoldenBetaFiles | None:
    if not local_dir.exists():
        return None

    csv_paths = [path for path in local_dir.glob("*.csv") if path.is_file()]
    judge1 = _latest_matching(csv_paths, "golden_beta_judge1_*.csv")
    judge2 = _latest_matching(csv_paths, "golden_beta_judge2_*.csv")
    if not judge1 or not judge2:
        return None

    return GoldenBetaFiles(judge1_path=judge1, judge2_path=judge2, source="local")


def _import_huggingface_hub():
    try:
        from huggingface_hub import hf_hub_download, list_repo_files
    except ImportError as exc:  # pragma: no cover - dependency should exist in app env
        raise PrivateDatasetError(
            "huggingface_hub est requis pour récupérer les datasets privés. "
            "Installez les dépendances du projet ou définissez ASSISTANT_RH_GOLDEN_BETA_SOURCE=local."
        ) from exc
    return hf_hub_download, list_repo_files


def _latest_hf_file(files: Iterable[str], *, subdir: str, pattern: str) -> str | None:
    prefix = f"{subdir.strip('/')}/" if subdir else ""
    matches = [path for path in files if path.startswith(prefix) and fnmatch.fnmatch(Path(path).name, pattern)]
    return sorted(matches, reverse=True)[0] if matches else None


def _resolve_hf_golden_beta_files(
    *,
    repo_id: str,
    subdir: str,
    cache_dir: Path,
    revision: str | None,
    token: str | None,
) -> GoldenBetaFiles:
    hf_hub_download, list_repo_files = _import_huggingface_hub()

    try:
        files = list_repo_files(
            repo_id=repo_id,
            repo_type="dataset",
            revision=revision,
            token=token,
        )
    except Exception as exc:
        raise PrivateDatasetError(
            f"Impossible de lister le dataset privé Hugging Face {repo_id!r}. "
            "Vérifiez HF_TOKEN/HUGGINGFACE_HUB_TOKEN et les droits d'accès."
        ) from exc

    judge1 = _latest_hf_file(files, subdir=subdir, pattern="golden_beta_judge1_*.csv")
    judge2 = _latest_hf_file(files, subdir=subdir, pattern="golden_beta_judge2_*.csv")
    if not judge1 or not judge2:
        raise PrivateDatasetError(
            f"Dataset privé {repo_id!r} incomplet: fichiers golden_beta_judge1_*.csv "
            f"et golden_beta_judge2_*.csv attendus dans {subdir!r}."
        )

    try:
        judge1_path = hf_hub_download(
            repo_id=repo_id,
            repo_type="dataset",
            filename=judge1,
            revision=revision,
            token=token,
            cache_dir=cache_dir,
        )
        judge2_path = hf_hub_download(
            repo_id=repo_id,
            repo_type="dataset",
            filename=judge2,
            revision=revision,
            token=token,
            cache_dir=cache_dir,
        )
    except Exception as exc:
        raise PrivateDatasetError(
            f"Impossible de télécharger les fichiers golden beta depuis {repo_id!r}."
        ) from exc

    return GoldenBetaFiles(
        judge1_path=Path(judge1_path),
        judge2_path=Path(judge2_path),
        source=f"hf://datasets/{repo_id}/{subdir.strip('/')}",
    )


def resolve_golden_beta_files(
    *,
    local_dir: Path,
    cache_dir: Path | None = None,
    source: SourceMode | None = None,
    repo_id: str | None = None,
    subdir: str | None = None,
    revision: str | None = None,
    token: str | None = None,
) -> GoldenBetaFiles:
    """Resolve golden beta judge CSVs from local files or a private HF dataset.

    Default ``source=auto`` keeps the current private repository working from
    tracked/local CSVs while allowing a clean public checkout to fetch the files
    from Hugging Face when local restricted data is absent.
    """
    resolved_source = (source or os.getenv("ASSISTANT_RH_GOLDEN_BETA_SOURCE") or "auto").lower()
    if resolved_source not in {"auto", "local", "hf"}:
        raise PrivateDatasetError(
            "ASSISTANT_RH_GOLDEN_BETA_SOURCE doit être l'un des suivants : auto, local, hf."
        )

    if resolved_source in {"auto", "local"}:
        local_files = _resolve_local_golden_beta_files(local_dir)
        if local_files:
            return local_files
        if resolved_source == "local":
            raise PrivateDatasetError(
                f"Fichiers golden beta introuvables dans {local_dir}."
            )

    hf_repo_id = repo_id or os.getenv("ASSISTANT_RH_PRIVATE_DATASET_REPO") or DEFAULT_PRIVATE_DATASET_REPO
    hf_subdir = subdir or os.getenv("ASSISTANT_RH_GOLDEN_BETA_SUBDIR") or DEFAULT_GOLDEN_BETA_SUBDIR
    hf_revision = revision or os.getenv("ASSISTANT_RH_PRIVATE_DATASET_REVISION") or None
    hf_token = token or os.getenv("HF_TOKEN") or os.getenv("HUGGINGFACE_HUB_TOKEN") or None
    hf_cache_dir = cache_dir or Path(os.getenv("ASSISTANT_RH_PRIVATE_DATASET_CACHE_DIR") or DEFAULT_CACHE_DIR)

    return _resolve_hf_golden_beta_files(
        repo_id=hf_repo_id,
        subdir=hf_subdir,
        cache_dir=hf_cache_dir,
        revision=hf_revision,
        token=hf_token,
    )
