"""C3 retrieval policy. Only injected ports perform I/O; every result is immutable.

No quality tuning: weighted intra-source RRF, missing-rank penalty, R2 pairs,
heading gates and cross-source calibration preserve the historical algorithm.
"""

import asyncio
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, replace
from difflib import SequenceMatcher
from types import MappingProxyType

from assistant_rh_api.core.errors import ApplicationError, MinistryConfigurationError
from assistant_rh_api.core.inference import Embedding, InferenceFailure
from assistant_rh_api.core.ministry_policy import MINISTRIES
from assistant_rh_api.core.models.rag_configuration import RetrievalConfig, SearchMode
from assistant_rh_api.core.models.retrieval import RawChunk, RetrievalSource, RetrievedChunk, SearchRequest, Source
from assistant_rh_api.core.ports.inference import EmbeddingPort
from assistant_rh_api.core.ports.retrieval import SearchPort

RRF_K = 60
HEADING_PREFIX = "heading:"
HEADING_STOPWORDS = frozenset(
    "a au aux avec ce ces dans de des du en est et la le les l pour qu que quel quelle quelles quels recevoir sont sur un une".split()
)


@dataclass(frozen=True, slots=True)
class RetrievalFailure:
    source: str
    lane: str


class ScopedRetrievalError(ApplicationError):
    code = "scoped_retrieval_failed"

    def __init__(self, failures: tuple[RetrievalFailure, ...]) -> None:
        super().__init__()
        self.failures = failures


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    chunks: tuple[RetrievedChunk, ...] = ()
    sources: tuple[Source, ...] = ()
    failures: tuple[RetrievalFailure, ...] = ()
    embedding: Embedding | None = None
    embedding_failed: bool = False


def _heading_score(chunk: RetrievedChunk) -> float:
    value = chunk.metadata.get("heading_match_score", 0.0) or 0.0
    if not isinstance(value, (int, float, str)):
        raise ValueError("invalid heading score")
    return float(value)


def chunk_sort_key(chunk: RetrievedChunk) -> tuple[float, float, str, str, str]:
    return (
        -chunk.score,
        -_heading_score(chunk),
        chunk.table_source,
        chunk.chunk_id,
        chunk.section_id or "",
    )


def _normalize_text(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value.lower())
    ascii_text = "".join(char for char in decomposed if not unicodedata.combining(char))
    return " ".join(re.findall(r"[a-z0-9]+", ascii_text))


def _heading_tokens(value: str) -> set[str]:
    return {token for token in _normalize_text(value).split() if len(token) > 1 and token not in HEADING_STOPWORDS}


def heading_match_score(heading: str, heading_path: str, query: str) -> float:
    query_norm = _normalize_text(query)
    candidates = [value for value in (_normalize_text(heading), _normalize_text(heading_path)) if value]
    if not query_norm or not candidates:
        return 0.0
    if any(candidate == query_norm or candidate in query_norm or query_norm in candidate for candidate in candidates):
        return 1.0
    query_tokens = _heading_tokens(query)
    heading_tokens = _heading_tokens(f"{heading} {heading_path}")
    if not query_tokens or not heading_tokens:
        return 0.0
    overlap = len(query_tokens & heading_tokens)
    if overlap == 0:
        return 0.0
    coverage_query = overlap / len(query_tokens)
    coverage_heading = overlap / len(heading_tokens)
    if coverage_query >= 0.75 and coverage_heading >= 0.45:
        return 1.0
    token_score = (0.7 * coverage_query) + (0.3 * coverage_heading)
    fuzzy_score = max(SequenceMatcher(None, query_norm, candidate).ratio() for candidate in candidates)
    return min(max(token_score, fuzzy_score if fuzzy_score >= 0.72 else 0.0), 0.99)


def _raw_order(chunks: tuple[RawChunk, ...]) -> tuple[RawChunk, ...]:
    # Ranks are assigned by the adapter before LIMIT with explicit id tie breaks.
    return tuple(sorted(chunks, key=lambda chunk: (chunk.rank, chunk.chunk_id, chunk.section_id or "")))


def fuse_hybrid(vector: tuple[RawChunk, ...], lexical: tuple[RawChunk, ...], *, alpha: float, fetch_k: int) -> tuple[RawChunk, ...]:
    """Exact legacy SQL RRF, including the finite penalty for an absent lane."""
    semantic = {chunk.chunk_id: chunk for chunk in _raw_order(vector)}
    lexemes = {chunk.chunk_id: chunk for chunk in _raw_order(lexical)}
    candidates = []
    for chunk_id in sorted(semantic.keys() | lexemes.keys()):
        sem = semantic.get(chunk_id)
        lex = lexemes.get(chunk_id)
        chunk = sem if sem is not None else lex
        assert chunk is not None
        score = alpha * (1.0 / (RRF_K + (sem.rank if sem is not None else fetch_k)))
        score += (1 - alpha) * (1.0 / (RRF_K + (lex.rank if lex is not None else fetch_k)))
        candidates.append(replace(chunk, score=score))
    return tuple(sorted(candidates, key=lambda chunk: (-chunk.score, chunk.chunk_id, chunk.section_id or ""))[:fetch_k])


def merge_r2_pairs(chunks: tuple[RetrievedChunk, ...], top_k: int) -> tuple[RetrievedChunk, ...]:
    result = []
    seen = set()
    for chunk in chunks:
        cid = str(chunk.metadata.get("cid") or "").strip()
        if cid and chunk.chunk_id in (f"{cid}_0", f"{cid}_r2s"):
            key = (chunk.table_source, cid)
            if key in seen:
                continue
            seen.add(key)
        result.append(chunk)
        if len(result) >= top_k:
            break
    return tuple(result)


def merge_sources(per_source: Mapping[str, tuple[RetrievedChunk, ...]]) -> tuple[RetrievedChunk, ...]:
    fused: dict[tuple[str, str], RetrievedChunk] = {}
    for source_name in sorted(per_source):
        for rank, chunk in enumerate(sorted(per_source[source_name], key=chunk_sort_key), 1):
            key = (chunk.table_source, chunk.chunk_id)
            contribution = 1.0 / (RRF_K + rank)
            is_heading = source_name.startswith(HEADING_PREFIX)
            previous = fused.get(key)
            if previous is None:
                metadata = dict(chunk.metadata)
                metadata.setdefault("source_score", chunk.score)
                metadata.setdefault("source_score_mode", "unknown")
                metadata.setdefault("score_source", source_name)
                if is_heading:
                    metadata["heading_search"] = True
                fused[key] = replace(chunk, score=contribution, metadata=metadata)
                continue
            metadata = dict(previous.metadata)
            if is_heading:
                metadata["heading_search"] = True
                metadata["heading_match_score"] = max(_heading_score(previous), _heading_score(chunk))
                metadata["retrieval_path"] = "chunk+heading"
            elif metadata.get("heading_search") is True:
                metadata["retrieval_path"] = "chunk+heading"
            previous_raw = metadata.get("source_score")
            if not isinstance(previous_raw, (int, float)) or chunk.score > previous_raw:
                metadata.update(
                    source_score=chunk.score, source_score_mode=chunk.metadata.get("source_score_mode", "unknown"), score_source=source_name
                )
            fused[key] = replace(previous, score=previous.score + contribution, metadata=metadata)
    theoretical_max = max(len(per_source), 1) * (1.0 / (RRF_K + 1))
    result = []
    for chunk in fused.values():
        metadata = dict(chunk.metadata)
        metadata.setdefault("fused_rrf_score", chunk.score)
        metadata["merged_score_mode"] = "rrf_source_ceiling"
        result.append(replace(chunk, score=min(chunk.score / theoretical_max, 1.0), metadata=metadata))
    return tuple(sorted(result, key=chunk_sort_key))


class Retriever:
    def __init__(self, search: SearchPort, embeddings: Mapping[str, EmbeddingPort], sources: tuple[RetrievalSource, ...]) -> None:
        self._search = search
        # Each injected chain owns its provider fallback and circuit state.
        # Choose the chain from this request's config, never from a previous run.
        self._embeddings = MappingProxyType(dict(embeddings))
        self._sources: Mapping[str, RetrievalSource] = MappingProxyType({source.key: source for source in sources})
        if len(self._sources) != len(sources) or len({source.name for source in sources}) != len(sources):
            raise ValueError("duplicate retrieval source")

    async def retrieve(
        self,
        query: str,
        config: RetrievalConfig,
        *,
        selected_ministry: str | None = None,
        tables: tuple[str, ...] | None = None,
        force_hybrid_tables: frozenset[str] = frozenset(),
        search_mode: SearchMode | None = None,
        top_k: int | None = None,
        strict_table_errors: bool = False,
    ) -> RetrievalResult:
        """Request-scoped policy; ministry is already authorized by B4/B5.

        A selected ministry always replaces configurable/evaluation tables. C6
        calls this only after C2's direct-response gate. The frozen pipeline
        always searches DGAFP in that scope, irrespective of legal intent.
        """
        table_keys = config.tables if tables is None else tables
        if selected_ministry is not None:
            if selected_ministry not in MINISTRIES:
                raise MinistryConfigurationError()
            table_keys = (selected_ministry, "service_public", "dgafp")
            force_hybrid_tables = frozenset({"dgafp"})
            strict_table_errors = True
        unknown = tuple(key for key in table_keys if key not in self._sources)
        if unknown and strict_table_errors:
            raise ValueError("unknown retrieval source")
        # Duplicate keys did not create duplicate logical fusion lanes historically.
        sources = tuple(self._sources[key] for key in dict.fromkeys(table_keys) if key in self._sources)
        keys = tuple(source.key for source in sources)
        mode = search_mode or config.search_mode
        limit = top_k or config.initial_top_k
        if limit < 1:
            raise ValueError("positive retrieval limit required")
        if config.embedding_model.value not in self._embeddings:
            raise ValueError("embedding model has no configured gateway")
        try:
            embedding = await self._embeddings[config.embedding_model.value].embed(query)
        except InferenceFailure:
            return RetrievalResult(sources=keys, embedding_failed=True)

        per_source: dict[str, tuple[RetrievedChunk, ...]] = {}
        failures: list[RetrievalFailure] = []

        async def run(source: RetrievalSource, *, heading: bool) -> None:
            lane = "heading" if heading else "chunks"
            name = HEADING_PREFIX + source.name if heading else source.name
            try:
                if heading:
                    chunks = await self._headings(source, query, limit, config.ivfflat_probes)
                else:
                    chunks = await self._chunks(source, query, embedding, config, mode, limit, source.key in force_hybrid_tables)
                per_source[name] = chunks
            except Exception:
                # Legacy unscoped _exec_de_table swallowed a failed lane as [],
                # which still counts in the calibration denominator. Preserve it.
                failures.append(RetrievalFailure(source.key, lane))
                per_source[name] = ()

        async with asyncio.TaskGroup() as tasks:
            for source in sources:
                tasks.create_task(run(source, heading=False))
                if source.headings:
                    tasks.create_task(run(source, heading=True))
        ordered_failures = tuple(sorted(failures, key=lambda failure: (failure.source, failure.lane)))
        if strict_table_errors and ordered_failures:
            raise ScopedRetrievalError(ordered_failures)
        return RetrievalResult(merge_sources(per_source), keys, ordered_failures, embedding)

    async def _chunks(
        self,
        source: RetrievalSource,
        query: str,
        embedding: Embedding,
        config: RetrievalConfig,
        mode: SearchMode,
        limit: int,
        force_hybrid: bool,
    ) -> tuple[RetrievedChunk, ...]:
        fetch_k = limit * 2 if source.r2_pairs else limit
        request = SearchRequest(source.key, "vector", query, embedding.vector, embedding.model, fetch_k, config.ivfflat_probes)
        if mode == SearchMode.LEXICAL:
            raw = _raw_order(await self._search.search(replace(request, mode="lexical")))
            score_mode = "lexical"
        elif mode == SearchMode.HYBRID or force_hybrid:
            # Both lanes share one snapshot and one failure boundary.
            vector, lexical = await self._search.hybrid_candidates(request)
            raw = fuse_hybrid(vector, lexical, alpha=config.alpha, fetch_k=fetch_k)
            score_mode = "hybrid"
        else:
            raw = _raw_order(await self._search.search(request))
            score_mode = "semantic"
        chunks = tuple(
            RetrievedChunk(
                chunk.chunk_id,
                chunk.text,
                chunk.score,
                source.publisher,
                {
                    **{
                        key: chunk.metadata[key]
                        for key in (*source.metadata_fields, "doc_short_id", "doc_title", "doc_url")
                        if chunk.metadata.get(key) is not None
                    },
                    "source_score_mode": score_mode,
                },
                chunk.section_id if source.headings else None,
                "lexical" if mode == SearchMode.LEXICAL else embedding.model,
            )
            for chunk in raw
        )
        if source.r2_pairs:
            chunks = merge_r2_pairs(chunks, limit)
        return tuple(sorted(chunks, key=chunk_sort_key))

    async def _headings(self, source: RetrievalSource, query: str, limit: int, probes: int) -> tuple[RetrievedChunk, ...]:
        raw = await self._search.search(SearchRequest(source.key, "heading", query=query, limit=limit, probes=probes))
        chunks = []
        for chunk in _raw_order(raw):
            heading = str(chunk.metadata.get("heading") or "")
            path = str(chunk.metadata.get("heading_path") or "")
            score = heading_match_score(heading, path, query)
            if score <= 0.0:
                continue
            metadata = {key: chunk.metadata[key] for key in ("source_name", "source_document_id", "doc_url") if chunk.metadata.get(key) is not None}
            metadata.update(
                retrieval_path="heading",
                source_score_mode="heading",
                heading_search=True,
                heading_match_score=score,
                matched_heading=heading,
                matched_heading_path=path,
            )
            chunks.append(RetrievedChunk(chunk.chunk_id, chunk.text, score, source.publisher, metadata, chunk.section_id, "heading"))
        return tuple(sorted(chunks, key=chunk_sort_key)[:limit])
