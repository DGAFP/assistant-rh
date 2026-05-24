from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

import psycopg
from dotenv import load_dotenv
from psycopg.rows import dict_row

cwd = Path.cwd().resolve()
REPO_ROOT = cwd.parent if cwd.name == "scripts" else cwd
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


TABLE_SPECS: dict[str, dict[str, str | list[str]]] = {
    "service_public": {
        "source_table": "public.rag_chunks_service_public",
        "target_table": "public.rag_chunks_service_public_scw",
        "pk": "hash_id",
        "columns": [
            "hash_id",
            "qa_id",
            "parent_qa_id",
            "source_name",
            "section_path",
            "role",
            "chunk_index",
            "text",
            "chunk_text",
            "lang",
            "thematique",
            "short_id",
            "source",
            "embedding_m3",
            "embedding_bge_scw",
        ],
        "select_sql": """
            SELECT
                hash_id,
                qa_id,
                parent_qa_id,
                source_name,
                section_path,
                role,
                chunk_index,
                text,
                chunk_text,
                lang,
                thematique,
                short_id,
                source,
                embedding_m3::text AS embedding_m3,
                embedding_bge_scw::text AS embedding_bge_scw
            FROM public.rag_chunks_service_public
            ORDER BY short_id, qa_id NULLS FIRST, role, chunk_index, hash_id
        """,
        "create_sql": """
            CREATE TABLE IF NOT EXISTS public.rag_chunks_service_public_scw (
                hash_id VARCHAR(64) PRIMARY KEY,
                qa_id TEXT,
                parent_qa_id TEXT,
                source_name VARCHAR(255),
                section_path TEXT,
                role TEXT,
                chunk_index INTEGER,
                text TEXT,
                chunk_text TEXT,
                lang TEXT DEFAULT 'fr',
                thematique TEXT,
                short_id VARCHAR(64),
                source TEXT,
                embedding_m3 vector(1024),
                embedding_bge_scw vector(3584),
                text_tsv tsvector GENERATED ALWAYS AS (
                    to_tsvector('french', coalesce(section_path, '') || ' ' || coalesce(chunk_text, ''))
                ) STORED
            )
        """,
        "insert_sql": """
            INSERT INTO public.rag_chunks_service_public_scw (
                hash_id,
                qa_id,
                parent_qa_id,
                source_name,
                section_path,
                role,
                chunk_index,
                text,
                chunk_text,
                lang,
                thematique,
                short_id,
                source,
                embedding_m3,
                embedding_bge_scw
            ) VALUES (
                %(hash_id)s,
                %(qa_id)s,
                %(parent_qa_id)s,
                %(source_name)s,
                %(section_path)s,
                %(role)s,
                %(chunk_index)s,
                %(text)s,
                %(chunk_text)s,
                %(lang)s,
                %(thematique)s,
                %(short_id)s,
                %(source)s,
                %(embedding_m3)s::vector,
                %(embedding_bge_scw)s::vector
            )
            ON CONFLICT (hash_id) DO UPDATE SET
                qa_id = EXCLUDED.qa_id,
                parent_qa_id = EXCLUDED.parent_qa_id,
                source_name = EXCLUDED.source_name,
                section_path = EXCLUDED.section_path,
                role = EXCLUDED.role,
                chunk_index = EXCLUDED.chunk_index,
                text = EXCLUDED.text,
                chunk_text = EXCLUDED.chunk_text,
                lang = EXCLUDED.lang,
                thematique = EXCLUDED.thematique,
                short_id = EXCLUDED.short_id,
                source = EXCLUDED.source,
                embedding_m3 = EXCLUDED.embedding_m3,
                embedding_bge_scw = EXCLUDED.embedding_bge_scw
        """,
        "post_sql": [
            "CREATE INDEX IF NOT EXISTS idx_rag_chunks_sp_scw_short_id ON public.rag_chunks_service_public_scw (short_id)",
            "CREATE INDEX IF NOT EXISTS idx_rag_chunks_sp_scw_tsv ON public.rag_chunks_service_public_scw USING GIN (text_tsv)",
            "CREATE INDEX IF NOT EXISTS idx_rag_chunks_sp_scw_embedding_m3 ON public.rag_chunks_service_public_scw USING ivfflat (embedding_m3 vector_cosine_ops) WITH (lists = 100)",
        ],
    },
    "dgafp": {
        "source_table": "public.rag_chunks_dgafp",
        "target_table": "public.rag_chunks_dgafp_scw",
        "pk": "chunk_id",
        "columns": [
            "chunk_id",
            "cid",
            "chunk_text",
            "text",
            "number",
            "title",
            "full_title",
            "subtitles",
            "nota",
            "status",
            "category",
            "source_name",
            "ministry",
            "url",
            "section_parent_cid",
            "section_parent_titre",
            "lien_citations",
            "lien_citations_count",
            "lien_modifications",
            "lien_modifications_count",
            "lien_concordes",
            "lien_concordes_count",
            "comporte_liens_sp",
            "chunk_number",
            "start_date",
            "end_date",
            "created_at",
            "updated_at",
            "embedding_m3",
            "embedding_bge_scw",
            "embedding_qwen3",
        ],
        "select_sql": """
            SELECT
                chunk_id,
                cid,
                chunk_text,
                text,
                number,
                title,
                full_title,
                subtitles,
                nota,
                status,
                category,
                source_name,
                ministry,
                url,
                section_parent_cid,
                section_parent_titre,
                lien_citations,
                lien_citations_count,
                lien_modifications,
                lien_modifications_count,
                lien_concordes,
                lien_concordes_count,
                comporte_liens_sp,
                chunk_number,
                start_date,
                end_date,
                created_at,
                updated_at,
                embedding_m3::text AS embedding_m3,
                embedding_bge_scw::text AS embedding_bge_scw,
                embedding_qwen3::text AS embedding_qwen3
            FROM public.rag_chunks_dgafp
            ORDER BY chunk_number, chunk_id
        """,
        "create_sql": """
            CREATE TABLE IF NOT EXISTS public.rag_chunks_dgafp_scw (
                id BIGSERIAL PRIMARY KEY,
                chunk_id VARCHAR(64) UNIQUE,
                cid TEXT,
                chunk_text TEXT,
                text TEXT,
                number TEXT,
                title TEXT,
                full_title TEXT,
                subtitles TEXT,
                nota TEXT,
                status TEXT,
                category TEXT,
                source_name TEXT,
                ministry TEXT,
                url TEXT,
                section_parent_cid TEXT,
                section_parent_titre TEXT,
                lien_citations TEXT,
                lien_citations_count INTEGER,
                lien_modifications TEXT,
                lien_modifications_count INTEGER,
                lien_concordes TEXT,
                lien_concordes_count INTEGER,
                comporte_liens_sp BOOLEAN,
                chunk_number INTEGER,
                created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                start_date DATE,
                end_date DATE,
                chunk_text_tsv tsvector GENERATED ALWAYS AS (
                    to_tsvector('french', COALESCE(chunk_text, ''))
                ) STORED,
                embedding_m3 vector(1024),
                embedding_bge_scw vector(3584),
                embedding_qwen3 vector(4096)
            )
        """,
        "insert_sql": """
            INSERT INTO public.rag_chunks_dgafp_scw (
                chunk_id,
                cid,
                chunk_text,
                text,
                number,
                title,
                full_title,
                subtitles,
                nota,
                status,
                category,
                source_name,
                ministry,
                url,
                section_parent_cid,
                section_parent_titre,
                lien_citations,
                lien_citations_count,
                lien_modifications,
                lien_modifications_count,
                lien_concordes,
                lien_concordes_count,
                comporte_liens_sp,
                chunk_number,
                start_date,
                end_date,
                created_at,
                updated_at,
                embedding_m3,
                embedding_bge_scw,
                embedding_qwen3
            ) VALUES (
                %(chunk_id)s,
                %(cid)s,
                %(chunk_text)s,
                %(text)s,
                %(number)s,
                %(title)s,
                %(full_title)s,
                %(subtitles)s,
                %(nota)s,
                %(status)s,
                %(category)s,
                %(source_name)s,
                %(ministry)s,
                %(url)s,
                %(section_parent_cid)s,
                %(section_parent_titre)s,
                %(lien_citations)s,
                %(lien_citations_count)s,
                %(lien_modifications)s,
                %(lien_modifications_count)s,
                %(lien_concordes)s,
                %(lien_concordes_count)s,
                %(comporte_liens_sp)s,
                %(chunk_number)s,
                %(start_date)s,
                %(end_date)s,
                %(created_at)s,
                %(updated_at)s,
                %(embedding_m3)s::vector,
                %(embedding_bge_scw)s::vector,
                %(embedding_qwen3)s::vector
            )
            ON CONFLICT (chunk_id) DO UPDATE SET
                cid = EXCLUDED.cid,
                chunk_text = EXCLUDED.chunk_text,
                text = EXCLUDED.text,
                number = EXCLUDED.number,
                title = EXCLUDED.title,
                full_title = EXCLUDED.full_title,
                subtitles = EXCLUDED.subtitles,
                nota = EXCLUDED.nota,
                status = EXCLUDED.status,
                category = EXCLUDED.category,
                source_name = EXCLUDED.source_name,
                ministry = EXCLUDED.ministry,
                url = EXCLUDED.url,
                section_parent_cid = EXCLUDED.section_parent_cid,
                section_parent_titre = EXCLUDED.section_parent_titre,
                lien_citations = EXCLUDED.lien_citations,
                lien_citations_count = EXCLUDED.lien_citations_count,
                lien_modifications = EXCLUDED.lien_modifications,
                lien_modifications_count = EXCLUDED.lien_modifications_count,
                lien_concordes = EXCLUDED.lien_concordes,
                lien_concordes_count = EXCLUDED.lien_concordes_count,
                comporte_liens_sp = EXCLUDED.comporte_liens_sp,
                chunk_number = EXCLUDED.chunk_number,
                start_date = EXCLUDED.start_date,
                end_date = EXCLUDED.end_date,
                created_at = EXCLUDED.created_at,
                updated_at = EXCLUDED.updated_at,
                embedding_m3 = EXCLUDED.embedding_m3,
                embedding_bge_scw = EXCLUDED.embedding_bge_scw,
                embedding_qwen3 = EXCLUDED.embedding_qwen3
        """,
        "post_sql": [
            "CREATE INDEX IF NOT EXISTS idx_rag_chunks_dgafp_scw_number ON public.rag_chunks_dgafp_scw (number)",
            "CREATE INDEX IF NOT EXISTS idx_rag_chunks_dgafp_scw_cid ON public.rag_chunks_dgafp_scw (cid)",
            "CREATE INDEX IF NOT EXISTS idx_rag_chunks_dgafp_scw_chunk_text_tsv ON public.rag_chunks_dgafp_scw USING GIN (chunk_text_tsv)",
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_rag_chunks_dgafp_scw_chunk_id_unique ON public.rag_chunks_dgafp_scw (chunk_id)",
        ],
    },
    "legifrance": {
        "source_table": "public.rag_chunks_legifrance",
        "target_table": "public.rag_chunks_legifrance_scw",
        "pk": "hash_id",
        "columns": [
            "hash_id",
            "qa_id",
            "parent_qa_id",
            "source_name",
            "section_path",
            "role",
            "chunk_index",
            "text",
            "chunk_text",
            "lang",
            "thematique",
            "source",
            "short_id",
            "references_juridiques",
            "section_id",
            "source_document_id",
            "chunk_id",
            "chunk_number",
            "title",
            "full_title",
            "number",
            "category",
            "status",
            "subtitles",
            "nota",
            "ministry",
            "url",
            "cid",
            "section_parent_cid",
            "section_parent_titre",
            "lien_citations",
            "lien_citations_count",
            "lien_modifications",
            "lien_modifications_count",
            "lien_concordes",
            "lien_concordes_count",
            "comporte_liens_sp",
            "start_date",
            "end_date",
            "created_at",
            "updated_at",
            "embedding_m3",
            "embedding_bge_scw",
        ],
        "select_sql": """
            SELECT
                hash_id,
                qa_id,
                parent_qa_id,
                source_name,
                section_path,
                role,
                chunk_index,
                text,
                chunk_text,
                lang,
                thematique,
                source,
                short_id,
                references_juridiques::text AS references_juridiques,
                section_id::text AS section_id,
                source_document_id::text AS source_document_id,
                chunk_id,
                chunk_number,
                title,
                full_title,
                number,
                category,
                status,
                subtitles,
                nota,
                ministry,
                url,
                cid,
                section_parent_cid,
                section_parent_titre,
                lien_citations::text AS lien_citations,
                lien_citations_count,
                lien_modifications::text AS lien_modifications,
                lien_modifications_count,
                lien_concordes::text AS lien_concordes,
                lien_concordes_count,
                comporte_liens_sp,
                start_date,
                end_date,
                created_at,
                updated_at,
                embedding_m3::text AS embedding_m3,
                embedding_bge_scw::text AS embedding_bge_scw
            FROM public.rag_chunks_legifrance
            ORDER BY short_id, qa_id NULLS FIRST, role, chunk_index, hash_id
        """,
        "create_sql": """
            CREATE TABLE IF NOT EXISTS public.rag_chunks_legifrance_scw (
                hash_id VARCHAR(64) PRIMARY KEY,
                qa_id TEXT,
                parent_qa_id TEXT,
                source_name TEXT,
                section_path TEXT,
                role TEXT,
                chunk_index INTEGER,
                text TEXT,
                chunk_text TEXT,
                lang TEXT DEFAULT 'fr',
                thematique TEXT,
                source TEXT,
                short_id VARCHAR(64),
                references_juridiques JSONB,
                section_id UUID,
                source_document_id UUID,
                chunk_id VARCHAR(64),
                chunk_number INTEGER,
                title TEXT,
                full_title TEXT,
                number TEXT,
                category TEXT,
                status TEXT,
                subtitles TEXT,
                nota TEXT,
                ministry TEXT,
                url TEXT,
                cid TEXT,
                section_parent_cid TEXT,
                section_parent_titre TEXT,
                lien_citations JSONB,
                lien_citations_count INTEGER,
                lien_modifications JSONB,
                lien_modifications_count INTEGER,
                lien_concordes JSONB,
                lien_concordes_count INTEGER,
                comporte_liens_sp BOOLEAN,
                start_date DATE,
                end_date DATE,
                created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                text_tsv tsvector GENERATED ALWAYS AS (
                    to_tsvector('french', COALESCE(section_path, '') || ' ' || COALESCE(chunk_text, ''))
                ) STORED,
                embedding_m3 vector(1024),
                embedding_bge_scw vector(3584)
            )
        """,
        "insert_sql": """
            INSERT INTO public.rag_chunks_legifrance_scw (
                hash_id,
                qa_id,
                parent_qa_id,
                source_name,
                section_path,
                role,
                chunk_index,
                text,
                chunk_text,
                lang,
                thematique,
                source,
                short_id,
                references_juridiques,
                section_id,
                source_document_id,
                chunk_id,
                chunk_number,
                title,
                full_title,
                number,
                category,
                status,
                subtitles,
                nota,
                ministry,
                url,
                cid,
                section_parent_cid,
                section_parent_titre,
                lien_citations,
                lien_citations_count,
                lien_modifications,
                lien_modifications_count,
                lien_concordes,
                lien_concordes_count,
                comporte_liens_sp,
                start_date,
                end_date,
                created_at,
                updated_at,
                embedding_m3,
                embedding_bge_scw
            ) VALUES (
                %(hash_id)s,
                %(qa_id)s,
                %(parent_qa_id)s,
                %(source_name)s,
                %(section_path)s,
                %(role)s,
                %(chunk_index)s,
                %(text)s,
                %(chunk_text)s,
                %(lang)s,
                %(thematique)s,
                %(source)s,
                %(short_id)s,
                %(references_juridiques)s::jsonb,
                %(section_id)s::uuid,
                %(source_document_id)s::uuid,
                %(chunk_id)s,
                %(chunk_number)s,
                %(title)s,
                %(full_title)s,
                %(number)s,
                %(category)s,
                %(status)s,
                %(subtitles)s,
                %(nota)s,
                %(ministry)s,
                %(url)s,
                %(cid)s,
                %(section_parent_cid)s,
                %(section_parent_titre)s,
                %(lien_citations)s::jsonb,
                %(lien_citations_count)s,
                %(lien_modifications)s::jsonb,
                %(lien_modifications_count)s,
                %(lien_concordes)s::jsonb,
                %(lien_concordes_count)s,
                %(comporte_liens_sp)s,
                %(start_date)s,
                %(end_date)s,
                %(created_at)s,
                %(updated_at)s,
                %(embedding_m3)s::vector,
                %(embedding_bge_scw)s::vector
            )
            ON CONFLICT (hash_id) DO UPDATE SET
                qa_id = EXCLUDED.qa_id,
                parent_qa_id = EXCLUDED.parent_qa_id,
                source_name = EXCLUDED.source_name,
                section_path = EXCLUDED.section_path,
                role = EXCLUDED.role,
                chunk_index = EXCLUDED.chunk_index,
                text = EXCLUDED.text,
                chunk_text = EXCLUDED.chunk_text,
                lang = EXCLUDED.lang,
                thematique = EXCLUDED.thematique,
                source = EXCLUDED.source,
                short_id = EXCLUDED.short_id,
                references_juridiques = EXCLUDED.references_juridiques,
                section_id = EXCLUDED.section_id,
                source_document_id = EXCLUDED.source_document_id,
                chunk_id = EXCLUDED.chunk_id,
                chunk_number = EXCLUDED.chunk_number,
                title = EXCLUDED.title,
                full_title = EXCLUDED.full_title,
                number = EXCLUDED.number,
                category = EXCLUDED.category,
                status = EXCLUDED.status,
                subtitles = EXCLUDED.subtitles,
                nota = EXCLUDED.nota,
                ministry = EXCLUDED.ministry,
                url = EXCLUDED.url,
                cid = EXCLUDED.cid,
                section_parent_cid = EXCLUDED.section_parent_cid,
                section_parent_titre = EXCLUDED.section_parent_titre,
                lien_citations = EXCLUDED.lien_citations,
                lien_citations_count = EXCLUDED.lien_citations_count,
                lien_modifications = EXCLUDED.lien_modifications,
                lien_modifications_count = EXCLUDED.lien_modifications_count,
                lien_concordes = EXCLUDED.lien_concordes,
                lien_concordes_count = EXCLUDED.lien_concordes_count,
                comporte_liens_sp = EXCLUDED.comporte_liens_sp,
                start_date = EXCLUDED.start_date,
                end_date = EXCLUDED.end_date,
                created_at = EXCLUDED.created_at,
                updated_at = EXCLUDED.updated_at,
                embedding_m3 = EXCLUDED.embedding_m3,
                embedding_bge_scw = EXCLUDED.embedding_bge_scw
        """,
        "post_sql": [
            "CREATE INDEX IF NOT EXISTS idx_rag_chunks_legifrance_scw_short_id ON public.rag_chunks_legifrance_scw (short_id)",
            "CREATE INDEX IF NOT EXISTS idx_rag_chunks_legifrance_scw_cid ON public.rag_chunks_legifrance_scw (cid)",
            "CREATE INDEX IF NOT EXISTS idx_rag_chunks_legifrance_scw_section_id ON public.rag_chunks_legifrance_scw (section_id)",
            "CREATE INDEX IF NOT EXISTS idx_rag_chunks_legifrance_scw_text_tsv ON public.rag_chunks_legifrance_scw USING GIN (text_tsv)",
        ],
    },
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Copie les tables chunks Scaleway vers les tables de comparaison _scw sur Scalingo.")
    parser.add_argument("--source-dsn-env", default="SCW_POSTGRES_DSN")
    parser.add_argument("--target-dsn-env", default="DATABASE_URL")
    parser.add_argument("--batch-size", type=int, default=1000)
    parser.add_argument("--keep-existing", action="store_true")
    parser.add_argument(
        "--tables",
        nargs="+",
        choices=sorted(TABLE_SPECS.keys()),
        default=["service_public", "dgafp", "legifrance"],
    )
    return parser


def get_env_or_die(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise SystemExit(f"Variable d'environnement manquante: {name}")
    return value


def resolve_target_dsn(env_name: str) -> str:
    direct_dsn = get_env_or_die(env_name)

    pg_host = os.getenv("PGHOST", "").strip()
    pg_port = os.getenv("PGPORT", "").strip()
    pg_database = os.getenv("PGDATABASE", "").strip()
    pg_user = os.getenv("PGUSER", "").strip()
    if not (pg_host and pg_port and pg_database and pg_user):
        return direct_dsn

    parsed = urlparse(direct_dsn)
    password = os.getenv("PGPASSWORD", "").strip()
    if not password:
        password = unquote(parsed.password or "")
    if not password:
        return direct_dsn

    return (
        f"postgresql://{quote(pg_user)}:{quote(password)}"
        f"@{pg_host}:{pg_port}/{pg_database}?sslmode=prefer"
    )


def chunked(items: list[dict], size: int) -> list[list[dict]]:
    if size <= 0:
        return [items]
    return [items[i : i + size] for i in range(0, len(items), size)]


def sync_table(
    source_conn: psycopg.Connection,
    target_conn: psycopg.Connection,
    table_key: str,
    batch_size: int,
    keep_existing: bool,
) -> dict[str, int | str]:
    spec = TABLE_SPECS[table_key]
    with source_conn.cursor(row_factory=dict_row) as cur:
        source_rows = cur.execute(str(spec["select_sql"])).fetchall()
    rows = [dict(row) for row in source_rows]

    with target_conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
        cur.execute(str(spec["create_sql"]))
        for statement in spec["post_sql"]:  # type: ignore[index]
            cur.execute(statement)
        if not keep_existing:
            cur.execute(f"TRUNCATE TABLE {spec['target_table']}")
        for batch in chunked(rows, batch_size):
            cur.executemany(str(spec["insert_sql"]), batch)
        cur.execute(f"SELECT COUNT(*) FROM {spec['target_table']}")
        target_count = cur.fetchone()[0]
    target_conn.commit()
    return {
        "source_table": str(spec["source_table"]),
        "target_table": str(spec["target_table"]),
        "source_rows": len(rows),
        "target_rows": target_count,
    }


def main() -> int:
    load_dotenv(REPO_ROOT / ".env")
    args = build_parser().parse_args()

    source_dsn = get_env_or_die(args.source_dsn_env)
    target_dsn = resolve_target_dsn(args.target_dsn_env)

    summary: dict[str, dict[str, int | str]] = {}
    with psycopg.connect(source_dsn) as source_conn, psycopg.connect(target_dsn) as target_conn:
        for table_key in args.tables:
            summary[table_key] = sync_table(
                source_conn,
                target_conn,
                table_key,
                args.batch_size,
                args.keep_existing,
            )

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
