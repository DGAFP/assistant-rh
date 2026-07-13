"""Primitives partagées du médaillon en mode ``--delta`` (E2.3-c, #289).

Mutualise entre les médaillons Service-Public (#308) et Légifrance (#20) la
logique delta : hydrater l'état précédent depuis l'Object Storage (jobs
Serverless Scaleway = stateless, disque local éphémère), mémoriser les checksums
silver AVANT le run (``run_silver`` les réécrit), et ne reconstruire gold +
embeddings que pour les documents nouveaux ou modifiés — les inchangés
réutilisent leur artefact gold existant.

Les deux sources partagent le même layout de lake (``utils/silver.py`` :
``documents/{short_id}.document.json`` ; ``utils/gold.py`` :
``chunks/{short_id}.chunks.jsonl``), keyé par ``short_id`` avec un champ
``checksum`` — d'où ces helpers agnostiques de la source.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def count_valid_gold_chunks(chunks_path: Path) -> int:
    """Nombre de chunks gold VALIDES (une ligne = un JSON). 0 si réutilisation impossible.

    Un gold vide, illisible (``OSError``) ou dont une ligne n'est pas du JSON
    valide (``JSONDecodeError``) renvoie 0 -> reconstruit plutôt que réutiliser un
    artefact corrompu (leçon retry_zero_chunk : un skip ici bloquerait chaque run
    delta sans jamais s'auto-réparer).
    """
    try:
        lines = [line for line in chunks_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except OSError:
        return 0
    count = 0
    for line in lines:
        try:
            json.loads(line)
        except json.JSONDecodeError:
            return 0
        count += 1
    return count


def capture_previous_checksums(silver_documents_dir: Path) -> dict[str, str]:
    """``{short_id (upper): checksum}`` lu depuis les ``*.document.json``.

    À appeler AVANT ``run_silver`` (qui réécrit les artefacts silver) pour
    décider quoi reconstruire en gold. Un document illisible/corrompu est ignoré.
    """
    previous: dict[str, str] = {}
    for doc_path in sorted(silver_documents_dir.glob("*.document.json")):
        try:
            payload = json.loads(doc_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        uid = str(payload.get("short_id") or "").strip().upper()
        if uid:
            previous[uid] = str(payload.get("checksum") or "")
    return previous


def reusable_gold_chunk_count(
    gold_chunks_dir: Path,
    uid: str,
    checksum: str,
    previous_checksums: dict[str, str],
) -> int:
    """Nombre de chunks gold réutilisables pour ``uid`` : >0 si le contenu source
    est inchangé (même checksum silver qu'au run précédent) ET le gold existant
    est valide ; 0 sinon (nouveau, modifié, ou gold absent/vide/corrompu -> rebuild).
    """
    if not checksum or previous_checksums.get(uid) != checksum:
        return 0
    chunks_path = gold_chunks_dir / f"{uid}.chunks.jsonl"
    if not chunks_path.exists():
        return 0
    return count_valid_gold_chunks(chunks_path)


def hydrate_silver_gold(syncer: Any, lake_root: Path, target_env: str, source_name: str) -> dict[str, str]:
    """Télécharge silver + gold depuis l'Object Storage AVANT un run ``--delta``.

    Sans cette hydratation, un job stateless (Serverless Jobs Scaleway) repart
    d'un disque vide : aucun checksum/gold précédent -> tout est reconstruit,
    annulant le bénéfice du delta. Bronze est exclu (re-téléchargé/recalculé par
    le chemin d'ingestion bronze propre à chaque source).
    """
    return syncer.download_medallion_root(
        lake_root,
        target_env,
        source_name=source_name,
        include_layers=("silver", "gold"),
    )
