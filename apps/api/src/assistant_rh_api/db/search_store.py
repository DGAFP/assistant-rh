"""Raw pgvector/lexical/heading candidates. Fusion and scoring policy stay in core."""

import math
from dataclasses import replace
from types import MappingProxyType

from psycopg import sql

from assistant_rh_api.core.models.retrieval import RawChunk, RetrievalSource, SearchRequest
from assistant_rh_api.core.ports.retrieval import SearchPort
from assistant_rh_api.db.content_store import columns, immutable_object, metadata_expression, section_expression
from assistant_rh_api.db.pool import Database
from assistant_rh_api.db.search_catalog import SearchTable, search_catalog

# Same OR lexemes as the historical retriever; no alpha/RRF/gate here.
TSQUERY = sql.SQL("""CASE WHEN plainto_tsquery('french', %s)::text = ''
    THEN plainto_tsquery('french', %s)
    ELSE to_tsquery('french', replace(plainto_tsquery('french', %s)::text, ' & ', ' | ')) END""")


def _query(request: SearchRequest, spec: SearchTable, existing: set[str]) -> tuple[sql.Composed, tuple]:
    table, id_column, tsv_column = spec.source.name, spec.id_column, spec.tsv_column
    base = sql.SQL("SELECT t.{id} AS chunk_id, t.chunk_text, {section} AS section_id, {meta} AS metadata FROM {table} t").format(
        id=sql.Identifier(id_column),
        section=section_expression(existing),
        meta=metadata_expression(existing),
        table=sql.Identifier("public", table),
    )
    params: tuple
    if request.mode == "vector":
        column = "embedding_m3" if request.embedding_model == "albert" else "embedding_bge_scw"
        statement = sql.SQL("""
            SELECT t.{id}::text, t.chunk_text, {section}, {meta}, 1 - (t.{vector} <=> %s::vector) AS score,
                   ROW_NUMBER() OVER (ORDER BY t.{vector} <=> %s::vector, t.{id}) AS rank
            FROM {table} t WHERE t.{vector} IS NOT NULL
            ORDER BY t.{vector} <=> %s::vector, t.{id} LIMIT %s
        """).format(
            id=sql.Identifier(id_column),
            section=section_expression(existing),
            meta=metadata_expression(existing),
            vector=sql.Identifier(column),
            table=sql.Identifier("public", table),
        )
        vector = "[" + ",".join(str(v) for v in request.embedding) + "]"
        params = (vector, vector, vector, request.limit)
    elif request.mode == "lexical":
        # Missing tsvector fails this lane, as in the frozen runtime.
        # Rebuilding it from text would silently tune candidate recall.
        tsv = sql.Identifier("t", tsv_column)
        statement = sql.SQL("""
            WITH parsed AS (SELECT {query} AS q)
            SELECT t.{id}::text, t.chunk_text, {section}, {meta}, ts_rank_cd({tsv}, parsed.q) AS score,
                   ROW_NUMBER() OVER (ORDER BY ts_rank_cd({tsv}, parsed.q) DESC, t.{id}) AS rank
            FROM {table} t CROSS JOIN parsed WHERE {tsv} @@ parsed.q
            ORDER BY score DESC, t.{id} LIMIT %s
        """).format(
            query=TSQUERY,
            id=sql.Identifier(id_column),
            section=section_expression(existing),
            meta=metadata_expression(existing),
            table=sql.Identifier("public", table),
            tsv=tsv,
        )
        params = (request.query, request.query, request.query, request.limit)
    else:
        statement = sql.SQL("""
            WITH parsed AS (SELECT {query} AS q), candidates AS ({base})
            SELECT c.chunk_id::text, c.chunk_text, c.section_id,
                   c.metadata || jsonb_build_object('heading', s.heading, 'heading_path', s.heading_path,
                       'doc_title', d.title, 'doc_url', d.source_url, 'doc_short_id', d.short_id,
                       'source_name', d.title, 'source_document_id', d.short_id),
                   ts_rank_cd(to_tsvector('french', concat_ws(' ', d.title, s.heading, s.heading_path)), parsed.q) AS score,
                   ROW_NUMBER() OVER (ORDER BY
                       ts_rank_cd(to_tsvector('french', concat_ws(' ', d.title, s.heading, s.heading_path)), parsed.q) DESC,
                       c.chunk_id, c.section_id) AS rank
            FROM candidates c JOIN public.rag_sections s ON s.section_id = c.section_id
            LEFT JOIN public.rag_documents d ON d.doc_id = s.doc_id CROSS JOIN parsed
            WHERE to_tsvector('french', concat_ws(' ', d.title, s.heading, s.heading_path)) @@ parsed.q
            ORDER BY score DESC, c.chunk_id, c.section_id LIMIT %s
        """).format(query=TSQUERY, base=base)
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
        async with self._database.transaction(read_only=True) as connection:
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
        async with self._database.transaction(read_only=True) as connection:
            existing = await columns(connection, spec.source.name)
            vector_sql, vector_params = _query(vector_request, spec, existing)
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
