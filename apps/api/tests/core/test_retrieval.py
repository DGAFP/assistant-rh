import asyncio
from dataclasses import FrozenInstanceError, replace

import pytest
from assistant_rh_api.core.errors import DatabaseFailure, DatabaseUnavailable, MinistryConfigurationError
from assistant_rh_api.core.errors.inference import InferenceFailure
from assistant_rh_api.core.models.inference import Embedding
from assistant_rh_api.core.models.rag_configuration import RetrievalConfig, SearchMode
from assistant_rh_api.core.models.retrieval import RawChunk, RetrievedChunk
from assistant_rh_api.core.pipeline.steps.retrieval import (
    Retriever,
    ScopedRetrievalError,
    fuse_hybrid,
    heading_match_score,
    merge_r2_pairs,
    merge_sources,
)
from assistant_rh_api.db.search_catalog import search_catalog
from assistant_rh_rag_pipeline.config import RetrievalConfig as LegacyConfig
from assistant_rh_rag_pipeline.models import RetrievedChunk as LegacyChunk
from assistant_rh_rag_pipeline.retriever import Retriever as LegacyRetriever

pytestmark = pytest.mark.anyio
SOURCES = tuple(table.source for table in search_catalog())


class Embeddings:
    async def embed(self, text):
        await asyncio.sleep(0)
        model = "albert" if text == "albert" else "bge_scaleway"
        return Embedding((1.0, 0.0), model, "albert" if model == "albert" else "scaleway", ())


class Search:
    def __init__(self, *, failures=(), reverse=False):
        self.calls = []
        self.failures = failures
        self.reverse = reverse

    async def search(self, request):
        self.calls.append(request)
        await asyncio.sleep(0.001 if (request.source == "dgafp") == self.reverse else 0)
        if (request.source, request.mode) in self.failures:
            raise DatabaseFailure()
        if request.mode == "heading":
            return ()
        return tuple(RawChunk(request.source, cid, "text", None, 0.5, rank, {}) for rank, cid in ((2, "b"), (1, "a")))

    async def hybrid_candidates(self, request):
        return await self.search(request), await self.search(replace(request, mode="lexical"))


@pytest.mark.parametrize("ministry", ["matte", "mso", "mi", "masa"])
async def test_ministry_replaces_config_and_overrides_and_forces_legal_hybrid(ministry):
    port = Search()
    result = await Retriever(port, {"albert": Embeddings()}, SOURCES).retrieve(
        "albert", RetrievalConfig(tables=("mi", "rgrh")), selected_ministry=ministry, tables=("dgafp_scw",)
    )
    assert result.sources == (ministry, "service_public", "dgafp")
    assert {call.source for call in port.calls} == {ministry, "service_public", "dgafp"}
    assert {(call.source, call.mode) for call in port.calls} == {
        (ministry, "vector"),
        (ministry, "heading"),
        ("service_public", "vector"),
        ("service_public", "heading"),
        ("dgafp", "vector"),
        ("dgafp", "lexical"),
    }


async def test_invalid_scope_fails_before_any_io():
    port = Search()
    with pytest.raises(MinistryConfigurationError):
        await Retriever(port, {"albert": Embeddings()}, SOURCES).retrieve("q", RetrievalConfig(), selected_ministry="unknown")
    with pytest.raises(ValueError):
        await Retriever(port, {"albert": Embeddings()}, SOURCES).retrieve("q", RetrievalConfig(tables=("missing",)), strict_table_errors=True)
    assert port.calls == []


async def test_concurrent_requests_keep_model_scope_config_and_results_separate():
    port = Search()
    retriever = Retriever(port, {"albert": Embeddings()}, SOURCES)
    config = RetrievalConfig()
    first, second = await asyncio.gather(
        retriever.retrieve("albert", config, selected_ministry="mso", top_k=3),
        retriever.retrieve("fallback", config, selected_ministry="mi", top_k=7),
    )
    assert first.embedding.model == "albert" and second.embedding.model == "bge_scaleway"
    assert first.sources == ("mso", "service_public", "dgafp")
    assert second.sources == ("mi", "service_public", "dgafp")
    assert config == RetrievalConfig()
    for call in port.calls:
        expected = 3 if call.query == "albert" else 7
        assert call.limit == expected * (2 if call.source == "dgafp" else 1)
        if call.mode == "vector":
            assert call.embedding_model == ("albert" if call.query == "albert" else "bge_scaleway")


async def test_order_does_not_depend_on_task_completion():
    config = RetrievalConfig(search_mode=SearchMode.HYBRID)
    runs = [await Retriever(Search(reverse=bool(i % 2)), {"albert": Embeddings()}, SOURCES).retrieve("albert", config) for i in range(10)]
    assert all(result == runs[0] for result in runs)
    assert [(c.table_source, c.chunk_id) for c in runs[0].chunks][:4] == [("DGAFP", "a"), ("MATTE", "a"), ("RGRH", "a"), ("Service-Public", "a")]


async def test_partial_hybrid_failure_discards_both_lanes_but_preserves_empty_source_ceiling():
    config = RetrievalConfig(tables=("dgafp", "rgrh"), search_mode=SearchMode.HYBRID)
    result = await Retriever(Search(failures={("dgafp", "lexical")}), {"albert": Embeddings()}, SOURCES).retrieve("q", config)
    assert {chunk.table_source for chunk in result.chunks} == {"RGRH"}
    assert result.chunks[0].score == 0.5  # failed legacy chunks lane still counts as empty
    assert [(f.source, f.lane) for f in result.failures] == [("dgafp", "chunks")]
    with pytest.raises(ScopedRetrievalError) as error:
        await Retriever(Search(failures={("dgafp", "lexical"), ("mso", "heading")}), {"albert": Embeddings()}, SOURCES).retrieve(
            "q", config, selected_ministry="mso"
        )
    assert [(f.source, f.lane) for f in error.value.failures] == [("dgafp", "chunks"), ("mso", "heading")]
    assert str(error.value) == "scoped_retrieval_failed"


async def test_embedding_failure_and_empty_pools_do_not_invent_fallback():
    class FailedEmbedding:
        async def embed(self, text):
            raise InferenceFailure(())

    port = Search()
    result = await Retriever(port, {"albert": FailedEmbedding()}, SOURCES).retrieve("q", RetrievalConfig(search_mode=SearchMode.LEXICAL))
    assert result.chunks == () and result.embedding_failed
    assert port.calls == []
    empty = await Retriever(port, {"albert": Embeddings()}, SOURCES).retrieve("q", RetrievalConfig(tables=()))
    assert empty.chunks == () and empty.failures == ()
    assert merge_sources({"empty": ()}) == ()


async def test_cancellation_propagates_and_joins_search_children():
    started = asyncio.Event()
    stopped = asyncio.Event()

    class WaitingSearch:
        async def search(self, request):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

    task = asyncio.create_task(Retriever(WaitingSearch(), {"albert": Embeddings()}, SOURCES).retrieve("q", RetrievalConfig(tables=("rgrh",))))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stopped.is_set()


async def test_result_metadata_is_deeply_detached():
    original = {"nested": [{"a": "before"}]}
    raw = RawChunk("matte", "a", "text", None, 1, 1, original)
    chunk = RetrievedChunk("a", "text", 1, "MATTE", original, None, "albert")
    original["nested"][0]["a"] = "after"
    assert raw.metadata["nested"][0]["a"] == chunk.metadata["nested"][0]["a"] == "before"
    with pytest.raises(TypeError):
        chunk.metadata["nested"][0]["a"] = "edit"
    with pytest.raises(FrozenInstanceError):
        chunk.score = 0


async def test_embedding_preference_selects_a_request_specific_gateway():
    from assistant_rh_api.core.models.rag_configuration import EmbeddingModel

    class Gateway:
        def __init__(self, model):
            self.model = model
            self.calls = []

        async def embed(self, text):
            self.calls.append(text)
            await asyncio.sleep(0)
            return Embedding((1.0, 0.0), self.model, "albert" if self.model == "albert" else "scaleway", ())

    albert, scaleway = Gateway("albert"), Gateway("bge_scaleway")
    search = Search()
    gateways = {"albert": albert, "bge_scaleway": scaleway}
    retriever = Retriever(search, gateways, SOURCES)
    gateways.clear()  # composition owns no mutable configuration in the core
    first, second = await asyncio.gather(
        retriever.retrieve("same question", RetrievalConfig(embedding_model=EmbeddingModel.ALBERT)),
        retriever.retrieve("same question", RetrievalConfig(embedding_model=EmbeddingModel.BGE_SCALEWAY)),
    )
    assert first.embedding.model == "albert" and second.embedding.model == "bge_scaleway"
    assert albert.calls == scaleway.calls == ["same question"]
    with pytest.raises(ValueError, match="gateway"):
        await Retriever(search, {}, SOURCES).retrieve("q", RetrievalConfig())


async def test_comparison_catalog_is_explicit_and_rejects_alias_collisions(monkeypatch):
    monkeypatch.setenv("DGAFP_COMPARE_TABLE", "should_not_read_environment")
    assert len(search_catalog()) == 7
    environment = {"DGAFP_COMPARE_TABLE": "legal_comparison"}
    tables = search_catalog(environment)
    environment["DGAFP_COMPARE_TABLE"] = "changed"
    assert tables[-1].source.name == "legal_comparison"
    for invalid in ("rag_chunks_dgafp", "injection; DROP TABLE rag_config", "public.legal", ""):
        with pytest.raises(ValueError):
            search_catalog({"DGAFP_COMPARE_TABLE": invalid})
    with pytest.raises(ValueError, match="duplicate"):
        Retriever(Search(), {}, (SOURCES[0], replace(SOURCES[1], name=SOURCES[0].name)))


@pytest.mark.parametrize("alpha", [0.0, 0.3, 0.5, 1.0])
async def test_hybrid_uses_finite_missing_rank_and_id_ties(alpha):
    vector = (RawChunk("dgafp", "a", "", None, 0.9, 1, {}),)
    lexical = (RawChunk("dgafp", "b", "", None, 4, 1, {}),)
    result = fuse_hybrid(vector, lexical, alpha=alpha, fetch_k=4)
    scores = {chunk.chunk_id: chunk.score for chunk in result}
    assert scores == {"a": alpha * (1 / 61) + (1 - alpha) * (1 / 64), "b": alpha * (1 / 64) + (1 - alpha) * (1 / 61)}
    if alpha == 0.5:
        assert [chunk.chunk_id for chunk in result] == ["a", "b"]


async def test_r2_keeps_best_summary_pair_before_cutoff_without_deduplicating_other_articles():
    chunks = tuple(RetrievedChunk(cid, "identical", 1, "DGAFP", {"cid": "law"}, None, "albert") for cid in ("law_r2s", "law_0", "law_1", "law_2"))
    assert [c.chunk_id for c in merge_r2_pairs(chunks, 3)] == ["law_r2s", "law_1", "law_2"]
    assert [c.chunk_id for c in merge_r2_pairs(tuple(replace(c, metadata={}) for c in chunks), 3)] == ["law_r2s", "law_0", "law_1"]


@pytest.mark.parametrize(
    "heading,path,query",
    [
        ("Congés", "Guide > Congés", "congés"),
        ("", "", "congés"),
        ("mobilité", "Guide", "le"),
        ("Temps partiel", "Temps de travail", "un agent peut-il travailler à temps partiel ?"),
        ("Formation professionnelle", "Congés > Formation", "congé de formation"),
        ("Salaire", "Rémunération", "mobilité"),
    ],
)
async def test_heading_gate_exact_legacy_parity(heading, path, query):
    assert heading_match_score(heading, path, query) == LegacyRetriever._heading_match_score(heading, path, query)


async def test_cross_source_fusion_matches_legacy_with_heading_duplicates_and_empty_lanes():
    def chunk(cid, score, publisher="MATTE", **metadata):
        return RetrievedChunk(cid, "text", score, publisher, metadata, "section", "albert")

    sources = {
        "rag_chunks_matte": (chunk("b", 0.7), chunk("a", 0.7)),
        "heading:rag_chunks_matte": (chunk("a", 1, heading_search=True, heading_match_score=1, retrieval_path="heading"),),
        "rag_chunks_dgafp": (chunk("a", 0.9, "DGAFP"),),
        "rag_chunks_rgrh": (),
    }
    legacy = LegacyRetriever(LegacyConfig(), dsn="unused")
    raw = {
        key: [
            LegacyChunk(c.chunk_id, c.text, c.score, c.table_source, dict(c.metadata), c.section_id, c.embedding_model_used)
            for c in sorted(chunks, key=legacy._chunk_sort_key)
        ]
        for key, chunks in sources.items()
    }
    expected = legacy._normalize_merged_scores(legacy._merge_cross_source_ranks(raw), source_count=len(raw))
    actual = merge_sources(sources)
    assert [(c.chunk_id, c.score, c.table_source, dict(c.metadata)) for c in actual] == [
        (c.chunk_id, c.score, c.table_source, c.metadata) for c in expected
    ]


@pytest.mark.parametrize("mode", list(SearchMode))
@pytest.mark.parametrize("ministry", [None, "mso"])
async def test_database_outage_is_reported_per_lane_or_raises_for_ministry(mode, ministry):
    class UnavailableSearch(Search):
        async def search(self, request):
            raise DatabaseUnavailable()

    retriever = Retriever(UnavailableSearch(), {"albert": Embeddings()}, SOURCES)
    config = RetrievalConfig(tables=("rgrh",), search_mode=mode)
    if ministry is None:
        result = await retriever.retrieve("albert", config)
        assert result.chunks == ()
        assert result.sources == ("rgrh",)
        assert result.embedding is not None and not result.embedding_failed
        assert [(failure.source, failure.lane) for failure in result.failures] == [("rgrh", "chunks")]
    else:
        with pytest.raises(ScopedRetrievalError) as caught:
            await retriever.retrieve("albert", config, selected_ministry=ministry)
        assert str(caught.value) == "scoped_retrieval_failed"
        assert [(failure.source, failure.lane) for failure in caught.value.failures] == [
            ("dgafp", "chunks"),
            ("mso", "chunks"),
            ("mso", "heading"),
            ("service_public", "chunks"),
            ("service_public", "heading"),
        ]
