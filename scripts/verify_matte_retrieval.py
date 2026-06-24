#!/usr/bin/env python3
"""Read-only MATTE embeddings/retrieval verification (issue #159).

Runs aggregate ``SELECT`` queries against a target environment (default
``SCW_POSTGRES_DSN_PROD``) and reports, per the issue #159 acceptance criteria:

* volumetrics + integrity (rows, duplicate ``hash_id``, empty text),
* embedding coverage (``embedding_m3`` canonical, ``embedding_bge_scw`` fallback),
* document linkage (``rag_chunks_matte.source_document_id`` -> ``rag_documents``)
  and document/chunk coverage,
* source URL / storage exploitability on ``rag_documents``,
* vector index presence on the canonical embedding column.

It is **read-only by construction**: every statement is a ``SELECT`` issued on an
autocommit connection; the script never writes, migrates, or creates indexes.
The remediation commands (BGE backfill, HNSW index) are listed by ``--remediation``
but never executed. Exit code is non-zero when a blocking gate fails.

Usage::

    uv run python scripts/verify_matte_retrieval.py --dsn-env SCW_POSTGRES_DSN_PROD
    uv run python scripts/verify_matte_retrieval.py --dsn-env SCW_POSTGRES_DSN_STAGING --markdown
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

import psycopg
from dotenv import load_dotenv
from psycopg import sql

CANONICAL_TABLE = "rag_chunks_matte"
# `embedding_m3` stores BGE-M3 (1024-d) vectors; it is the *canonical* retrieval
# column (Albert primary). `embedding_bge_scw` (3584-d) is read only when the
# Albert circuit-breaker trips and the query is embedded with Scaleway BGE.
CANONICAL_EMBED_COL = "embedding_m3"
FALLBACK_EMBED_COL = "embedding_bge_scw"
DOCUMENTS_TABLE = "rag_documents"
DOCUMENTS_SOURCE = "matte"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Vérification read-only embeddings/retrieval MATTE (issue #159).")
    parser.add_argument("--dsn-env", default="SCW_POSTGRES_DSN_PROD", help="Variable d'env du DSN (défaut: prod).")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--schema", default="public")
    parser.add_argument("--table", default=CANONICAL_TABLE)
    parser.add_argument("--markdown", action="store_true", help="Émet aussi un résumé markdown sur stderr.")
    parser.add_argument("--remediation", action="store_true", help="Imprime les commandes de remédiation (non exécutées).")
    return parser


def _scalar(cur: psycopg.Cursor, query: sql.Composable, params: tuple | None = None) -> Any:
    cur.execute(query, params)
    row = cur.fetchone()
    return row[0] if row else None


def verify(conn: psycopg.Connection, schema: str, table: str) -> dict[str, Any]:
    tbl = sql.Identifier(schema, table)
    docs = sql.Identifier(schema, DOCUMENTS_TABLE)
    report: dict[str, Any] = {"schema": schema, "table": table}
    with conn.cursor() as cur:
        # --- volumetrics + integrity ---
        cur.execute(
            sql.SQL(
                """SELECT COUNT(*),
                          COUNT(DISTINCT hash_id),
                          COUNT(DISTINCT text),
                          COUNT(*) FILTER (WHERE chunk_text IS NULL OR TRIM(chunk_text)=''),
                          COUNT(*) FILTER (WHERE text IS NULL OR TRIM(text)='')
                   FROM {tbl}"""
            ).format(tbl=tbl)
        )
        total, distinct_hash, distinct_text, empty_chunk_text, empty_text = cur.fetchone()
        report["volumetrics"] = {
            "total": total,
            "distinct_hash_id": distinct_hash,
            "duplicate_hash_id": total - distinct_hash,
            "distinct_text": distinct_text,
            "empty_chunk_text": empty_chunk_text,
            "empty_text": empty_text,
        }

        # --- embedding coverage ---
        report["embeddings"] = {}
        for col in (CANONICAL_EMBED_COL, FALLBACK_EMBED_COL):
            non_null = _scalar(
                cur,
                sql.SQL("SELECT COUNT({col}) FROM {tbl}").format(col=sql.Identifier(col), tbl=tbl),
            )
            report["embeddings"][col] = {
                "non_null": non_null,
                "null": total - non_null,
                "coverage_pct": round(100.0 * non_null / total, 2) if total else 0.0,
            }

        # --- document linkage (source_document_id -> rag_documents.legacy_doc_id) ---
        linked = _scalar(cur, sql.SQL("SELECT COUNT(*) FILTER (WHERE source_document_id IS NOT NULL) FROM {tbl}").format(tbl=tbl))
        report["chunk_linkage"] = {"linked": linked, "unlinked": total - linked}

        # --- documents coverage + URL exploitability ---
        cur.execute(
            sql.SQL(
                """SELECT COUNT(*),
                          COUNT(*) FILTER (WHERE source_url IS NOT NULL AND TRIM(source_url)<>''),
                          COUNT(*) FILTER (WHERE storage_path IS NOT NULL AND TRIM(storage_path)<>''),
                          COUNT(*) FILTER (WHERE doc_markdown IS NOT NULL AND TRIM(doc_markdown)<>'')
                   FROM {docs} WHERE source=%s"""
            ).format(docs=docs),
            (DOCUMENTS_SOURCE,),
        )
        doc_total, with_url, with_storage, with_md = cur.fetchone()
        docs_with_chunks = _scalar(
            cur,
            sql.SQL(
                """SELECT COUNT(*) FROM {docs} d WHERE d.source=%s
                   AND EXISTS (SELECT 1 FROM {tbl} c WHERE c.source_document_id = d.legacy_doc_id::text)"""
            ).format(docs=docs, tbl=tbl),
            (DOCUMENTS_SOURCE,),
        )
        report["documents"] = {
            "total": doc_total,
            "with_source_url": with_url,
            "with_storage_path": with_storage,
            "with_markdown": with_md,
            "with_chunks": docs_with_chunks,
            "without_chunks": doc_total - docs_with_chunks,
        }

        # --- vector index on the canonical embedding column ---
        cur.execute(
            sql.SQL("""SELECT indexdef FROM pg_indexes WHERE schemaname=%s AND tablename=%s""").format(),
            (schema, table),
        )
        defs = [r[0] for r in cur.fetchall()]
        report["has_vector_index_on_canonical"] = any(CANONICAL_EMBED_COL in d and ("hnsw" in d.lower() or "ivfflat" in d.lower()) for d in defs)
        report["index_count"] = len(defs)
    return report


def evaluate(report: dict[str, Any]) -> tuple[int, list[str]]:
    """Blocking gates from issue #159 acceptance criteria."""
    problems: list[str] = []
    vol = report["volumetrics"]
    if vol["total"] == 0:
        problems.append(f"{report['table']}: table vide (0 ligne)")
    if vol["duplicate_hash_id"] > 0:
        problems.append(f"{report['table']}: {vol['duplicate_hash_id']} hash_id dupliqué(s)")
    if vol["empty_chunk_text"] > 0:
        problems.append(f"{report['table']}: {vol['empty_chunk_text']} chunk_text vide(s)")
    m3 = report["embeddings"][CANONICAL_EMBED_COL]
    if m3["coverage_pct"] < 100.0:
        problems.append(f"{CANONICAL_EMBED_COL}: couverture {m3['coverage_pct']}% < 100% ({m3['null']} NULL)")
    return (1 if problems else 0), problems


def to_markdown(report: dict[str, Any], problems: list[str]) -> str:
    vol, emb, doc = report["volumetrics"], report["embeddings"], report["documents"]
    lines = [
        f"### Vérification {report['table']} ({report['schema']})",
        "",
        f"- Chunks: **{vol['total']}** (hash_id distincts {vol['distinct_hash_id']}, dup {vol['duplicate_hash_id']}; "
        f"texte vide {vol['empty_chunk_text']})",
        f"- `{CANONICAL_EMBED_COL}` (canonique): **{emb[CANONICAL_EMBED_COL]['coverage_pct']}%** ({emb[CANONICAL_EMBED_COL]['null']} NULL)",
        f"- `{FALLBACK_EMBED_COL}` (fallback): **{emb[FALLBACK_EMBED_COL]['coverage_pct']}%** ({emb[FALLBACK_EMBED_COL]['null']} NULL)",
        f"- Liaison chunks->documents: {report['chunk_linkage']['linked']} liés / {report['chunk_linkage']['unlinked']} non liés",
        f"- Documents `{DOCUMENTS_SOURCE}`: {doc['total']} (avec chunks {doc['with_chunks']}, sans {doc['without_chunks']}); "
        f"source_url {doc['with_source_url']}/{doc['total']}, storage_path {doc['with_storage_path']}/{doc['total']}",
        f"- Index vectoriel sur `{CANONICAL_EMBED_COL}`: {'oui' if report['has_vector_index_on_canonical'] else 'NON (scan séquentiel)'}",
        "",
        ("**Gates: OK**" if not problems else "**Gates en échec:**\n" + "\n".join(f"- {p}" for p in problems)),
    ]
    return "\n".join(lines)


REMEDIATION = """\
-- Remédiation (à valider/approuver — NON exécutée par ce script):
-- 1) Backfill embedding_bge_scw (fallback) sur les 762 chunks legacy:
uv run data-ingestion embeddings backfill --config config/matte_embedding_tables.json \\
  --only-table rag_chunks_matte --only-column embedding_bge_scw --dsn-env SCW_POSTGRES_DSN_PROD
-- 2) Index vectoriel HNSW sur la colonne canonique (hors heures de pointe):
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_rag_chunks_matte_embedding_m3_hnsw
  ON rag_chunks_matte USING hnsw (embedding_m3 vector_cosine_ops);
"""


def main() -> int:
    args = build_parser().parse_args()
    load_dotenv(Path(args.env_file))
    dsn = os.getenv(args.dsn_env)
    if not dsn:
        raise SystemExit(f"{args.dsn_env} manquant.")

    with psycopg.connect(dsn, autocommit=True) as conn:
        report = verify(conn, args.schema, args.table)
    exit_code, problems = evaluate(report)
    report["dsn_env"] = args.dsn_env
    report["problems"] = problems
    report["exit_code"] = exit_code

    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    if args.markdown:
        import sys

        print(to_markdown(report, problems), file=sys.stderr)
    if args.remediation:
        import sys

        print("\n" + REMEDIATION, file=sys.stderr)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
