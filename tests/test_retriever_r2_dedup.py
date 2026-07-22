"""Dédup PRÉCOCE des paires R2 au retriever (revue #332, round 2) : la paire
{cid}_0/{cid}_r2s ne doit pas consommer deux places du top_k — sur-échantillon
x2 puis fusion avant troncature, la place libérée profite au candidat suivant."""

from __future__ import annotations

from assistant_rh_rag_pipeline.models import RetrievedChunk
from assistant_rh_rag_pipeline.retriever import Retriever


def _chunk(chunk_id: str, cid: str | None = None, score: float = 0.9) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=chunk_id,
        text="t",
        score=score,
        table_source="rag_chunks_dgafp",
        metadata={"cid": cid} if cid else {},
    )


def test_merge_r2_pairs_keeps_best_and_frees_the_slot() -> None:
    chunks = [
        _chunk("A_r2s", "A", 0.95),
        _chunk("A_0", "A", 0.90),
        _chunk("B_1", "B", 0.85),
        _chunk("C_0", "C", 0.80),
        _chunk("D_0", "D", 0.75),
    ]
    merged = Retriever._merge_r2_pairs(chunks, top_k=3)
    # A_0 fusionné derrière A_r2s (mieux classé) ; la place libérée va à C_0.
    assert [c.chunk_id for c in merged] == ["A_r2s", "B_1", "C_0"]


def test_merge_r2_pairs_leaves_positional_chunks_alone() -> None:
    chunks = [_chunk("A_1", "A"), _chunk("A_2", "A"), _chunk("A_r2s", "A")]
    merged = Retriever._merge_r2_pairs(chunks, top_k=10)
    # _1/_2 ne font pas partie de la paire {_0,_r2s} : comportement inchangé.
    assert [c.chunk_id for c in merged] == ["A_1", "A_2", "A_r2s"]


def test_merge_r2_pairs_passthrough_without_cid_metadata() -> None:
    chunks = [_chunk("X"), _chunk("Y")]
    assert [c.chunk_id for c in Retriever._merge_r2_pairs(chunks, top_k=2)] == ["X", "Y"]
