"""Capture legacy storage inputs before heading scoring, fusion or aggregation."""

from __future__ import annotations

from contextlib import contextmanager
from threading import local

from scripts.conformance.m0b_values import invocation, plain, require

ACTIVE = local()


@contextmanager
def capture_rows():
    previous = getattr(ACTIVE, "rows", None)
    rows = []
    ACTIVE.rows = rows
    try:
        yield rows
    finally:
        ACTIVE.rows = previous


def recording_cursor():
    import psycopg

    class Cursor(psycopg.Cursor):
        def fetchall(self):
            rows = super().fetchall()
            capture = getattr(ACTIVE, "rows", None)
            columns = {column.name for column in self.description or ()}
            if capture is not None and ("chunk_id" in columns or {"number", "cid"} <= columns):
                capture.append(plain(rows))
            return rows

    return Cursor


def store_call(records, operation, args, response, evidence=None):
    records.append({"operation": operation, "request": invocation(args, {}, operation), "response": plain(response), "evidence": evidence})


def raw_chunks(source, rows):
    from assistant_rh_api.core.models.retrieval import RawChunk

    return tuple(
        RawChunk(
            source,
            str(row["chunk_id"]),
            row["chunk_text"] or "",
            str(row["section_id"]) if row.get("section_id") else None,
            float(row.get("score", row.get("lexical_score", 0))),
            int(row.get("raw_rank", index)),
            {key: value for key, value in row.items() if key not in ("chunk_id", "chunk_text", "section_id", "score", "lexical_score", "raw_rank")},
        )
        for index, row in enumerate(rows, 1)
    )


def hybrid_lanes(retriever, table, statement, params):
    """Read the original CTE ranks, without applying its RRF or final LIMIT."""
    import psycopg
    from psycopg.rows import dict_row

    prefix = statement[: statement.index("            rrf AS (")].rstrip().removesuffix(",")
    start = statement.index("            SELECT\n                t.")
    projection = statement[start : statement.index("            FROM rrf r")]
    embedding_column = table.embed_col_albert if ACTIVE.scope["model"] == "albert" else table.embed_col_bge
    result, evidence = [], []
    for lane, rank, score, extra, extra_params in (
        ("semantic_ranked", "sem_rank", f"1 - (t.{embedding_column} <=> %s::vector)", "", (params[3],)),
        ("lexical_ranked", "lex_rank", f"ts_rank_cd(t.{table.tsv_col}, pq.q)", "CROSS JOIN parsed_query pq", ()),
    ):
        select = projection.replace("SELECT\n", f"SELECT lane.{rank} AS raw_rank,\n", 1).replace("r.rrf_score", score)
        query = prefix + select + f" FROM {lane} lane JOIN {table.name} t ON t.{table.id_col} = lane.chunk_id {extra} ORDER BY lane.{rank}"
        parameters = tuple(params[:7]) + extra_params
        with psycopg.connect(retriever.dsn, row_factory=dict_row) as conn:
            conn.execute("SELECT set_config('ivfflat.probes', %s, true)", (str(retriever.config.ivfflat_probes),))
            rows = conn.execute(query, parameters).fetchall()
        result.append(raw_chunks(ACTIVE.scope["source"], rows))
        evidence.append({"sql": query, "params": plain(parameters), "rows": plain(rows)})
    return tuple(result), evidence


def attach_search(retriever, records):
    from assistant_rh_api.core.models.retrieval import SearchRequest
    from assistant_rh_rag_pipeline.config import CHUNK_TABLES

    sources = {table.name: key for key, table in CHUNK_TABLES.items()}
    original_table = retriever._search_table
    original_exec = retriever._exec_de_table
    original_headings = retriever._search_table_headings

    def table_search(table, embedding, model_used, query, **kwargs):
        ACTIVE.scope = {"source": sources[table.name], "embedding": embedding, "model": model_used, "query": query}
        try:
            return original_table(table, embedding, model_used, query, **kwargs)
        finally:
            del ACTIVE.scope

    def execute(table, statement, params, model_used, **kwargs):
        scope = ACTIVE.scope
        hybrid = "semantic_ranked AS" in statement
        mode = "lexical" if model_used == "lexical" else "vector"
        request = SearchRequest(
            scope["source"], mode, scope["query"], tuple(scope["embedding"]), scope["model"], params[-1], retriever.config.ivfflat_probes
        )
        if hybrid:
            lanes, evidence = hybrid_lanes(retriever, table, statement, params)
            store_call(records, "search.hybrid_candidates", (request,), lanes, evidence)
        with capture_rows() as reads:
            result = original_exec(table, statement, params, model_used, **kwargs)
        require(len(reads) == 1, "Capture every raw retrieval outcome, including empty lanes")
        if not hybrid:
            store_call(
                records,
                "search.search",
                (request,),
                raw_chunks(scope["source"], reads[0]),
                {"sql": statement, "params": plain(params), "rows": reads[0]},
            )
        return result

    def headings(table, query, **kwargs):
        with capture_rows() as reads:
            result = original_headings(table, query, **kwargs)
        require(len(reads) == 1, "Heading lane must be recorded before its score/filter")
        request = SearchRequest(
            sources[table.name],
            "heading",
            query=query,
            limit=kwargs.get("top_k") or retriever.config.initial_top_k,
            probes=retriever.config.ivfflat_probes,
        )
        store_call(records, "search.search", (request,), raw_chunks(sources[table.name], reads[0]), {"rows": reads[0]})
        return result

    retriever._search_table = table_search
    retriever._exec_de_table = execute
    retriever._search_table_headings = headings


def attach_content(aggregator, builder, records):
    from assistant_rh_api.core.models.retrieval import Document, LegalReference, Section

    original_sections = aggregator._fetch_sections
    original_document = builder._load_full_document
    original_references = builder._resolve_cids

    def sections(ids):
        rows = original_sections(ids)
        require(rows is not None, "Successful section read required")
        result = []
        for section_id, row in rows.items():
            document = None
            if row.get("doc_id"):
                document = Document(
                    str(row["doc_id"]),
                    row["doc_short_id"],
                    row["doc_title"],
                    row["doc_url"],
                    row["doc_publisher"],
                    "",
                    row["doc_token_count"],
                    row["doc_date"],
                )
            result.append(
                Section(
                    str(section_id),
                    str(row["doc_id"]) if row["doc_id"] else None,
                    row["heading"] or "",
                    row["heading_path"],
                    row["section_markdown"],
                    row["references_juridiques"],
                    document,
                )
            )
        store_call(records, "content.sections", (tuple(ids),), result, {"rows": plain(rows)})
        return rows

    def document(doc_id):
        row = original_document(doc_id)
        result = (
            ()
            if row is None
            else (
                Document(str(row["doc_id"]), None, row["title"], row["source_url"], row["publisher"], row["doc_markdown"] or "", row["token_count"]),
            )
        )
        store_call(records, "content.documents", ((doc_id,),), result, {"row": plain(row)})
        return row

    def references(numbers):
        if not numbers:
            return original_references(numbers)
        with capture_rows() as reads:
            result = original_references(numbers)
        require(len(reads) == 1, "Reference row order must be recorded")
        rows = tuple(LegalReference(row["number"], row["cid"] or "", row["url"] or "", row["full_title"] or "") for row in reads[0])
        store_call(records, "content.references", (tuple(numbers),), rows, {"rows": reads[0]})
        return result

    aggregator._fetch_sections = sections
    builder._load_full_document = document
    builder._resolve_cids = references
