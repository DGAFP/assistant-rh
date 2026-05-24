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
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Job d'ingestion Légifrance: relit les artefacts silver/gold "
            "et applique les UPSERTs en base."
        )
    )
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

    article_config_path = REPO_ROOT / args.article_config
    article_config = load_article_config(article_config_path)
    reference_csv = args.reference_csv or article_config.get("reference_csv_path")
    load_all_artifacts = bool(args.load_all_artifacts or article_config.get("legacy_texts_path"))
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
        raise SystemExit(
            f"Aucun DSN trouvé pour l'ingestion. Passe --dsn ou définis {args.dsn_env}."
        )
    writer = LegifranceDbWriter(
        schema=args.schema,
        dsn=dsn,
        legacy_table_name=args.legacy_table_name,
        modern_table_name=args.modern_table_name,
    )

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
