"""Tests du SET ivfflat.probes dans le funnel _exec_de_table.

Sans SET explicite, PostgreSQL sonde 1 liste IVFFLAT sur 100 (probes=1):
recall silencieusement amputé, constaté au goldset du 05/07/2026 (fiches gold
présentes en base mais absentes du pool de rerank).
"""

from __future__ import annotations

import pytest
from assistant_rh_rag_pipeline.config import CHUNK_TABLES, RetrievalConfig
from assistant_rh_rag_pipeline.retriever import Retriever


class FakeConnection:
    def __init__(self, statements: list[str]):
        self._statements = statements

    def __enter__(self):
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(self, sql: object, params: object = None):
        self._statements.append(" ".join(str(sql).split()))

        class _Result:
            @staticmethod
            def fetchall() -> list:
                return []

        return _Result()


@pytest.fixture
def capture(monkeypatch: pytest.MonkeyPatch):
    statements: list[str] = []
    monkeypatch.setattr(
        "assistant_rh_rag_pipeline.retriever.psycopg.connect",
        lambda *args, **kwargs: FakeConnection(statements),
    )
    return statements


def make_retriever(**config_kwargs) -> Retriever:
    return Retriever(RetrievalConfig(**config_kwargs), dsn="postgresql://unused")


def test_exec_de_table_sets_probes_before_the_query(capture: list[str]) -> None:
    retriever = make_retriever(ivfflat_probes=15)

    retriever._exec_de_table(CHUNK_TABLES["matte"], "SELECT 1", (), "albert")

    # L'introspection de colonnes passe par le même psycopg.connect: on
    # vérifie que le SET précède immédiatement la requête de recherche.
    idx = capture.index("SELECT 1")
    assert capture[idx - 1] == "SET ivfflat.probes = 15"


def test_exec_de_table_zero_probes_keeps_server_default(capture: list[str]) -> None:
    retriever = make_retriever(ivfflat_probes=0)

    retriever._exec_de_table(CHUNK_TABLES["matte"], "SELECT 1", (), "albert")

    assert not any(s.startswith("SET ivfflat.probes") for s in capture)
    assert capture[-1] == "SELECT 1"


def test_default_config_widens_funnel() -> None:
    config = RetrievalConfig()
    # probes=5: sweet spot (recall plein, moitié moins de bruit qu'à 15) —
    # ablation goldset du 06/07/2026.
    assert config.ivfflat_probes == 5
    assert config.initial_top_k == 30
