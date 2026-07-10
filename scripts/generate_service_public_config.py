#!/usr/bin/env python3
"""Génère ``config/service_public_fiches.json`` depuis le référentiel Grist (E2.1, #289).

Le fichier cesse d'être maintenu à la main : les fiches à ingérer sont les lignes
Service-Public de Grist dont ``statut ∈ {a_ingerer, ingere, erreur}`` et
``abroge ≠ oui``.
Grist devient la source de vérité de la sélection ; ce fichier est un **artefact
généré** (le job SP le consomme via ``--fiche-config``, inchangé).

Usage::

    uv run python scripts/generate_service_public_config.py --dry-run   # imprime le diff
    uv run python scripts/generate_service_public_config.py             # écrit le fichier

Lecture seule côté Grist (``list_records``) et côté fichier. Aucune écriture Grist.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = REPO_ROOT / "config" / "service_public_fiches.json"

# Statuts (colonne `statut`, ex-`statut_cible`) qui expriment l'intention d'avoir
# la fiche au corpus. a_supprimer/supprime/en_attente/... sont exclus.
WANT_STATUTS = {"a_ingerer", "ingere", "erreur"}
SP_CORPUS_MARKER = "service-public"
_F_CODE_RE = re.compile(r"F\d+", re.IGNORECASE)


def is_service_public(fields: dict) -> bool:
    return SP_CORPUS_MARKER in str(fields.get("source_corpus") or "").strip().lower()


def extract_fiche_id(fields: dict) -> str | None:
    """F-code de la ligne : ``id_extraction``, sinon le titre, sinon l'``uid``."""
    for source in (fields.get("id_extraction"), fields.get("titre_document"), fields.get("uid")):
        match = _F_CODE_RE.search(str(source or ""))
        if match:
            return match.group(0).upper()
    return None


def selected_fiche_ids(records: list[dict]) -> list[str]:
    """F-codes à ingérer, triés & dédupliqués.

    Une ligne est retenue si : corpus Service-Public, ``statut ∈ WANT_STATUTS``,
    ``abroge ≠ oui``.
    """
    fiche_ids: set[str] = set()
    for record in records:
        fields = record.get("fields") or {}
        if not is_service_public(fields):
            continue
        if str(fields.get("statut") or "").strip().lower() not in WANT_STATUTS:
            continue
        if str(fields.get("abroge") or "").strip().lower() == "oui":
            continue
        code = extract_fiche_id(fields)
        if code:
            fiche_ids.add(code)
    return sorted(fiche_ids)


def render_config(fiche_ids: list[str]) -> dict:
    return {
        "source": "service_public",
        "description": (
            "Liste de production des fiches Service-Public — GÉNÉRÉE depuis le référentiel "
            "Grist (lignes statut a_ingerer/ingere/erreur, abroge != oui). Ne pas éditer à la main : "
            "régénérer via scripts/generate_service_public_config.py."
        ),
        "situation": "FPE",
        "fiche_ids": fiche_ids,
    }


def _current_fiche_ids(config_path: Path) -> list[str]:
    if not config_path.exists():
        return []
    return list(json.loads(config_path.read_text(encoding="utf-8")).get("fiche_ids") or [])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--dry-run", action="store_true", help="imprime le diff sans écrire")
    args = parser.parse_args(argv)

    from assistant_rh_data_engineering.utils.grist import GristClient

    records = GristClient().list_records()
    fiche_ids = selected_fiche_ids(records)
    if not fiche_ids:
        print("Aucune fiche Service-Public retenue en Grist — refus d'écrire un config vide.", file=sys.stderr)
        return 1

    current = _current_fiche_ids(args.config)
    added = sorted(set(fiche_ids) - set(current))
    removed = sorted(set(current) - set(fiche_ids))
    print(f"Service-Public : {len(current)} -> {len(fiche_ids)} fiches (source = Grist)")
    print(f"  + {len(added)} ajoutées : {added}")
    print(f"  - {len(removed)} retirées : {removed}")

    if args.dry_run:
        print("\n[dry-run] config non modifié.")
        return 0
    args.config.write_text(json.dumps(render_config(fiche_ids), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"\n✓ {args.config.relative_to(REPO_ROOT)} généré ({len(fiche_ids)} fiches).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
