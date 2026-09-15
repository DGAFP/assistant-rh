"""Executable differential conformance with synthetic inputs, not M0b replay."""

from __future__ import annotations

import asyncio
import copy
from collections.abc import Mapping
from dataclasses import asdict, fields, is_dataclass, replace
from unittest.mock import Mock

import pytest
from assistant_rh_api.core.errors import DatabaseConflict, DatabaseFailure, DatabaseUnavailable, InferenceFailure
from assistant_rh_api.core.models.context import AggregatedSection
from assistant_rh_api.core.models.inference import Attempt, RankedDocument, Reranking
from assistant_rh_api.core.models.rag_configuration import ContextBuildConfig, ContextMode, SectionAggregationConfig
from assistant_rh_api.core.models.retrieval import Document, LegalReference, RetrievedChunk, Section
from assistant_rh_api.core.pipeline.steps.aggregation import SectionAggregator
from assistant_rh_api.core.pipeline.steps.context_builder import ContextBuilder
from assistant_rh_api.core.pipeline.steps.context_formatting import format_for_prompt
from assistant_rh_rag_pipeline import config as legacy_config
from assistant_rh_rag_pipeline import models as legacy_models
from assistant_rh_rag_pipeline.context_builder import ContextBuilder as LegacyBuilder
from assistant_rh_rag_pipeline.section_aggregator import SectionAggregator as LegacyAggregator

pytestmark = pytest.mark.anyio


def plain(value):
    if is_dataclass(value):
        return {field.name: plain(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Mapping):
        return {key: plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(item) for item in value]
    return value


class Content:
    def __init__(self, section_rows=(), document_rows=(), references=()):
        self.section_rows = section_rows
        self.document_rows = document_rows
        self.reference_rows = references
        self.calls = []

    async def sections(self, ids):
        self.calls.append(("sections", ids))
        await asyncio.sleep(0)
        return tuple(row for row in self.section_rows if row.section_id in ids)

    async def documents(self, ids):
        self.calls.append(("documents", ids))
        await asyncio.sleep(0)
        return tuple(row for row in self.document_rows if row.doc_id in ids)

    async def references(self, numbers):
        self.calls.append(("references", numbers))
        await asyncio.sleep(0)
        return tuple(row for row in self.reference_rows if row.number in numbers)

    async def chunks(self, source, ids):
        raise AssertionError("C4 consumes retrieved chunks, never searches again")


class Reranker:
    def __init__(self, fallback=False):
        self.calls = []
        self.fallback = fallback

    async def rerank(self, query, documents, *, top_k=None):
        self.calls.append((query, documents, top_k))
        await asyncio.sleep(0)
        rows = self.rank(documents, top_k=top_k)
        return Reranking(tuple(RankedDocument(*row) for row in rows), (), self.fallback)

    def rank(self, documents, *, top_k=None):
        if self.fallback:
            return [(i, 1 - i * 0.001) for i in range(len(documents))][:top_k]
        return sorted(((i, (i % 3) / 2) for i in range(len(documents))), key=lambda row: (-row[1], row[0]))[:top_k]


def document_row(doc):
    return {
        "doc_id": doc.doc_id,
        "title": doc.title,
        "source_url": doc.url,
        "publisher": doc.publisher,
        "doc_markdown": doc.markdown,
        "token_count": doc.token_count,
    }


def section_row(section):
    doc = section.document
    return {
        "section_id": section.section_id,
        "heading": section.heading,
        "section_markdown": section.markdown,
        "heading_path": section.heading_path,
        "references_juridiques": plain(section.legal_references),
        "doc_id": section.doc_id,
        "doc_short_id": doc.short_id if doc else None,
        "doc_title": doc.title if doc else None,
        "doc_url": doc.url if doc else None,
        "doc_publisher": doc.publisher if doc else None,
        "doc_token_count": doc.token_count if doc else None,
        "doc_date": doc.updated_date if doc else None,
    }


def corpus():
    doc = Document("doc-a", "short-a", "Guide congés", "https://example.org/guide", "MATTE", "Document intégral " * 30, 120, "2026-01-01")
    sections = (
        Section("s-a", doc.doc_id, "Congés", "Guide > Congés", "Contenu complet " * 150, ({"number": "L1", "label": "Article"},), doc),
        Section("s-b", doc.doc_id, "Renouvellement", "Guide > Contrat", "Section B " * 10, ({"number": "L2"},), doc),
        Section("orphan", "missing-doc", "Orpheline", "", "Section sans document", None, None),
    )
    legal = {"cid": "CID", "number": "L1", "full_title": "Code", "title": "Article", "category": "CODE", "url": "https://example.org/CID"}
    chunks = (
        RetrievedChunk("c-a", "Extrait court", 0.8, "MATTE", {}, "s-a", "albert"),
        RetrievedChunk("c-b", "Extrait B", 0.8, "MATTE", {}, "s-b", "albert"),
        RetrievedChunk("c-a2", "Second extrait", 0.6, "MATTE", {}, "s-a", "albert"),
        RetrievedChunk("CID_0", "Texte authentique", 0.7, "DGAFP", legal, None, "albert"),
        RetrievedChunk("CID_r2s", "Texte authentique", 0.7, "DGAFP", legal, None, "albert"),
        RetrievedChunk("CID_1", "Deuxième partie", 0.7, "DGAFP", legal, None, "albert"),
        RetrievedChunk("CID_r2s", "Autre source", 0.7, "DGAFP (Scaleway)", legal, None, "bge_scaleway"),
        RetrievedChunk(
            "missing",
            "Fallback chunk",
            0.7,
            "Service-Public",
            {"source_document_id": "short-sp", "doc_title": "Fiche", "doc_url": "https://example.org/sp"},
            "missing-section",
            "albert",
        ),
        RetrievedChunk("orphan-c", "Extrait orphelin", 0.1, "MATTE", {}, "orphan", "albert"),
    )
    return chunks, Content(sections, (doc,), (LegalReference("L1", "CID", "https://example.org/CID", "Code"),))


@pytest.mark.parametrize(
    "enabled,query,input_k,top_k,fallback",
    [
        (False, "question", 20, 20, False),
        (True, None, 20, 20, False),
        (True, "question", 2, 3, False),
        (True, "question", 20, 4, False),
        (True, "question", 20, 20, True),
    ],
)
async def test_aggregation_matches_retained_runtime(enabled, query, input_k, top_k, fallback):
    chunks, store = corpus()
    before = plain(chunks)
    config = SectionAggregationConfig(enable_section_reranker=enabled, rerank_input_k=input_k, section_rerank_top_k=top_k)
    reranker = Reranker(fallback)
    legacy = LegacyAggregator(legacy_config.SectionAggregationConfig(**asdict(config)), dsn="unused")
    legacy._fetch_sections = Mock(return_value={row.section_id: section_row(row) for row in store.section_rows})
    legacy._reranker = Mock()
    legacy._reranker.rerank.side_effect = lambda query, texts, top_k: reranker.rank(texts, top_k=top_k)
    expected = legacy.aggregate_with_diagnostics([legacy_models.RetrievedChunk(**plain(c)) for c in chunks], query)
    actual = await SectionAggregator(config, store, reranker).aggregate_with_diagnostics(chunks, query)
    assert plain(actual.sections) == plain(expected.sections)
    for name in plain(expected.diagnostics):
        assert plain(getattr(actual.diagnostics, name)) == plain(getattr(expected.diagnostics, name))
    assert actual.diagnostics.reranker_fallback == fallback
    assert plain(chunks) == before
    assert store.calls == [("sections", ("s-a", "s-b", "missing-section", "orphan"))]
    if reranker.calls:
        assert list(reranker.calls[0][1]) == legacy._reranker.rerank.call_args.args[1]
        assert len(reranker.calls[0][1]) <= max(input_k, top_k)


@pytest.mark.parametrize("mode", [ContextMode.STANDARD, ContextMode.WIDE])
@pytest.mark.parametrize(
    "case", ["full", "budget", "missing", "empty-document", "zero-tokens", "unknown-tokens", "ties", "triangulation", "refs-cap", "no-refs-budget"]
)
async def test_context_matches_retained_runtime(mode, case):
    chunks, store = corpus()
    sections = list(await SectionAggregator(SectionAggregationConfig(enable_section_reranker=False), store, Reranker()).aggregate(chunks))
    kwargs = {"context_mode": mode}
    if case == "budget":
        kwargs.update(token_budget=4, token_budget_wide=5, triangulation_sections=0)
    if case == "missing":
        store.document_rows = ()
    if case == "empty-document":
        store.document_rows = (replace(store.document_rows[0], markdown=""),)
    if case == "zero-tokens":
        store.document_rows = (replace(store.document_rows[0], token_count=0),)
    if case == "unknown-tokens":
        sections = [replace(s, metadata={**s.metadata, "doc_token_count": 0}) for s in sections]
    if case == "ties":
        sections = [replace(s, score=0.5) for s in sections]
    if case == "triangulation":
        kwargs.update(max_sections=1, max_sections_wide=1, max_full_docs=0, max_full_docs_wide=0, token_budget=10, token_budget_wide=10)
    if case == "refs-cap":
        kwargs.update(legal_refs_budget=1, legal_refs_budget_wide=1)
    if case == "no-refs-budget":
        kwargs.update(legal_refs_budget=0, legal_refs_budget_wide=0)
    config = ContextBuildConfig(**kwargs)
    values = asdict(config)
    values["context_mode"] = legacy_config.ContextMode(mode.value)
    legacy = LegacyBuilder(legacy_config.ContextBuildConfig(**values), dsn="unused")
    docs = {row.doc_id: document_row(row) for row in store.document_rows}
    refs = {row.number: {"cid": row.cid, "url": row.url, "title": row.title} for row in store.reference_rows if row.cid}
    legacy._load_full_document = Mock(side_effect=lambda doc_id: copy.deepcopy(docs.get(doc_id)))
    legacy._resolve_cids = Mock(side_effect=lambda numbers: {n: copy.deepcopy(refs[n]) for n in numbers if n in refs})
    legacy_sections = [
        legacy_models.AggregatedSection(
            **{
                **plain(s),
                "chunks": [legacy_models.RetrievedChunk(**plain(c)) for c in s.chunks],
            }
        )
        for s in sections
    ]
    before = plain(sections)
    expected = legacy.build(legacy_sections)
    actual = await ContextBuilder(config, store).build(sections)
    assert plain(actual.items) == plain(expected)
    assert plain(actual.resolved_refs) == legacy.last_resolved_refs
    assert format_for_prompt(actual.items) == legacy.format_for_prompt(expected)
    assert actual.diagnostics.tokens_used == sum(item.token_estimate for item in actual.items)
    assert plain(sections) == before
    assert [call[1][0] for call in store.calls if call[0] == "documents"] == [call.args[0] for call in legacy._load_full_document.call_args_list]


@pytest.mark.parametrize("refs", [[{"number": "L1"}], '[{"number":"L1"}]', "[broken json", {"code": "L1"}, [], None])
async def test_legal_reference_formats_and_metadata_immutability(refs):
    section = AggregatedSection(
        "s", "Section", "Texte", (), 0.5, publisher="MATTE", references_juridiques=refs, metadata={"doc_title": "Guide", "nested": {"values": [1]}}
    )
    _, store = corpus()
    legacy = LegacyBuilder(legacy_config.ContextBuildConfig(), dsn="unused")
    legacy._resolve_cids = Mock(return_value={"L1": {"cid": "CID", "url": "https://example.org/CID", "title": "Code"}})
    before = plain(section)
    expected = legacy.build([legacy_models.AggregatedSection(**plain(section))])
    actual = await ContextBuilder(ContextBuildConfig(), store).build((section,))
    assert plain(actual.items) == plain(expected)
    assert plain(section) == before
    with pytest.raises(TypeError):
        actual.items[0].metadata["nested"]["values"][0] = 2


@pytest.mark.parametrize("error_type", [DatabaseUnavailable, DatabaseConflict, DatabaseFailure])
@pytest.mark.parametrize("operation", ["sections", "documents", "references"])
async def test_content_failures_preserve_fallback_and_report_safe_code(error_type, operation):
    chunks, store = corpus()
    config = SectionAggregationConfig(enable_section_reranker=False)
    sections = await SectionAggregator(config, store, Reranker()).aggregate(chunks)

    async def fail(*args):
        raise error_type() from RuntimeError("sensitive-dsn-sentinel")

    setattr(store, operation, fail)
    if operation == "sections":
        actual = await SectionAggregator(config, store, Reranker()).aggregate_with_diagnostics(chunks)
        assert actual.diagnostics.store_errors == (error_type.code,)
        assert next(s for s in actual.sections if s.section_id == "s-a").markdown == chunks[0].text
    else:
        actual = await ContextBuilder(ContextBuildConfig(), store).build(sections)
        assert error_type.code in actual.diagnostics.store_errors
        if operation == "documents":
            assert not any(item.metadata.get("is_doc_entire") for item in actual.items)
        else:
            assert actual.resolved_refs == {}
    assert "sensitive-dsn-sentinel" not in repr(actual)


@pytest.mark.parametrize("error", [ValueError("bug"), asyncio.CancelledError()])
@pytest.mark.parametrize("operation", ["sections", "documents", "references", "rerank"])
async def test_bugs_and_cancellation_propagate(error, operation):
    chunks, store = corpus()
    reranker = Reranker()
    sections = await SectionAggregator(SectionAggregationConfig(enable_section_reranker=False), store, reranker).aggregate(chunks)

    async def fail(*args, **kwargs):
        raise error

    setattr(reranker if operation == "rerank" else store, operation, fail)
    with pytest.raises(type(error)):
        if operation in {"sections", "rerank"}:
            await SectionAggregator(SectionAggregationConfig(), store, reranker).aggregate(chunks, "question")
        else:
            await ContextBuilder(ContextBuildConfig(), store).build(sections)


async def test_rerank_failure_keeps_aggregate_scores_and_top_k():
    chunks, store = corpus()
    attempt = Attempt("albert", "rerank", "timeout")
    reranker = Reranker()

    async def fail(*args, **kwargs):
        raise InferenceFailure((attempt,))

    reranker.rerank = fail
    config = SectionAggregationConfig(section_rerank_top_k=2)
    expected = await SectionAggregator(replace(config, enable_section_reranker=False), store, reranker).aggregate(chunks)
    result = await SectionAggregator(config, store, reranker).aggregate_with_diagnostics(chunks, "question")
    assert result.sections == expected[:2]
    assert result.diagnostics.reranker_error == "inference_failure"
    assert result.diagnostics.reranker_attempts == (attempt,)
    assert all(row["rerank_score"] is None for row in result.diagnostics.chunks_after_rerank)


async def test_concurrent_builds_and_empty_call_do_not_share_references():
    store = Content(references=(LegalReference("A", "CID-A", "url-a", "A"), LegalReference("B", "CID-B", "url-b", "B")))
    builder = ContextBuilder(ContextBuildConfig(), store)
    sections = [AggregatedSection(name, name, name, (), 0.5, references_juridiques=({"number": name},)) for name in ("A", "B")]
    before = plain(sections)
    a, b = await asyncio.gather(builder.build((sections[0],)), builder.build((sections[1],)))
    assert set(a.resolved_refs) == {"A"}
    assert set(b.resolved_refs) == {"B"}
    empty = await builder.build(())
    assert empty.items == () and empty.resolved_refs == {}
    assert set(a.resolved_refs) == {"A"}
    assert plain(sections) == before


async def test_empty_aggregation_does_not_call_ports():
    store = Content()
    reranker = Reranker()
    result = await SectionAggregator(SectionAggregationConfig(), store, reranker).aggregate_with_diagnostics((), "question")
    assert result.sections == ()
    assert result.diagnostics.reranker_status == "skipped_no_chunks"
    assert store.calls == reranker.calls == []


async def test_primary_references_get_budget_before_higher_scored_triangulation():
    # Selector order can differ from score order. Only two items fit the cap;
    # diversity then adds a higher-scored secondary publisher outside the budget.
    sections = (
        AggregatedSection("top", "Top", "text", (), 1, publisher="DGAFP"),
        AggregatedSection("primary", "Primary", "text", (), 0.2, publisher="DGAFP", references_juridiques=({"number": "A"},)),
        AggregatedSection("tri", "Tri", "text", (), 0.8, publisher="MATTE", references_juridiques=({"number": "B"},)),
    )
    refs_budget = legacy_models.estimate_tokens(LegacyBuilder._format_references([{"number": "A"}]))
    config = ContextBuildConfig(max_sections=2, legal_refs_budget=refs_budget, token_budget=2)
    result = await ContextBuilder(config, Content()).build(sections)
    assert [item.section_id for item in result.items] == ["top", "tri", "primary"]
    assert result.items[1].metadata["is_triangulation"] is True
    assert "References juridiques" not in result.items[1].content
    assert "References juridiques" in result.items[2].content
    assert result.diagnostics.tokens_used > config.token_budget
    assert result.diagnostics.reference_tokens == refs_budget


async def test_concurrent_aggregation_diagnostics_and_scores_are_isolated():
    chunks, store = corpus()
    step = SectionAggregator(SectionAggregationConfig(), store, Reranker())
    before = plain(chunks)
    ranked, skipped = await asyncio.gather(
        step.aggregate_with_diagnostics(chunks, "question"),
        step.aggregate_with_diagnostics(chunks[:1], None),
    )
    assert ranked.diagnostics.reranker_status == "completed"
    assert skipped.diagnostics.reranker_status == "skipped_no_query"
    assert skipped.sections[0].score == 0.5 * 0.8 + 0.3 * 0.8 + 0.2
    assert plain(chunks) == before
    with pytest.raises(TypeError):
        ranked.diagnostics.chunks_after_rerank[0]["doc_id"] = "other-request"
