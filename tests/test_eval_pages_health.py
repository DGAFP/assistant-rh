"""
Cleanup des pages d'éval : plus de dépendances au schéma mort.

Vérifie via les sources (les pages ne s'importent pas sans session Streamlit)
que les pages actives ne référencent plus les tables/colonnes disparues des
bases actuelles, et que les pages obsolètes sont bien hors de `pages/`.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PAGES_DIR = REPO_ROOT / "apps" / "streamlit-ui" / "pages"
ARCHIVE_DIR = REPO_ROOT / "apps" / "streamlit-ui" / "archive"


def _page_source(name: str) -> str:
    return (PAGES_DIR / name).read_text(encoding="utf-8")


def test_goldset_explorer_uses_current_eval_schema() -> None:
    source = _page_source("06_Goldset_Explorer.py")
    assert "FROM goldset_runs" not in source
    assert "rag_quality_eval_items" in source


def test_pipeline_evaluation_does_not_select_dead_embedding_column() -> None:
    assert "embedding_albert" not in _page_source("09_Pipeline_Evaluation.py")


def test_no_active_page_queries_goldset_runs() -> None:
    # "FROM goldset_runs" cible les requêtes SQL ; une mention en docstring est légitime.
    offenders = [p.name for p in PAGES_DIR.glob("*.py") if "FROM goldset_runs" in p.read_text(encoding="utf-8")]
    assert not offenders, f"Pages requêtant la table morte goldset_runs : {offenders}"


def test_dead_pages_are_archived_not_served() -> None:
    assert not (PAGES_DIR / "07_Eval_Comparison.py").exists()
    assert not (PAGES_DIR / "11_Golden_Beta_Analysis.py").exists()
    assert (ARCHIVE_DIR / "07_Eval_Comparison.py").exists()
    assert (ARCHIVE_DIR / "11_Golden_Beta_Analysis.py").exists()
