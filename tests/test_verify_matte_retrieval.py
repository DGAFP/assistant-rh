"""Offline unit tests for the MATTE retrieval verifier (issue #159).

No DB: only the pure gate-evaluation and markdown rendering are exercised.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "verify_matte_retrieval.py"


def _load_module():
    if "verify_matte_retrieval" in sys.modules:
        return sys.modules["verify_matte_retrieval"]
    spec = importlib.util.spec_from_file_location("verify_matte_retrieval", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["verify_matte_retrieval"] = module
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


mod = _load_module()


def _prod_like_report() -> dict:
    return {
        "schema": "public",
        "table": "rag_chunks_matte",
        "volumetrics": {
            "total": 959,
            "distinct_hash_id": 959,
            "duplicate_hash_id": 0,
            "distinct_text": 957,
            "empty_chunk_text": 0,
            "empty_text": 0,
        },
        "embeddings": {
            "embedding_m3": {"non_null": 959, "null": 0, "coverage_pct": 100.0},
            "embedding_bge_scw": {"non_null": 197, "null": 762, "coverage_pct": 20.54},
        },
        "chunk_linkage": {"linked": 197, "unlinked": 762},
        "documents": {
            "total": 44,
            "with_source_url": 15,
            "with_storage_path": 0,
            "with_markdown": 44,
            "with_chunks": 13,
            "without_chunks": 31,
        },
        "has_vector_index_on_canonical": False,
        "index_count": 1,
    }


def test_prod_canonical_gates_pass_despite_fallback_gap() -> None:
    # embedding_m3 is 100% so canonical gates pass even though bge_scw is 20.54%.
    exit_code, problems = mod.evaluate(_prod_like_report())
    assert exit_code == 0
    assert problems == []


def test_canonical_embedding_gap_is_blocking() -> None:
    report = _prod_like_report()
    report["embeddings"]["embedding_m3"] = {"non_null": 900, "null": 59, "coverage_pct": 93.85}
    exit_code, problems = mod.evaluate(report)
    assert exit_code == 1
    assert any("embedding_m3" in p and "59 NULL" in p for p in problems)


def test_duplicate_hash_and_empty_text_are_blocking() -> None:
    report = _prod_like_report()
    report["volumetrics"]["duplicate_hash_id"] = 3
    report["volumetrics"]["empty_chunk_text"] = 2
    exit_code, problems = mod.evaluate(report)
    assert exit_code == 1
    assert any("hash_id" in p for p in problems)
    assert any("chunk_text" in p for p in problems)


def test_empty_table_is_blocking() -> None:
    report = _prod_like_report()
    report["volumetrics"]["total"] = 0
    exit_code, problems = mod.evaluate(report)
    assert exit_code == 1
    assert any("vide" in p for p in problems)


def test_markdown_flags_missing_vector_index() -> None:
    md = mod.to_markdown(_prod_like_report(), [])
    assert "scan séquentiel" in md
    assert "20.54%" in md
    assert "Gates: OK" in md
