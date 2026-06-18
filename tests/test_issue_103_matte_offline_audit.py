from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "audit_matte_ingestion.py"


@pytest.fixture(scope="module")
def audit_mod():
    spec = importlib.util.spec_from_file_location("audit_matte_ingestion", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("audit_matte_ingestion", module)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def write_notebook(path: Path, sources: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "cells": [{"cell_type": "code", "metadata": {}, "outputs": [], "source": sources}],
                "metadata": {},
                "nbformat": 4,
                "nbformat_minor": 5,
            }
        ),
        encoding="utf-8",
    )


def test_parse_pdf_paths_from_notebook_deduplicates_and_ignores_non_pdf(audit_mod, tmp_path) -> None:
    notebook = tmp_path / "amelioration_matte.ipynb"
    write_notebook(
        notebook,
        [
            "PDF_PATHS = [\n",
            "Path('./data/in/A.pdf'),\n",
            "Path('./data/in/A.pdf'),\n",
            "Path('./data/in/B.pdf'),\n",
            "Path('./data/in/not_pdf.txt'),\n",
            "]\n",
        ],
    )

    assert audit_mod.parse_pdf_paths_from_notebook(notebook) == ["./data/in/A.pdf", "./data/in/B.pdf"]


def test_parse_real_amelioration_notebook_when_present(audit_mod) -> None:
    notebook = REPO_ROOT / "scripts" / "amelioration_matte.ipynb"
    if not notebook.exists():
        pytest.skip("amelioration_matte.ipynb absent")

    paths = audit_mod.parse_pdf_paths_from_notebook(notebook)

    # TODO(review): relax to `>= 1` — `== 3` couples test to current notebook content.
    assert len(paths) == 3
    assert all(path.endswith(".pdf") for path in paths)


def test_build_report_flags_missing_referenced_notebooks(audit_mod, tmp_path) -> None:
    scripts = tmp_path / "scripts"
    write_notebook(scripts / "amelioration_matte.ipynb", ["PDF_PATHS = [Path('./source.pdf')]\n"])

    report = audit_mod.build_report(tmp_path)

    assert report["canonical_table"] == "rag_chunks_matte"
    assert report["pdf_paths_declared"] == ["./source.pdf"]
    assert "STALE_NOTEBOOKS" in report["diagnostics"]
    absent = [item["path"] for item in report["notebooks"] if not item["present"]]
    assert absent == ["scripts/extract_matte.ipynb", "scripts/ingestion_matte.ipynb"]


def test_sql_statements_are_select_only(audit_mod) -> None:
    statements = audit_mod.build_sql_statements()

    assert len(statements) == 5
    for statement in statements:
        sql = statement["sql"].strip().upper()
        assert sql.startswith("SELECT")
        # TODO(review): substring guard is a smoke check, not a security boundary — would miss `-- update later` comments.
        assert all(keyword not in sql for keyword in ["UPDATE ", "INSERT ", "DELETE ", "CREATE ", "DROP ", "ALTER "])


def test_canonical_embedding_columns_sql_lists_every_known_column(audit_mod) -> None:
    statements = {item["name"]: item["sql"] for item in audit_mod.build_sql_statements()}

    canonical_sql = statements["canonical_embedding_columns"]
    for column in audit_mod.KNOWN_EMBED_COLS:
        assert f"'{column}'" in canonical_sql, f"{column} missing from canonical_embedding_columns IN clause"


def test_parse_pdf_paths_handles_null_cells(audit_mod, tmp_path) -> None:
    notebook = tmp_path / "broken.ipynb"
    notebook.write_text(
        json.dumps({"cells": None, "metadata": {}, "nbformat": 4, "nbformat_minor": 5}),
        encoding="utf-8",
    )

    assert audit_mod.parse_pdf_paths_from_notebook(notebook) == []


def test_parse_pdf_paths_returns_empty_on_non_dict_payload(audit_mod, tmp_path) -> None:
    notebook = tmp_path / "list.ipynb"
    notebook.write_text(json.dumps([]), encoding="utf-8")

    assert audit_mod.parse_pdf_paths_from_notebook(notebook) == []


def test_cli_outputs_json_report(audit_mod, tmp_path, monkeypatch, capsys) -> None:
    scripts = tmp_path / "scripts"
    write_notebook(scripts / "amelioration_matte.ipynb", ["PDF_PATHS = [Path('./source.pdf')]\n"])
    monkeypatch.setattr("sys.argv", ["audit_matte_ingestion", "--repo-root", str(tmp_path)])

    assert audit_mod.main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["repo_root"] == str(tmp_path.resolve())
    assert payload["sql_statements"]
    assert payload["pdf_paths_declared"] == ["./source.pdf"]
    assert payload["notebooks"]


def test_cli_sql_only_omits_repo_inspection(audit_mod, tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setattr("sys.argv", ["audit_matte_ingestion", "--repo-root", str(tmp_path), "--sql-only"])

    assert audit_mod.main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert list(payload.keys()) == ["sql_statements"]
    assert payload["sql_statements"]
