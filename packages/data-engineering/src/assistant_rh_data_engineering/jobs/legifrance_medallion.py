from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

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


def chunked(items: list[Any], size: int) -> list[list[Any]]:
    if size <= 0:
        return [items]
    return [items[i : i + size] for i in range(0, len(items), size)]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=("Pipeline médaillon Légifrance ne traitant que le bronze/raw déjà disponible. Le dump LEGI doit être préparé séparément.")
    )
    parser.add_argument(
        "--lake-root",
        default="data/lake/legifrance",
        help="Racine locale du data lake.",
    )
    parser.add_argument(
        "--article-cids-json",
        default="config/legifrance_article_cids.json",
        help=(
            "Cache follow-live des articles (clé 'articles' = paires version/alias → cid chronique) : "
            "rétablit l'identité stable en bronze (le XML DILA ne porte pas le cid chronique)."
        ),
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=100,
        help="Taille des lots de traitement après le bronze.",
    )
    parser.add_argument(
        "--target-env",
        choices=["staging", "prod"],
        default="prod",
        help="Préfixe Object Storage cible.",
    )
    parser.add_argument(
        "--from-object-storage",
        action="store_true",
        help="Télécharge bronze/raw depuis les buckets Scaleway avant transformation.",
    )
    parser.add_argument(
        "--sync-object-storage",
        action="store_true",
        help="Synchronise bronze/silver/gold vers les buckets Scaleway après transformation.",
    )
    parser.add_argument(
        "--delete-remote",
        action="store_true",
        help="Supprime les objets distants absents du run courant lors du sync Object Storage.",
    )
    parser.add_argument(
        "--no-embed",
        action="store_true",
        help="Désactive la génération d'embeddings en gold.",
    )
    parser.add_argument(
        "--with-scaleway-bge",
        action="store_true",
        help="Ajoute embedding_bge_scw via l'API Scaleway et active aussi embedding_m3.",
    )
    parser.add_argument(
        "--with-m3",
        action="store_true",
        help="Active embedding_m3 localement.",
    )
    parser.add_argument(
        "--ingest",
        action="store_true",
        help="Pousse rag_documents/rag_sections/rag_chunks_dgafp/rag_chunks_legifrance en base.",
    )
    parser.add_argument(
        "--schema",
        default="public",
        help="Schéma Postgres cible.",
    )
    parser.add_argument(
        "--dsn",
        help="DSN Postgres explicite pour l'ingestion.",
    )
    parser.add_argument(
        "--legacy-table-name",
        default="rag_chunks_dgafp",
        help="Table legacy cible des chunks en base.",
    )
    parser.add_argument(
        "--modern-table-name",
        default="rag_chunks_legifrance",
        help="Table canonique cible des chunks en base.",
    )
    parser.add_argument(
        "--single-chunk-per-article",
        action="store_true",
        help="Force un chunk unique par article.",
    )
    parser.add_argument(
        "--multi-chunk",
        action="store_true",
        help="Autorise plusieurs chunks par article.",
    )
    return parser


def main() -> int:
    from dotenv import load_dotenv

    from assistant_rh_data_engineering.legifrance import LegifrancePipeline, LegifrancePipelineConfig
    from assistant_rh_data_engineering.legifrance.config import LakePaths
    from assistant_rh_data_engineering.utils.object_storage import ObjectStorageConfig, ScalewayObjectStorageSync

    load_dotenv(REPO_ROOT / ".env")
    args = build_parser().parse_args()

    config = LegifrancePipelineConfig(paths=LakePaths(root_dir=Path(args.lake_root)))
    config.gold.legacy_table_name = args.legacy_table_name
    config.gold.modern_table_name = args.modern_table_name

    # Identité stable (revue #307) : le XML DILA ne porte pas le cid chronique.
    # Le mapping alias→chronique vient du cache follow-live (clé "articles").
    cids_path = Path(args.article_cids_json)
    if not cids_path.is_absolute():
        cids_path = REPO_ROOT / cids_path
    if cids_path.exists():
        cache_payload = json.loads(cids_path.read_text(encoding="utf-8"))
        mapping: dict[str, str] = {}
        for entry in cache_payload.get("articles") or []:
            chronical = str(entry.get("cid") or "").strip().upper()
            if not chronical:
                continue
            for alias in [entry.get("version_id"), *(entry.get("aliases") or [])]:
                alias_key = str(alias or "").strip().upper()
                if alias_key:
                    mapping[alias_key] = chronical
        config.bronze.article_cid_mapping = mapping
        if not mapping:
            print(f"[warn] {cids_path.name} sans clé 'articles' (ancien format) : identités non stabilisées (cid = ID de version).")
    else:
        print(f"[warn] cache CIDs introuvable ({cids_path}) : identités non stabilisées (cid = ID de version).")

    if args.single_chunk_per_article:
        config.gold.single_chunk_per_article = True
    elif args.multi_chunk:
        config.gold.single_chunk_per_article = False

    if args.no_embed:
        config.embeddings.enable_m3 = False
        config.embeddings.enable_bge_scaleway = False
    else:
        if args.with_m3 or args.with_scaleway_bge:
            config.embeddings.enable_m3 = True
        if args.with_scaleway_bge:
            config.embeddings.enable_bge_scaleway = True

    syncer = None
    if args.from_object_storage:
        syncer = ScalewayObjectStorageSync(ObjectStorageConfig.from_env())

    pipeline = LegifrancePipeline(config)
    bronze_assets = (
        pipeline.bronze_builder.fetch_from_object_storage(
            pipeline.bronze_repo,
            syncer,
            args.target_env,
        )
        if syncer is not None
        else pipeline.run_bronze()
    )
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
                dsn=args.dsn,
            )

    object_storage = None
    if args.sync_object_storage:
        if syncer is None:
            syncer = ScalewayObjectStorageSync(ObjectStorageConfig.from_env())
        object_storage = syncer.sync_medallion_root(
            config.paths.root_dir,
            args.target_env,
            source_name="legifrance",
            delete=args.delete_remote,
        )

    print(
        json.dumps(
            {
                "lake_root": str(config.paths.root_dir),
                "bronze_assets": len(bronze_assets),
                "silver_documents": len(silver_bundles),
                "gold_documents": len(gold_bundles),
                "gold_chunks": sum(len(bundle.chunks) for bundle in gold_bundles),
                "legacy_table_name": config.gold.legacy_table_name,
                "modern_table_name": config.gold.modern_table_name,
                "single_chunk_per_article": config.gold.single_chunk_per_article,
                "target_env": args.target_env,
                "object_storage": object_storage,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
