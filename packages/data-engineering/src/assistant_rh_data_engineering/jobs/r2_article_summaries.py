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
    source_sha,
    split_stale_sources,
    summary_chunk_id,
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


def _select_canonical_article_rows(article_rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
    """Retain the canonical ``{cid}_0`` source row for each legal article.

    Legacy imports may contain several chunks for the same CID.  R2 indexes
    one article per CID, so allowing the later ``rows_by_cid`` dict to choose
    a row would silently make the lexicographically last chunk authoritative.
    CIDs that have chunks but no canonical ``_0`` row are returned separately
    so the plan reports them instead of generating from an arbitrary fragment.
    """
    canonical_by_cid: dict[str, dict[str, Any]] = {}
    chunk_ids_by_cid: dict[str, list[str]] = {}
    cid_order: list[str] = []

    for row in article_rows:
        cid = str(row.get("cid") or "").strip()
        if not cid:
            continue
        if cid not in chunk_ids_by_cid:
            cid_order.append(cid)
            chunk_ids_by_cid[cid] = []
        chunk_id = str(row.get("chunk_id") or "").strip()
        chunk_ids_by_cid[cid].append(chunk_id)
        if chunk_id == f"{cid}_0":
            canonical_by_cid[cid] = row

    missing_canonical = {cid: chunk_ids_by_cid[cid] for cid in cid_order if cid not in canonical_by_cid}
    canonical = [canonical_by_cid[cid] for cid in cid_order if cid in canonical_by_cid]
    return canonical, missing_canonical


def fetch_article_rows(
    conn: psycopg.Connection,
    schema: str,
    table: str,
    *,
    uids: list[str] | None = None,
    has_index_variant: bool,
    for_update: bool = False,
) -> list[dict[str, Any]]:
    """Lignes ARTICLE (jamais les lignes-résumé) de la table legacy.

    Double garde : ``index_variant IS NULL`` quand la colonne existe, et
    exclusion par suffixe de chunk_id sinon (base pas encore migrée).

    ``for_update`` : verrouille les lignes lues jusqu'au commit de ``conn`` —
    requis par la revalidation pré-upsert de l'apply R2 (revue #332, round 2 :
    sans verrou, une ingestion concurrente peut supprimer/modifier l'article
    ENTRE le SELECT de revalidation et l'upsert).
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
    query = sql.SQL("SELECT {} FROM {}.{} WHERE {} ORDER BY cid, chunk_id{}").format(
        columns,
        sql.Identifier(schema),
        sql.Identifier(table),
        sql.SQL(" AND ").join(conditions),
        sql.SQL(" FOR UPDATE") if for_update else sql.SQL(""),
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


def remove_orphaned_summaries(
    conn: psycopg.Connection,
    schema: str,
    table: str,
    *,
    cids: list[str],
    source_shas: dict[str, str],
    has_index_variant: bool,
) -> dict[str, str]:
    """Compensation POST-commit de l'apply (interleaving EvalPlanQual prouvé
    sur staging par tests/test_r2_pg_interleaving.py) : un DELETE d'ingestion
    resté bloqué derrière notre verrou FOR UPDATE ne voit PAS la ligne R2
    insérée pendant son attente — elle survivrait orpheline avec l'ancien
    texte. Sous un snapshot FRAIS : si l'article a disparu/changé depuis le
    commit, la ligne R2 correspondante est supprimée (elle sera régénérée par
    le prochain plan si l'article revit).

    Sélection minimale (cid + chunk_text) : la compensation ne dépend pas des
    colonnes métier de la table (testable sur une table réduite). FOR UPDATE
    (revue #332 round 3) : si le DELETE d'une ingestion concurrente est encore
    en cours, la vérification BLOQUE derrière ses verrous et ne lit qu'après
    son commit — jamais un snapshot d'avant-suppression qui conclurait à tort
    « article présent »."""
    conditions = [
        sql.SQL("UPPER(TRIM(cid)) = ANY(%s)"),
        sql.SQL("chunk_id NOT LIKE %s"),
        sql.SQL("chunk_id = TRIM(cid) || '_0'"),
    ]
    params: list[Any] = [[str(c).strip().upper() for c in cids], f"%{SUMMARY_CHUNK_SUFFIX}"]
    if has_index_variant:
        conditions.append(sql.SQL("index_variant IS NULL"))
    query = sql.SQL("SELECT cid, chunk_text FROM {}.{} WHERE {} FOR UPDATE").format(
        sql.Identifier(schema), sql.Identifier(table), sql.SQL(" AND ").join(conditions)
    )
    current = conn.execute(query, params).fetchall()
    texts = {str(cid).strip(): str(chunk_text or "") for cid, chunk_text in current}
    _, orphaned = split_stale_sources({cid: source_shas[cid] for cid in cids}, texts)
    if orphaned:
        conn.execute(
            sql.SQL("DELETE FROM {}.{} WHERE chunk_id = ANY(%s)").format(sql.Identifier(schema), sql.Identifier(table)),
            ([summary_chunk_id(cid) for cid in orphaned],),
        )
        conn.commit()
    return orphaned


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
    # Le modèle d'embedding fait partie de la clé de fraîcheur (revue #332) :
    # résolu AVANT le plan, pas seulement à l'apply.
    embed_model = os.getenv("ALBERT_EMBED_MODEL", "openweight-embeddings")

    uids = _load_uids(args)
    with psycopg.connect(dsn) as conn:
        conn.read_only = not args.apply
        has_variant_col = _table_has_index_variant(conn, args.schema, args.table)
        fetched_article_rows = fetch_article_rows(conn, args.schema, args.table, uids=uids or None, has_index_variant=has_variant_col)
        article_rows, missing_canonical = _select_canonical_article_rows(fetched_article_rows)
        existing = fetch_existing_variants(conn, args.schema, args.table, has_index_variant=has_variant_col)

    missing = plan_missing_summaries(article_rows, existing, summarizer.version, embed_model=embed_model)
    missing_total = len(missing)
    todo = missing[: max(0, int(args.limit))] if args.limit else missing

    report: dict[str, Any] = {
        "summarizer": summarizer.name,
        "version": summarizer.version,
        "model": summarizer.model,
        "embed_model": embed_model,
        "articles_in_scope": len(article_rows),
        "source_cids_without_canonical": len(missing_canonical),
        "source_cids_without_canonical_sample": list(missing_canonical)[:20],
        # « à jour » = hors manquants TOTAUX — jamais tronqué par --limit
        # (revue #332 : le rapport annonçait 4 107 à jour avec --limit 100).
        "summaries_up_to_date": len(article_rows) - missing_total,
        "summaries_missing": missing_total,
        "selected_for_run": len(todo),
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
                    payload["index_variant"] = build_index_variant(summarizer.version, source_text, embed_model=embed_model)
                out_handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

        summarize_articles(articles, summarizer, cache, max_workers=args.max_workers, on_result=_on_result)
    finally:
        if out_handle is not None:
            out_handle.close()

    report["mode"] = "generate"
    if args.apply and accepted:
        from assistant_rh_data_engineering.utils.gold import AlbertApiEmbedder

        embedder = AlbertApiEmbedder(model_name=embed_model, column_name="embedding_m3")
        # ⚠️ L'embedding encode le RÉSUMÉ (jamais chunk_text) et est toujours
        # fourni à l'insert — cf. piège backfill (summary_rows.py).
        vectors = embedder.embed_texts([item.summary for item in accepted])
        # Revalidation SOUS TRANSACTION juste avant l'upsert (revue #332) :
        # la génération a duré — une ingestion concurrente a pu supprimer ou
        # modifier l'article ; réinsérer depuis le snapshot recréerait un
        # texte périmé APRÈS la purge delta. Les lignes obsolètes sont
        # ignorées et rapportées, jamais upsertées.
        source_shas = {cid: source_sha(str(row.get("chunk_text") or "")) for cid, row in rows_by_cid.items()}
        writer = LegifranceDbWriter(schema=args.schema, dsn=dsn, legacy_table_name=args.table)
        # DDL AVANT d'ouvrir la transaction apply : ensure passe par sa propre
        # connexion, et son ALTER (ACCESS EXCLUSIVE) se mettrait en file
        # derrière les verrous FOR UPDATE d'apply_conn — auto-deadlock qui a
        # gelé le retrieval staging le 23/07.
        writer.ensure_legacy_target_table()
        has_variant_col = True
        with psycopg.connect(dsn) as apply_conn:
            # FOR UPDATE : les lignes-article restent verrouillées jusqu'au
            # commit — une ingestion concurrente (DELETE/UPDATE par cid) bloque
            # derrière le verrou au lieu de s'intercaler entre la revalidation
            # et l'upsert (revue #332, round 2).
            fetched_current_rows = fetch_article_rows(
                apply_conn,
                args.schema,
                args.table,
                uids=[item.uid for item in accepted],
                has_index_variant=has_variant_col,
                for_update=True,
            )
            current_rows, _missing_current_canonical = _select_canonical_article_rows(fetched_current_rows)
            current_texts = {str(r["cid"]).strip(): str(r.get("chunk_text") or "") for r in current_rows}
            fresh_cids, stale = split_stale_sources({item.uid: source_shas[item.uid] for item in accepted}, current_texts)
            fresh_set = set(fresh_cids)
            fresh_pairs = [(item, vector) for item, vector in zip(accepted, vectors, strict=True) if item.uid in fresh_set]
            # Lignes R2 construites depuis la ligne COURANTE (revue #332
            # round 3) : une modification métadonnées-seules (statut, dates,
            # titre) passe le checksum texte — copier depuis le snapshot de
            # génération persisterait des métadonnées périmées.
            current_by_cid = {str(r["cid"]).strip(): r for r in current_rows}
            chunk_rows = [
                build_summary_chunk_row(
                    current_by_cid[item.uid],
                    item.summary,
                    vector,
                    summarizer_version=summarizer.version,
                    embed_model=embed_model,
                )
                for item, vector in fresh_pairs
            ]
            report["applied"] = writer.upsert_legacy_chunks(chunk_rows, conn=apply_conn) if chunk_rows else 0
            apply_conn.commit()
        report["stale_skipped"] = len(stale)
        if stale:
            report["stale_detail"] = stale
        if chunk_rows:
            with psycopg.connect(dsn) as verify_conn:
                orphaned = remove_orphaned_summaries(
                    verify_conn,
                    args.schema,
                    args.table,
                    cids=[item.uid for item, _ in fresh_pairs],
                    source_shas=source_shas,
                    has_index_variant=has_variant_col,
                )
            report["orphans_removed"] = len(orphaned)
            if orphaned:
                report["orphans_detail"] = orphaned
        report["mode"] = "apply"

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return report


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    args = build_parser().parse_args(argv)
    run(args)


if __name__ == "__main__":
    main()
