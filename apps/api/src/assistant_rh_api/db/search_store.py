"""Raw pgvector/lexical/heading candidates. Fusion and scoring policy stay in core."""

import math
from dataclasses import replace
from types import MappingProxyType

from psycopg import sql

from assistant_rh_api.core.models.retrieval import RawChunk, RetrievalSource, SearchRequest
from assistant_rh_api.core.ports.retrieval import SearchPort
from assistant_rh_api.db.content_store import (
    columns,
    document_source_clause,
    explicit_document_id,
    immutable_object,
    metadata_expression,
    section_expression,
)
from assistant_rh_api.db.pool import Database
from assistant_rh_api.db.search_catalog import SearchTable, search_catalog

# Same OR lexemes as the historical retriever; no alpha/RRF/gate here.
TSQUERY = sql.SQL("""CASE WHEN plainto_tsquery('french', %s)::text = ''
    THEN plainto_tsquery('french', %s)
    ELSE to_tsquery('french', replace(plainto_tsquery('french', %s)::text, ' & ', ' | ')) END""")


def scoped_chunk_filter(existing: set[str], corpus: str | None) -> sql.Composable:
    if corpus is None:
        return sql.SQL("")
    scope = document_source_clause(corpus)
    clause = sql.SQL("")
    document_id = explicit_document_id(existing)
    if "section_id" in existing:
        clause += sql.SQL("""
            AND (t.section_id IS NULL OR EXISTS (
                SELECT 1 FROM public.rag_sections linked
                JOIN public.rag_documents d ON d.doc_id = linked.doc_id
                WHERE linked.section_id = t.section_id {scope} AND (({id}) IS NULL OR d.doc_id = ({id}))
            ))
        """).format(scope=scope, id=document_id)
    if "source_document_id" in existing:
        clause += sql.SQL("""
            AND (({id}) IS NULL OR EXISTS (
                SELECT 1 FROM public.rag_documents d WHERE d.doc_id = ({id}) {scope}
            ))
        """).format(id=document_id, scope=scope)
    return clause


def _query(request: SearchRequest, spec: SearchTable, existing: set[str], *, hybrid: bool = False) -> tuple[sql.Composed, tuple]:
    table, id_column, tsv_column = spec.source.name, spec.id_column, spec.tsv_column
    corpus = None
    if request.scoped:
        corpus = "legifrance" if request.source == "dgafp" else request.source
    scope = document_source_clause(corpus)
    chunk_scope = scoped_chunk_filter(existing, corpus)
    section = section_expression(existing, corpus)
    metadata = metadata_expression(existing, corpus)
    base = sql.SQL("""
        SELECT t.{id} AS chunk_id, t.chunk_text, {section} AS section_id, {meta} AS metadata
        FROM {table} t WHERE TRUE {chunk_scope}
    """).format(
        id=sql.Identifier(id_column),
        section=section,
        meta=metadata,
        table=sql.Identifier("public", table),
        chunk_scope=chunk_scope,
    )
    params: tuple
    if request.mode == "vector":
        column = "embedding_m3" if request.embedding_model == "albert" else "embedding_bge_scw"
        ranking = (
            sql.SQL(", ROW_NUMBER() OVER (ORDER BY t.{vector} <=> %s::vector, t.{id}) AS rank").format(
                vector=sql.Identifier(column), id=sql.Identifier(id_column)
            )
            if hybrid
            else sql.SQL("")
        )
        statement = sql.SQL("""
            SELECT t.{id}::text, t.chunk_text, {section}, {meta}, 1 - (t.{vector} <=> %s::vector) AS score {ranking}
            FROM {table} t WHERE t.{vector} IS NOT NULL {chunk_scope}
            ORDER BY t.{vector} <=> %s::vector, t.{id} LIMIT %s
        """).format(
            id=sql.Identifier(id_column),
            section=section,
            meta=metadata,
            vector=sql.Identifier(column),
            table=sql.Identifier("public", table),
            ranking=ranking,
            chunk_scope=chunk_scope,
        )
        vector = "[" + ",".join(str(v) for v in request.embedding) + "]"
        params = (vector, vector, vector, request.limit) if hybrid else (vector, vector, request.limit)
        if not hybrid:
            # Ranking before LIMIT can switch exact semantic search to an ANN
            # index plan. Preserve the legacy query, then number its candidates.
            statement = sql.SQL("SELECT candidates.*, ROW_NUMBER() OVER () AS rank FROM ({}) candidates").format(statement)
    elif request.mode == "lexical":
        # Missing tsvector fails this lane, as in the frozen runtime.
        # Rebuilding it from text would silently tune candidate recall.
        tsv = sql.Identifier("t", tsv_column)
        statement = sql.SQL("""
            WITH parsed AS (SELECT {query} AS q)
            SELECT t.{id}::text, t.chunk_text, {section}, {meta}, ts_rank_cd({tsv}, parsed.q) AS score,
                   ROW_NUMBER() OVER (ORDER BY ts_rank_cd({tsv}, parsed.q) DESC, t.{id}) AS rank
            FROM {table} t CROSS JOIN parsed WHERE {tsv} @@ parsed.q {chunk_scope}
            ORDER BY score DESC, t.{id} LIMIT %s
        """).format(
            query=TSQUERY,
            id=sql.Identifier(id_column),
            section=section,
            meta=metadata,
            table=sql.Identifier("public", table),
            tsv=tsv,
            chunk_scope=chunk_scope,
        )
        params = (request.query, request.query, request.query, request.limit)
    else:
        resolutions = sql.SQL("")
        sections = sql.Identifier("public", "rag_sections")
        candidates = sql.SQL("""
            SELECT t.{id} AS chunk_id, {section} AS section_id, {document} AS explicit_doc_id FROM {table} t
        """).format(
            id=sql.Identifier(id_column),
            section=section,
            document=explicit_document_id(existing),
            table=sql.Identifier("public", table),
        )
        if {"short_id", "section_path"} <= existing:
            # Resolve a legacy section once per document/path, rather than once
            # per chunk and again for joins/order. Keep the historical resolver,
            # including exact-path priority and UUID tie-break, unchanged.
            direct = sql.SQL("t.section_id") if "section_id" in existing else sql.SQL("NULL::uuid")
            unresolved = sql.SQL("WHERE section_id IS NULL") if "section_id" in existing else sql.SQL("")
            legacy_columns = {"short_id", "section_path"}
            canonical_column = sql.SQL("")
            canonical_join = sql.SQL("")
            if corpus is not None and "source_document_id" in existing:
                legacy_columns.add("source_document_id")
                canonical_column = sql.SQL(", source_document_id")
                canonical_join = sql.SQL("AND r.source_document_id IS NOT DISTINCT FROM t.source_document_id")
            relations = sql.SQL("""
                FROM {table} t LEFT JOIN resolved_sections r
                  ON r.short_id = t.short_id AND r.section_path = t.section_path {canonical_join}
                            """).format(table=sql.Identifier("public", table), canonical_join=canonical_join)
            resolutions = sql.SQL("""
                section_keys AS MATERIALIZED (
                    SELECT DISTINCT short_id, section_path {canonical_column} FROM {table} {unresolved}
                ), resolved_sections AS MATERIALIZED (
                    SELECT t.short_id, t.section_path {canonical_column}, {section} AS section_id FROM section_keys t
                ), candidate_sections AS (
                    SELECT DISTINCT COALESCE({direct}, r.section_id) AS section_id {relations}
                ), relevant_sections AS MATERIALIZED (
                    SELECT s.section_id, s.doc_id, s.heading, s.heading_path FROM public.rag_sections s
                    JOIN candidate_sections c ON c.section_id = s.section_id
                ),
            """).format(
                table=sql.Identifier("public", table),
                unresolved=unresolved,
                section=section_expression(legacy_columns, corpus),
                canonical_column=canonical_column,
                direct=direct,
                relations=relations,
            )
            candidates = sql.SQL("""
                SELECT t.{id} AS chunk_id, COALESCE({direct}, r.section_id) AS section_id, {document} AS explicit_doc_id
                {relations}
            """).format(id=sql.Identifier(id_column), direct=direct, document=explicit_document_id(existing), relations=relations)
            # Avoid evaluating French text search over sections absent from this
            # source. Materialize IDs/sections, not chunk text or chunk metadata.
            sections = sql.Identifier("relevant_sections")
            base = sql.SQL("""
                SELECT t.{id} AS chunk_id, t.chunk_text, COALESCE({direct}, r.section_id) AS section_id, {meta} AS metadata
                {relations}
            """).format(
                id=sql.Identifier(id_column),
                direct=direct,
                meta=metadata,
                relations=relations,
            )
        if request.scoped:
            # Score eligible titles once per section; require any explicit
            # document ID to agree with that section before ranking its chunks.
            statement = sql.SQL("""
                WITH parsed AS (SELECT {query} AS q),
                {resolutions}
                candidates AS MATERIALIZED ({candidates}),
                section_scores AS MATERIALIZED (
                    SELECT s.section_id, s.heading, s.heading_path, d.doc_id,
                           d.title, d.source_url, d.short_id,
                           ts_rank_cd(to_tsvector('french', concat_ws(' ', d.title, s.heading, s.heading_path)), parsed.q) AS score
                    FROM {sections} s JOIN public.rag_documents d ON d.doc_id = s.doc_id
                    CROSS JOIN parsed
                    WHERE to_tsvector('french', concat_ws(' ', d.title, s.heading, s.heading_path)) @@ parsed.q {scope}
                ),
                ranked AS MATERIALIZED (
                    SELECT c.chunk_id, s.*,
                           ROW_NUMBER() OVER (ORDER BY s.score DESC, c.chunk_id, s.section_id) AS rank
                    FROM candidates c JOIN section_scores s ON s.section_id = c.section_id
                      AND (c.explicit_doc_id IS NULL OR c.explicit_doc_id = s.doc_id)
                    ORDER BY s.score DESC, c.chunk_id, s.section_id LIMIT %s
                )
                SELECT r.chunk_id::text, t.chunk_text, r.section_id,
                       {metadata} || jsonb_build_object('heading', r.heading, 'heading_path', r.heading_path,
                           'doc_title', r.title, 'doc_url', r.source_url, 'doc_short_id', r.short_id,
                           'source_name', r.title, 'source_document_id', r.short_id),
                       r.score, r.rank
                FROM ranked r JOIN {table} t ON t.{id} = r.chunk_id
                ORDER BY r.rank
            """).format(
                query=TSQUERY,
                resolutions=resolutions,
                candidates=candidates,
                sections=sections,
                scope=scope,
                metadata=metadata,
                table=sql.Identifier("public", table),
                id=sql.Identifier(id_column),
            )
            return statement, (request.query, request.query, request.query, request.limit)
        statement = sql.SQL("""
            WITH parsed AS (SELECT {query} AS q), {resolutions} candidates AS ({base})
            SELECT c.chunk_id::text, c.chunk_text, c.section_id,
                   c.metadata || jsonb_build_object('heading', s.heading, 'heading_path', s.heading_path,
                       'doc_title', d.title, 'doc_url', d.source_url, 'doc_short_id', d.short_id,
                       'source_name', d.title, 'source_document_id', d.short_id),
                   ts_rank_cd(to_tsvector('french', concat_ws(' ', d.title, s.heading, s.heading_path)), parsed.q) AS score,
                   ROW_NUMBER() OVER (ORDER BY
                       ts_rank_cd(to_tsvector('french', concat_ws(' ', d.title, s.heading, s.heading_path)), parsed.q) DESC,
                       c.chunk_id, c.section_id) AS rank
            FROM candidates c JOIN {sections} s ON s.section_id = c.section_id
            LEFT JOIN public.rag_documents d ON d.doc_id = s.doc_id CROSS JOIN parsed
            WHERE to_tsvector('french', concat_ws(' ', d.title, s.heading, s.heading_path)) @@ parsed.q {scope}
            ORDER BY score DESC, c.chunk_id, c.section_id LIMIT %s
        """).format(query=TSQUERY, resolutions=resolutions, base=base, sections=sections, scope=scope)
        params = (request.query, request.query, request.query, request.limit)
    return statement, params


class SearchStore(SearchPort):
    def __init__(self, database: Database, catalog: tuple[SearchTable, ...] | None = None) -> None:
        self._database = database
        tables = search_catalog() if catalog is None else catalog
        if len({table.source.key for table in tables}) != len(tables) or len({table.source.name for table in tables}) != len(tables):
            raise ValueError("duplicate search source")
        self._catalog = MappingProxyType({table.source.key: table for table in tables})

    @property
    def sources(self) -> tuple[RetrievalSource, ...]:
        return tuple(table.source for table in self._catalog.values())

    def _validate(self, request: SearchRequest) -> SearchTable:
        if request.source not in self._catalog:
            raise ValueError("unknown logical source")
        if request.mode not in {"vector", "lexical", "heading"}:
            raise ValueError("unknown search mode")
        if not 1 <= request.limit <= 1000 or not 0 <= request.probes <= 32768:
            raise ValueError("invalid candidate limit or probes")
        if request.embedding_model not in {"albert", "bge_scaleway"}:
            raise ValueError("unknown embedding model")
        if request.mode == "vector" and (not request.embedding or not all(math.isfinite(v) for v in request.embedding)):
            raise ValueError("finite nonempty embedding required")
        return self._catalog[request.source]

    @staticmethod
    def _chunk(request: SearchRequest, row: tuple) -> RawChunk:
        return RawChunk(request.source, row[0], row[1] or "", str(row[2]) if row[2] else None, float(row[4]), int(row[5]), immutable_object(row[3]))

    async def search(self, request: SearchRequest) -> tuple[RawChunk, ...]:
        spec = self._validate(request)
        async with self._database.search_transaction() as connection:
            existing = await columns(connection, spec.source.name)
            statement, params = _query(request, spec, existing)
            if request.mode == "vector" and request.probes:
                await connection.execute("SELECT set_config('ivfflat.probes', %s, true)", (str(request.probes),))
            rows = await (await connection.execute(statement, params)).fetchall()
        return tuple(self._chunk(request, row) for row in rows)

    async def hybrid_candidates(self, request: SearchRequest) -> tuple[tuple[RawChunk, ...], tuple[RawChunk, ...]]:
        """Two raw lanes from one statement snapshot, with no SQL fusion policy."""
        vector_request = replace(request, mode="vector")
        spec = self._validate(vector_request)
        async with self._database.search_transaction() as connection:
            existing = await columns(connection, spec.source.name)
            vector_sql, vector_params = _query(vector_request, spec, existing, hybrid=True)
            lexical_sql, lexical_params = _query(replace(request, mode="lexical"), spec, existing)
            statement = sql.SQL("""
                WITH vector_candidates(chunk_id, chunk_text, section_id, metadata, score, rank) AS ({vector}),
                     lexical_candidates(chunk_id, chunk_text, section_id, metadata, score, rank) AS ({lexical})
                SELECT 0 AS lane, chunk_id, chunk_text, section_id, metadata, score::text, rank FROM vector_candidates
                UNION ALL
                SELECT 1 AS lane, chunk_id, chunk_text, section_id, metadata, score::text, rank FROM lexical_candidates
                ORDER BY lane, rank
            """).format(vector=vector_sql, lexical=lexical_sql)
            # Text preserves each lane's original wire representation. UNION's
            # implicit float4 -> float8 cast otherwise changes lexical scores.
            if request.probes:
                await connection.execute("SELECT set_config('ivfflat.probes', %s, true)", (str(request.probes),))
            rows = await (await connection.execute(statement, vector_params + lexical_params)).fetchall()
        return (
            tuple(self._chunk(request, row[1:]) for row in rows if row[0] == 0),
            tuple(self._chunk(request, row[1:]) for row in rows if row[0] == 1),
        )
