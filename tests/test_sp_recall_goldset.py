"""Service-Public recall regression guard, driven by the SP contractuels goldset.

Loads ``tests/conformance/queries.sp-recall-goldset.jsonl`` (one pytest case per
row) and asserts that, for each question, at least one of the expected fiche
``short_id`` values is present in the Service-Public semantic top-15.

This is an OPT-IN integration test: it needs a live PostgreSQL DSN *and* a
reachable Albert embedding service, so it is skipped unless explicitly enabled.
Enable it with::

    RUN_SP_RECALL=1 SP_RECALL_DSN="$SCW_POSTGRES_DSN_STAGING" \
        uv run --group dev python -m pytest tests/test_sp_recall_goldset.py -v

Design notes
------------
* The test connection sets ``ivfflat.probes = 20``. That is the *recommended
  serving* configuration — the live default (``probes = 1``) silently drops
  ~1/3 of true neighbours on the ivfflat-indexed Service-Public table (measured
  recall@10 ≈ 0.68), which is the migration regression this goldset documents.
  Asserting at probes=20 makes this a guard for the fixed state, not the broken
  one. If you point it at a DB whose index/probes are still mis-tuned, the
  MASKED_IVFFLAT rows are expected to fail — that is the regression signalling.
* Rows whose ``recall_status`` is ``ABSENT_EXACT`` (currently F527) are not
  retrievable by pure semantic search for the given phrasing even with an exact
  scan — a downstream query-rewrite / chunking issue, not the index. They are
  marked ``xfail`` so the suite stays green while keeping the case visible.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

_FIXTURE_PATH = Path(__file__).parent / "conformance" / "queries.sp-recall-goldset.jsonl"
_TOP_K = 15
_PROBES = 20

_DSN = os.getenv("SP_RECALL_DSN") or os.getenv("SCW_POSTGRES_DSN_STAGING") or os.getenv("SCW_POSTGRES_DSN_PROD")
_ENABLED = os.getenv("RUN_SP_RECALL") == "1" and bool(_DSN)

pytestmark = pytest.mark.skipif(
    not _ENABLED,
    reason="Set RUN_SP_RECALL=1 and a DSN (SP_RECALL_DSN / SCW_POSTGRES_DSN_*) to run the live SP recall guard.",
)


def _load_cases() -> list[dict]:
    """Load fixture rows. A malformed line or a row missing ``id`` raises, so a
    future fixture typo fails one clearly-named case instead of the whole file."""
    if not _FIXTURE_PATH.exists():
        raise FileNotFoundError(f"missing recall fixture: {_FIXTURE_PATH}")
    cases: list[dict] = []
    for lineno, line in enumerate(_FIXTURE_PATH.read_text(encoding="utf-8").splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{_FIXTURE_PATH.name}:{lineno}: malformed JSON ({exc})") from exc
        if "id" not in row:
            raise ValueError(f"{_FIXTURE_PATH.name}:{lineno}: row missing required 'id' key")
        cases.append(row)
    return cases


_CASES = _load_cases() if _FIXTURE_PATH.exists() else []
_CASE_IDS = [c.get("id", f"row{i}") for i, c in enumerate(_CASES)]


@pytest.fixture(scope="module")
def _conn():
    import psycopg

    with psycopg.connect(_DSN, connect_timeout=15) as conn:
        yield conn


@pytest.fixture(scope="module")
def _embedder():
    from assistant_rh_rag_pipeline.embedder import FallbackEmbedder

    return FallbackEmbedder()


def _semantic_short_ids(conn, vec: list[float], top_k: int = _TOP_K) -> list[str]:
    vec_literal = "[" + ",".join(f"{x:.6f}" for x in vec) + "]"
    with conn.cursor() as cur:
        cur.execute(f"SET LOCAL ivfflat.probes = {_PROBES}")
        cur.execute(
            """
            SELECT short_id
            FROM rag_chunks_service_public
            WHERE embedding_m3 IS NOT NULL
            ORDER BY embedding_m3 <=> %s::vector, hash_id
            LIMIT %s
            """,
            (vec_literal, top_k),
        )
        return [r[0] for r in cur.fetchall()]


@pytest.mark.parametrize("case", _CASES, ids=_CASE_IDS)
def test_sp_recall_goldset_case(case, _conn, _embedder):
    """One assertion per goldset row: an expected fiche must appear in SP top-15."""
    expected = case.get("expected_short_ids") or []
    assert expected, f"{case['id']}: fixture row has no expected_short_ids"

    if case.get("recall_status") == "ABSENT_EXACT":
        pytest.xfail(f"{case['id']}: known downstream recall miss (query-rewrite/chunking), not the ivfflat index")

    vec = _embedder.embed_query(case["query"])
    assert vec is not None, f"{case['id']}: embedding service returned no vector (Albert/BGE both down?)"

    retrieved = _semantic_short_ids(_conn, vec)
    assert any(sid in retrieved for sid in expected), (
        f"{case['id']}: none of {expected} in SP top-{_TOP_K} (probes={_PROBES}). "
        f"Got {retrieved[:5]}. If many rows fail here, the ivfflat index/probes are mis-tuned."
    )
