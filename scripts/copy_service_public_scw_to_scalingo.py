from __future__ import annotations

import argparse
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


CREATE_TARGET_TABLE_SQL = """
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
);

CREATE INDEX IF NOT EXISTS idx_rag_chunks_sp_scw_short_id
    ON public.rag_chunks_service_public_scw (short_id);

CREATE INDEX IF NOT EXISTS idx_rag_chunks_sp_scw_tsv
    ON public.rag_chunks_service_public_scw USING GIN (text_tsv);

CREATE INDEX IF NOT EXISTS idx_rag_chunks_sp_scw_embedding_m3
    ON public.rag_chunks_service_public_scw USING ivfflat (embedding_m3 vector_cosine_ops) WITH (lists = 100);
"""


SOURCE_SELECT_SQL = """
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
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Copie la table Service-Public simplifiée de la DB Scaleway "
            "vers une table de comparaison sur la DB actuelle."
        )
    )
    parser.add_argument(
        "--source-dsn-env",
        default="SCW_POSTGRES_DSN",
        help="Variable d'environnement contenant le DSN source.",
    )
    parser.add_argument(
        "--target-dsn-env",
        default="DATABASE_URL",
        help="Variable d'environnement contenant le DSN cible.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=1000,
        help="Nombre de lignes insérées par lot.",
    )
    parser.add_argument(
        "--keep-existing",
        action="store_true",
        help="N'efface pas la table cible avant recharge.",
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


def main() -> int:
    load_dotenv(REPO_ROOT / ".env")
    args = build_parser().parse_args()

    source_dsn = get_env_or_die(args.source_dsn_env)
    target_dsn = resolve_target_dsn(args.target_dsn_env)

    with psycopg.connect(source_dsn, row_factory=dict_row) as source_conn:
        source_rows = source_conn.execute(SOURCE_SELECT_SQL).fetchall()

    rows = [dict(row) for row in source_rows]

    insert_sql = """
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
    """

    with psycopg.connect(target_dsn) as target_conn:
        with target_conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
            cur.execute(CREATE_TARGET_TABLE_SQL)
            if not args.keep_existing:
                cur.execute("TRUNCATE TABLE public.rag_chunks_service_public_scw")
            for batch in chunked(rows, args.batch_size):
                cur.executemany(insert_sql, batch)
            cur.execute(
                "SELECT COUNT(*) FROM public.rag_chunks_service_public_scw"
            )
            target_count = cur.fetchone()[0]
        target_conn.commit()

    print(
        {
            "status": "ok",
            "source_rows": len(rows),
            "target_rows": target_count,
            "target_table": "public.rag_chunks_service_public_scw",
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
