"""Job R2 : résumés d'article en langage métier RH -> lignes d'index additives.

Produit (plan / génération / application) les lignes-résumé de
``rag_chunks_dgafp`` décrites dans ``legifrance/summary_rows.py`` :
embedding_m3 calculé sur le RÉSUMÉ, chunk_text = texte juridique authentique
(« le résumé TROUVE, il ne DIT jamais »).

Trois niveaux, du moins au plus engageant :
- défaut (**plan**) : lecture seule — liste les articles dont la ligne R2 est
  absente ou périmée (delta version + checksum), imprime un résumé JSON ;
- ``--generate`` : appelle Albert (throttle ``MAX_SUMMARY_WORKERS``), alimente
  le cache versionné et, si ``--out``, écrit un JSONL d'inspection
  (article_cid, checksum, résumé, tokens) — AUCUNE écriture en base ;
- ``--apply`` : embedde les résumés (même espace que le retrieval :
  ``ALBERT_EMBED_MODEL``, défaut openweight-embeddings) et upserte les lignes
  via ``LegifranceDbWriter.upsert_legacy_chunks`` (idempotent sur chunk_id).
  **Gaté par revue humaine** — jamais lancé par défaut.

Reprise idempotente : cache-hit = zéro appel LLM ; upsert sur ``chunk_id``
stable = zéro doublon ; article ré-ingéré (checksum changé) = ligne R2 purgée
par le delta d'ingestion puis re-planifiée ici.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
from typing import Any

import psycopg
from dotenv import load_dotenv
from psycopg import sql
from psycopg.rows import dict_row

from assistant_rh_data_engineering.legifrance.db import LegifranceDbWriter
from assistant_rh_data_engineering.legifrance.summary_rows import (
    INDEX_VARIANT_PREFIX,
    SUMMARY_CHUNK_SUFFIX,
    build_index_variant,
    build_summary_chunk_row,
    plan_missing_summaries,
)
from assistant_rh_data_engineering.utils.article_summary import (
    MAX_SUMMARY_WORKERS,
    AlbertArticleSummarizer,
    ArticleSummaryCache,
    SummaryBatchItem,
    summarize_articles,
)

logger = logging.getLogger(__name__)

# Colonnes lues sur la ligne article : la matière de la ligne-résumé
# (chunk_text = source du résumé ET texte servi) + les méta copiées.
_ARTICLE_SELECT_COLUMNS = (
    "cid",
    "chunk_id",
    "chunk_text",
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
    "start_date",
    "end_date",
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Résumés d'article R2 (index additif rag_chunks_dgafp) — plan/generate/apply.")
    parser.add_argument("--dsn-env", default="SCW_POSTGRES_DSN", help="Variable d'environnement portant le DSN Postgres.")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--schema", default="public")
    parser.add_argument("--table", default="rag_chunks_dgafp")
    parser.add_argument("--uid", action="append", default=[], help="Restreindre à ce(s) cid (répétable).")
    parser.add_argument("--uids-file", help="Fichier texte, un cid par ligne.")
    parser.add_argument("--limit", type=int, help="Plafond d'articles traités (après plan).")
    parser.add_argument("--cache-dir", default="data/cache/r2_summaries", help="Racine du cache versionné des résumés.")
    parser.add_argument("--out", help="JSONL d'inspection (un objet par article traité).")
    parser.add_argument("--model", default=None, help="Modèle Albert de génération (défaut: ALBERT_R2_SUMMARY_MODEL ou openweight-medium).")
    parser.add_argument("--max-workers", type=int, default=MAX_SUMMARY_WORKERS)
    parser.add_argument("--generate", action="store_true", help="Appeler le LLM et alimenter cache/--out (pas d'écriture DB).")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Embedder les résumés et upserter les lignes R2 en base (gate revue humaine). Implique --generate.",
    )
    return parser


def _table_has_index_variant(conn: psycopg.Connection, schema: str, table: str) -> bool:
    row = conn.execute(
        """
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = %s AND table_name = %s AND column_name = 'index_variant'
        """,
        (schema, table),
    ).fetchone()
    return row is not None


def fetch_article_rows(
    conn: psycopg.Connection,
    schema: str,
    table: str,
    *,
    uids: list[str] | None = None,
    has_index_variant: bool,
) -> list[dict[str, Any]]:
    """Lignes ARTICLE (jamais les lignes-résumé) de la table legacy.

    Double garde : ``index_variant IS NULL`` quand la colonne existe, et
    exclusion par suffixe de chunk_id sinon (base pas encore migrée).
    """
    columns = sql.SQL(", ").join(sql.Identifier(c) for c in _ARTICLE_SELECT_COLUMNS)
    conditions = [sql.SQL("cid IS NOT NULL"), sql.SQL("COALESCE(chunk_text, '') <> ''")]
    params: list[Any] = []
    if has_index_variant:
        conditions.append(sql.SQL("index_variant IS NULL"))
    conditions.append(sql.SQL("chunk_id NOT LIKE %s"))
    params.append(f"%{SUMMARY_CHUNK_SUFFIX}")
    if uids:
        conditions.append(sql.SQL("UPPER(TRIM(cid)) = ANY(%s)"))
        params.append([str(uid).strip().upper() for uid in uids])
    query = sql.SQL("SELECT {} FROM {}.{} WHERE {} ORDER BY cid, chunk_id").format(
        columns,
        sql.Identifier(schema),
        sql.Identifier(table),
        sql.SQL(" AND ").join(conditions),
    )
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(query, params)
        return [dict(row) for row in cur.fetchall()]


def fetch_existing_variants(
    conn: psycopg.Connection,
    schema: str,
    table: str,
    *,
    has_index_variant: bool,
) -> dict[str, str]:
    """cid -> index_variant des lignes R2 déjà en base (fraîcheur du plan)."""
    if not has_index_variant:
        return {}
    query = sql.SQL("SELECT cid, index_variant FROM {}.{} WHERE index_variant LIKE %s").format(
        sql.Identifier(schema),
        sql.Identifier(table),
    )
    return {str(cid).strip(): str(variant) for cid, variant in conn.execute(query, (f"{INDEX_VARIANT_PREFIX}/%",)).fetchall() if cid}


def _load_uids(args: argparse.Namespace) -> list[str]:
    uids = [str(uid).strip() for uid in (args.uid or []) if str(uid).strip()]
    if args.uids_file:
        for line in Path(args.uids_file).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                uids.append(line)
    return uids


def _item_payload(item: SummaryBatchItem) -> dict[str, Any]:
    return {
        "article_cid": item.uid,
        "checksum": item.checksum,
        "status": item.status,
        "index_variant": None,  # rempli par l'appelant quand un résumé existe
        "summary": item.summary or None,
        "prompt_tokens": item.prompt_tokens,
        "completion_tokens": item.completion_tokens,
        "reason": item.reason or None,
    }


def run(args: argparse.Namespace) -> dict[str, Any]:
    load_dotenv(args.env_file)
    dsn = os.getenv(args.dsn_env, "")
    if not dsn:
        raise SystemExit(f"DSN manquant: variable {args.dsn_env} vide.")

    summarizer = AlbertArticleSummarizer(model=args.model)
    cache = ArticleSummaryCache(args.cache_dir, summarizer.name, summarizer.version)

    uids = _load_uids(args)
    with psycopg.connect(dsn) as conn:
        conn.read_only = not args.apply
        has_variant_col = _table_has_index_variant(conn, args.schema, args.table)
        article_rows = fetch_article_rows(conn, args.schema, args.table, uids=uids or None, has_index_variant=has_variant_col)
        existing = fetch_existing_variants(conn, args.schema, args.table, has_index_variant=has_variant_col)

    todo = plan_missing_summaries(article_rows, existing, summarizer.version)
    if args.limit:
        todo = todo[: max(0, int(args.limit))]

    report: dict[str, Any] = {
        "summarizer": summarizer.name,
        "version": summarizer.version,
        "model": summarizer.model,
        "articles_in_scope": len(article_rows),
        "summaries_up_to_date": len(article_rows) - len(todo),
        "to_generate": len(todo),
        "generated": 0,
        "cached": 0,
        "rejected": 0,
        "failed": 0,
        "applied": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
    }
    if not (args.generate or args.apply):
        report["mode"] = "plan"
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return report

    rows_by_cid = {str(row["cid"]).strip(): row for row in todo}
    articles = [{"uid": cid, "source_text": str(row.get("chunk_text") or "")} for cid, row in rows_by_cid.items()]

    out_path = Path(args.out) if args.out else None
    out_handle = out_path.open("a", encoding="utf-8") if out_path else None
    accepted: list[SummaryBatchItem] = []
    try:

        def _on_result(item: SummaryBatchItem) -> None:
            report[{"ok": "generated", "cached": "cached", "rejected": "rejected", "failed": "failed"}[item.status]] += 1
            report["prompt_tokens"] += item.prompt_tokens
            report["completion_tokens"] += item.completion_tokens
            if item.status in ("ok", "cached"):
                accepted.append(item)
            if out_handle is not None:
                payload = _item_payload(item)
                if item.summary:
                    source_text = str(rows_by_cid.get(item.uid, {}).get("chunk_text") or "")
                    payload["index_variant"] = build_index_variant(summarizer.version, source_text)
                out_handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

        summarize_articles(articles, summarizer, cache, max_workers=args.max_workers, on_result=_on_result)
    finally:
        if out_handle is not None:
            out_handle.close()

    report["mode"] = "generate"
    if args.apply and accepted:
        from assistant_rh_data_engineering.utils.gold import AlbertApiEmbedder

        embed_model = os.getenv("ALBERT_EMBED_MODEL", "openweight-embeddings")
        embedder = AlbertApiEmbedder(model_name=embed_model, column_name="embedding_m3")
        # ⚠️ L'embedding encode le RÉSUMÉ (jamais chunk_text) et est toujours
        # fourni à l'insert — cf. piège backfill (summary_rows.py).
        vectors = embedder.embed_texts([item.summary for item in accepted])
        chunk_rows = [
            build_summary_chunk_row(
                rows_by_cid[item.uid],
                item.summary,
                vector,
                summarizer_version=summarizer.version,
            )
            for item, vector in zip(accepted, vectors, strict=True)
        ]
        writer = LegifranceDbWriter(schema=args.schema, dsn=dsn, legacy_table_name=args.table)
        report["applied"] = writer.upsert_legacy_chunks(chunk_rows)
        report["mode"] = "apply"

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return report


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    args = build_parser().parse_args(argv)
    run(args)


if __name__ == "__main__":
    main()
