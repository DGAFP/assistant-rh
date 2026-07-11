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


def summarize_pipeline_outputs(
    requested_fiche_ids: list[str],
    bronze_assets: list[Any],
    silver_bundles: list[Any],
    gold_bundles: list[Any],
    gold_reused_chunks: dict[str, int] | None = None,
) -> dict[str, dict[str, int]]:
    bronze_ids = {str(getattr(asset, "fiche_id", "")).strip().upper() for asset in bronze_assets}
    silver_by_id = {
        str(bundle.document.get("short_id", "")).strip().upper(): bundle for bundle in silver_bundles if getattr(bundle, "document", None) is not None
    }
    gold_by_id = {
        str(bundle.document.get("short_id", "")).strip().upper(): bundle for bundle in gold_bundles if getattr(bundle, "document", None) is not None
    }
    # Mode --delta : fiches dont le gold existant a été réutilisé (contenu
    # source inchangé) — comptées comme complètes, pas comme manquantes.
    reused = dict(gold_reused_chunks or {})

    per_fiche: dict[str, dict[str, int]] = {}
    errors: list[str] = []
    for fiche_id in requested_fiche_ids:
        silver_bundle = silver_by_id.get(fiche_id)
        gold_bundle = gold_by_id.get(fiche_id)
        reused_count = reused.get(fiche_id)
        counts = {
            "bronze": 1 if fiche_id in bronze_ids else 0,
            "documents": 1 if silver_bundle is not None else 0,
            "sections": len(getattr(silver_bundle, "sections", []) or []),
            "gold_documents": 1 if (gold_bundle is not None or reused_count is not None) else 0,
            "chunks": len(getattr(gold_bundle, "chunks", []) or []) if gold_bundle is not None else int(reused_count or 0),
        }
        per_fiche[fiche_id] = counts

        missing: list[str] = []
        if counts["bronze"] == 0:
            missing.append("bronze XML")
        if counts["documents"] == 0:
            missing.append("silver document")
        if counts["sections"] == 0:
            missing.append("silver sections")
        if counts["gold_documents"] == 0:
            missing.append("gold document")
        if counts["chunks"] == 0:
            missing.append("gold chunks")
        if missing:
            errors.append(f"{fiche_id}: {', '.join(missing)}")

    if errors:
        detail = "\n".join(f"- {error}" for error in errors)
        raise RuntimeError(f"Pipeline Service-Public incomplet:\n{detail}")

    return per_fiche


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=("Pipeline médaillon Service-Public basé sur le XML officiel DILA."))
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
        "--from-grist",
        action="store_true",
        help=(
            "Sélection depuis le référentiel Grist (lignes Service-Public actives : a_ingerer/ingere/erreur, "
            "abroge != oui) au lieu de --fiche-config — la config committée devient un cache (E2.3-c, #289)."
        ),
    )
    parser.add_argument(
        "--grist-table-id",
        default=None,
        help="Table Grist du référentiel (défaut : variable d'environnement GRIST_TABLE_ID).",
    )
    parser.add_argument(
        "--delta",
        action="store_true",
        help=(
            "Ne reconstruit gold+embeddings que pour les fiches nouvelles ou dont le contenu source a changé "
            "(hash silver vs artefact existant) ; les inchangées réutilisent leur gold. Le fetch bronze reste "
            "complet — nécessaire à la détection de changement."
        ),
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
    if args.delta and args.ingest:
        raise SystemExit(
            "--ingest est incompatible avec --delta : utiliser le job d'ingestion en mode --delta (E2.3-a), qui réconcilie Grist↔corpus."
        )

    config = ServicePublicPipelineConfig(paths=LakePaths(root_dir=Path(args.lake_root)))
    fiche_config_path = Path(args.fiche_config)
    if args.from_grist:
        from assistant_rh_data_engineering.service_public.reconcile import select_manifest_rows
        from assistant_rh_data_engineering.utils.grist import GristClient, GristError

        try:
            grist = GristClient()
            records = grist.list_records(args.grist_table_id) if args.grist_table_id else grist.list_records()
            manifest_rows = select_manifest_rows(records)
        except GristError as exc:
            raise SystemExit(f"Échec Grist en mode --from-grist (aucune écriture): {exc}") from exc
        fiche_ids = resolve_fiche_ids([row.uid for row in manifest_rows if row.active] + list(args.fiche_ids or []))
        # Même défaut que la config générée par le générateur E2.1 ("FPE").
        situation = args.situation or "FPE"
    else:
        fiche_config = load_fiche_config(fiche_config_path)
        fiche_ids = resolve_fiche_ids(list(fiche_config.get("fiche_ids", [])) + list(args.fiche_ids or []))
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
        if args.from_grist:
            raise SystemExit("Aucune ligne Service-Public active dans le référentiel Grist (statuts a_ingerer/ingere/erreur, abroge != oui).")
        raise SystemExit(
            "Aucune fiche fournie. Renseigne config/service_public_fiches.json, --fiche-id, ou utilise --batch-from-db pour la migration."
        )
    config.fiche_ids = fiche_ids or None
    config.silver.situation_filter = situation
    if args.no_embed:
        config.embeddings.enable_m3 = False
        config.embeddings.enable_bge_scaleway = False
    elif args.with_scaleway_bge:
        config.embeddings.enable_bge_scaleway = True

    # Mode --delta : mémoriser les checksums silver AVANT le run (les artefacts
    # sont réécrits par run_silver) pour décider quoi reconstruire en gold.
    previous_checksums: dict[str, str] = {}
    if args.delta:
        for doc_path in sorted((config.paths.silver_dir / "documents").glob("*.document.json")):
            try:
                payload = json.loads(doc_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            uid = str(payload.get("short_id") or "").strip().upper()
            if uid:
                previous_checksums[uid] = str(payload.get("checksum") or "")

    pipeline = ServicePublicPipeline(config)
    bronze_assets = pipeline.run_bronze()
    silver_bundles = []
    gold_bundles = []
    reused_gold_chunks: dict[str, int] = {}

    for batch in chunked(bronze_assets, args.batch_size):
        silver_batch = pipeline.run_silver(batch)
        if args.delta:
            to_build = []
            for bundle in silver_batch:
                uid = str(bundle.document.get("short_id") or "").strip().upper()
                checksum = str(bundle.document.get("checksum") or "")
                chunks_path = config.paths.gold_dir / "chunks" / f"{uid}.chunks.jsonl"
                reused_count = 0
                if checksum and previous_checksums.get(uid) == checksum and chunks_path.exists():
                    reused_count = sum(1 for line in chunks_path.read_text(encoding="utf-8").splitlines() if line.strip())
                if reused_count > 0:
                    # Contenu source inchangé et gold déjà construit : on ne
                    # re-paye pas les embeddings, l'artefact existant est réutilisé.
                    reused_gold_chunks[uid] = reused_count
                    continue
                # Nouveau, modifié, OU gold existant vide/corrompu malgré un hash
                # inchangé : reconstruire (leçon retry_zero_chunk — un skip ici
                # ferait échouer chaque run delta sans jamais s'auto-réparer).
                to_build.append(bundle)
            gold_batch = pipeline.run_gold(to_build) if to_build else []
        else:
            gold_batch = pipeline.run_gold(silver_batch)
        silver_bundles.extend(silver_batch)
        gold_bundles.extend(gold_batch)

    per_fiche = summarize_pipeline_outputs(
        fiche_ids,
        bronze_assets,
        silver_bundles,
        gold_bundles,
        gold_reused_chunks=reused_gold_chunks,
    )

    ingested: dict[str, int] | None = None
    if args.ingest:
        ingested = pipeline.ingest_from_silver_and_gold(
            silver_bundles,
            gold_bundles,
            schema=args.schema,
        )

    result = {
        "requested_fiche_ids": fiche_ids,
        "fiche_config_path": None if args.from_grist else str(fiche_config_path),
        "from_grist": args.from_grist,
        "delta": args.delta,
        "bronze_assets": len(bronze_assets),
        "silver_documents": len(silver_bundles),
        "gold_documents": len(gold_bundles),
        "gold_chunks": sum(len(bundle.chunks) for bundle in gold_bundles),
        "gold_skipped_unchanged": sorted(reused_gold_chunks),
        "gold_chunks_reused": sum(reused_gold_chunks.values()),
        "lake_root": str(config.paths.root_dir),
        "batch_size": args.batch_size,
        "target_env": args.target_env,
        "situation": situation,
        "per_fiche": per_fiche,
    }
    if ingested is not None:
        result["ingested"] = ingested

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
