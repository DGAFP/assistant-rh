"""Primitives partagées du médaillon en mode ``--delta`` (E2.3-c, #289).

Mutualise entre les médaillons Service-Public (#308) et Légifrance (#20) la
logique delta : hydrater l'état précédent depuis l'Object Storage (jobs
Serverless Scaleway = stateless, disque local éphémère), mémoriser les checksums
silver AVANT le run (``run_silver`` les réécrit), et ne reconstruire gold +
embeddings que pour les documents nouveaux ou modifiés — les inchangés
réutilisent leur artefact gold existant.

La réutilisation ne peut PAS se fonder sur le seul checksum silver (contenu
source) : un changement de la config gold/embeddings (chunking ``--multi-chunk``,
flags/modèles d'embeddings, version du builder) doit invalider TOUS les golds
inchangés. On compare donc aussi une empreinte de cette config (``gold_reuse_fingerprint``,
persistée à côté du gold et round-trippée via l'Object Storage).

Les deux sources partagent le même layout de lake (``utils/silver.py`` :
``documents/{short_id}.document.json`` ; ``utils/gold.py`` :
``chunks/{short_id}.chunks.jsonl``), keyé par ``short_id`` avec un champ
``checksum`` — d'où ces helpers agnostiques de la source.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

# Bumper si la logique gold/chunking change de façon incompatible avec les
# artefacts déjà écrits (force la reconstruction de tous les golds au run suivant).
GOLD_DELTA_VERSION = "1"

# Empreinte de config persistée à la racine du gold (round-trip via l'Object
# Storage avec la couche gold). Préfixe point : ignoré par les globs *.chunks.jsonl.
GOLD_STATE_FILENAME = ".medallion_delta_state.json"


def count_valid_gold_chunks(chunks_path: Path) -> int:
    """Nombre de chunks gold VALIDES (une ligne = un objet JSON non vide). 0 sinon.

    0 (=> reconstruction) si le gold est vide, illisible (``OSError``), en
    encodage invalide (``UnicodeDecodeError``), ou dont une ligne n'est pas un
    objet JSON non vide (``JSONDecodeError``, ``null``, liste, scalaire) — un
    artefact structurellement corrompu ne doit jamais être réutilisé (leçon
    retry_zero_chunk : un skip ici bloquerait chaque run delta sans s'auto-réparer).
    """
    try:
        text = chunks_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return 0
    count = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            return 0
        # Contrat chunk minimum : objet JSON non vide (rejette null/liste/scalaire).
        if not isinstance(row, dict) or not row:
            return 0
        count += 1
    return count


def capture_previous_checksums(silver_documents_dir: Path) -> dict[str, str]:
    """``{short_id (upper): checksum}`` lu depuis les ``*.document.json``.

    À appeler AVANT ``run_silver`` (qui réécrit les artefacts silver) pour
    décider quoi reconstruire en gold. Un document illisible, en encodage
    invalide, non-JSON ou non-objet (``null``, liste) est ignoré.
    """
    previous: dict[str, str] = {}
    for doc_path in sorted(silver_documents_dir.glob("*.document.json")):
        try:
            payload = json.loads(doc_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
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

    NB : l'appelant doit gater cette réutilisation par ``gold_reuse_fingerprint``
    (config gold/embeddings inchangée) — le checksum silver seul ne couvre pas un
    changement de chunking/embeddings.
    """
    if not checksum or previous_checksums.get(uid) != checksum:
        return 0
    chunks_path = gold_chunks_dir / f"{uid}.chunks.jsonl"
    if not chunks_path.exists():
        return 0
    return count_valid_gold_chunks(chunks_path)


def gold_reuse_fingerprint(*, single_chunk_per_article: bool, embeddings: Any) -> str:
    """Empreinte de la config qui détermine la SORTIE gold+embeddings.

    Deux runs au même checksum silver mais à config différente (chunking ou
    embeddings) ne doivent PAS réutiliser leurs golds : cette empreinte, comparée
    à celle du run précédent, invalide toute réutilisation en cas de changement.
    """
    payload = {
        "version": GOLD_DELTA_VERSION,
        "single_chunk_per_article": bool(single_chunk_per_article),
        "enable_m3": bool(getattr(embeddings, "enable_m3", False)),
        "m3_backend": str(getattr(embeddings, "m3_backend", "")),
        "m3_model_name": str(getattr(embeddings, "m3_model_name", "")),
        "enable_bge_scaleway": bool(getattr(embeddings, "enable_bge_scaleway", False)),
        "scaleway_model_name": str(getattr(embeddings, "scaleway_model_name", "")),
        "normalize": bool(getattr(embeddings, "normalize", True)),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def read_gold_fingerprints(gold_dir: Path) -> dict[str, str]:
    """``{short_id (upper): empreinte config}`` PAR document du run précédent.

    Par document (pas global) car les médaillons supportent des runs PARTIELS
    (``--fiche-id``, sous-ensemble Grist) : une empreinte globale, avancée après
    un rebuild partiel, ferait passer TOUT le lake pour la nouvelle config et
    autoriserait à tort la réutilisation du gold périmé des documents non
    reconstruits. ``{}`` si l'état est absent/illisible/corrompu.
    """
    try:
        payload = json.loads((gold_dir / GOLD_STATE_FILENAME).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    fingerprints = payload.get("gold_fingerprints")
    if not isinstance(fingerprints, dict):
        return {}
    return {str(uid).strip().upper(): str(fp) for uid, fp in fingerprints.items() if str(fp or "").strip()}


def write_gold_fingerprints(gold_dir: Path, fingerprints: dict[str, str]) -> None:
    """Persiste la map ``{uid: empreinte}`` à la racine du gold (round-trip OS).

    L'appelant doit PRÉSERVER les entrées des documents hors du run courant
    (subset-safe) et ne mettre à jour que celles des documents traités.
    """
    gold_dir.mkdir(parents=True, exist_ok=True)
    (gold_dir / GOLD_STATE_FILENAME).write_text(
        json.dumps({"gold_fingerprints": fingerprints}, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )


def hydrate_silver_gold(syncer: Any, lake_root: Path, target_env: str, source_name: str) -> dict[str, str]:
    """Télécharge silver + gold depuis l'Object Storage AVANT un run ``--delta``.

    Sans cette hydratation, un job stateless (Serverless Jobs Scaleway) repart
    d'un disque vide : aucun checksum/gold/empreinte précédent -> tout est
    reconstruit, annulant le bénéfice du delta. Bronze est exclu (re-téléchargé/
    recalculé par le chemin d'ingestion bronze propre à chaque source).
    """
    return syncer.download_medallion_root(
        lake_root,
        target_env,
        source_name=source_name,
        include_layers=("silver", "gold"),
    )
