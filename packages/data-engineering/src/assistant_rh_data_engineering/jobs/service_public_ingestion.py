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

        document: dict[str, Any] | None = None
        if document_path.exists():
            try:
                document = read_json(document_path)
            except read_errors as exc:
                errors.append(f"{short_id}: document silver illisible ({document_path}): {exc}")
            if document:
                documents.append(document)
            elif document is not None:
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
) -> dict[str, int]:
    from assistant_rh_data_engineering.utils.helpers import stable_section_uuid

    existing_doc_ids = {str(short_id).strip().upper(): str(doc_id) for short_id, doc_id in existing_doc_ids_by_short_id.items()}
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
        if source_doc_id and source_doc_id != target_doc_id:
            doc_id_map[source_doc_id] = target_doc_id
        if document.get("doc_id") != target_doc_id:
            document["doc_id"] = target_doc_id
            remapped_documents += 1

    section_id_map: dict[str, str] = {}
    remapped_sections = 0
    for section in sections:
        source_doc_id = str(section.get("doc_id") or "")
        target_doc_id = doc_id_map.get(source_doc_id)
        if not target_doc_id:
            continue

        section["doc_id"] = target_doc_id
        section_index = section.get("section_index")
        if section_index is not None:
            old_section_id = str(section.get("section_id") or "")
            new_section_id = stable_section_uuid(target_doc_id, int(section_index))
            if old_section_id and old_section_id != new_section_id:
                section_id_map[old_section_id] = new_section_id
            section["section_id"] = new_section_id
        remapped_sections += 1

    for section in sections:
        parent_section_id = section.get("parent_section_id")
        if parent_section_id in section_id_map:
            section["parent_section_id"] = section_id_map[parent_section_id]

    remapped_chunks = 0
    for chunk in chunks:
        source_document_id = str(chunk.get("source_document_id") or "")
        target_doc_id = doc_id_map.get(source_document_id)
        if target_doc_id:
            chunk["source_document_id"] = target_doc_id
            remapped_chunks += 1

        section_id = chunk.get("section_id")
        if section_id in section_id_map:
            chunk["section_id"] = section_id_map[section_id]

    return {
        "documents": remapped_documents,
        "sections": remapped_sections,
        "chunks": remapped_chunks,
    }


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
    return parser


def main() -> int:
    from assistant_rh_data_engineering.service_public.db import ServicePublicDbWriter
    from assistant_rh_data_engineering.utils.object_storage import (
        ObjectStorageConfig,
        ScalewayObjectStorageSync,
    )

    load_dotenv(REPO_ROOT / ".env")
    args = build_parser().parse_args()

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

    documents, sections, chunks, per_fiche = load_artifacts(
        lake_root,
        short_ids,
        skip_chunks=args.skip_chunks,
    )
    chunks = dedupe_chunk_hash_ids(chunks)
    dsn = args.dsn or os.getenv(args.dsn_env)
    if not dsn:
        raise SystemExit(f"Aucun DSN trouvé pour l'ingestion. Passe --dsn ou définis {args.dsn_env}.")
    writer = ServicePublicDbWriter(schema=args.schema, dsn=dsn)
    remapped = remap_existing_document_ids(
        documents,
        sections,
        chunks,
        writer.list_document_ids_by_short_id(short_ids),
    )

    ingested = {"documents": 0, "sections": 0, "chunks": 0}
    for batch in chunked(documents, args.batch_size):
        ingested["documents"] += writer.upsert_documents(batch)
    for batch in chunked(sections, args.batch_size):
        ingested["sections"] += writer.upsert_sections(batch)
    if not args.skip_chunks:
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
