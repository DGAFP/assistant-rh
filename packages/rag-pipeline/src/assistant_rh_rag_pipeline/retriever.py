"""
Multi-table parallel retriever for the RAG V3 Clean pipeline.

Searches the 4 DE chunk tables in parallel using a ThreadPoolExecutor,
then merges results with cross-source rank fusion. Supports semantic search (default) and
hybrid (semantic + lexical via RRF) when tsvector columns are available.

Tables queried (configurable):
  rag_chunks_matte, rag_chunks_service_public, rag_chunks_dgafp, rag_chunks_rgrh

Dependencies (internal only):
  - config (RetrievalConfig, CHUNK_TABLES, ChunkTable, get_dsn, SearchMode, EmbeddingModel)
  - embedder (FallbackEmbedder)
  - models (RetrievedChunk)
"""

from __future__ import annotations

import logging
import re
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from difflib import SequenceMatcher
from typing import Dict, List, Tuple

import psycopg
from psycopg.rows import dict_row

from .config import (
    CHUNK_TABLES,
    ChunkTable,
    EmbeddingModel,
    RetrievalConfig,
    SearchMode,
)
from .db_helpers import get_dsn
from .embedder import FallbackEmbedder
from .models import RetrievedChunk

logger = logging.getLogger(__name__)

# OR-based tsquery: converts AND-linked plainto_tsquery to OR so any word
# matches, while ts_rank_cd still scores multi-word matches higher.
_TSQUERY_OR = """
    CASE WHEN plainto_tsquery('french', {p})::text = ''
         THEN plainto_tsquery('french', {p})
         ELSE to_tsquery('french', replace(plainto_tsquery('french', {p})::text, ' & ', ' | '))
    END
"""

_CROSS_SOURCE_RRF_K = 60
_HEADING_SOURCE_PREFIX = "heading:"
_HEADING_STOPWORDS = {
    "a",
    "au",
    "aux",
    "avec",
    "ce",
    "ces",
    "dans",
    "de",
    "des",
    "du",
    "en",
    "est",
    "et",
    "la",
    "le",
    "les",
    "l",
    "pour",
    "qu",
    "que",
    "quel",
    "quelle",
    "quelles",
    "quels",
    "recevoir",
    "sont",
    "sur",
    "un",
    "une",
}


class Retriever:
    """
    Retrieve chunks from the DE tables and enrich with ``rag_sections`` metadata.

    Usage::

        r = Retriever(config)
        chunks = r.retrieve("Qu'est-ce que le RIFSEEP ?")
    """

    def __init__(self, config: RetrievalConfig, dsn: str | None = None):
        self.config = config
        self.dsn = dsn or get_dsn()
        self._embedder: FallbackEmbedder | None = None
        self._table_columns_cache: dict[str, set[str]] = {}

    @staticmethod
    def _chunk_sort_key(chunk: RetrievedChunk) -> tuple[float, float, str, str, str]:
        """Canonical deterministic sort key for retrieved chunks.

        Primary order keeps ranking semantics (score DESC). Heading matches
        break exact score ties before stable identifiers.
        """
        return (
            -float(chunk.score),
            -float(chunk.metadata.get("heading_match_score", 0.0) or 0.0),
            str(chunk.table_source or ""),
            str(chunk.chunk_id or ""),
            str(chunk.section_id or ""),
        )

    def _sort_chunks_deterministically(self, chunks: List[RetrievedChunk]) -> List[RetrievedChunk]:
        chunks.sort(key=self._chunk_sort_key)
        return chunks

    @staticmethod
    def _normalize_text(value: str) -> str:
        """Normalize French headings and queries for lexical title matching."""
        decomposed = unicodedata.normalize("NFKD", value.lower())
        ascii_text = "".join(char for char in decomposed if not unicodedata.combining(char))
        return " ".join(re.findall(r"[a-z0-9]+", ascii_text))

    @classmethod
    def _tokenize_for_heading_match(cls, value: str) -> set[str]:
        return {token for token in cls._normalize_text(value).split() if len(token) > 1 and token not in _HEADING_STOPWORDS}

    @classmethod
    def _heading_match_score(cls, heading: str, heading_path: str, query: str) -> float:
        """Return a generic title/intertitle relevance score in [0, 1]."""
        query_norm = cls._normalize_text(query)
        heading_norm = cls._normalize_text(heading)
        path_norm = cls._normalize_text(heading_path)
        if not query_norm or not (heading_norm or path_norm):
            return 0.0

        candidates = [candidate for candidate in (heading_norm, path_norm) if candidate]
        if any(candidate == query_norm or candidate in query_norm or query_norm in candidate for candidate in candidates):
            return 1.0

        query_tokens = cls._tokenize_for_heading_match(query)
        heading_tokens = cls._tokenize_for_heading_match(f"{heading} {heading_path}")
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
        score = max(token_score, fuzzy_score if fuzzy_score >= 0.72 else 0.0)
        return min(score, 0.99)

    @property
    def embedder(self) -> FallbackEmbedder:
        if self._embedder is None:
            primary = "albert" if self.config.embedding_model == EmbeddingModel.ALBERT else "bge_scaleway"
            fallback = "bge_scaleway" if primary == "albert" else None
            self._embedder = FallbackEmbedder(primary=primary, fallback=fallback, timeout=10)
        return self._embedder

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def retrieve(
        self,
        query: str,
        force_hybrid_tables: set[str] | None = None,
        tables: list[str] | None = None,
        search_mode: SearchMode | None = None,
        top_k: int | None = None,
        strict_table_errors: bool = False,
    ) -> List[RetrievedChunk]:
        """Embed *query*, search all configured tables in parallel, return merged results.

        *force_hybrid_tables*: table keys (e.g. ``{"dgafp"}``) that should use
        hybrid search regardless of the global ``search_mode``.
        *tables*: optional request-scoped table keys to search without mutating
        ``self.config.tables``.
        *search_mode* and *top_k*: optional request-scoped overrides used by
        fallback/retry paths without mutating ``self.config``.
        *strict_table_errors*: raise if a requested table key/table query fails.
        Legacy unscoped retrieval keeps the previous partial-result behavior.
        """
        t0 = time.time()
        _force_names = {CHUNK_TABLES[k].name for k in (force_hybrid_tables or set()) if k in CHUNK_TABLES}
        table_keys = self.config.tables if tables is None else tables
        unknown_table_keys = [k for k in table_keys if k not in CHUNK_TABLES]
        if unknown_table_keys and strict_table_errors:
            raise ValueError(f"Unknown retrieval table key(s): {', '.join(unknown_table_keys)}")

        embedding = self.embedder.embed_query(query)
        if embedding is None:
            logger.warning("Embedding failed – returning empty results")
            return []

        embed_model_used = self.embedder.last_model_used or "albert"
        effective_search_mode = search_mode or self.config.search_mode
        effective_top_k = top_k or self.config.initial_top_k
        is_hybrid = effective_search_mode == SearchMode.HYBRID
        is_lexical = effective_search_mode == SearchMode.LEXICAL

        chunk_tables = [CHUNK_TABLES[k] for k in table_keys if k in CHUNK_TABLES]
        if not chunk_tables:
            return []

        n_workers = len(chunk_tables) * 2
        per_source_results: Dict[str, List[RetrievedChunk]] = {}
        table_errors: list[str] = []
        with ThreadPoolExecutor(max_workers=max(n_workers, 1)) as pool:
            futures = {
                pool.submit(
                    self._search_table,
                    tbl,
                    embedding,
                    embed_model_used,
                    query,
                    force_hybrid=(tbl.name in _force_names),
                    search_mode=effective_search_mode,
                    top_k=effective_top_k,
                    strict_errors=strict_table_errors,
                ): tbl.name
                for tbl in chunk_tables
            }
            futures.update(
                {
                    pool.submit(
                        self._search_table_headings,
                        tbl,
                        query,
                        top_k=effective_top_k,
                        strict_errors=strict_table_errors,
                    ): f"{_HEADING_SOURCE_PREFIX}{tbl.name}"
                    for tbl in chunk_tables
                    if tbl.has_sections
                }
            )

            for future in as_completed(futures):
                name = futures[future]
                try:
                    result = future.result()
                    if result and "source_score_mode" not in result[0].metadata:
                        inferred_mode = "lexical" if is_lexical else "hybrid" if is_hybrid else "semantic"
                        for chunk in result:
                            chunk.metadata.setdefault("source_score_mode", inferred_mode)
                    self._sort_chunks_deterministically(result)
                    logger.info("  %s → %d chunks", name, len(result))
                    per_source_results[name] = result
                except Exception as exc:
                    logger.error("Search on %s failed (%s): %s", name, type(exc).__name__, exc)
                    if strict_table_errors:
                        table_errors.append(f"{name}: {type(exc).__name__}: {exc}")

        if table_errors:
            raise RuntimeError("Scoped retrieval failed on requested table(s): " + "; ".join(table_errors))

        all_chunks = self._merge_cross_source_ranks(per_source_results)
        all_chunks = self._normalize_merged_scores(
            all_chunks,
            source_count=len(per_source_results),
        )
        mode_label = effective_search_mode.value
        logger.info(
            "Retrieved %d chunks from %d sources (%s, cross-source RRF + source-ceiling score calibration) in %.0fms",
            len(all_chunks),
            n_workers,
            mode_label,
            (time.time() - t0) * 1000,
        )
        return all_chunks

    def _normalize_merged_scores(
        self,
        chunks: List[RetrievedChunk],
        *,
        source_count: int,
    ) -> List[RetrievedChunk]:
        """Rescale merged RRF scores to [0,1] for downstream aggregation stability.

        We normalize against the theoretical best-case RRF score for the number
        of participating sources (all sources rank the same chunk at position 1).
        """
        if not chunks:
            return chunks

        source_count = max(source_count, 1)
        theoretical_max = source_count * (1.0 / (_CROSS_SOURCE_RRF_K + 1))

        for chunk in chunks:
            chunk.metadata.setdefault("fused_rrf_score", chunk.score)

        for chunk in chunks:
            chunk.score = min(chunk.score / theoretical_max, 1.0)
            chunk.metadata["merged_score_mode"] = "rrf_source_ceiling"

        self._sort_chunks_deterministically(chunks)
        return chunks

    def _merge_cross_source_ranks(
        self,
        per_source_results: Dict[str, List[RetrievedChunk]],
    ) -> List[RetrievedChunk]:
        """Merge source-specific rankings with Reciprocal Rank Fusion.

        Raw similarity scores are not directly comparable across source tables,
        which can over-favor one publisher when simply sorting by score.
        """
        fused: Dict[Tuple[str, str], RetrievedChunk] = {}

        for source_name in sorted(per_source_results):
            chunks = per_source_results[source_name]
            for rank, chunk in enumerate(chunks, start=1):
                key = (chunk.table_source, chunk.chunk_id)
                contribution = 1.0 / (_CROSS_SOURCE_RRF_K + rank)

                source_is_heading = source_name.startswith(_HEADING_SOURCE_PREFIX)
                if key not in fused:
                    fused_chunk = RetrievedChunk(
                        chunk_id=chunk.chunk_id,
                        text=chunk.text,
                        score=contribution,
                        table_source=chunk.table_source,
                        metadata=dict(chunk.metadata),
                        section_id=chunk.section_id,
                        embedding_model_used=chunk.embedding_model_used,
                    )
                    fused_chunk.metadata.setdefault("source_score", chunk.score)
                    fused_chunk.metadata.setdefault(
                        "source_score_mode",
                        chunk.metadata.get("source_score_mode", "unknown"),
                    )
                    fused_chunk.metadata.setdefault("score_source", source_name)
                    if source_is_heading:
                        fused_chunk.metadata["heading_search"] = True
                    fused[key] = fused_chunk
                    continue

                fused[key].score += contribution
                if source_is_heading:
                    fused[key].metadata["heading_search"] = True
                    fused[key].metadata["heading_match_score"] = max(
                        float(fused[key].metadata.get("heading_match_score", 0.0) or 0.0),
                        float(chunk.metadata.get("heading_match_score", 0.0) or 0.0),
                    )
                    fused[key].metadata["retrieval_path"] = "chunk+heading"
                elif fused[key].metadata.get("heading_search") is True:
                    fused[key].metadata["retrieval_path"] = "chunk+heading"
                previous_raw = fused[key].metadata.get("source_score")
                if not isinstance(previous_raw, (int, float)) or chunk.score > float(previous_raw):
                    fused[key].metadata["source_score"] = chunk.score
                    fused[key].metadata["source_score_mode"] = chunk.metadata.get(
                        "source_score_mode",
                        "unknown",
                    )
                    fused[key].metadata["score_source"] = source_name

        merged = list(fused.values())
        self._sort_chunks_deterministically(merged)
        return merged

    # ------------------------------------------------------------------
    # Per-table search (DE tables)
    # ------------------------------------------------------------------

    _TABLE_META_COLS: Dict[str, List[str]] = {
        "rag_chunks_matte": ["source_name", "section_path", "role", "thematique", "references_juridiques", "source_document_id"],
        "rag_chunks_mso": ["source_name", "section_path", "role", "thematique", "references_juridiques", "source_document_id"],
        "rag_chunks_mi": ["source_name", "section_path", "role", "thematique", "references_juridiques", "source_document_id"],
        "rag_chunks_service_public": ["source_name", "section_path", "role", "thematique", "references_juridiques", "source_document_id"],
        CHUNK_TABLES["service_public_scw"].name: ["source_name", "section_path", "role", "thematique", "short_id", "source"],
        "rag_chunks_dgafp": ["title", "full_title", "number", "category", "url", "cid"],
        CHUNK_TABLES["dgafp_scw"].name: ["title", "full_title", "number", "category", "url", "cid"],
        "rag_chunks_rgrh": ["source_name", "section_path", "role", "thematique", "references_juridiques", "source_document_id"],
    }

    def _get_table_columns(self, table_name: str) -> set[str]:
        """Return existing columns for a chunk table, cached per retriever instance."""
        if table_name in self._table_columns_cache:
            return self._table_columns_cache[table_name]

        try:
            with psycopg.connect(self.dsn) as conn:
                rows = conn.execute(
                    """
                    SELECT attname
                    FROM pg_attribute
                    WHERE attrelid = %s::regclass
                      AND attnum > 0
                      AND NOT attisdropped
                    """,
                    (table_name,),
                ).fetchall()
            columns = {str(row[0]) for row in rows}
        except Exception as exc:
            logger.warning("Column introspection failed for %s: %s", table_name, exc)
            columns = set()

        self._table_columns_cache[table_name] = columns
        return columns

    def _select_existing_meta_cols(self, table: ChunkTable) -> list[str]:
        """Return configured metadata columns that exist in the current DB schema."""
        expected = self._TABLE_META_COLS.get(table.name, [])
        if not expected:
            return []
        existing = self._get_table_columns(table.name)
        if not existing:
            return expected
        return [col for col in expected if col in existing]

    def _document_meta_select_sql(self, table: ChunkTable) -> str:
        """Expose canonical document metadata for chunk tables carrying short_id.

        Legacy Service-Public chunk tables do not always store section_id or
        source_document_id. When section resolution fails, this metadata lets
        downstream source pills still display the rag_documents title and URL
        instead of falling back to values such as ``F527.xml`` with no link.
        """
        existing = self._get_table_columns(table.name)
        if "short_id" not in existing:
            return ""

        return """
                , t.short_id AS doc_short_id
                , (
                    SELECT d.title
                    FROM rag_documents d
                    WHERE d.short_id = t.short_id
                    LIMIT 1
                ) AS doc_title
                , (
                    SELECT d.source_url
                    FROM rag_documents d
                    WHERE d.short_id = t.short_id
                    LIMIT 1
                ) AS doc_url
            """

    def _search_table_headings(
        self,
        table: ChunkTable,
        query: str,
        *,
        top_k: int | None = None,
        strict_errors: bool = False,
    ) -> List[RetrievedChunk]:
        """Search document titles, section headings and heading paths for section-backed tables."""
        if not table.has_sections:
            return []

        existing = self._get_table_columns(table.name)
        if "section_id" not in existing and not {"short_id", "section_path"}.issubset(existing):
            return []

        effective_top_k = top_k or self.config.initial_top_k
        section_sql = self._section_select_sql(table)
        extra_cols = self._select_existing_meta_cols(table)
        extra_sql = "".join(f", t.{c}" for c in extra_cols)
        tsq = _TSQUERY_OR.format(p="%s")

        sql = f"""
            WITH parsed_query AS (
                SELECT ({tsq}) AS q
            ),
            candidate_chunks AS (
                SELECT
                    t.{table.id_col} AS chunk_id,
                    t.{table.text_col} AS chunk_text
                    {extra_sql}
                    {section_sql}
                FROM {table.name} t
            )
            SELECT
                c.chunk_id,
                c.chunk_text,
                c.section_id,
                s.heading,
                s.heading_path,
                d.title AS source_name,
                d.short_id AS source_document_id,
                d.source_url AS doc_url,
                ts_rank_cd(
                    to_tsvector('french', concat_ws(' ', d.title, s.heading, s.heading_path)),
                    pq.q
                ) AS lexical_score
            FROM candidate_chunks c
            JOIN rag_sections s ON s.section_id = c.section_id
            LEFT JOIN rag_documents d ON d.doc_id = s.doc_id
            CROSS JOIN parsed_query pq
            WHERE to_tsvector('french', concat_ws(' ', d.title, s.heading, s.heading_path)) @@ pq.q
            ORDER BY lexical_score DESC, c.chunk_id
            LIMIT %s
        """
        params: Tuple = (query, query, query, effective_top_k)
        chunks: List[RetrievedChunk] = []
        try:
            with psycopg.connect(self.dsn, row_factory=dict_row) as conn:
                rows = conn.execute(sql, params).fetchall()

            for row in rows:
                heading = str(row.get("heading") or "")
                heading_path = str(row.get("heading_path") or "")
                match_score = self._heading_match_score(heading, heading_path, query)
                if match_score <= 0.0:
                    continue

                meta = {
                    "retrieval_path": "heading",
                    "source_score_mode": "heading",
                    "heading_search": True,
                    "heading_match_score": match_score,
                    "matched_heading": heading,
                    "matched_heading_path": heading_path,
                }
                for key in ("source_name", "source_document_id", "doc_url"):
                    if row.get(key) is not None:
                        meta[key] = row.get(key)

                chunks.append(
                    RetrievedChunk(
                        chunk_id=str(row["chunk_id"]),
                        text=row["chunk_text"] or "",
                        score=match_score,
                        table_source=table.publisher,
                        metadata=meta,
                        section_id=row.get("section_id"),
                        embedding_model_used="heading",
                    )
                )
        except Exception as exc:
            logger.error("Heading query on %s failed (%s): %s", table.name, type(exc).__name__, exc)
            if strict_errors:
                raise

        self._sort_chunks_deterministically(chunks)
        return chunks[:effective_top_k]

    def _section_select_sql(self, table: ChunkTable) -> str:
        """Return SQL that exposes a `section_id` column when the table supports sections.

        Some ingested tables store a direct `section_id`; Service-Public's current
        production table only stores `short_id` + `section_path`, so we resolve the
        section through `rag_documents`/`rag_sections` at query time.
        """
        if not table.has_sections:
            return ""

        existing = self._get_table_columns(table.name)
        if "section_id" in existing:
            return ", t.section_id"

        if {"short_id", "section_path"}.issubset(existing):
            return r"""
                , (
                    SELECT s.section_id
                    FROM rag_documents d
                    JOIN rag_sections s ON s.doc_id = d.doc_id
                    WHERE d.short_id = t.short_id
                      AND (
                        s.heading_path = t.section_path
                        OR s.heading = btrim(regexp_replace(t.section_path, '^.*>\s*', ''))
                      )
                    ORDER BY CASE WHEN s.heading_path = t.section_path THEN 0 ELSE 1 END
                    LIMIT 1
                ) AS section_id
            """

        return ""

    def _search_table(
        self,
        table: ChunkTable,
        embedding: List[float],
        model_used: str,
        query: str,
        *,
        force_hybrid: bool = False,
        search_mode: SearchMode | None = None,
        top_k: int | None = None,
        strict_errors: bool = False,
    ) -> List[RetrievedChunk]:
        """Search a DE table. Uses hybrid RRF when tsvector is available, else semantic."""
        effective_search_mode = search_mode or self.config.search_mode
        effective_top_k = top_k or self.config.initial_top_k
        use_hybrid = (effective_search_mode in (SearchMode.HYBRID, SearchMode.LEXICAL) or force_hybrid) and table.tsv_col
        if effective_search_mode == SearchMode.LEXICAL and table.tsv_col:
            mode = "lexical"
        else:
            mode = "hybrid" if use_hybrid else "semantic"
        logger.info("Searching %s [%s] tsv_col=%s force=%s", table.name, mode, table.tsv_col or "—", force_hybrid)
        if use_hybrid:
            chunks = self._search_table_hybrid(
                table,
                embedding,
                model_used,
                query,
                search_mode=effective_search_mode,
                top_k=effective_top_k,
                strict_errors=strict_errors,
            )
        else:
            chunks = self._search_table_semantic(
                table,
                embedding,
                model_used,
                top_k=effective_top_k,
                strict_errors=strict_errors,
            )

        for chunk in chunks:
            chunk.metadata.setdefault("source_score_mode", mode)

        return chunks

    def _search_table_semantic(
        self,
        table: ChunkTable,
        embedding: List[float],
        model_used: str,
        *,
        top_k: int | None = None,
        strict_errors: bool = False,
    ) -> List[RetrievedChunk]:
        """Pure semantic (cosine) search on a DE table."""
        embed_col = table.embed_col_albert if model_used == "albert" else table.embed_col_bge
        effective_top_k = top_k or self.config.initial_top_k

        extra_cols = self._select_existing_meta_cols(table)
        extra_sql = "".join(f", t.{c}" for c in extra_cols)
        section_sql = self._section_select_sql(table)
        doc_meta_sql = self._document_meta_select_sql(table)

        sql = f"""
            SELECT
                t.{table.id_col}  AS chunk_id,
                t.{table.text_col} AS chunk_text,
                1 - (t.{embed_col} <=> %s::vector) AS score
                {extra_sql}
                {section_sql}
                {doc_meta_sql}
            FROM {table.name} t
            WHERE t.{embed_col} IS NOT NULL
            ORDER BY t.{embed_col} <=> %s::vector, t.{table.id_col}
            LIMIT %s
        """
        params: Tuple = (embedding, embedding, effective_top_k)
        return self._exec_de_table(table, sql, params, model_used, strict_errors=strict_errors)

    def _search_table_hybrid(
        self,
        table: ChunkTable,
        embedding: List[float],
        model_used: str,
        query: str,
        *,
        search_mode: SearchMode | None = None,
        top_k: int | None = None,
        strict_errors: bool = False,
    ) -> List[RetrievedChunk]:
        """Hybrid RRF search on a DE table that has a tsvector column."""
        embed_col = table.embed_col_albert if model_used == "albert" else table.embed_col_bge
        tsv = table.tsv_col
        alpha = self.config.alpha
        rrf_k = 60
        top_k = top_k or self.config.initial_top_k
        effective_search_mode = search_mode or self.config.search_mode
        is_lexical_only = effective_search_mode == SearchMode.LEXICAL

        extra_cols = self._select_existing_meta_cols(table)
        extra_sql = "".join(f", t.{c}" for c in extra_cols)
        section_sql = self._section_select_sql(table)
        doc_meta_sql = self._document_meta_select_sql(table)
        tsq = _TSQUERY_OR.format(p="%s")

        if is_lexical_only:
            sql = f"""
                WITH parsed_query AS (
                    SELECT ({tsq}) AS q
                )
                SELECT
                    t.{table.id_col} AS chunk_id,
                    t.{table.text_col} AS chunk_text,
                    ts_rank_cd(t.{tsv}, pq.q) AS score
                    {extra_sql}
                    {section_sql}
                    {doc_meta_sql}
                FROM {table.name} t
                CROSS JOIN parsed_query pq
                WHERE t.{tsv} @@ pq.q
                ORDER BY ts_rank_cd(t.{tsv}, pq.q) DESC, t.{table.id_col}
                LIMIT %s
            """
            params: Tuple = (query, query, query, top_k)
            return self._exec_de_table(table, sql, params, "lexical", strict_errors=strict_errors)

        # Full hybrid: RRF of semantic + lexical
        sql = f"""
            WITH parsed_query AS (
                SELECT ({tsq}) AS q
            ),
            semantic_ranked AS (
                SELECT t.{table.id_col} AS chunk_id,
                       ROW_NUMBER() OVER (ORDER BY t.{embed_col} <=> %s::vector, t.{table.id_col}) AS sem_rank
                FROM {table.name} t
                WHERE t.{embed_col} IS NOT NULL
                ORDER BY t.{embed_col} <=> %s::vector, t.{table.id_col}
                LIMIT %s
            ),
            lexical_ranked AS (
                SELECT t.{table.id_col} AS chunk_id,
                       ROW_NUMBER() OVER (ORDER BY ts_rank_cd(t.{tsv}, pq.q) DESC, t.{table.id_col}) AS lex_rank
                FROM {table.name} t
                CROSS JOIN parsed_query pq
                WHERE t.{tsv} @@ pq.q
                ORDER BY ts_rank_cd(t.{tsv}, pq.q) DESC, t.{table.id_col}
                LIMIT %s
            ),
            rrf AS (
                SELECT COALESCE(s.chunk_id, l.chunk_id) AS chunk_id,
                       %s * (1.0 / (%s + COALESCE(s.sem_rank, %s)))
                       + (1 - %s) * (1.0 / (%s + COALESCE(l.lex_rank, %s))) AS rrf_score
                FROM semantic_ranked s
                FULL OUTER JOIN lexical_ranked l ON s.chunk_id = l.chunk_id
            )
            SELECT
                t.{table.id_col} AS chunk_id,
                t.{table.text_col} AS chunk_text,
                r.rrf_score AS score
                {extra_sql}
                {section_sql}
                {doc_meta_sql}
            FROM rrf r
            JOIN {table.name} t ON t.{table.id_col} = r.chunk_id
            ORDER BY r.rrf_score DESC, t.{table.id_col}
            LIMIT %s
        """
        params = (
            query,
            query,
            query,  # parsed_query
            embedding,
            embedding,
            top_k,  # semantic_ranked
            top_k,  # lexical_ranked
            alpha,
            rrf_k,
            top_k,  # rrf semantic part
            alpha,
            rrf_k,
            top_k,  # rrf lexical part
            top_k,  # final limit
        )
        return self._exec_de_table(table, sql, params, model_used, strict_errors=strict_errors)

    def _exec_de_table(
        self,
        table: ChunkTable,
        sql: str,
        params: Tuple,
        model_used: str,
        *,
        strict_errors: bool = False,
    ) -> List[RetrievedChunk]:
        """Execute a search query on a DE table and return parsed chunks."""
        extra_cols = self._select_existing_meta_cols(table)
        chunks: List[RetrievedChunk] = []
        try:
            with psycopg.connect(self.dsn, row_factory=dict_row) as conn:
                rows = conn.execute(sql, params).fetchall()

            for row in rows:
                meta = {c: row.get(c) for c in extra_cols if row.get(c) is not None}
                for key in ("doc_short_id", "doc_title", "doc_url"):
                    if row.get(key) is not None:
                        meta[key] = row.get(key)

                chunks.append(
                    RetrievedChunk(
                        chunk_id=str(row["chunk_id"]),
                        text=row["chunk_text"] or "",
                        score=float(row["score"]),
                        table_source=table.publisher,
                        metadata=meta,
                        section_id=row.get("section_id"),
                        embedding_model_used=model_used,
                    )
                )
        except Exception as exc:
            logger.error("Query on %s failed (%s): %s", table.name, type(exc).__name__, exc)
            if strict_errors:
                raise

        return chunks
