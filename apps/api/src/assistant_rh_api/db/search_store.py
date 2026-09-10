"""Raw pgvector/lexical/heading candidates. Fusion and scoring policy stay in core."""

import math

from psycopg import sql

from assistant_rh_api.core.db_diagnostics import DBOperation
from assistant_rh_api.core.models.retrieval import RawChunk, SearchRequest
from assistant_rh_api.core.ports.retrieval import SearchPort
from assistant_rh_api.db.content_store import columns, immutable_object, metadata_expression, section_expression, table_spec
from assistant_rh_api.db.pool import Database

# Same OR lexemes as the historical retriever; no alpha/RRF/gate here.
TSQUERY = sql.SQL("""CASE WHEN plainto_tsquery('french', %s)::text = ''
    THEN plainto_tsquery('french', %s)
    ELSE to_tsquery('french', replace(plainto_tsquery('french', %s)::text, ' & ', ' | ')) END""")


class SearchStore(SearchPort):
    def __init__(self, database: Database) -> None:
        self._database = database

    async def search(self, request: SearchRequest) -> tuple[RawChunk, ...]:
        table, id_column, tsv_column = table_spec(request.source)
        if request.mode not in {"vector", "lexical", "heading"}:
            raise ValueError("unknown search mode")
        if not 1 <= request.limit <= 1000 or not 0 <= request.probes <= 32768:
            raise ValueError("invalid candidate limit or probes")
        if request.embedding_model not in {"albert", "bge_scaleway"}:
            raise ValueError("unknown embedding model")
        if request.mode == "vector" and (not request.embedding or not all(math.isfinite(v) for v in request.embedding)):
            raise ValueError("finite nonempty embedding required")
        async with self._database.transaction(read_only=True, operation=DBOperation.SEARCH) as connection:
            existing = await columns(connection, table)
            base = sql.SQL("SELECT t.{id} AS chunk_id, t.chunk_text, {section} AS section_id, {meta} AS metadata FROM {table} t").format(
                id=sql.Identifier(id_column),
                section=section_expression(existing),
                meta=metadata_expression(existing),
                table=sql.Identifier("public", table),
            )
            params: tuple
            if request.mode == "vector":
                column = "embedding_m3" if request.embedding_model == "albert" else "embedding_bge_scw"
                if request.probes:
                    await connection.execute("SELECT set_config('ivfflat.probes', %s, true)", (str(request.probes),))
                statement = sql.SQL("""
                    SELECT t.{id}::text, t.chunk_text, {section}, {meta}, 1 - (t.{vector} <=> %s::vector) AS score
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
                params = (vector, vector, request.limit)
            elif request.mode == "lexical":
                # Missing legacy tsvector is a supported capability: expose a
                # lexical lane built from text, leaving fusion to the caller.
                tsv = sql.Identifier("t", tsv_column) if tsv_column in existing else sql.SQL("to_tsvector('french', t.chunk_text)")
                statement = sql.SQL("""
                    WITH parsed AS (SELECT {query} AS q)
                    SELECT t.{id}::text, t.chunk_text, {section}, {meta}, ts_rank_cd({tsv}, parsed.q) AS score
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
                               'doc_title', d.title, 'doc_url', d.source_url, 'doc_short_id', d.short_id),
                           ts_rank_cd(to_tsvector('french', concat_ws(' ', d.title, s.heading, s.heading_path)), parsed.q) AS score
                    FROM candidates c JOIN public.rag_sections s ON s.section_id = c.section_id
                    LEFT JOIN public.rag_documents d ON d.doc_id = s.doc_id CROSS JOIN parsed
                    WHERE to_tsvector('french', concat_ws(' ', d.title, s.heading, s.heading_path)) @@ parsed.q
                    ORDER BY score DESC, c.chunk_id, c.section_id LIMIT %s
                """).format(query=TSQUERY, base=base)
                params = (request.query, request.query, request.query, request.limit)
            rows = await (await connection.execute(statement, params)).fetchall()
        return tuple(
            RawChunk(request.source, r[0], r[1] or "", str(r[2]) if r[2] else None, float(r[4]), i, immutable_object(r[3]))
            for i, r in enumerate(rows, 1)
        )
