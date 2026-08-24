from __future__ import annotations

from assistant_rh_rag_pipeline import retriever as retriever_module
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


def test_retriever_falls_back_for_null_migrated_service_public_section_id(monkeypatch):
    retriever = Retriever(RetrievalConfig(), dsn="unused")
    monkeypatch.setattr(
        retriever,
        "_get_table_columns",
        lambda _table_name: {"hash_id", "short_id", "section_path", "section_id"},
    )

    section_sql = retriever._section_select_sql(CHUNK_TABLES["service_public"])

    assert "COALESCE" in section_sql
    assert "t.section_id" in section_sql
    assert "d.short_id = t.short_id" in section_sql


def test_retriever_exposes_document_metadata_from_short_id(monkeypatch):
    retriever = Retriever(RetrievalConfig(), dsn="unused")
    monkeypatch.setattr(
        retriever,
        "_get_table_columns",
        lambda _table_name: {"hash_id", "short_id", "section_path"},
    )

    doc_meta_sql = retriever._document_meta_select_sql(CHUNK_TABLES["service_public"])

    assert "t.short_id AS doc_short_id" in doc_meta_sql
    assert "SELECT d.title" in doc_meta_sql
    assert "SELECT d.source_url" in doc_meta_sql
    assert "WHERE d.short_id = t.short_id" in doc_meta_sql


def test_heading_match_score_rewards_exact_and_near_title_matches():
    retriever = Retriever(RetrievalConfig(), dsn="unused")

    exact = retriever._heading_match_score(
        "Supplément familial de traitement (SFT) dans la fonction publique",
        "",
        "Quelles sont les conditions pour recevoir le supplément familial de traitement ?",
    )
    near = retriever._heading_match_score(
        "Conditions d'attribution du supplément familial de traitement",
        "Supplément familial de traitement (SFT) dans la fonction publique > Conditions d'attribution",
        "Quelles sont les conditions pour recevoir le SFT ?",
    )
    unrelated = retriever._heading_match_score(
        "Compte épargne-temps",
        "Temps de travail > Compte épargne-temps",
        "Quelles sont les conditions pour recevoir le SFT ?",
    )

    assert exact == 1.0
    assert near > 0.55
    assert unrelated == 0.0


def test_merge_preserves_heading_search_contribution_and_determinism():
    retriever = Retriever(RetrievalConfig(), dsn="unused")
    chunk_result = _chunk(chunk_id="c2", score=0.8, table_source="Service-Public", section_id="s2")
    title_result = _chunk(chunk_id="title-sft", score=1.0, table_source="Service-Public", section_id="s1")
    title_result.metadata = {
        "retrieval_path": "heading",
        "heading_match_score": 1.0,
        "matched_heading": "Supplément familial de traitement (SFT) dans la fonction publique",
    }

    merged = retriever._merge_cross_source_ranks(
        {
            "rag_chunks_service_public": [chunk_result],
            "heading:rag_chunks_service_public": [title_result],
        }
    )
    retriever._normalize_merged_scores(merged, source_count=2)

    assert [chunk.chunk_id for chunk in merged] == ["title-sft", "c2"]
    assert merged[0].metadata["retrieval_path"] == "heading"
    assert merged[0].metadata["heading_search"] is True
    assert merged[0].metadata["heading_match_score"] == 1.0
    assert merged[0].metadata["score_source"] == "heading:rag_chunks_service_public"


def test_merge_marks_chunk_and_heading_path_when_heading_source_is_first():
    retriever = Retriever(RetrievalConfig(), dsn="unused")
    chunk_result = _chunk(chunk_id="same", score=0.8, table_source="Service-Public", section_id="s1")
    heading_result = _chunk(chunk_id="same", score=1.0, table_source="Service-Public", section_id="s1")
    heading_result.metadata = {
        "retrieval_path": "heading",
        "heading_match_score": 1.0,
    }

    merged = retriever._merge_cross_source_ranks(
        {
            "heading:rag_chunks_service_public": [heading_result],
            "rag_chunks_service_public": [chunk_result],
        }
    )

    assert len(merged) == 1
    assert merged[0].metadata["retrieval_path"] == "chunk+heading"
    assert merged[0].metadata["heading_search"] is True
    assert merged[0].metadata["heading_match_score"] == 1.0


def test_heading_search_filters_before_limiting_candidates(monkeypatch):
    captured: dict[str, object] = {}
    retriever = Retriever(RetrievalConfig(initial_top_k=3), dsn="unused")
    monkeypatch.setattr(
        retriever,
        "_get_table_columns",
        lambda _table_name: {"hash_id", "text", "section_id"},
    )
    monkeypatch.setattr(retriever, "_heading_match_score", lambda *_args: 1.0)

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, sql, params):
            captured["sql"] = sql
            captured["params"] = params
            return self

        def fetchall(self):
            return [
                {
                    "chunk_id": "late-chunk",
                    "chunk_text": "Late chunk",
                    "section_id": "section-1",
                    "heading": "Supplément familial de traitement",
                    "heading_path": "Famille > Supplément familial de traitement",
                }
            ]

    monkeypatch.setattr(retriever_module.psycopg, "connect", lambda *_args, **_kwargs: FakeConnection())

    chunks = retriever._search_table_headings(CHUNK_TABLES["service_public"], "supplément familial", top_k=3)

    sql = str(captured["sql"])
    candidate_cte = sql.split("candidate_chunks AS (", 1)[1].split("FROM candidate_chunks c", 1)[0]
    assert "FROM rag_chunks_service_public t" in candidate_cte
    assert "LIMIT" not in candidate_cte
    assert captured["params"] == ("supplément familial", "supplément familial", "supplément familial", 3)
    assert chunks[0].chunk_id == "late-chunk"
