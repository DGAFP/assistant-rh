from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

cwd = Path.cwd().resolve()
REPO_ROOT = cwd.parent if cwd.name == "scripts" else cwd
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


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


def load_fiche_config(config_path: Path) -> dict[str, Any]:
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Le fichier fiche config doit contenir un objet JSON.")
    return payload


def resolve_short_ids(raw_ids: list[str]) -> list[str]:
    seen: set[str] = set()
    short_ids: list[str] = []
    for raw in raw_ids:
        short_id = str(raw).strip().upper()
        if not short_id or short_id in seen:
            continue
        seen.add(short_id)
        short_ids.append(short_id)
    return short_ids


def load_artifacts(
    lake_root: Path,
    short_ids: list[str],
    *,
    skip_chunks: bool = False,
    tolerate_missing: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], dict[str, dict[str, int]]]:
    silver_documents_dir = lake_root / "silver" / "documents"
    silver_sections_dir = lake_root / "silver" / "sections"
    gold_chunks_dir = lake_root / "gold" / "chunks"

    documents: list[dict[str, Any]] = []
    sections: list[dict[str, Any]] = []
    chunks: list[dict[str, Any]] = []
    per_fiche: dict[str, dict[str, int]] = {}
    errors: list[str] = []
    # Lecture+parsing isolés par artefact : un fichier corrompu est collecté dans
    # `errors` comme un fichier manquant, pour rapporter tout le corpus en un run.
    read_errors = (json.JSONDecodeError, OSError, UnicodeDecodeError)

    for short_id in short_ids:
        document_path = silver_documents_dir / f"{short_id}.document.json"
        sections_path = silver_sections_dir / f"{short_id}.sections.jsonl"
        chunks_path = gold_chunks_dir / f"{short_id}.chunks.jsonl"

        # Mode delta : une fiche ENTIÈREMENT absente du lake (ex. fiche à supprimer
        # jamais reconstruite) est tolérée — la réconciliation la cascade via
        # Grist+corpus, sans artefact. Un artefact PARTIEL/corrompu reste une erreur.
        if tolerate_missing and not document_path.exists() and not sections_path.exists() and not chunks_path.exists():
            per_fiche[short_id] = {"documents": 0, "sections": 0, "chunks": 0}
            continue

        document: dict[str, Any] | None = None
        if document_path.exists():
            try:
                document = read_json(document_path)
            except read_errors as exc:
                errors.append(f"{short_id}: document silver illisible ({document_path}): {exc}")
            else:
                if document:
                    documents.append(document)
                else:
                    errors.append(f"{short_id}: document silver vide ({document_path})")
        else:
            errors.append(f"{short_id}: document silver manquant ({document_path})")

        section_rows: list[dict[str, Any]] = []
        try:
            section_rows = read_jsonl(sections_path) if sections_path.exists() else []
        except read_errors as exc:
            errors.append(f"{short_id}: sections silver illisibles ({sections_path}): {exc}")
        else:
            if section_rows:
                sections.extend(section_rows)
            else:
                errors.append(f"{short_id}: sections silver manquantes ou vides ({sections_path})")

        chunk_rows: list[dict[str, Any]] = []
        if not skip_chunks:
            try:
                chunk_rows = read_jsonl(chunks_path) if chunks_path.exists() else []
            except read_errors as exc:
                errors.append(f"{short_id}: chunks gold illisibles ({chunks_path}): {exc}")
            else:
                if chunk_rows:
                    chunks.extend(chunk_rows)
                else:
                    errors.append(f"{short_id}: chunks gold manquants ou vides ({chunks_path})")

        per_fiche[short_id] = {
            "documents": 1 if document else 0,
            "sections": len(section_rows),
            "chunks": len(chunk_rows),
        }

    if errors:
        detail = "\n".join(f"- {error}" for error in errors)
        raise RuntimeError(f"Artefacts Service-Public incomplets pour l'ingestion:\n{detail}")

    return documents, sections, chunks, per_fiche


def dedupe_chunk_hash_ids(chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: dict[str, int] = {}
    output: list[dict[str, Any]] = []
    for row in chunks:
        item = dict(row)
        hash_id = str(item.get("hash_id", "")).strip()
        if not hash_id:
            seed = "|".join(
                [
                    str(item.get("short_id", "")),
                    str(item.get("qa_id", "")),
                    str(item.get("role", "")),
                    str(item.get("chunk_index", "")),
                    str(item.get("text", "")),
                ]
            )
            hash_id = hashlib.sha1(seed.encode("utf-8")).hexdigest()

        occurrence = seen.get(hash_id, 0)
        if occurrence:
            unique_seed = "|".join(
                [
                    hash_id,
                    str(item.get("short_id", "")),
                    str(item.get("section_path", "")),
                    str(item.get("qa_id", "")),
                    str(item.get("chunk_index", "")),
                    str(occurrence),
                ]
            )
            item["hash_id"] = hashlib.sha1(unique_seed.encode("utf-8")).hexdigest()
        else:
            item["hash_id"] = hash_id

        seen[hash_id] = occurrence + 1
        output.append(item)
    return output


def remap_existing_document_ids(
    documents: list[dict[str, Any]],
    sections: list[dict[str, Any]],
    chunks: list[dict[str, Any]],
    existing_doc_ids_by_short_id: dict[str, str],
    existing_section_ids_by_doc_index: dict[tuple[str, int], str] | None = None,
) -> dict[str, int]:
    """Réécrit les IDs régénérés pour réutiliser ceux déjà en base.

    Mute ``documents``, ``sections`` et ``chunks`` en place: les ``doc_id``
    sont remplacés par ceux trouvés via ``short_id``, les ``section_id`` par
    ceux trouvés via ``(doc_id, section_index)``, et les références
    (``parent_section_id``, ``source_document_id``, ``section_id`` des chunks)
    sont propagées. Retourne le nombre de lignes remappées par type.
    """
    from assistant_rh_data_engineering.utils.helpers import stable_section_uuid

    existing_doc_ids = {str(short_id).strip().upper(): str(doc_id) for short_id, doc_id in existing_doc_ids_by_short_id.items()}
    existing_section_ids = existing_section_ids_by_doc_index or {}
    if not existing_doc_ids:
        return {"documents": 0, "sections": 0, "chunks": 0}

    doc_id_map: dict[str, str] = {}
    remapped_documents = 0
    for document in documents:
        short_id = str(document.get("short_id", "")).strip().upper()
        target_doc_id = existing_doc_ids.get(short_id)
        if not target_doc_id:
            continue

        source_doc_id = str(document.get("doc_id") or "")
        if source_doc_id:
            doc_id_map[source_doc_id] = target_doc_id
        if document.get("doc_id") != target_doc_id:
            document["doc_id"] = target_doc_id
            remapped_documents += 1

    section_id_map: dict[str, str] = {}
    remapped_section_indexes: set[int] = set()
    for index, section in enumerate(sections):
        source_doc_id = str(section.get("doc_id") or "")
        target_doc_id = doc_id_map.get(source_doc_id)
        if not target_doc_id:
            continue

        section_remapped = False
        if section.get("doc_id") != target_doc_id:
            section["doc_id"] = target_doc_id
            section_remapped = True
        section_index = section.get("section_index")
        if section_index is not None:
            section_index_int = int(section_index)
            old_section_id = str(section.get("section_id") or "")
            new_section_id = existing_section_ids.get(
                (target_doc_id, section_index_int),
                stable_section_uuid(target_doc_id, section_index_int),
            )
            if old_section_id and old_section_id != new_section_id:
                section_id_map[old_section_id] = new_section_id
            if section.get("section_id") != new_section_id:
                section["section_id"] = new_section_id
                section_remapped = True
        if section_remapped:
            remapped_section_indexes.add(index)

    for index, section in enumerate(sections):
        parent_section_id = section.get("parent_section_id")
        if parent_section_id in section_id_map:
            section["parent_section_id"] = section_id_map[parent_section_id]
            remapped_section_indexes.add(index)

    remapped_chunks = 0
    for chunk in chunks:
        chunk_remapped = False
        source_document_id = str(chunk.get("source_document_id") or "")
        target_doc_id = doc_id_map.get(source_document_id)
        if target_doc_id and chunk.get("source_document_id") != target_doc_id:
            chunk["source_document_id"] = target_doc_id
            chunk_remapped = True

        section_id = chunk.get("section_id")
        if section_id in section_id_map:
            chunk["section_id"] = section_id_map[section_id]
            chunk_remapped = True
        if chunk_remapped:
            remapped_chunks += 1

    return {
        "documents": remapped_documents,
        "sections": len(remapped_section_indexes),
        "chunks": remapped_chunks,
    }


def _group_artifacts_by_fiche(
    documents: list[dict[str, Any]],
    sections: list[dict[str, Any]],
    chunks: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Regroupe les artefacts chargés par F-code (``short_id`` en upper).

    Les sections ne portent pas de ``short_id`` : elles sont rattachées via
    ``doc_id -> uid`` (le document porte les deux). Les chunks portent ``short_id``.
    Sert au chemin delta, qui ré-ingère fiche par fiche (``ingest_document_bundle``).
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
        uid = str(chunk.get("short_id") or "").strip().upper()
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
    documents: list[dict[str, Any]],
    sections: list[dict[str, Any]],
    chunks: list[dict[str, Any]],
    *,
    source: str = "service_public",
    requested: set[str] | None = None,
    dry_run: bool = False,
    writeback_enabled: bool = True,
    grist_table_id: str | None = None,
) -> dict[str, Any]:
    """Ingestion delta-aware Service-Public (E2.3-a, #289).

    Lit le manifest Grist + l'état corpus, calcule le plan de réconciliation
    (``build_plan``), puis — hors ``dry_run`` — ré-ingère fiche par fiche
    (atomique) les nouvelles/modifiées, cascade les abrogées/retirées, et écrit
    le statut en retour dans Grist. Retourne un résumé JSON (plan + compteurs).
    """
    from assistant_rh_data_engineering.service_public.reconcile import (
        STATUT_ERREUR,
        STATUT_INGERE,
        STATUT_REEL_INGERE,
        STATUT_REEL_NON_TROUVE,
        STATUT_SUPPRIME,
        build_service_public_plan,
        plan_summary,
        select_manifest_rows,
        writeback_fiche,
    )

    records = grist.list_records(grist_table_id) if grist_table_id else grist.list_records()
    manifest_rows = select_manifest_rows(records)
    silver_checksums = {
        str(document.get("short_id") or "").strip().upper(): str(document.get("checksum") or "") for document in documents if document.get("short_id")
    }
    corpus = writer.list_short_ids_with_checksum(source)
    sp_plan = build_service_public_plan(manifest_rows, silver_checksums, corpus, requested=requested)
    plan = sp_plan.plan
    record_ids = sp_plan.record_ids

    summary: dict[str, Any] = {
        "status": "ok",
        "mode": "delta",
        "dry_run": dry_run,
        "source": source,
        "manifest_rows": len(manifest_rows),
        "corpus_documents": len(corpus),
        "plan": plan_summary(sp_plan),
    }
    if dry_run:
        summary["applied"] = {"ingested": 0, "skipped": 0, "deleted": 0, "failed": 0}
        return summary

    bundles = _group_artifacts_by_fiche(documents, sections, chunks)

    def _writeback(uid: str, **kwargs: Any) -> None:
        if writeback_enabled:
            writeback_fiche(grist, record_ids.get(uid), table_id=grist_table_id, **kwargs)

    ingested: list[str] = []
    failures: dict[str, str] = {}
    for uid in plan.to_ingest:
        bundle = bundles.get(uid)
        if bundle is None:
            # Fiche attendue par Grist mais absente du lake (config/artefacts non
            # régénérés) : tracée en erreur, jamais silencieusement sautée.
            failures[uid] = "artefact silver/gold absent du lake"
            continue
        try:
            counts = writer.ingest_document_bundle(bundle["document"], bundle["sections"], bundle["chunks"])
            nb_chunks = int(counts.get("chunks") or 0)
            ingested.append(uid)
            _writeback(
                uid,
                statut=STATUT_INGERE,
                statut_reel=STATUT_REEL_INGERE,
                nb_chunks=nb_chunks,
                hash_contenu=str(bundle["document"].get("checksum") or ""),
            )
        except Exception as exc:  # noqa: BLE001 — erreur par fiche, le run continue
            failures[uid] = str(exc)

    skipped: list[str] = []
    for uid in plan.unchanged:
        nb_chunks = int(corpus.get(uid, {}).get("nb_chunks") or 0)
        skipped.append(uid)
        _writeback(
            uid,
            statut=STATUT_INGERE,
            statut_reel=STATUT_REEL_INGERE,
            nb_chunks=nb_chunks,
            hash_contenu=silver_checksums.get(uid, ""),
        )

    for uid, error in failures.items():
        _writeback(uid, statut=STATUT_ERREUR, erreur=error[:500])

    deleted: list[str] = []
    cascade: dict[str, int] = {}
    removals = list(plan.auto_removals)
    if removals:
        cascade = writer.delete_documents_cascade(removals, source=source)
        deleted = removals
        for uid in removals:
            _writeback(uid, statut=STATUT_SUPPRIME, statut_reel=STATUT_REEL_NON_TROUVE, nb_chunks=0)

    for uid in plan.acknowledged:
        # Abrogée/retirée mais déjà absente du corpus : acquittement terminal.
        _writeback(uid, statut=STATUT_SUPPRIME, statut_reel=STATUT_REEL_NON_TROUVE, nb_chunks=0)

    summary["applied"] = {
        "ingested": len(ingested),
        "skipped": len(skipped),
        "deleted": len(deleted),
        "failed": len(failures),
    }
    summary["ingested"] = sorted(ingested)
    summary["deleted"] = sorted(deleted)
    summary["failed"] = failures
    summary["cascade"] = cascade
    if failures:
        summary["status"] = "partial"
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=("Job d'ingestion Service-Public: relit les artefacts silver/gold et applique les UPSERTs du notebook ingestion_pdf en base.")
    )
    parser.add_argument(
        "--lake-root",
        default="data/lake/service_public",
        help="Racine locale des artefacts bronze/silver/gold.",
    )
    parser.add_argument(
        "--fiche-config",
        default="config/service_public_fiches.json",
        help="JSON listant les fiches à ingérer.",
    )
    parser.add_argument(
        "--fiche-id",
        dest="fiche_ids",
        action="append",
        help="ID de fiche à ingérer, ex: F12391. Peut être répété.",
    )
    parser.add_argument(
        "--target-env",
        choices=["staging", "prod"],
        default="prod",
        help="Préfixe Object Storage à utiliser si --from-object-storage est activé.",
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
        "--skip-chunks",
        action="store_true",
        help="N'ingère pas rag_chunks_service_public.",
    )
    parser.add_argument(
        "--wipe-existing-chunks",
        action="store_true",
        help="Supprime les chunks Service-Public existants des fiches ciblées avant ré-ingestion.",
    )
    parser.add_argument(
        "--delta",
        action="store_true",
        help=(
            "Ingestion delta-aware (socle #288) : lit le référentiel Grist + l'état corpus, "
            "ne (ré)ingère que les fiches nouvelles/modifiées, cascade les abrogées/retirées, "
            "et écrit le statut en retour dans Grist. Sans ce flag : upsert-all (compat)."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Calcule et imprime le plan de réconciliation delta sans aucune écriture (Postgres ni Grist). Implique --delta.",
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
    return parser


def main() -> int:
    from assistant_rh_data_engineering.service_public.db import ServicePublicDbWriter
    from assistant_rh_data_engineering.utils.object_storage import (
        ObjectStorageConfig,
        ScalewayObjectStorageSync,
    )

    load_dotenv(REPO_ROOT / ".env")
    args = build_parser().parse_args()
    if args.wipe_existing_chunks and args.skip_chunks:
        raise SystemExit("--wipe-existing-chunks ne peut pas être combiné avec --skip-chunks.")
    delta = args.delta or args.dry_run
    if delta and args.wipe_existing_chunks:
        raise SystemExit("--wipe-existing-chunks est inutile en mode --delta (ré-ingestion atomique par fiche).")
    if delta and args.skip_chunks:
        raise SystemExit("--skip-chunks ne peut pas être combiné avec --delta (le delta ré-ingère des bundles complets).")

    fiche_config_path = REPO_ROOT / args.fiche_config
    fiche_config = load_fiche_config(fiche_config_path)
    if args.fiche_ids:
        short_ids = resolve_short_ids(list(args.fiche_ids))
    else:
        short_ids = resolve_short_ids(list(fiche_config.get("fiche_ids", [])))
    if not short_ids:
        raise SystemExit("Aucune fiche à ingérer.")

    lake_root = REPO_ROOT / args.lake_root
    if args.from_object_storage:
        syncer = ScalewayObjectStorageSync(ObjectStorageConfig.from_env())
        syncer.download_medallion_root(
            lake_root,
            args.target_env,
            include_layers=("silver", "gold"),
        )

    # En dry-run delta, le plan ne dépend que des checksums silver : inutile
    # d'exiger tout le lake gold pour afficher le plan. En delta, une fiche
    # entièrement absente du lake (fiche à supprimer) est tolérée : la cascade
    # se fait via Grist+corpus (cf. tolerate_missing).
    documents, sections, chunks, per_fiche = load_artifacts(
        lake_root,
        short_ids,
        skip_chunks=args.skip_chunks or (delta and args.dry_run),
        tolerate_missing=delta,
    )
    chunks = dedupe_chunk_hash_ids(chunks)
    dsn = args.dsn or os.getenv(args.dsn_env)
    if not dsn:
        raise SystemExit(f"Aucun DSN trouvé pour l'ingestion. Passe --dsn ou définis {args.dsn_env}.")
    writer = ServicePublicDbWriter(schema=args.schema, dsn=dsn)

    if delta:
        from assistant_rh_data_engineering.utils.grist import GristClient

        grist = GristClient()
        summary = ingest_delta(
            writer,
            grist,
            documents,
            sections,
            chunks,
            source="service_public",
            # Un run ciblé --fiche-id ne réconcilie/supprime QUE le sous-ensemble
            # demandé ; un run complet voit tout le corpus.
            requested=(set(short_ids) if args.fiche_ids else None),
            dry_run=args.dry_run,
            writeback_enabled=not (args.dry_run or args.skip_grist_writeback),
            grist_table_id=args.grist_table_id,
        )
        summary["schema"] = args.schema
        summary["target_env"] = args.target_env
        summary["lake_root"] = str(lake_root)
        summary["short_ids"] = short_ids
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 1 if summary.get("failed") else 0

    existing_doc_ids_by_short_id = writer.list_document_ids_by_short_id(short_ids)
    remapped = remap_existing_document_ids(
        documents,
        sections,
        chunks,
        existing_doc_ids_by_short_id,
        writer.list_section_ids_by_doc_id_and_index(list(existing_doc_ids_by_short_id.values())),
    )

    ingested = {"documents": 0, "sections": 0, "chunks": 0}
    for batch in chunked(documents, args.batch_size):
        ingested["documents"] += writer.upsert_documents(batch)
    for batch in chunked(sections, args.batch_size):
        ingested["sections"] += writer.upsert_sections(batch)
    deleted_existing_chunks = 0
    if args.wipe_existing_chunks:
        deleted_existing_chunks, ingested["chunks"] = writer.replace_chunks_by_short_ids(
            short_ids,
            chunks,
            batch_size=args.batch_size,
        )
    elif not args.skip_chunks:
        for batch in chunked(chunks, args.batch_size):
            ingested["chunks"] += writer.upsert_chunks(batch)

    print(
        json.dumps(
            {
                "status": "ok",
                "schema": args.schema,
                "dsn_env": args.dsn_env,
                "target_env": args.target_env,
                "lake_root": str(lake_root),
                "short_ids": short_ids,
                "loaded": {
                    "documents": len(documents),
                    "sections": len(sections),
                    "chunks": len(chunks),
                },
                "per_fiche": per_fiche,
                "remapped_existing_ids": remapped,
                "wipe_existing_chunks": args.wipe_existing_chunks,
                "deleted_existing_chunks": deleted_existing_chunks,
                "ingested": ingested,
                "from_object_storage": args.from_object_storage,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
