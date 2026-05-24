import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.ui.private_datasets import PrivateDatasetError, resolve_golden_beta_files


def _write_csv(path: Path) -> None:
    path.write_text("question_id,question\n1,Test\n", encoding="utf-8")


def test_resolve_golden_beta_prefers_local_files(tmp_path: Path) -> None:
    data_dir = tmp_path / "golden_beta"
    data_dir.mkdir()
    judge1 = data_dir / "golden_beta_judge1_20260218_0859.csv"
    judge2 = data_dir / "golden_beta_judge2_20260217_2354.csv"
    _write_csv(judge1)
    _write_csv(judge2)

    files = resolve_golden_beta_files(local_dir=data_dir, source="auto")

    assert files.source == "local"
    assert files.judge1_path == judge1
    assert files.judge2_path == judge2


def test_resolve_golden_beta_local_mode_requires_files(tmp_path: Path) -> None:
    with pytest.raises(PrivateDatasetError, match="introuvables"):
        resolve_golden_beta_files(local_dir=tmp_path, source="local")


def test_resolve_golden_beta_rejects_invalid_source(tmp_path: Path) -> None:
    with pytest.raises(PrivateDatasetError, match="auto, local, hf"):
        resolve_golden_beta_files(local_dir=tmp_path, source="bad")  # type: ignore[arg-type]
