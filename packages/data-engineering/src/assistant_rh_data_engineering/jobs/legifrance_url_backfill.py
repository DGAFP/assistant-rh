"""Backfill de ``rag_chunks_dgafp.url`` depuis les artefacts silver (issue #350).

Contexte : la route Légifrance ``article_lc/{id}`` n'accepte que des ids de
VERSION, mais depuis la revue #307 le gold écrivait ``url`` sur le cid
chronique → ~43 % de 404 sur le CGFP (« contenu non disponible ») et affichage
d'anciennes versions pour les articles modifiés. Le gold est corrigé (#350),
mais l'ingestion ``--delta`` réconcilie par checksum silver (texte) : une URL
changée à texte inchangé n'est jamais ré-ingérée. Ce job corrige donc la base
directement : UPDATE-only de la colonne ``url`` (jamais d'INSERT/DELETE), keyé
par ``cid`` chronique, depuis ``metadata.article_id`` (id de version,
``META_COMMUN/ID`` du dump DILA) des documents silver.

L'identité corpus n'est pas touchée : ni ``rag_documents.source_url`` (entrée
du doc_id silver, acquis #307), ni ``cid``/``chunk_id``/embeddings/textes. Les
lignes-résumé R2 (même cid) sont couvertes — voulu. Idempotent
(``IS DISTINCT FROM``), re-runnable. ``--check-only`` est SELECT-only.

Modelé sur ``chunk_text_backfill.py`` (argparse + load_dotenv + psycopg + JSON
summary + exit code) et ``legifrance_ingestion.py`` (hydratation Object
Storage ; la couche silver suffit, pas besoin d'attendre la reconstruction
gold du bump GOLD_DELTA_VERSION).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

import psycopg
from dotenv import load_dotenv
from psycopg import sql

cwd = Path.cwd().resolve()
REPO_ROOT = cwd.parent if cwd.name == "scripts" else cwd
PYTHONPATH_ENTRIES = [
    REPO_ROOT,
    REPO_ROOT / "packages/data-engineering/src",
    REPO_ROOT / "packages/shared-config/src",
]
for entry in reversed(PYTHONPATH_ENTRIES):
    entry_str = str(entry)
    if entry_str not in sys.path:
        sys.path.insert(0, entry_str)

from assistant_rh_data_engineering.legifrance.helpers import build_legifrance_article_url  # noqa: E402

logger = logging.getLogger(__name__)

BATCH_SIZE = 500


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Backfill rag_chunks_dgafp.url (id de version, #350) depuis les artefacts silver Légifrance.",
    )
    parser.add_argument(
        "--lake-root",
        default="data/lake/legifrance",
        help="Racine locale des artefacts bronze/silver/gold.",
    )
    parser.add_argument(
        "--from-object-storage",
        action="store_true",
        help="Télécharge la couche silver depuis les buckets Scaleway avant le backfill.",
    )
    parser.add_argument(
        "--target-env",
        choices=["staging", "prod"],
        default="prod",
        help="Environnement cible : préfixe Object Storage (si --from-object-storage).",
    )
    parser.add_argument("--schema", default="public")
    parser.add_argument("--table", default="rag_chunks_dgafp")
    parser.add_argument("--dsn", help="DSN Postgres cible. Prioritaire sur --dsn-env.")
    parser.add_argument("--dsn-env", default="SCW_POSTGRES_DSN")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument(
        "--check-only",
        "--dry-run",
        dest="check_only",
        action="store_true",
        help=(
            "Audit read-only (autocommit, SELECT-only) : compte les lignes dont l'url diverge de l'url attendue. Exit 1 s'il reste des divergences."
        ),
    )
    return parser


def load_expected_urls(lake_root: Path) -> tuple[dict[str, str], dict[str, int]]:
    """Mapping cid chronique (upper) -> url attendue, depuis silver/documents.

    Skippe les textes legacy (``legacy_qna_source_name``, table moderne, URL non
    concernée par le bug cid) et les documents sans ``metadata.article_id`` (pas
    d'id de version connu : l'URL chronique existante reste le moins-pire).
    """
    documents_dir = lake_root / "silver" / "documents"
    expected: dict[str, str] = {}
    stats = {"documents": 0, "legacy_skipped": 0, "missing_article_id": 0}
    for document_path in sorted(documents_dir.glob("*.document.json")):
        stats["documents"] += 1
        document = json.loads(document_path.read_text(encoding="utf-8"))
        metadata = document.get("metadata") or {}
        if metadata.get("legacy_qna_source_name"):
            stats["legacy_skipped"] += 1
            continue
        version_id = str(metadata.get("article_id") or "").strip()
        if not version_id:
            stats["missing_article_id"] += 1
            continue
        cid = str(metadata.get("cid") or document.get("short_id") or "").strip().upper()
        if not cid:
            continue
        expected[cid] = build_legifrance_article_url(version_id, metadata.get("category"))
    return expected, stats


def _batches(items: list[tuple[str, str]], size: int = BATCH_SIZE) -> list[list[tuple[str, str]]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def _values_clause(pairs: list[tuple[str, str]]) -> sql.Composed:
    return sql.SQL(", ").join(sql.SQL("(%s, %s)") for _ in pairs)


def _flatten(pairs: list[tuple[str, str]]) -> list[str]:
    return [value for pair in pairs for value in pair]


def audit_urls(conn: psycopg.Connection, schema: str, table: str, expected: dict[str, str]) -> dict[str, int]:
    """SELECT-only : lignes couvertes par le mapping et divergences restantes."""
    matched = 0
    mismatched = 0
    with conn.cursor() as cur:
        cur.execute(sql.SQL("SELECT COUNT(*) FROM {schema}.{table}").format(schema=sql.Identifier(schema), table=sql.Identifier(table)))
        total = int(cur.fetchone()[0] or 0)
        for batch in _batches(sorted(expected.items())):
            cur.execute(
                sql.SQL(
                    """
                    SELECT
                        COUNT(*) AS matched,
                        COUNT(*) FILTER (WHERE t.url IS DISTINCT FROM v.url) AS mismatched
                    FROM {schema}.{table} AS t
                    JOIN (VALUES {values}) AS v(cid, url) ON UPPER(TRIM(t.cid)) = v.cid
                    """
                ).format(
                    schema=sql.Identifier(schema),
                    table=sql.Identifier(table),
                    values=_values_clause(batch),
                ),
                _flatten(batch),
            )
            row_matched, row_mismatched = cur.fetchone()
            matched += int(row_matched or 0)
            mismatched += int(row_mismatched or 0)
    return {
        "table_rows": total,
        "rows_matched_by_mapping": matched,
        "rows_not_covered": total - matched,
        "rows_mismatched": mismatched,
    }


def backfill_urls(conn: psycopg.Connection, schema: str, table: str, expected: dict[str, str]) -> int:
    """UPDATE-only de la colonne url (+ updated_at), par lots. Retourne le nombre de lignes modifiées."""
    updated = 0
    with conn.cursor() as cur:
        for batch in _batches(sorted(expected.items())):
            cur.execute(
                sql.SQL(
                    """
                    UPDATE {schema}.{table} AS t
                    SET url = v.url, updated_at = CURRENT_TIMESTAMP
                    FROM (VALUES {values}) AS v(cid, url)
                    WHERE UPPER(TRIM(t.cid)) = v.cid AND t.url IS DISTINCT FROM v.url
                    """
                ).format(
                    schema=sql.Identifier(schema),
                    table=sql.Identifier(table),
                    values=_values_clause(batch),
                ),
                _flatten(batch),
            )
            updated += int(cur.rowcount or 0)
    conn.commit()
    return updated


def main() -> int:
    args = build_parser().parse_args()
    load_dotenv(Path(args.env_file))
    dsn = args.dsn or os.getenv(args.dsn_env)
    if not dsn:
        raise SystemExit(f"Aucun DSN trouvé. Passe --dsn ou définis {args.dsn_env}.")

    lake_root = Path(args.lake_root)
    if not lake_root.is_absolute():
        lake_root = REPO_ROOT / lake_root
    if args.from_object_storage:
        from assistant_rh_data_engineering.utils.object_storage import (
            ObjectStorageConfig,
            ScalewayObjectStorageSync,
        )

        syncer = ScalewayObjectStorageSync(ObjectStorageConfig.from_env())
        syncer.download_medallion_root(
            lake_root,
            args.target_env,
            source_name="legifrance",
            include_layers=("silver",),
        )

    expected, silver_stats = load_expected_urls(lake_root)
    summary: dict[str, Any] = {
        "schema": args.schema,
        "table": args.table,
        "target_env": args.target_env,
        "check_only": bool(args.check_only),
        "silver": silver_stats,
        "expected_urls": len(expected),
    }
    if not expected:
        summary["error"] = f"Aucun document silver exploitable sous {lake_root} (couche silver hydratée ?)."
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 1

    with psycopg.connect(dsn) as conn:
        if args.check_only:
            conn.autocommit = True
            report = audit_urls(conn, args.schema, args.table, expected)
            summary["audit"] = report
            exit_code = 1 if report["rows_mismatched"] else 0
            summary["exit_code"] = exit_code
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            if exit_code:
                logger.warning("%s ligne(s) avec une url divergente.", report["rows_mismatched"])
            return exit_code

        summary["updated_rows"] = backfill_urls(conn, args.schema, args.table, expected)

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
