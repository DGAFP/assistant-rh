from __future__ import annotations

from assistant_rh_rag_pipeline.config import CHUNK_TABLES, RetrievalConfig
from assistant_rh_rag_pipeline.models import RetrievedChunk
from assistant_rh_rag_pipeline.retriever import Retriever


def _chunk(*, chunk_id: str, score: float, table_source: str = "MATTE", section_id: str | None = None) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=chunk_id,
        text=f"chunk {chunk_id}",
        score=score,
        table_source=table_source,
        metadata={},
        section_id=section_id,
    )


def test_chunk_sort_key_is_deterministic_for_tied_scores():
    retriever = Retriever(RetrievalConfig(), dsn="unused")
    chunks = [
        _chunk(chunk_id="c2", score=0.5, table_source="B", section_id="s2"),
        _chunk(chunk_id="c1", score=0.5, table_source="B", section_id="s1"),
        _chunk(chunk_id="c3", score=0.5, table_source="A", section_id="s3"),
        _chunk(chunk_id="c0", score=0.7, table_source="Z", section_id="s0"),
    ]

    retriever._sort_chunks_deterministically(chunks)

    assert [c.chunk_id for c in chunks] == ["c0", "c3", "c1", "c2"]


def test_merge_cross_source_ranks_is_stable_when_source_iteration_differs():
    retriever = Retriever(RetrievalConfig(), dsn="unused")

    # Two sources with mirrored ranks so c1/c2 end with identical fused scores.
    src_a = [
        _chunk(chunk_id="c1", score=0.9, table_source="MATTE"),
        _chunk(chunk_id="c2", score=0.8, table_source="MATTE"),
    ]
    src_b = [
        _chunk(chunk_id="c2", score=0.95, table_source="MATTE"),
        _chunk(chunk_id="c1", score=0.85, table_source="MATTE"),
    ]

    merged_1 = retriever._merge_cross_source_ranks(
        {
            "source-z": src_b,
            "source-a": src_a,
        }
    )
    merged_2 = retriever._merge_cross_source_ranks(
        {
            "source-a": src_a,
            "source-z": src_b,
        }
    )

    assert [c.chunk_id for c in merged_1] == [c.chunk_id for c in merged_2]
    assert [c.chunk_id for c in merged_1] == ["c1", "c2"]


def test_rrf_normalization_rescales_to_unit_interval_and_keeps_metadata():
    retriever = Retriever(RetrievalConfig(), dsn="unused")

    # For source_count=2, theoretical max is 2 * (1 / 61).
    theoretical_max = 2 * (1.0 / 61.0)
    chunks = [
        _chunk(chunk_id="c-top", score=theoretical_max, table_source="MATTE"),
        _chunk(chunk_id="c-mid", score=theoretical_max / 2.0, table_source="MATTE"),
    ]

    normalized = retriever._normalize_merged_scores(chunks, source_count=2)

    assert normalized[0].chunk_id == "c-top"
    assert normalized[0].score == 1.0
    assert 0.0 <= normalized[1].score <= 1.0
    assert normalized[1].score == 0.5

    for chunk in normalized:
        assert "fused_rrf_score" in chunk.metadata
        assert chunk.metadata["merged_score_mode"] == "rrf_source_ceiling"


def test_retriever_ignores_missing_metadata_columns(monkeypatch):
    retriever = Retriever(RetrievalConfig(), dsn="unused")
    monkeypatch.setattr(
        retriever,
        "_get_table_columns",
        lambda _table_name: {"source_name", "section_path", "role", "thematique", "short_id"},
    )

    cols = retriever._select_existing_meta_cols(CHUNK_TABLES["service_public"])

    assert cols == ["source_name", "section_path", "role", "thematique"]


def test_retriever_resolves_section_id_from_service_public_short_id(monkeypatch):
    retriever = Retriever(RetrievalConfig(), dsn="unused")
    monkeypatch.setattr(
        retriever,
        "_get_table_columns",
        lambda _table_name: {"hash_id", "short_id", "section_path"},
    )

    section_sql = retriever._section_select_sql(CHUNK_TABLES["service_public"])

    assert "AS section_id" in section_sql
    assert "d.short_id = t.short_id" in section_sql
    assert "s.heading_path = t.section_path" in section_sql
