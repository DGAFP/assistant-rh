"""Mapping cid chronique → URL de version du backfill #350 (sans DB)."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

from assistant_rh_data_engineering.jobs.legifrance_url_backfill import load_expected_urls

REPO_ROOT = Path(__file__).resolve().parents[1]
CLI_MAIN_PATH = REPO_ROOT / "apps" / "data-ingestion-cli" / "src" / "assistant_rh_data_ingestion_cli" / "main.py"
VERSION_ID = "LEGIARTI000046874572"
CHRONIQUE = "LEGIARTI000044423597"


def _load_cli_main():
    if "data_ingestion_cli_main" in sys.modules:
        return sys.modules["data_ingestion_cli_main"]
    spec = importlib.util.spec_from_file_location("data_ingestion_cli_main", CLI_MAIN_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["data_ingestion_cli_main"] = module
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def _write_document(documents_dir: Path, short_id: str, payload: dict) -> None:
    documents_dir.mkdir(parents=True, exist_ok=True)
    (documents_dir / f"{short_id}.document.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_load_expected_urls_builds_version_urls_and_skips_legacy(tmp_path: Path) -> None:
    documents_dir = tmp_path / "silver" / "documents"
    _write_document(
        documents_dir,
        CHRONIQUE,
        {
            "short_id": CHRONIQUE,
            "metadata": {"article_id": VERSION_ID, "cid": CHRONIQUE, "category": "CODE"},
        },
    )
    _write_document(
        documents_dir,
        "decret_test",
        {
            "short_id": "decret_test",
            "metadata": {"legacy_qna_source_name": "Décret test.txt"},
        },
    )
    _write_document(
        documents_dir,
        "sans_version",
        {
            "short_id": "LEGIARTI000000000001",
            "metadata": {"cid": "LEGIARTI000000000001", "category": "CODE"},
        },
    )

    expected, stats = load_expected_urls(tmp_path)

    assert expected == {CHRONIQUE: f"https://www.legifrance.gouv.fr/codes/article_lc/{VERSION_ID}"}
    assert stats == {"documents": 3, "legacy_skipped": 1, "missing_article_id": 1}


def test_load_expected_urls_respects_loda_route(tmp_path: Path) -> None:
    documents_dir = tmp_path / "silver" / "documents"
    _write_document(
        documents_dir,
        "LEGIARTI000006486629",
        {
            "short_id": "LEGIARTI000006486629",
            "metadata": {"article_id": "LEGIARTI000045662481", "cid": "LEGIARTI000006486629", "category": "DECRET"},
        },
    )

    expected, _ = load_expected_urls(tmp_path)

    assert expected["LEGIARTI000006486629"] == "https://www.legifrance.gouv.fr/loda/article_lc/LEGIARTI000045662481"


def test_cli_resolves_legifrance_url_backfill_and_preserves_args() -> None:
    cli = _load_cli_main()

    resolved = cli._resolve_command(["legifrance", "url-backfill", "--target-env", "staging", "--from-object-storage", "--check-only"])

    assert resolved is not None
    spec, job_args = resolved
    assert spec.module == "assistant_rh_data_engineering.jobs.legifrance_url_backfill"
    assert job_args == ["--target-env", "staging", "--from-object-storage", "--check-only"]
