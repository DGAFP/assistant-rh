from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

cwd = Path.cwd().resolve()
REPO_ROOT = cwd.parent if cwd.name == "scripts" else cwd
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Orchestrateur unique des corpus PDF (manifest Grist + dropzone + OCR).
# Un seul job paramétré par ministère; MATTE/MSO s'enregistrent ici
# en phase D (issue #248).
MINISTERES = ("mi", "masa")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Pipeline médaillon des sources PDF ministérielles (manifest Grist, OCR, réconciliation).")
    parser.add_argument(
        "--ministere",
        choices=MINISTERES,
        required=True,
        help="Corpus ministériel à traiter.",
    )
    parser.add_argument(
        "--doc-id",
        dest="doc_ids",
        action="append",
        help="uid du manifest à traiter (répétable). Désactive la suppression des orphelins.",
    )
    parser.add_argument(
        "--lake-root",
        help="Racine locale du data lake (défaut: data/lake/pdf_sources/<ministere>).",
    )
    parser.add_argument(
        "--target-env",
        choices=["staging", "prod"],
        default="staging",
        help="Préfixe Object Storage cible (cache OCR bronze inclus).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Affiche le plan de réconciliation (ingest/ignore/suppression) sans OCR ni écriture.",
    )
    parser.add_argument(
        "--force-reocr",
        action="store_true",
        help="Ignore le cache OCR bronze et le delta sha256: tout est retraité.",
    )
    parser.add_argument(
        "--skip-grist-writeback",
        action="store_true",
        help="N'écrit pas les statuts d'ingestion dans le référentiel Grist.",
    )
    parser.add_argument(
        "--ocr-provider",
        help="Fournisseur OCR (défaut: OCR_PROVIDER env ou albert).",
    )
    parser.add_argument(
        "--no-embed",
        action="store_true",
        help="Désactive la génération d'embeddings en gold.",
    )
    parser.add_argument(
        "--no-scaleway-bge",
        action="store_true",
        help="Désactive la colonne de secours embedding_bge_scw (API Scaleway).",
    )
    parser.add_argument(
        "--ingest",
        action="store_true",
        help="Pousse documents/sections/chunks en base et réconcilie (suppressions cascades).",
    )
    parser.add_argument(
        "--sync-object-storage",
        action="store_true",
        help="Synchronise bronze/silver/gold locaux vers les buckets Scaleway.",
    )
    parser.add_argument(
        "--schema",
        default="public",
        help="Schéma Postgres cible.",
    )
    return parser


def main() -> int:
    from dotenv import load_dotenv

    load_dotenv(REPO_ROOT / ".env")

    args = build_parser().parse_args()

    # Registre implicite par convention de package: chaque ministère expose
    # Pipeline/PipelineConfig/LakePaths/OBJECT_STORAGE_SOURCE_NAME sous des
    # noms uniformes (mi/__init__.py, masa/__init__.py, ...). Ajouter un
    # ministère = l\'ajouter à MINISTERES + créer son package — pas de if/elif.
    import importlib

    try:
        module = importlib.import_module(f"assistant_rh_data_engineering.{args.ministere}")
        pipeline_class = module.Pipeline
        config_class = module.PipelineConfig
        lake_paths_class = module.LakePaths
        object_storage_source_name = module.OBJECT_STORAGE_SOURCE_NAME
    except (ImportError, AttributeError) as exc:
        raise SystemExit(f"Ministère non câblé dans le dispatch du job: {args.ministere!r} ({exc})") from exc

    config = config_class(target_env=args.target_env, ocr_provider_name=args.ocr_provider)
    if args.lake_root:
        config.paths = lake_paths_class(root_dir=Path(args.lake_root))
    if args.no_embed:
        config.embeddings.enable_m3 = False
        config.embeddings.enable_bge_scaleway = False
    elif args.no_scaleway_bge:
        config.embeddings.enable_bge_scaleway = False

    pipeline = pipeline_class(config, schema=args.schema)

    summary = pipeline.run(
        doc_ids=args.doc_ids,
        dry_run=args.dry_run,
        force_reocr=args.force_reocr,
        skip_grist_writeback=args.skip_grist_writeback,
        ingest=args.ingest,
    )

    if args.sync_object_storage and not args.dry_run:
        summary["object_storage"] = pipeline.store.sync.sync_medallion_root(
            config.paths.root_dir,
            args.target_env,
            source_name=object_storage_source_name,
        )

    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    return 1 if int(summary.get("failed_count") or 0) > 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
