"""
Tests for issue #103 — MATTE source ingestion audit.

Scope :
- Lecture seule / offline par défaut.
- Pas de connexion PostgreSQL, pas d'appel réseau.
- Pas d'accès au notebook d'origine (`amelioration_matte.ipynb`) : on
  génère des notebooks synthétiques dans ``tmp_path`` pour tester le
  parsing et la détection d'absence.

Ces tests couvrent :
- ``parse_pdf_paths_from_notebook`` : extraction des chemins PDF
  déclarés dans la liste ``PDF_PATHS: List[Path] = [...]`` ;
- ``parse_env_example`` : détection des variables ``MATTE_*`` ;
- ``inspect_artifact`` : audit d'un JSONL local (lignes, hash_id uniques,
  texte vide, dimension d'embedding) ;
- ``build_report`` : assemblage du rapport ;
- ``run_db_readonly`` : refus des mots-clés d'écriture, exécution simulée ;
- CLI : ``--help``, ``--sql-only``, refus de ``--db-readonly`` sans
  ``MATTE_AUDIT_DSN``, refus de muter la base.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

# ---------------------------------------------------------------------------
# Localisation du script (utilise importlib pour ne pas modifier sys.path)
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "audit_matte_ingestion.py"
DOCS_PATH = REPO_ROOT / "docs" / "MATTE_SOURCE_INGESTION_AUDIT.md"


def _load_script_module() -> Any:
    """Charge le script d'audit comme module Python sans toucher sys.path
    globalement (l'audit peut être importé depuis plusieurs contextes).

    On enregistre le module dans ``sys.modules`` pour que ``dataclasses``
    puisse résoudre les annotations stringifiées (le script utilise
    ``from __future__ import annotations``).
    """
    spec = importlib.util.spec_from_file_location("audit_matte_ingestion", SCRIPT_PATH)
    if spec is None or spec.loader is None:  # pragma: no cover - script always exists
        raise RuntimeError(f"Impossible de charger {SCRIPT_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("audit_matte_ingestion", module)
    spec.loader.exec_module(module)
    return module


# Cache du module chargé (chargé une seule fois par session de tests).
_script_module: Any = None


@pytest.fixture(scope="module")
def audit_mod() -> Any:
    global _script_module
    if _script_module is None:
        _script_module = _load_script_module()
    return _script_module


# ---------------------------------------------------------------------------
# Helpers —fabrication de notebooks synthétiques
# ---------------------------------------------------------------------------


def _make_notebook(cells_source: List[str]) -> Dict[str, Any]:
    """Construit un faux .ipynb (JSON) avec les sources de cellules fournies."""
    return {
        "cells": [
            {
                "cell_type": "code",
                "execution_count": None,
                "metadata": {},
                "outputs": [],
                "source": [s],
            }
            for s in cells_source
        ],
        "metadata": {
            "kernelspec": {"display_name": "python3", "language": "python", "name": "python3"},
        },
        "nbformat": 4,
        "nbformat_minor": 5,
    }


def _write_notebook(path: Path, cells_source: List[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(_make_notebook(cells_source), ensure_ascii=False),
        encoding="utf-8",
    )


def _write_env_example(path: Path, lines: List[str]) -> None:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# Tests — parse_pdf_paths_from_notebook
# ---------------------------------------------------------------------------


class TestParsePdfPaths:
    def test_extracts_single_line_paths(self, audit_mod: Any, tmp_path: Path) -> None:
        nb = tmp_path / "amelio.ipynb"
        _write_notebook(
            nb,
            [
                "PDF_PATHS: List[Path] = [\n",
                "    Path('./data/in/temps_du_travail/A.pdf'),\n",
                "    Path('./data/in/temps_du_travail/B.pdf'),\n",
                "]\n",
            ],
        )
        assert audit_mod.parse_pdf_paths_from_notebook(nb) == [
            "./data/in/temps_du_travail/A.pdf",
            "./data/in/temps_du_travail/B.pdf",
        ]

    def test_extracts_double_quoted_paths(self, audit_mod: Any, tmp_path: Path) -> None:
        nb = tmp_path / "amelio.ipynb"
        _write_notebook(
            nb,
            [
                "PDF_PATHS: List[Path] = [\n",
                '    Path("./data/in/plan/Cadrage 2009.pdf"),\n',
                "]\n",
            ],
        )
        assert audit_mod.parse_pdf_paths_from_notebook(nb) == [
            "./data/in/plan/Cadrage 2009.pdf",
        ]

    def test_dedups_repeated_paths(self, audit_mod: Any, tmp_path: Path) -> None:
        nb = tmp_path / "amelio.ipynb"
        _write_notebook(
            nb,
            [
                "PDF_PATHS = [\n",
                "    Path('./a.pdf'),\n",
                "    Path('./a.pdf'),\n",
                "    Path('./b.pdf'),\n",
                "]\n",
            ],
        )
        result = audit_mod.parse_pdf_paths_from_notebook(nb)
        assert result == ["./a.pdf", "./b.pdf"]

    def test_ignores_non_pdf_paths(self, audit_mod: Any, tmp_path: Path) -> None:
        nb = tmp_path / "amelio.ipynb"
        _write_notebook(
            nb,
            [
                "PDF_PATHS = [\n",
                "    Path('./a.pdf'),\n",
                "    Path('./image.png'),\n",
                "    Path('./notes.txt'),\n",
                "]\n",
            ],
        )
        result = audit_mod.parse_pdf_paths_from_notebook(nb)
        assert result == ["./a.pdf"]

    def test_missing_notebook_returns_empty(self, audit_mod: Any, tmp_path: Path) -> None:
        assert audit_mod.parse_pdf_paths_from_notebook(tmp_path / "absent.ipynb") == []

    def test_ignores_markdown_cells(self, audit_mod: Any, tmp_path: Path) -> None:
        nb = tmp_path / "amelio.ipynb"
        # On injecte un Path() dans une cellule markdown, qui doit être ignorée.
        nb_data = _make_notebook(
            [
                "PDF_PATHS = [\n",
                "    Path('./keep.pdf'),\n",
            ]
        )
        nb_data["cells"].append(
            {
                "cell_type": "markdown",
                "metadata": {},
                "source": ["PDF_PATHS = [Path('./drop.pdf')]\n"],
            }
        )
        nb.write_text(json.dumps(nb_data), encoding="utf-8")
        assert audit_mod.parse_pdf_paths_from_notebook(nb) == ["./keep.pdf"]

    def test_real_amelioration_matte_notebook_parses_three_pdfs(self, audit_mod: Any) -> None:
        """Le notebook livré dans le repo doit parser les 3 PDF d'origine."""
        real_nb = REPO_ROOT / "scripts" / "amelioration_matte.ipynb"
        if not real_nb.exists():
            pytest.skip("Notebook amelioration_matte.ipynb absent — non testable sur cette base.")
        paths = audit_mod.parse_pdf_paths_from_notebook(real_nb)
        assert len(paths) == 3
        assert all("temps_du_travail" in p for p in paths)
        # Noms attendus (peuvent varier mais on garde au moins 1 match)
        joined = " | ".join(paths).lower()
        assert "cadrage" in joined
        assert "instruction" in joined
        assert "reglement" in joined

    def test_invalid_json_raises_value_error(self, audit_mod: Any, tmp_path: Path) -> None:
        nb = tmp_path / "broken.ipynb"
        nb.write_text("not a json", encoding="utf-8")
        with pytest.raises(ValueError, match="Impossible de parser"):
            audit_mod.parse_pdf_paths_from_notebook(nb)


# ---------------------------------------------------------------------------
# Tests — parse_env_example
# ---------------------------------------------------------------------------


class TestParseEnvExample:
    def test_extracts_all_keys(self, audit_mod: Any, tmp_path: Path) -> None:
        env = tmp_path / ".env.example"
        _write_env_example(
            env,
            [
                "# Comment",
                "MATTE_BASE_IN=./data/in",
                "MATTE_TABLE=rag_chunks_matte",
                "OTHER=value",
            ],
        )
        assert audit_mod.parse_env_example(env) == {
            "MATTE_BASE_IN",
            "MATTE_TABLE",
            "OTHER",
        }

    def test_missing_file_returns_empty(self, audit_mod: Any, tmp_path: Path) -> None:
        assert audit_mod.parse_env_example(tmp_path / "absent") == set()

    def test_handles_blank_lines_and_inline_comments(self, audit_mod: Any, tmp_path: Path) -> None:
        env = tmp_path / ".env.example"
        _write_env_example(
            env,
            [
                "",
                "# Section",
                "MATTE_FOO=bar",
                "MATTE_BAZ=qux  # inline",
            ],
        )
        assert audit_mod.parse_env_example(env) == {"MATTE_FOO", "MATTE_BAZ"}


# ---------------------------------------------------------------------------
# Tests — inspect_artifact
# ---------------------------------------------------------------------------


class TestInspectArtifact:
    def test_missing_artifact_marks_absent(self, audit_mod: Any, tmp_path: Path) -> None:
        finding = audit_mod.inspect_artifact(tmp_path / "absent.jsonl")
        assert finding.present is False
        assert finding.row_count is None

    def test_valid_artifact_counts_rows_and_unique_hashes(self, audit_mod: Any, tmp_path: Path) -> None:
        path = tmp_path / "art.jsonl"
        rows = [
            {
                "hash_id": f"h{i}",
                "text": f"text {i}",
                "embedding_m3": [0.0] * 4,
            }
            for i in range(5)
        ]
        _write_jsonl(path, rows)
        finding = audit_mod.inspect_artifact(path)
        assert finding.present is True
        assert finding.row_count == 5
        assert finding.unique_hash_id == 5
        assert finding.empty_text == 0
        assert finding.embedding_dim == 4

    def test_duplicate_hash_ids_flagged(self, audit_mod: Any, tmp_path: Path) -> None:
        path = tmp_path / "art.jsonl"
        rows = [
            {"hash_id": "h1", "text": "t1", "embedding_m3": [0.0] * 2},
            {"hash_id": "h1", "text": "t2", "embedding_m3": [0.0] * 2},  # duplicate
            {"hash_id": "h2", "text": "t3", "embedding_m3": [0.0] * 2},
        ]
        _write_jsonl(path, rows)
        finding = audit_mod.inspect_artifact(path)
        assert finding.row_count == 3
        assert finding.unique_hash_id == 2
        assert "doublons" in finding.note.lower()

    def test_empty_text_flagged(self, audit_mod: Any, tmp_path: Path) -> None:
        path = tmp_path / "art.jsonl"
        rows = [
            {"hash_id": "h1", "text": "non-empty", "embedding_m3": [0.0] * 2},
            {"hash_id": "h2", "text": "", "embedding_m3": [0.0] * 2},
            {"hash_id": "h3", "text": "   ", "embedding_m3": [0.0] * 2},
        ]
        _write_jsonl(path, rows)
        finding = audit_mod.inspect_artifact(path)
        assert finding.empty_text == 2
        assert "vide" in finding.note.lower()

    def test_chunk_text_alias_counted(self, audit_mod: Any, tmp_path: Path) -> None:
        path = tmp_path / "art.jsonl"
        rows = [
            {"hash_id": "h1", "chunk_text": "via chunk_text", "embedding_m3": [0.0] * 2},
        ]
        _write_jsonl(path, rows)
        finding = audit_mod.inspect_artifact(path)
        assert finding.row_count == 1
        assert finding.empty_text == 0


# ---------------------------------------------------------------------------
# Tests — build_report
# ---------------------------------------------------------------------------


def _make_fake_repo(
    tmp_path: Path,
    *,
    with_amelio: bool = True,
    with_extract: bool = False,
    with_ingestion: bool = False,
    pdf_paths: Optional[List[str]] = None,
    env_lines: Optional[List[str]] = None,
) -> Path:
    """Construit un repo minimal pour tester ``build_report``."""
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True, exist_ok=True)
    (repo / "data" / "out" / "chunked").mkdir(parents=True, exist_ok=True)

    if with_amelio:
        pdf_paths = pdf_paths or [
            "./data/in/temps_du_travail/sample.pdf",
        ]
        cells = ["PDF_PATHS = [\n"]
        for p in pdf_paths:
            cells.append(f"    Path('{p}'),\n")
        cells.append("]\n")
        _write_notebook(repo / "scripts" / "amelioration_matte.ipynb", cells)
    if with_extract:
        _write_notebook(repo / "scripts" / "extract_matte.ipynb", ["# extract\n"])
    if with_ingestion:
        _write_notebook(repo / "scripts" / "ingestion_matte.ipynb", ["# ing\n"])

    env = env_lines or [
        "MATTE_BASE_IN=./data/in/temps_partiel",
        "MATTE_BASE_OUT=./data/out/temps_partiel",
        "MATTE_AMELIORATION_CLEAN_JSONL=./data/out/chunked/matte.jsonl",
        "MATTE_AMELIORATION_IN_JSONL=./data/out/chunked/matte.jsonl",
        "MATTE_AMELIORATION_OUT_PARQUET=./data/out/matte.parquet",
        "MATTE_AMELIORATION_OUT_NPY=./data/out/matte.npy",
        "MATTE_AMELIORATION_OUT_JSONL_WITH_EMB=./data/out/matte_with_emb.jsonl",
        "MATTE_IN_JSONL_WITH_EMB=./data/out/matte_with_emb.jsonl",
        "MATTE_TABLE=rag_chunks_matte",
    ]
    _write_env_example(repo / ".env.example", env)
    return repo


class TestBuildReport:
    def test_detects_missing_notebooks(self, audit_mod: Any, tmp_path: Path) -> None:
        repo = _make_fake_repo(tmp_path, with_amelio=True, with_extract=False, with_ingestion=False)
        report = audit_mod.build_report(repo, db_readonly=False)
        paths = {n.path: n.present for n in report.notebooks}
        assert paths["scripts/amelioration_matte.ipynb"] is True
        assert paths["scripts/extract_matte.ipynb"] is False
        assert paths["scripts/ingestion_matte.ipynb"] is False
        assert any("STALE_NOTEBOOKS" in d for d in report.diagnostics)
        assert report.errors == []

    def test_reports_all_notebooks_present(self, audit_mod: Any, tmp_path: Path) -> None:
        repo = _make_fake_repo(
            tmp_path,
            with_amelio=True,
            with_extract=True,
            with_ingestion=True,
        )
        report = audit_mod.build_report(repo, db_readonly=False)
        assert all(n.present for n in report.notebooks)
        assert all("STALE_NOTEBOOKS" not in d for d in report.diagnostics)

    def test_extracts_pdf_paths(self, audit_mod: Any, tmp_path: Path) -> None:
        repo = _make_fake_repo(
            tmp_path,
            pdf_paths=[
                "./data/in/A.pdf",
                "./data/in/B.pdf",
            ],
        )
        report = audit_mod.build_report(repo, db_readonly=False)
        assert report.pdf_paths_declared == ["./data/in/A.pdf", "./data/in/B.pdf"]

    def test_emits_sql_statements(self, audit_mod: Any, tmp_path: Path) -> None:
        repo = _make_fake_repo(tmp_path)
        report = audit_mod.build_report(repo, db_readonly=False)
        names = {s.name for s in report.sql_statements}
        assert "coverage_embeddings" in names
        assert "canonical_columns" in names
        assert "duplicate_hash_ids" in names
        assert "duplicate_text" in names
        assert "section_fk_coverage" in names
        assert "empty_text" in names
        assert "indexes" in names
        assert "vector_columns_dim" in names
        # Toutes les requêtes doivent être read-only (filet de sécurité).
        for s in report.sql_statements:
            low = s.sql.lower()
            assert "insert " not in low, f"SQL {s.name} contient INSERT"
            assert "update " not in low, f"SQL {s.name} contient UPDATE"
            assert "delete " not in low, f"SQL {s.name} contient DELETE"
            assert "create " not in low, f"SQL {s.name} contient CREATE"
            assert "drop " not in low, f"SQL {s.name} contient DROP"
            assert "alter " not in low, f"SQL {s.name} contient ALTER"
            assert "truncate " not in low, f"SQL {s.name} contient TRUNCATE"

    def test_sql_uses_canonical_columns_and_table(self, audit_mod: Any, tmp_path: Path) -> None:
        repo = _make_fake_repo(tmp_path)
        report = audit_mod.build_report(repo, db_readonly=False)
        coverage = next(s for s in report.sql_statements if s.name == "coverage_embeddings")
        assert "rag_chunks_matte" in coverage.sql
        for col in ("embedding_m3", "embedding_bge_scw", "embedding_qwen3", "embedding_ctx", "embedding_bge"):
            assert col in coverage.sql, f"Colonne {col} manquante dans coverage_embeddings"

    def test_env_var_diagnostic_when_missing(self, audit_mod: Any, tmp_path: Path) -> None:
        repo = _make_fake_repo(
            tmp_path,
            env_lines=["MATTE_BASE_IN=./data/in"],  # très incomplet
        )
        report = audit_mod.build_report(repo, db_readonly=False)
        missing = [v.path for v in report.env_vars if not v.present]
        assert "MATTE_TABLE" in missing
        assert any("STALE_ENV_VARS" in d for d in report.diagnostics)

    def test_db_readonly_requires_dsn(self, audit_mod: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        repo = _make_fake_repo(tmp_path)
        monkeypatch.delenv("MATTE_AUDIT_DSN", raising=False)
        report = audit_mod.build_report(repo, db_readonly=True)
        assert any("MATTE_AUDIT_DSN" in e for e in report.errors)
        assert report.db_results == {}

    def test_artifacts_handled_when_present(self, audit_mod: Any, tmp_path: Path) -> None:
        repo = _make_fake_repo(tmp_path)
        _write_jsonl(
            repo / "data/out/chunked/matte_temps_travail_3pdf_clean.jsonl",
            [{"hash_id": "h1", "text": "abc", "embedding_m3": [0.0] * 4}],
        )
        report = audit_mod.build_report(repo, db_readonly=False)
        present = [a for a in report.artifacts if a.present]
        assert any(a.row_count == 1 for a in present)

    def test_artifacts_absent_are_non_blocking(self, audit_mod: Any, tmp_path: Path) -> None:
        repo = _make_fake_repo(tmp_path)
        report = audit_mod.build_report(repo, db_readonly=False)
        assert all(a.note.startswith("Artefact absent") for a in report.artifacts if not a.present)
        assert report.errors == []


# ---------------------------------------------------------------------------
# Tests — run_db_readonly (filet de sécurité)
# ---------------------------------------------------------------------------


class TestRunDbReadonly:
    def test_refuses_insert_keyword(self, audit_mod: Any) -> None:
        bad = audit_mod.SqlStatement(
            name="bad",
            description="bad",
            sql="INSERT INTO rag_chunks_matte DEFAULT VALUES",
        )
        # On passe un DSN bidon : la fonction doit court-circuiter grâce au
        # filtre de mots-clés d'écriture avant d'essayer de connecter.
        results = audit_mod.run_db_readonly(
            "postgresql://u:p@127.0.0.1:1/db",
            [bad],
        )
        assert "bad" in results
        assert "forbidden" in results["bad"]["error"].lower()

    def test_refuses_update_keyword(self, audit_mod: Any) -> None:
        bad = audit_mod.SqlStatement(
            name="bad2",
            description="bad",
            sql="UPDATE rag_chunks_matte SET text = ''",
        )
        results = audit_mod.run_db_readonly("postgresql://x", [bad])
        assert "forbidden" in results["bad2"]["error"].lower()

    def test_refuses_create_index(self, audit_mod: Any) -> None:
        bad = audit_mod.SqlStatement(
            name="bad3",
            description="bad",
            sql="CREATE INDEX foo ON rag_chunks_matte (hash_id)",
        )
        results = audit_mod.run_db_readonly("postgresql://x", [bad])
        assert "forbidden" in results["bad3"]["error"].lower()

    def test_refuses_drop(self, audit_mod: Any) -> None:
        bad = audit_mod.SqlStatement(
            name="bad4",
            description="bad",
            sql="DROP TABLE rag_chunks_matte",
        )
        results = audit_mod.run_db_readonly("postgresql://x", [bad])
        assert "forbidden" in results["bad4"]["error"].lower()

    def test_refuses_alter(self, audit_mod: Any) -> None:
        bad = audit_mod.SqlStatement(
            name="bad5",
            description="bad",
            sql="ALTER TABLE rag_chunks_matte ADD COLUMN foo TEXT",
        )
        results = audit_mod.run_db_readonly("postgresql://x", [bad])
        assert "forbidden" in results["bad5"]["error"].lower()

    def test_refuses_delete(self, audit_mod: Any) -> None:
        bad = audit_mod.SqlStatement(
            name="bad6",
            description="bad",
            sql="DELETE FROM rag_chunks_matte WHERE hash_id = 'x'",
        )
        results = audit_mod.run_db_readonly("postgresql://x", [bad])
        assert "forbidden" in results["bad6"]["error"].lower()

    def test_refuses_set_session_param(self, audit_mod: Any) -> None:
        bad = audit_mod.SqlStatement(
            name="bad7",
            description="bad",
            sql="SET hnsw.ef_search = 80",
        )
        results = audit_mod.run_db_readonly("postgresql://x", [bad])
        assert "forbidden" in results["bad7"]["error"].lower()

    def test_handles_no_psycopg(self, audit_mod: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        # Force l'import psycopg à échouer dans run_db_readonly en
        # remplaçant le builtins.__import__ pour cette fonction. Cela
        # vérifie que le mode read-only reste robuste si psycopg n'est
        # pas installé (ex: dans une CI qui n'a pas besoin de DB).
        import builtins

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "psycopg":
                raise ImportError("psycopg disabled in test")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        stmt = audit_mod.SqlStatement(
            name="x",
            description="x",
            sql="SELECT 1",
        )
        results = audit_mod.run_db_readonly("postgresql://x", [stmt])
        # run_db_readonly retourne un rapport global {"error": "..."} si
        # psycopg n'est pas importable. C'est le contrat de la fonction.
        assert "error" in results
        assert "psycopg" in results["error"].lower()

    def test_connection_error_isolated_per_statement(self, audit_mod: Any) -> None:
        # DSN bidon : psycopg.connect lèvera. La fonction doit remonter
        # l'erreur par statement sans planter.
        stmt = audit_mod.SqlStatement(name="s1", description="x", sql="SELECT 1")
        results = audit_mod.run_db_readonly("postgresql://nope:nope@127.0.0.1:1/db", [stmt])
        assert "s1" in results
        assert "error" in results["s1"]


# ---------------------------------------------------------------------------
# Tests — CLI
# ---------------------------------------------------------------------------


def _run_cli(
    audit_mod: Any,
    argv: List[str],
    capsys: pytest.CaptureFixture[str],
) -> tuple[int, str, str]:
    """Helper : appelle main() du script avec les bons args et capture
    stdout/stderr. Retourne (rc, stdout, stderr). Patche sys.argv pour
    respecter argparse."""
    saved_argv = sys.argv
    sys.argv = ["audit_matte_ingestion.py", *argv]
    try:
        rc = audit_mod.main()
    finally:
        sys.argv = saved_argv
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


class TestCliSqlOnly:
    def test_help(self, audit_mod: Any, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as exc:
            _run_cli(audit_mod, ["--help"], capsys)
        assert exc.value.code == 0

    def test_sql_only_outputs_json(self, audit_mod: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        repo = _make_fake_repo(tmp_path)
        rc, out, _ = _run_cli(audit_mod, ["--repo-root", str(repo), "--sql-only"], capsys)
        assert rc == 0
        assert out, "stdout doit contenir le rapport JSON"
        data = json.loads(out)
        assert data["canonical_table"] == "rag_chunks_matte"
        assert data["canonical_embed_col_albert"] == "embedding_m3"
        assert isinstance(data["sql_statements"], list) and data["sql_statements"]

    def test_sql_only_in_real_repo(self, audit_mod: Any, capsys: pytest.CaptureFixture[str]) -> None:
        """Exécute le CLI sur le worktree réel (pas un fake repo)."""
        rc, out, _ = _run_cli(audit_mod, ["--repo-root", str(REPO_ROOT), "--sql-only"], capsys)
        assert rc == 0
        data = json.loads(out)
        # Le notebook livré doit être détecté
        amelio = next(n for n in data["notebooks"] if n["path"] == "scripts/amelioration_matte.ipynb")
        assert amelio["present"] is True
        # Les deux notebooks manquants doivent être flaggués
        for n in data["notebooks"]:
            if n["path"] in (
                "scripts/extract_matte.ipynb",
                "scripts/ingestion_matte.ipynb",
            ):
                assert n["present"] is False
        # 3 PDF doivent être parsés
        assert len(data["pdf_paths_declared"]) == 3

    def test_format_markdown(self, audit_mod: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        repo = _make_fake_repo(tmp_path)
        rc, out, _ = _run_cli(
            audit_mod,
            ["--repo-root", str(repo), "--sql-only", "--format", "markdown"],
            capsys,
        )
        assert rc == 0
        assert "# Audit MATTE" in out
        assert "`rag_chunks_matte`" in out
        assert "## Requêtes SQL" in out

    def test_db_readonly_without_dsn_returns_error(
        self,
        audit_mod: Any,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("MATTE_AUDIT_DSN", raising=False)
        repo = _make_fake_repo(tmp_path)
        rc, out, _ = _run_cli(
            audit_mod,
            ["--repo-root", str(repo), "--db-readonly"],
            capsys,
        )
        # erreurs -> code 1
        assert rc == 1
        data = json.loads(out)
        assert any("MATTE_AUDIT_DSN" in e for e in data["errors"])

    def test_missing_repo_root_returns_error(self, audit_mod: Any, capsys: pytest.CaptureFixture[str]) -> None:
        rc, _, err = _run_cli(
            audit_mod,
            ["--repo-root", "/this/does/not/exist/at/all", "--sql-only"],
            capsys,
        )
        assert rc == 2
        assert "introuvable" in err.lower()


# ---------------------------------------------------------------------------
# Tests — propriétés globales du script
# ---------------------------------------------------------------------------


class TestScriptInvariants:
    def test_canonical_columns_match_runtime(self, audit_mod: Any) -> None:
        """Le script doit utiliser les colonnes confirmées par le retriever
        et le retriever ne lit QUE embedding_m3 pour matte (Albert/BGE-M3)."""
        # Lecture du code source pour s'assurer qu'on n'a pas dérivé
        src = SCRIPT_PATH.read_text(encoding="utf-8")
        assert 'CANONICAL_EMBED_COL_ALBERT = "embedding_m3"' in src
        assert 'CANONICAL_EMBED_COL_BGE = "embedding_bge_scw"' in src
        assert 'CANONICAL_TABLE = "rag_chunks_matte"' in src

    def test_no_db_module_import_at_module_level(self, audit_mod: Any) -> None:
        """psycopg ne doit pas être importé au chargement du module — l'audit
        doit fonctionner sans dépendance DB installée."""
        # Vérifier qu'aucun import psycopg top-level n'apparaît dans le source
        src = SCRIPT_PATH.read_text(encoding="utf-8")
        # Il ne doit y avoir AUCUN import psycopg au top-level du module.
        # On accepte un import lazy à l'intérieur de run_db_readonly.
        lazy_count = src.count("import psycopg")
        assert lazy_count <= 1, f"psycopg doit être lazy (trouvé {lazy_count} import(s) dans le source)"

    def test_audit_doc_exists(self) -> None:
        """Le document d'audit doit être livré avec la PR."""
        assert DOCS_PATH.exists(), "docs/MATTE_SOURCE_INGESTION_AUDIT.md manquant — la doc d'audit est un livrable obligatoire de l'issue #103."
        content = DOCS_PATH.read_text(encoding="utf-8")
        # Le document doit mentionner les éléments structurants
        for needle in [
            "Issue",
            "rag_chunks_matte",
            "embedding_m3",
            "amelioration_matte.ipynb",
            "extract_matte.ipynb",
            "ingestion_matte.ipynb",
            "read-only",
        ]:
            assert needle in content, f"Section {needle!r} manquante dans le doc d'audit"
