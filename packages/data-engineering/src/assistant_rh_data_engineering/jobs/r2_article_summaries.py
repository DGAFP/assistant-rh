"""Job R2 : résumés d'article en langage métier RH -> lignes d'index additives.

Produit (plan / génération / application) les lignes-résumé de
``rag_chunks_dgafp`` décrites dans ``legifrance/summary_rows.py`` :
embedding_m3 calculé sur le RÉSUMÉ, chunk_text = texte juridique authentique
(« le résumé TROUVE, il ne DIT jamais »).

Trois niveaux, du moins au plus engageant (``--mode``) :
- **plan** (défaut) : lecture seule — liste les articles dont la ligne R2 est
  absente ou périmée (delta version + checksum), imprime un résumé JSON ;
- **generate** : appelle Albert (throttle ``MAX_SUMMARY_WORKERS``), alimente
  le cache versionné et, si ``--out``, écrit un JSONL d'inspection
  (article_cid, checksum, résumé, tokens) — AUCUNE écriture en base ;
- **apply** : exige la confirmation ``--reviewed-cache``, ne génère jamais,
  embedde les résumés du cache (même espace que le retrieval :
  ``ALBERT_EMBED_MODEL``, défaut openweight-embeddings) et upserte les lignes
  via ``LegifranceDbWriter.upsert_legacy_chunks`` (idempotent sur chunk_id).
  **Gaté par revue humaine** — jamais lancé automatiquement.

Reprise idempotente : cache-hit = zéro appel LLM ; upsert sur ``chunk_id``
stable = zéro doublon ; article ré-ingéré (checksum changé) = ligne R2 purgée
par le delta d'ingestion puis re-planifiée ici.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from datetime import UTC, datetime
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
    source_checksum,
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
    parser.add_argument(
        "--mode",
        choices=("plan", "generate", "apply"),
        default=None,
        help="Mode sûr du job serverless. apply consomme uniquement le cache revu et ne génère jamais.",
    )
    parser.add_argument("--target-env", choices=("staging", "prod"), default=None)
    parser.add_argument(
        "--cache-source-env",
        choices=("staging", "prod"),
        default=None,
        help="Environnement dont le cache Gold est hydraté avant le cache cible (promotion staging -> prod).",
    )
    parser.add_argument("--sync-object-storage", action="store_true", help="Hydrater et persister le cache R2 dans le Gold Object Storage.")
    parser.add_argument(
        "--reviewed-cache",
        action="store_true",
        help="Confirmation opérateur obligatoire pour --mode apply : le cache généré a été revu.",
    )
    parser.add_argument(
        "--allow-cache-misses",
        action="store_true",
        help="Appliquer les résumés revus disponibles même si certains articles n'ont pas de cache.",
    )
    # Compatibilité locale historique. L'orchestration officielle utilise
    # exclusivement --mode, dont apply est volontairement cache-only.
    parser.add_argument("--generate", action="store_true", help="Appeler le LLM et alimenter cache/--out (pas d'écriture DB).")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Embedder les résumés et upserter les lignes R2 en base (gate revue humaine). Implique --generate.",
    )
    return parser


def resolve_run_mode(args: argparse.Namespace) -> tuple[str, bool]:
    """Return ``(mode, legacy_generate_during_apply)``.

    ``--mode apply`` est la voie déployée : cache-only + confirmation de revue.
    Les anciens flags restent disponibles pour les scripts locaux historiques,
    sans modifier leur comportement generate+apply.
    """
    explicit = getattr(args, "mode", None)
    legacy_generate = bool(getattr(args, "generate", False))
    legacy_apply = bool(getattr(args, "apply", False))
    if explicit and (legacy_generate or legacy_apply):
        raise ValueError("--mode ne peut pas être combiné avec les anciens flags --generate/--apply.")
    if explicit:
        return str(explicit), False
    if legacy_apply:
        return "apply", True
    if legacy_generate:
        return "generate", False
    return "plan", False


def _cache_storage_location(syncer: Any, target_env: str, summarizer: AlbertArticleSummarizer) -> tuple[str, str]:
    return syncer.medallion_prefix(
        target_env,
        "gold",
        "legifrance",
        f"r2_article_summaries/{summarizer.name}/{summarizer.version}",
    )


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
    """Retain one authoritative source row per legal article (CID).

    Legacy imports may contain several chunks for the same CID.  R2 indexes
    one article per CID, so allowing the later ``rows_by_cid`` dict to choose
    a row would silently make the lexicographically last chunk authoritative.
    The ``{cid}_0`` row is retained when present.  A single row without any
    ``chunk_id`` is also trusted : reduced fetches (doubles de test, tables
    minimales) ne transportent pas la colonne, et sans fragments concurrents
    il n'y a pas d'ambiguïté.  A row carrying an explicit non-``_0`` chunk_id
    is a tail fragment : its CID is returned separately so the plan reports it
    instead of generating from an arbitrary fragment.
    """
    rows_by_cid: dict[str, list[dict[str, Any]]] = {}
    cid_order: list[str] = []

    for row in article_rows:
        cid = str(row.get("cid") or "").strip()
        if not cid:
            continue
        if cid not in rows_by_cid:
            cid_order.append(cid)
            rows_by_cid[cid] = []
        rows_by_cid[cid].append(row)

    canonical: list[dict[str, Any]] = []
    missing_canonical: dict[str, list[str]] = {}
    for cid in cid_order:
        rows = rows_by_cid[cid]
        chosen = next((r for r in rows if str(r.get("chunk_id") or "").strip() == f"{cid}_0"), None)
        if chosen is None and len(rows) == 1 and not str(rows[0].get("chunk_id") or "").strip():
            chosen = rows[0]
        if chosen is not None:
            canonical.append(chosen)
        else:
            missing_canonical[cid] = [str(r.get("chunk_id") or "").strip() for r in rows]
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
    ]
    params: list[Any] = [[str(c).strip().upper() for c in cids], f"%{SUMMARY_CHUNK_SUFFIX}"]
    if has_index_variant:
        conditions.append(sql.SQL("index_variant IS NULL"))
    query = sql.SQL("SELECT cid, chunk_id, chunk_text FROM {}.{} WHERE {} FOR UPDATE").format(
        sql.Identifier(schema), sql.Identifier(table), sql.SQL(" AND ").join(conditions)
    )
    current = conn.execute(query, params).fetchall()
    # Même règle de canonicité que le plan : une ligne unique fait foi, en
    # présence de fragments concurrents seule la ligne ``{cid}_0`` compte —
    # sinon la vérification conclurait « présent » sur un fragment arbitraire.
    canonical_rows, _ = _select_canonical_article_rows(
        [{"cid": cid, "chunk_id": chunk_id, "chunk_text": chunk_text} for cid, chunk_id, chunk_text in current]
    )
    texts = {str(row["cid"]).strip(): str(row.get("chunk_text") or "") for row in canonical_rows}
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

    mode, legacy_generate_during_apply = resolve_run_mode(args)
    if mode == "apply" and not legacy_generate_during_apply and not bool(getattr(args, "reviewed_cache", False)):
        raise RuntimeError("--mode apply exige --reviewed-cache après revue humaine du lot généré.")

    summarizer = AlbertArticleSummarizer(model=args.model)
    cache = ArticleSummaryCache(args.cache_dir, summarizer.name, summarizer.version)
    syncer: Any | None = None
    target_cache_location: tuple[str, str] | None = None
    hydrated_cache_uris: list[str] = []
    target_env = str(getattr(args, "target_env", None) or os.getenv("TARGET_ENV") or "staging")
    if target_env not in {"staging", "prod"}:
        raise ValueError(f"Environnement R2 invalide: {target_env!r}")

    if bool(getattr(args, "sync_object_storage", False)) and mode != "plan":
        from assistant_rh_data_engineering.utils.object_storage import ObjectStorageConfig, ScalewayObjectStorageSync

        syncer = ScalewayObjectStorageSync(ObjectStorageConfig.from_env())
        source_env = str(getattr(args, "cache_source_env", None) or target_env)
        # Promotion de cache : hydrater d'abord staging, puis la cible. Le cache
        # cible gagne en cas de collision et conserve ses deltas déjà générés.
        for cache_env in dict.fromkeys((source_env, target_env)):
            bucket, prefix = _cache_storage_location(syncer, cache_env, summarizer)
            hydrated_cache_uris.append(syncer.download_directory(bucket, prefix, cache.base_dir))
        target_cache_location = _cache_storage_location(syncer, target_env, summarizer)

        def _checkpoint(path: Path, article_uid: str, checksum: str) -> None:
            assert target_cache_location is not None
            bucket, prefix = target_cache_location
            relative = path.relative_to(cache.base_dir).as_posix()
            syncer.upload_object(path, bucket, f"{prefix}/{relative}")

        # Chaque résumé accepté devient durable immédiatement : un timeout du
        # job serverless ne perd pas les heures déjà calculées.
        cache.on_put = _checkpoint

    # Le modèle d'embedding fait partie de la clé de fraîcheur (revue #332) :
    # résolu AVANT le plan, pas seulement à l'apply.
    embed_model = os.getenv("ALBERT_EMBED_MODEL", "openweight-embeddings")

    uids = _load_uids(args)
    with psycopg.connect(dsn) as conn:
        conn.read_only = mode != "apply"
        has_variant_col = _table_has_index_variant(conn, args.schema, args.table)
        fetched_article_rows = fetch_article_rows(conn, args.schema, args.table, uids=uids or None, has_index_variant=has_variant_col)
        article_rows, missing_canonical = _select_canonical_article_rows(fetched_article_rows)
        existing = fetch_existing_variants(conn, args.schema, args.table, has_index_variant=has_variant_col)

    missing = plan_missing_summaries(article_rows, existing, summarizer.version, embed_model=embed_model)
    missing_total = len(missing)
    limit = max(0, int(args.limit)) if args.limit else 0
    if limit and (mode == "generate" or legacy_generate_during_apply):
        # generate n'écrit rien en base : le plan DB resélectionne toujours les
        # mêmes premiers articles. Les cache-hits ne consomment donc pas la
        # limite, sinon les runs par lots (--limit N) resteraient bloqués sur
        # le premier lot déjà généré.
        todo = [row for row in missing if cache.get(str(row["cid"]).strip(), source_checksum(str(row.get("chunk_text") or ""))) is None][:limit]
    else:
        todo = missing[:limit] if limit else missing

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
        "cache_missing": 0,
    }
    if hydrated_cache_uris:
        report["cache_hydrated_from"] = hydrated_cache_uris
    if mode == "plan":
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

        if mode == "generate" or legacy_generate_during_apply:
            summarize_articles(articles, summarizer, cache, max_workers=args.max_workers, on_result=_on_result)
        else:
            # Apply déployé = cache-only. Aucun appel LLM n'est possible entre
            # la revue humaine et l'écriture DB.
            for article in articles:
                uid = str(article["uid"])
                checksum = source_checksum(str(article["source_text"]))
                hit = cache.get(uid, checksum)
                if hit is None:
                    report["cache_missing"] += 1
                    continue
                _on_result(
                    SummaryBatchItem(
                        uid=uid,
                        checksum=checksum,
                        status="cached",
                        summary=str(hit.get("summary") or ""),
                        prompt_tokens=int(hit.get("prompt_tokens") or 0),
                        completion_tokens=int(hit.get("completion_tokens") or 0),
                    )
                )
    finally:
        if out_handle is not None:
            out_handle.close()

    report["mode"] = mode

    if target_cache_location is not None:
        # Copie aussi les hits hydratés depuis staging vers la cible prod. La
        # synchro est additive : les anciennes versions restent auditables.
        bucket, prefix = target_cache_location
        report["cache_persisted_to"] = syncer.sync_directory(cache.base_dir, bucket, prefix)
        if out_path is not None and out_path.exists():
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            inspection = syncer.upload_object(out_path, bucket, f"{prefix}/reports/{stamp}-{mode}.jsonl")
            report["inspection_uri"] = inspection.uri

    if mode == "apply" and report["cache_missing"] and not bool(getattr(args, "allow_cache_misses", False)):
        print(json.dumps(report, ensure_ascii=False, indent=2))
        raise RuntimeError(
            f"Apply R2 interrompu avant écriture: {report['cache_missing']} article(s) sans cache revu. "
            "Relancer generate/revue, ou utiliser explicitement --allow-cache-misses."
        )

    if mode == "apply" and accepted:
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
