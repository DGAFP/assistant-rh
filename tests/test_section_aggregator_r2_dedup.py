"""Dédup à l'agrégation des lignes d'index additives R2 (section_aggregator).

La ligne-résumé ``{cid}_r2s`` (rag_chunks_dgafp) porte le même chunk_text
authentique que le chunk article ``{cid}_0`` : quand le retrieval remonte les
deux, ils doivent fusionner en UNE section (double hit = signal chunk_count)
au lieu de consommer deux des 20 places du reranker avec un texte identique.
Les chunks positionnels (``_1``, ``_2``…) et les chunks d'autres articles
gardent leur clé propre — comportement historique inchangé.
"""

from __future__ import annotations

from assistant_rh_rag_pipeline.config import SectionAggregationConfig
from assistant_rh_rag_pipeline.models import RetrievedChunk
from assistant_rh_rag_pipeline.section_aggregator import SectionAggregator

CID = "LEGIARTI000044420769"
ARTICLE_TEXT = "Article L123-2\n\nL'agent contractuel ne peut occuper un autre emploi permanent à temps complet."
META = {"cid": CID, "number": "L123-2", "full_title": "Code général de la fonction publique", "category": "CODE", "url": "https://example"}


def _aggregator() -> SectionAggregator:
    return SectionAggregator(SectionAggregationConfig(enable_section_reranker=False), dsn="postgresql://unused/unused")


def _chunk(chunk_id: str, score: float, metadata: dict | None = None, table_source: str = "DGAFP") -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=chunk_id,
        text=ARTICLE_TEXT,
        score=score,
        table_source=table_source,
        metadata=dict(metadata if metadata is not None else META),
        section_id=None,
        embedding_model_used="albert",
    )


def test_summary_row_and_article_chunk_fuse_into_one_section() -> None:
    sections = _aggregator().aggregate(
        [
            _chunk(f"{CID}_0", 0.80),
            _chunk(f"{CID}_r2s", 0.70),
        ]
    )
    assert len(sections) == 1
    section = sections[0]
    # Le texte servi reste le texte juridique authentique.
    assert section.markdown == ARTICLE_TEXT
    assert section.metadata["chunk_count"] == 2  # double hit = signal d'agrégat
    assert section.metadata["cid"] == CID  # méta pill préservée (standalone)
    assert section.section_id is None


def test_summary_row_alone_serves_authentic_text() -> None:
    sections = _aggregator().aggregate([_chunk(f"{CID}_r2s", 0.70)])
    assert len(sections) == 1
    assert sections[0].markdown == ARTICLE_TEXT
    assert sections[0].metadata["number"] == "L123-2"


def test_positional_chunks_keep_their_own_sections() -> None:
    # Article multi-chunk hypothétique: _1 ne fusionne ni avec _0 ni avec _r2s.
    sections = _aggregator().aggregate(
        [
            _chunk(f"{CID}_0", 0.80),
            _chunk(f"{CID}_1", 0.75),
            _chunk(f"{CID}_r2s", 0.70),
        ]
    )
    assert len(sections) == 2
    counts = sorted(s.metadata["chunk_count"] for s in sections)
    assert counts == [1, 2]  # (_0 + _r2s) fusionnés, _1 seul


def test_no_fusion_across_table_sources() -> None:
    sections = _aggregator().aggregate(
        [
            _chunk(f"{CID}_0", 0.80, table_source="DGAFP"),
            _chunk(f"{CID}_r2s", 0.70, table_source="DGAFP (Scaleway)"),
        ]
    )
    assert len(sections) == 2  # jamais de fusion inter-source


def test_chunks_without_cid_keep_legacy_grouping() -> None:
    sections = _aggregator().aggregate(
        [
            _chunk("abc123", 0.8, metadata={"source_name": "fiche X"}),
            _chunk("def456", 0.7, metadata={"source_name": "fiche X"}),
        ]
    )
    assert len(sections) == 2  # comportement historique: 1 chunk standalone = 1 section
