from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

cwd = Path.cwd().resolve()
REPO_ROOT = cwd.parent if cwd.name == "scripts" else cwd
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def chunked(items: list[str], size: int) -> list[list[str]]:
    if size <= 0:
        return [items]
    return [items[i : i + size] for i in range(0, len(items), size)]


def load_fiche_config(config_path: Path) -> dict[str, Any]:
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Le fichier de config JSON doit contenir un objet JSON.")
    return payload


def resolve_fiche_ids(raw_ids: list[str]) -> list[str]:
    seen: set[str] = set()
    fiche_ids: list[str] = []
    for raw_id in raw_ids:
        fiche_id = str(raw_id).strip().upper()
        if not fiche_id or fiche_id in seen:
            continue
        seen.add(fiche_id)
        fiche_ids.append(fiche_id)
    return fiche_ids


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Pipeline médaillon Service-Public "
            "basé sur le XML officiel DILA."
        )
    )
    parser.add_argument(
        "--fiche-id",
        dest="fiche_ids",
        action="append",
        help="ID de fiche à traiter, ex: F12391. Peut être répété.",
    )
    parser.add_argument(
        "--fiche-config",
        default="config/service_public_fiches.json",
        help="Fichier JSON de configuration des fiches à traiter.",
    )
    parser.add_argument(
        "--situation",
        choices=["FPE", "FPT", "FPH"],
        help="Filtre une situation XML quand la fiche expose plusieurs onglets.",
    )
    parser.add_argument(
        "--batch-from-db",
        action="store_true",
        help="Mode migration uniquement: récupère les FXXX depuis la DB.",
    )
    parser.add_argument(
        "--batch-table",
        default="rag_chunks_service_public",
        help="Table DB source pour récupérer les FXXX.",
    )
    parser.add_argument(
        "--batch-column",
        default="short_id",
        help="Colonne DB contenant les IDs FXXX.",
    )
    parser.add_argument(
        "--batch-limit",
        type=int,
        help="Limite le nombre de fiches récupérées depuis la DB.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=100,
        help="Taille des lots de traitement après le fetch XML.",
    )
    parser.add_argument(
        "--lake-root",
        default="data/lake/service_public",
        help="Racine locale du data lake.",
    )
    parser.add_argument(
        "--target-env",
        choices=["staging", "prod"],
        default="prod",
        help="Préfixe Object Storage cible. `prod` par défaut.",
    )
    parser.add_argument(
        "--sync-object-storage",
        action="store_true",
        help="Synchronise bronze/silver/gold vers les buckets Scaleway.",
    )
    parser.add_argument(
        "--no-embed",
        action="store_true",
        help="Désactive la génération d'embeddings en gold.",
    )
    parser.add_argument(
        "--with-scaleway-bge",
        action="store_true",
        help="Ajoute embedding_bge_scw via l'API Scaleway.",
    )
    parser.add_argument(
        "--ingest",
        action="store_true",
        help="Pousse rag_documents/rag_sections/rag_chunks_service_public en base.",
    )
    parser.add_argument(
        "--schema",
        default="public",
        help="Schéma Postgres cible.",
    )
    return parser


def main() -> int:
    from dotenv import load_dotenv

    from assistant_rh_data_engineering.service_public import (
        ServicePublicPipeline,
        ServicePublicPipelineConfig,
    )
    from assistant_rh_data_engineering.service_public.config import LakePaths
    from assistant_rh_data_engineering.service_public.db import ServicePublicDbWriter
    from assistant_rh_data_engineering.utils.object_storage import (
        ObjectStorageConfig,
        ScalewayObjectStorageSync,
    )

    load_dotenv(REPO_ROOT / ".env")

    args = build_parser().parse_args()

    config = ServicePublicPipelineConfig(paths=LakePaths(root_dir=Path(args.lake_root)))
    fiche_config_path = Path(args.fiche_config)
    fiche_config = load_fiche_config(fiche_config_path)
    fiche_ids = resolve_fiche_ids(
        list(fiche_config.get("fiche_ids", [])) + list(args.fiche_ids or [])
    )
    situation = args.situation or fiche_config.get("situation")
    if args.batch_from_db:
        db = ServicePublicDbWriter(schema=args.schema)
        fiche_ids = resolve_fiche_ids(
            db.list_fiche_ids(
                table=args.batch_table,
                id_column=args.batch_column,
                limit=args.batch_limit,
            )
        )
    if not fiche_ids:
        raise SystemExit(
            "Aucune fiche fournie. Renseigne config/service_public_fiches.json, "
            "--fiche-id, ou utilise --batch-from-db pour la migration."
        )
    config.fiche_ids = fiche_ids or None
    config.silver.situation_filter = situation
    if args.no_embed:
        config.embeddings.enable_m3 = False
        config.embeddings.enable_bge_scaleway = False
    elif args.with_scaleway_bge:
        config.embeddings.enable_bge_scaleway = True

    pipeline = ServicePublicPipeline(config)
    bronze_assets = pipeline.run_bronze()
    silver_bundles = []
    gold_bundles = []

    for batch in chunked(bronze_assets, args.batch_size):
        silver_batch = pipeline.run_silver(batch)
        gold_batch = pipeline.run_gold(silver_batch)
        silver_bundles.extend(silver_batch)
        gold_bundles.extend(gold_batch)
        if args.ingest:
            pipeline.ingest_from_silver_and_gold(
                silver_batch,
                gold_batch,
                schema=args.schema,
            )

    result = {
        "requested_fiche_ids": fiche_ids,
        "fiche_config_path": str(fiche_config_path),
        "bronze_assets": len(bronze_assets),
        "silver_documents": len(silver_bundles),
        "gold_documents": len(gold_bundles),
        "gold_chunks": sum(len(bundle.chunks) for bundle in gold_bundles),
        "lake_root": str(config.paths.root_dir),
        "batch_size": args.batch_size,
        "target_env": args.target_env,
        "situation": situation,
    }

    if args.sync_object_storage:
        syncer = ScalewayObjectStorageSync(ObjectStorageConfig.from_env())
        result["object_storage"] = syncer.sync_medallion_root(
            config.paths.root_dir,
            args.target_env,
        )

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
