from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Télécharge et extrait le dump LEGI dans bronze/raw/legi_bulk.")
    parser.add_argument(
        "--lake-root",
        default="data/lake/legifrance",
        help="Racine locale du data lake.",
    )
    parser.add_argument(
        "--index-url",
        default="https://echanges.dila.gouv.fr/OPENDATA/LEGI/",
        help="Index DILA LEGI.",
    )
    parser.add_argument(
        "--archive-url",
        help="URL explicite d'une archive .tar.gz à utiliser.",
    )
    parser.add_argument(
        "--target-env",
        choices=["staging", "prod"],
        default="prod",
        help="Préfixe Object Storage cible.",
    )
    parser.add_argument(
        "--sync-object-storage",
        action="store_true",
        help="Synchronise bronze/raw vers les buckets Scaleway après téléchargement.",
    )
    parser.add_argument(
        "--delete-remote",
        action="store_true",
        help="Supprime les objets distants absents du lake local lors du sync Object Storage.",
    )
    parser.add_argument(
        "--prefer-delta",
        action="store_true",
        help="Préfère une archive delta au lieu d'un snapshot global.",
    )
    parser.add_argument(
        "--without-deltas",
        action="store_true",
        help="N'applique pas les deltas postérieurs au snapshot global.",
    )
    parser.add_argument(
        "--extract-full-snapshot",
        action="store_true",
        help="Extrait tous les XML de l'archive sélectionnée dans bronze/raw/legi_bulk/articles.",
    )
    parser.add_argument(
        "--delete-local-archive",
        action="store_true",
        help="Supprime l'archive .tar.gz locale après extraction pour réduire le stockage éphémère.",
    )
    parser.add_argument(
        "--article-ids-json",
        help=(
            "Chemin d'un manifest JSON contenant les LEGIARTI... à extraire. "
            "Échec non-zéro si un seul ID demandé est absent du snapshot ; "
            "utiliser --allow-partial pour tolérer une extraction incomplète."
        ),
    )
    parser.add_argument(
        "--allow-partial",
        action="store_true",
        help="Tolère une extraction --article-ids-json incomplète au lieu d'échouer.",
    )
    return parser


def load_article_ids_from_json(path_str: str) -> tuple[list[str], int]:
    """Returns (deduped_normalized_ids, raw_requested_count_before_dedup)."""
    path = Path(path_str)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, list):
        article_ids = payload
    elif isinstance(payload, dict):
        article_ids = payload.get("article_cids") or payload.get("article_ids") or []
    else:
        article_ids = []
    raw_count = sum(1 for item in article_ids if str(item).strip())
    normalized = sorted({str(item).strip() for item in article_ids if str(item).strip()})
    if not normalized:
        raise RuntimeError(f"Aucun article_id exploitable trouvé dans {path}")
    return normalized, raw_count


def main() -> int:
    from assistant_rh_data_engineering.legifrance.bulk_dump import LegiBulkDumpClient, LegiBulkDumpConfig
    from assistant_rh_data_engineering.legifrance.config import LakePaths
    from assistant_rh_data_engineering.utils.object_storage import ObjectStorageConfig, ScalewayObjectStorageSync

    args = build_parser().parse_args()
    paths = LakePaths(root_dir=Path(args.lake_root))
    raw_dir = paths.bronze_dir / "raw"

    # Validate cheap inputs (manifest path + parse) before any network I/O.
    article_ids: list[str] | None = None
    requested_article_ids: int | None = None
    if args.article_ids_json:
        article_ids, requested_article_ids = load_article_ids_from_json(args.article_ids_json)

    client = LegiBulkDumpClient(
        LegiBulkDumpConfig(
            index_url=args.index_url,
            archive_url=args.archive_url,
            prefer_full_snapshot=not args.prefer_delta,
            include_delta_updates=not args.without_deltas,
        )
    )
    snapshot = client.resolve_snapshot(raw_dir)

    extracted_count: int | None = None
    missing_ids: list[str] = []
    extraction_mode = "none"
    if article_ids is not None:
        extracted = client.extract_articles(snapshot, article_ids)
        extracted_count = len(extracted)
        missing_ids = sorted(set(article_ids) - set(extracted))
        extraction_mode = "article_ids_json"
    elif args.extract_full_snapshot:
        extracted_count = len(client.extract_full_snapshot(snapshot))
        extraction_mode = "full_snapshot"

    strict_articles: bool | None = None
    missing_article_count: int | None = None
    missing_article_ids_sample: list[str] | None = None
    if extraction_mode == "article_ids_json":
        strict_articles = not args.allow_partial
        missing_article_count = len(missing_ids)
        missing_article_ids_sample = missing_ids[:5]

    # Fail-fast BEFORE any success-only side-effects (sync, delete_local_archive)
    # so partial data is never pushed remote and the local archive remains
    # available for the operator to retry against the same snapshot.
    if missing_ids and strict_articles:
        print(
            json.dumps(
                {
                    "status": "error",
                    "reason": "incomplete_article_extraction",
                    "extraction_mode": extraction_mode,
                    "requested_article_ids": requested_article_ids,
                    "extracted_xml_count": extracted_count,
                    "missing_article_count": missing_article_count,
                    "missing_article_ids_sample": missing_article_ids_sample,
                    "article_ids_json": args.article_ids_json,
                    "strict_articles": strict_articles,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        raise SystemExit(
            "Extraction incomplète pour --article-ids-json: "
            f"{len(missing_ids)}/{requested_article_ids} article(s) absent(s). "
            "Réessayer avec un snapshot plus récent ou utiliser --allow-partial."
        )

    object_storage = None
    if args.sync_object_storage:
        syncer = ScalewayObjectStorageSync(ObjectStorageConfig.from_env())
        object_storage = syncer.sync_medallion_root(
            paths.root_dir,
            args.target_env,
            source_name="legifrance",
            delete=args.delete_remote,
        )

    deleted_local_archive = False
    if args.delete_local_archive and extraction_mode != "none":
        deleted_local_archive = client.delete_local_archive(snapshot)

    print(
        json.dumps(
            {
                "status": "ok",
                "lake_root": str(Path(args.lake_root)),
                "archive_name": snapshot.archive_name,
                "archive_url": snapshot.archive_url,
                "archive_path": str(snapshot.archive_path),
                "extract_dir": str(snapshot.extract_dir),
                "extract_full_snapshot": args.extract_full_snapshot,
                "article_ids_json": args.article_ids_json,
                "extraction_mode": extraction_mode,
                "requested_article_ids": requested_article_ids,
                "extracted_xml_count": extracted_count,
                "missing_article_count": missing_article_count,
                "missing_article_ids_sample": missing_article_ids_sample,
                "strict_articles": strict_articles,
                "delete_local_archive": args.delete_local_archive,
                "deleted_local_archive": deleted_local_archive,
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
