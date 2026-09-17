"""C4 conformance through real content SQL on the guarded synthetic database."""

from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from unittest.mock import Mock

import pytest
from assistant_rh_api.core.models.rag_configuration import ContextBuildConfig, SectionAggregationConfig
from assistant_rh_api.core.models.retrieval import RetrievedChunk
from assistant_rh_api.core.pipeline.steps.aggregation import SectionAggregator
from assistant_rh_api.core.pipeline.steps.context_builder import ContextBuilder
from assistant_rh_api.core.pipeline.steps.context_formatting import format_for_prompt
from assistant_rh_api.db.content_store import ContentStore
from assistant_rh_rag_pipeline import config as legacy_config
from assistant_rh_rag_pipeline import models as legacy_models
from assistant_rh_rag_pipeline.context_builder import ContextBuilder as LegacyBuilder
from assistant_rh_rag_pipeline.section_aggregator import SectionAggregator as LegacyAggregator

pytestmark = pytest.mark.anyio
DOC = "00000000-0000-0000-0000-000000000001"
SEC = "00000000-0000-0000-0000-000000000011"


def plain(value):
    if is_dataclass(value):
        return {field.name: plain(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Mapping):
        return {key: plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(item) for item in value]
    return value


@pytest.mark.parametrize("case", ["complete", "nullable-source", "nullable-title", "nullable-token-count", "no-document"])
async def test_content_sql_matches_legacy_context(repository_db, repository_dsn, case):
    async with repository_db.transaction() as connection:
        await connection.execute("ALTER TABLE public.rag_documents ADD COLUMN last_updated_date DATE")
    try:
        async with repository_db.transaction() as connection:
            await connection.execute(
                """INSERT INTO public.rag_documents (doc_id, short_id, title, source_url, publisher, doc_markdown, token_count)
                   VALUES (%s, 'F1', 'Guide', 'https://example.invalid/guide', 'Synthetic', 'Document complet', 4)""",
                (DOC,),
            )
            if case == "nullable-source":
                await connection.execute("UPDATE public.rag_documents SET source_url = NULL, publisher = NULL")
            if case == "nullable-title":
                await connection.execute("UPDATE public.rag_documents SET title = NULL, token_count = NULL")
            if case == "nullable-token-count":
                await connection.execute("UPDATE public.rag_documents SET token_count = NULL")
            await connection.execute(
                """INSERT INTO public.rag_sections (section_id, doc_id, heading, heading_path, section_markdown, references_juridiques)
                   VALUES (%s, %s, 'Section', NULL, 'Contenu de la section', '[{"number":"1"}]')""",
                (SEC, None if case == "no-document" else DOC),
            )
            await connection.execute(
                """INSERT INTO public.rag_chunks_dgafp (chunk_id, chunk_text, number, cid, url, full_title)
                   VALUES ('c', 'Loi', '1', 'CID', 'https://example.invalid/law', 'Loi synthétique')""",
            )
        chunk = RetrievedChunk("chunk", "Extrait", 0.8, "MATTE", {}, SEC, "albert")
        legacy_aggregator = LegacyAggregator(legacy_config.SectionAggregationConfig(enable_section_reranker=False), dsn=repository_dsn)
        expected_sections = legacy_aggregator.aggregate([legacy_models.RetrievedChunk(**plain(chunk))])
        actual_sections = await SectionAggregator(
            SectionAggregationConfig(enable_section_reranker=False),
            ContentStore(repository_db),
            Mock(),
        ).aggregate((chunk,))
        assert plain(actual_sections) == plain(expected_sections)
        legacy_builder = LegacyBuilder(legacy_config.ContextBuildConfig(), dsn=repository_dsn)
        expected = legacy_builder.build(expected_sections)
        actual = await ContextBuilder(ContextBuildConfig(), ContentStore(repository_db)).build(actual_sections)
        assert plain(actual.items) == plain(expected)
        assert plain(actual.resolved_refs) == legacy_builder.last_resolved_refs
        assert format_for_prompt(actual.items) == legacy_builder.format_for_prompt(expected)
    finally:
        async with repository_db.transaction() as connection:
            await connection.execute("ALTER TABLE public.rag_documents DROP COLUMN last_updated_date")
