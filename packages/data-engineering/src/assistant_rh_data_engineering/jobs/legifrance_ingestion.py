from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

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


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def chunked(items: list[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
    if size <= 0:
        return [items]
    return [items[i : i + size] for i in range(0, len(items), size)]


def load_article_config(config_path: Path) -> dict[str, Any]:
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Le fichier article config doit contenir un objet JSON.")
    return payload


def normalize_article_number(value: str) -> str:
    return re.sub(r"[.\s]+", "", (value or "").strip())


def resolve_short_ids(raw_numbers: list[str]) -> list[str]:
    seen: set[str] = set()
    short_ids: list[str] = []
    for raw in raw_numbers:
        article_number = str(raw).strip()
        if not article_number:
            continue
        short_id = normalize_article_number(article_number)
        if short_id in seen:
            continue
        seen.add(short_id)
        short_ids.append(short_id)
    return short_ids


def resolve_short_ids_from_reference_csv(csv_path: Path) -> list[str]:
    import csv

    csv.field_size_limit(10**8)
    with csv_path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        seen: set[str] = set()
        short_ids: list[str] = []
        for row in reader:
            cid = str(row.get("cid") or "").strip()
            if not cid or cid in seen:
                continue
            seen.add(cid)
            short_ids.append(cid)
    return short_ids


def load_artifacts(
    lake_root: Path,
    short_ids: list[str] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    silver_documents_dir = lake_root / "silver" / "documents"
    silver_sections_dir = lake_root / "silver" / "sections"
    gold_chunks_dir = lake_root / "gold" / "chunks"

    documents: list[dict[str, Any]] = []
    sections: list[dict[str, Any]] = []
    chunks: list[dict[str, Any]] = []

    if short_ids:
        for short_id in short_ids:
            document_path = silver_documents_dir / f"{short_id}.document.json"
            sections_path = silver_sections_dir / f"{short_id}.sections.jsonl"
            chunks_path = gold_chunks_dir / f"{short_id}.chunks.jsonl"
            if document_path.exists():
                documents.append(read_json(document_path))
            sections.extend(read_jsonl(sections_path))
            chunks.extend(read_jsonl(chunks_path))
        return documents, sections, chunks

    for document_path in sorted(silver_documents_dir.glob("*.document.json")):
        documents.append(read_json(document_path))
    for sections_path in sorted(silver_sections_dir.glob("*.sections.jsonl")):
        sections.extend(read_jsonl(sections_path))
    for chunks_path in sorted(gold_chunks_dir.glob("*.chunks.jsonl")):
        chunks.extend(read_jsonl(chunks_path))

    return documents, sections, chunks


def dedupe_chunk_ids(chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: dict[str, int] = {}
    output: list[dict[str, Any]] = []
    for row in chunks:
        item = dict(row)
        chunk_id = str(item.get("chunk_id") or item.get("hash_id") or "").strip()
        if not chunk_id:
            seed = "|".join(
                [
                    str(item.get("short_id", "")),
                    str(item.get("qa_id", "")),
                    str(item.get("role", "")),
                    str(item.get("chunk_index", "")),
                    str(item.get("text", "")),
                ]
            )
            chunk_id = "sha1:" + hashlib.sha1(seed.encode("utf-8")).hexdigest()

        occurrence = seen.get(chunk_id, 0)
        if occurrence:
            unique_seed = "|".join(
                [
                    chunk_id,
                    str(item.get("short_id", "")),
                    str(item.get("section_path", "")),
                    str(item.get("qa_id", "")),
                    str(item.get("chunk_index", "")),
                    str(occurrence),
                ]
            )
            unique_id = "sha1:" + hashlib.sha1(unique_seed.encode("utf-8")).hexdigest()
            item["chunk_id"] = unique_id
            item["hash_id"] = unique_id
        else:
            item["chunk_id"] = chunk_id
            item["hash_id"] = chunk_id

        seen[chunk_id] = occurrence + 1
        output.append(item)
    return output


def _group_artifacts_by_uid(
    documents: list[dict[str, Any]],
    sections: list[dict[str, Any]],
    chunks: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Regroupe les artefacts chargés par uid (short_id en upper).

    Les sections sont rattachées via ``doc_id -> uid`` ; les chunks portent
    ``short_id`` (textes) ou ``cid`` (articles). Sert au chemin delta, qui
    ré-ingère document par document.
    """
    docs_by_uid: dict[str, dict[str, Any]] = {}
    docid_to_uid: dict[str, str] = {}
    for document in documents:
        uid = str(document.get("short_id") or "").strip().upper()
        if not uid:
            continue
        docs_by_uid[uid] = document
        doc_id = str(document.get("doc_id") or "")
        if doc_id:
            docid_to_uid[doc_id] = uid

    sections_by_uid: dict[str, list[dict[str, Any]]] = {uid: [] for uid in docs_by_uid}
    for section in sections:
        uid = docid_to_uid.get(str(section.get("doc_id") or ""))
        if uid is not None:
            sections_by_uid[uid].append(section)

    chunks_by_uid: dict[str, list[dict[str, Any]]] = {uid: [] for uid in docs_by_uid}
    for chunk in chunks:
        uid = str(chunk.get("short_id") or chunk.get("cid") or "").strip().upper()
        if uid in chunks_by_uid:
            chunks_by_uid[uid].append(chunk)

    return {
        uid: {
            "document": docs_by_uid[uid],
            "sections": sections_by_uid.get(uid, []),
            "chunks": chunks_by_uid.get(uid, []),
        }
        for uid in docs_by_uid
    }


def ingest_delta(
    writer: Any,
    grist: Any,
    piste: Any,
    documents: list[dict[str, Any]],
    sections: list[dict[str, Any]],
    chunks: list[dict[str, Any]],
    *,
    source: str = "legifrance",
    requested: set[str] | None = None,
    dry_run: bool = False,
    writeback_enabled: bool = True,
    grist_table_id: str | None = None,
    target_env: str = "prod",
    toc_date_millis: int | None = None,
) -> dict[str, Any]:
    """Ingestion delta-aware Légifrance (E2.3-b, #289).

    Manifest = référentiel Grist (lignes ``legifrance_texte``) + follow-live
    PISTE (articles du/des codes suivis, lignes ``legifrance_code``). Hors
    ``dry_run`` : ré-ingère new/changed document par document (atomique, route
    articles → table legacy / textes → table moderne), cascade les
    abrogés/retirés/stale, et écrit le statut en Grist — per-ligne pour les
    textes, **agrégé** sur la ligne code suivi pour ses ~2500 articles.
    """
    import time as _time

    import requests as _requests

    from assistant_rh_data_engineering.legifrance.piste import PisteError, walk_table_matieres
    from assistant_rh_data_engineering.legifrance.reconcile import (
        STATUT_ERREUR,
        STATUT_INGERE,
        STATUT_REEL_INGERE,
        STATUT_REEL_NON_TROUVE,
        STATUT_SUPPRIME,
        build_legifrance_plan,
        build_writeback_fields,
        is_article_uid,
        plan_summary,
        select_legifrance_rows,
        writeback_fiches,
    )

    records = grist.list_records(grist_table_id) if grist_table_id else grist.list_records()
    selection = select_legifrance_rows(records)

    toc_by_legitext: dict[str, Any] = {}
    date_millis = toc_date_millis if toc_date_millis is not None else int(_time.time() * 1000)
    for row in selection.code_rows:
        if not row.active:
            continue
        try:
            payload = piste.table_matieres(row.uid, date_millis)
        except _requests.RequestException as exc:
            raise PisteError(f"tableMatieres({row.uid}) en échec: {exc}") from exc
        toc_by_legitext[row.uid] = walk_table_matieres(payload)

    silver_checksums = {
        str(document.get("short_id") or "").strip().upper(): str(document.get("checksum") or "") for document in documents if document.get("short_id")
    }
    corpus = writer.list_legifrance_corpus(source)
    lf_plan = build_legifrance_plan(selection, toc_by_legitext, silver_checksums, corpus, requested=requested)
    plan = lf_plan.plan

    summary: dict[str, Any] = {
        "status": "ok",
        "mode": "delta",
        "dry_run": dry_run,
        "source": source,
        "manifest_rows": len(selection.rows),
        "corpus_documents": len(corpus),
        "plan": plan_summary(lf_plan),
    }
    if dry_run:
        summary["applied"] = {"ingested": 0, "skipped": 0, "deleted": 0, "failed": 0}
        return summary

    bundles = _group_artifacts_by_uid(documents, sections, chunks)

    writebacks: list[tuple[int, dict[str, Any]]] = []
    texte_rows_by_uid = {row.uid: row for row in selection.texte_rows}

    def _writeback_texte(uid: str, **kwargs: Any) -> None:
        record_id = lf_plan.texte_record_ids.get(uid)
        if writeback_enabled and record_id is not None:
            writebacks.append((record_id, build_writeback_fields(env=target_env, **kwargs)))

    ingested: list[str] = []
    ingested_chunks: dict[str, int] = {}
    failures: dict[str, str] = {}
    for uid in plan.to_ingest:
        bundle = bundles.get(uid)
        if bundle is None:
            failures[uid] = "artefact silver/gold absent du lake"
            continue
        try:
            if is_article_uid(uid):
                counts = writer.ingest_article_bundle(bundle["document"], bundle["sections"], bundle["chunks"])
            else:
                counts = writer.ingest_texte_bundle(bundle["document"], bundle["sections"], bundle["chunks"])
            nb_chunks = int(counts.get("chunks") or 0)
            ingested.append(uid)
            ingested_chunks[uid] = nb_chunks
            if not is_article_uid(uid):
                _writeback_texte(
                    uid,
                    statut=STATUT_INGERE,
                    statut_reel=STATUT_REEL_INGERE,
                    nb_chunks=nb_chunks,
                    hash_contenu=str(bundle["document"].get("checksum") or ""),
                    corpus_present=True,
                )
        except Exception as exc:  # noqa: BLE001 — erreur par document, le run continue
            failures[uid] = str(exc)

    skipped: list[str] = []
    for uid in plan.unchanged:
        skipped.append(uid)
        if not is_article_uid(uid):
            _writeback_texte(
                uid,
                statut=STATUT_INGERE,
                statut_reel=STATUT_REEL_INGERE,
                nb_chunks=int(corpus.get(uid, {}).get("nb_chunks") or 0),
                hash_contenu=silver_checksums.get(uid, ""),
                corpus_present=True,
            )

    for uid, error in failures.items():
        if not is_article_uid(uid):
            # Réalité corpus inconnue après un échec : toggle non touché.
            _writeback_texte(uid, statut=STATUT_ERREUR, erreur=error[:500])

    removals = list(plan.auto_removals)
    article_removals = [uid for uid in removals if is_article_uid(uid)]
    texte_removals = [uid for uid in removals if not is_article_uid(uid)]
    cascade: dict[str, int] = {"chunks": 0, "sections": 0, "documents": 0}
    if article_removals:
        counts = writer.delete_articles_cascade(article_removals, source=source)
        for key in cascade:
            cascade[key] += int(counts.get(key) or 0)
    if texte_removals:
        counts = writer.delete_textes_cascade(texte_removals, source=source)
        for key in cascade:
            cascade[key] += int(counts.get(key) or 0)
    for uid in texte_removals:
        _writeback_texte(uid, statut=STATUT_SUPPRIME, statut_reel=STATUT_REEL_NON_TROUVE, nb_chunks=0, corpus_present=False)

    for uid in plan.acknowledged:
        if is_article_uid(uid):
            continue
        row = texte_rows_by_uid.get(uid)
        already_terminal = row is not None and str(row.fields.get("statut") or "").strip().lower() == STATUT_SUPPRIME
        if not already_terminal:
            _writeback_texte(uid, statut=STATUT_SUPPRIME, statut_reel=STATUT_REEL_NON_TROUVE, nb_chunks=0, corpus_present=False)

    # Writeback AGRÉGÉ des lignes code suivi : 1 ligne Grist ↔ ~2500 articles.
    code_rows_by_uid = {row.uid: row for row in selection.code_rows}
    for legitext, record_id in lf_plan.code_record_ids.items():
        if not writeback_enabled:
            continue
        arts = lf_plan.code_articles.get(legitext, frozenset())
        code_row = code_rows_by_uid.get(legitext)
        art_ingested = [uid for uid in ingested if uid in arts]
        art_unchanged = [uid for uid in plan.unchanged if uid in arts]
        art_failed = sorted(uid for uid in failures if uid in arts)
        present = len(art_ingested) + len(art_unchanged)
        nb_chunks_total = sum(ingested_chunks.get(uid, 0) for uid in art_ingested) + sum(
            int(corpus.get(uid, {}).get("nb_chunks") or 0) for uid in art_unchanged
        )
        if code_row is not None and code_row.abrogated:
            statut = STATUT_SUPPRIME
            erreur = ""
        elif art_failed:
            statut = STATUT_ERREUR
            erreur = f"{len(art_failed)} articles en échec (ex: {', '.join(art_failed[:5])})"
        else:
            statut = STATUT_INGERE
            erreur = ""
        writebacks.append(
            (
                record_id,
                build_writeback_fields(
                    statut=statut,
                    statut_reel=STATUT_REEL_INGERE if present else STATUT_REEL_NON_TROUVE,
                    nb_chunks=nb_chunks_total,
                    erreur=erreur,
                    env=target_env,
                    corpus_present=present > 0,
                ),
            )
        )

    writeback_fiches(grist, writebacks, table_id=grist_table_id)

    summary["applied"] = {
        "ingested": len(ingested),
        "skipped": len(skipped),
        "deleted": len(removals),
        "failed": len(failures),
    }
    summary["ingested"] = sorted(ingested)
    summary["deleted"] = sorted(removals)
    summary["failed"] = failures
    summary["cascade"] = cascade
    if failures:
        summary["status"] = "partial"
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=("Job d'ingestion Légifrance: relit les artefacts silver/gold et applique les UPSERTs en base."))
    parser.add_argument(
        "--lake-root",
        default="data/lake/legifrance",
        help="Racine locale des artefacts bronze/silver/gold.",
    )
    parser.add_argument(
        "--article-config",
        default="config/legifrance_articles.json",
        help="JSON listant les articles à ingérer.",
    )
    parser.add_argument(
        "--article-number",
        dest="article_numbers",
        action="append",
        help="Article à ingérer, ex: R.331-7. Peut être répété.",
    )
    parser.add_argument(
        "--reference-csv",
        help="Export CSV de référence rag_chunks_dgafp pour charger les artefacts générés en mode migration.",
    )
    parser.add_argument(
        "--load-all-artifacts",
        action="store_true",
        help="Ignore la résolution par short_id et ingère tous les artefacts présents dans le lake root.",
    )
    parser.add_argument(
        "--target-env",
        choices=["staging", "prod"],
        default="prod",
        help=(
            "Environnement cible : préfixe Object Storage (si --from-object-storage) et routage du "
            "writeback Grist en mode --delta (prod = statut canonique + ingere_prod ; staging = ingere_staging seul)."
        ),
    )
    parser.add_argument(
        "--delta",
        action="store_true",
        help=(
            "Ingestion delta-aware (socle #288) : manifest = référentiel Grist (textes legacy) + follow-live "
            "PISTE (articles du code suivi), ne (ré)ingère que le new/changed, cascade abrogés/retirés/stale, "
            "writeback Grist. Sans ce flag : upsert-all (compat)."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Calcule et imprime le plan de réconciliation delta sans aucune écriture (Postgres ni Grist). Implique --delta.",
    )
    parser.add_argument(
        "--uid",
        dest="uids",
        action="append",
        help="Mode delta : restreint la réconciliation à cet uid (CID LEGIARTI ou short_id texte). Peut être répété.",
    )
    parser.add_argument(
        "--skip-grist-writeback",
        action="store_true",
        help="Mode delta : n'écrit pas le statut d'ingestion en retour dans Grist.",
    )
    parser.add_argument(
        "--grist-table-id",
        default=None,
        help="Table Grist du référentiel (défaut : variable d'environnement GRIST_TABLE_ID).",
    )
    parser.add_argument(
        "--schema",
        default="public",
        help="Schéma Postgres cible.",
    )
    parser.add_argument(
        "--dsn-env",
        default="SCW_POSTGRES_DSN",
        help="Variable d'environnement DSN utilisée par ce job d'ingestion.",
    )
    parser.add_argument(
        "--dsn",
        help="DSN Postgres cible. Prioritaire sur --dsn-env.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=1000,
        help="Taille des lots UPSERT.",
    )
    parser.add_argument(
        "--from-object-storage",
        action="store_true",
        help="Télécharge silver/gold depuis les buckets Scaleway avant ingestion.",
    )
    parser.add_argument(
        "--skip-documents",
        action="store_true",
        help="N'ingère pas rag_documents.",
    )
    parser.add_argument(
        "--skip-sections",
        action="store_true",
        help="N'ingère pas rag_sections.",
    )
    parser.add_argument(
        "--skip-chunks",
        action="store_true",
        help="N'ingère ni rag_chunks_dgafp ni rag_chunks_legifrance.",
    )
    parser.add_argument(
        "--legacy-table-name",
        default="rag_chunks_dgafp",
        help="Table legacy cible des chunks.",
    )
    parser.add_argument(
        "--modern-table-name",
        default="rag_chunks_legifrance",
        help="Table canonique cible des chunks.",
    )
    return parser


def main() -> int:
    from assistant_rh_data_engineering.legifrance.db import LegifranceDbWriter
    from assistant_rh_data_engineering.utils.object_storage import (
        ObjectStorageConfig,
        ScalewayObjectStorageSync,
    )

    load_dotenv(REPO_ROOT / ".env")
    args = build_parser().parse_args()
    delta = args.delta or args.dry_run
    if delta and (args.skip_documents or args.skip_sections or args.skip_chunks):
        raise SystemExit("--skip-documents/--skip-sections/--skip-chunks incompatibles avec --delta (le delta ré-ingère des bundles complets).")

    article_config_path = REPO_ROOT / args.article_config
    article_config = load_article_config(article_config_path)
    reference_csv = args.reference_csv or article_config.get("reference_csv_path")
    # En delta, le manifest vient de Grist+PISTE : on charge tout le lake
    # (tolérant), jamais la sélection config.
    load_all_artifacts = bool(delta or args.load_all_artifacts or article_config.get("legacy_texts_path"))
    raw_numbers = list(article_config.get("article_numbers", [])) + list(args.article_numbers or [])
    if reference_csv:
        reference_csv_path = (REPO_ROOT / reference_csv).resolve() if not Path(reference_csv).is_absolute() else Path(reference_csv)
        short_ids = resolve_short_ids_from_reference_csv(reference_csv_path)
    else:
        reference_csv_path = None
        short_ids = resolve_short_ids(raw_numbers)
    if not short_ids and not load_all_artifacts:
        raise SystemExit("Aucun article à ingérer.")

    lake_root = REPO_ROOT / args.lake_root
    if args.from_object_storage:
        syncer = ScalewayObjectStorageSync(ObjectStorageConfig.from_env())
        syncer.download_medallion_root(
            lake_root,
            args.target_env,
            source_name="legifrance",
            include_layers=("silver", "gold"),
        )

    documents, sections, chunks = load_artifacts(
        lake_root,
        None if load_all_artifacts else short_ids,
    )
    chunks = dedupe_chunk_ids(chunks)
    dsn = args.dsn or os.getenv(args.dsn_env)
    if not dsn:
        raise SystemExit(f"Aucun DSN trouvé pour l'ingestion. Passe --dsn ou définis {args.dsn_env}.")
    writer = LegifranceDbWriter(
        schema=args.schema,
        dsn=dsn,
        legacy_table_name=args.legacy_table_name,
        modern_table_name=args.modern_table_name,
    )

    if delta:
        from assistant_rh_data_engineering.legifrance.piste import PisteClient, PisteError
        from assistant_rh_data_engineering.utils.grist import GristClient, GristError

        try:
            grist = GristClient()
            piste = PisteClient()
            summary = ingest_delta(
                writer,
                grist,
                piste,
                documents,
                sections,
                chunks,
                source="legifrance",
                # Un run ciblé --uid ne réconcilie/supprime QUE le sous-ensemble
                # demandé ; un run complet voit tout le corpus.
                requested=({str(uid).strip().upper() for uid in args.uids} if args.uids else None),
                dry_run=args.dry_run,
                writeback_enabled=not (args.dry_run or args.skip_grist_writeback),
                grist_table_id=args.grist_table_id,
                target_env=args.target_env,
            )
        except (GristError, PisteError) as exc:
            # Config/fetch/contrat Grist ou PISTE en échec : message opérateur
            # propre, aucun plan destructif n'a été appliqué.
            raise SystemExit(f"Échec Grist/PISTE en mode --delta (aucune écriture corpus): {exc}") from exc
        summary["schema"] = args.schema
        summary["target_env"] = args.target_env
        summary["lake_root"] = str(lake_root)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 1 if summary.get("failed") else 0

    ingested = {"documents": 0, "sections": 0, "legacy_chunks": 0, "modern_chunks": 0}
    if not args.skip_documents:
        for batch in chunked(documents, args.batch_size):
            ingested["documents"] += writer.upsert_documents(batch)
    if not args.skip_sections:
        for batch in chunked(sections, args.batch_size):
            ingested["sections"] += writer.upsert_sections(batch)
    if not args.skip_chunks:
        for batch in chunked(chunks, args.batch_size):
            ingested["legacy_chunks"] += writer.upsert_legacy_chunks(batch)
            ingested["modern_chunks"] += writer.upsert_modern_chunks(batch)

    print(
        json.dumps(
            {
                "status": "ok",
                "schema": args.schema,
                "dsn_env": args.dsn_env,
                "target_env": args.target_env,
                "lake_root": str(lake_root),
                "short_ids": short_ids,
                "reference_csv_path": str(reference_csv_path) if reference_csv_path else None,
                "legacy_table_name": args.legacy_table_name,
                "modern_table_name": args.modern_table_name,
                "loaded": {
                    "documents": len(documents),
                    "sections": len(sections),
                    "chunks": len(chunks),
                },
                "ingested": ingested,
                "from_object_storage": args.from_object_storage,
                "load_all_artifacts": load_all_artifacts,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
