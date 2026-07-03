#!/usr/bin/env python3
"""Contrôle en lecture seule du contrat d'ingestion Grist (issue #245).

Vérifie que la table manifest expose les colonnes requises et de writeback, puis
valide les lignes par ministère. N'écrit rien, ni dans Grist ni en base.

Usage:
    uv run python scripts/check_grist_contract.py [--table TABLE_ID] [--ministere mi ...]
"""

from __future__ import annotations

import argparse
import sys

from assistant_rh_data_engineering.utils.grist import (
    REQUIRED_MANIFEST_COLUMNS,
    WRITEBACK_MANIFEST_COLUMNS,
    GristClient,
    GristContractError,
    validate_manifest_columns,
    validate_manifest_records,
)
from dotenv import load_dotenv

DEFAULT_CORPORA = ("MI", "MASA", "MATTE", "MSO")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--table", default=None, help="Table Grist (défaut: GRIST_TABLE_ID)")
    parser.add_argument(
        "--corpus",
        action="append",
        default=None,
        help="Corpus PDF à valider (défaut: MI, MASA, MATTE, MSO)",
    )
    args = parser.parse_args()

    load_dotenv(".env")
    client = GristClient()

    columns = client.list_columns(args.table)
    print(f"Colonnes présentes ({len(columns)}): {', '.join(sorted(columns))}")

    exit_code = 0
    try:
        validate_manifest_columns(columns)
        print(f"✓ Contrat de lecture OK ({', '.join(REQUIRED_MANIFEST_COLUMNS)})")
    except GristContractError as exc:
        print(f"✗ Contrat de lecture NON respecté: {exc}")
        exit_code = 1

    missing_writeback = [column for column in WRITEBACK_MANIFEST_COLUMNS if column not in columns]
    if missing_writeback:
        print(f"✗ Colonnes de writeback absentes: {', '.join(missing_writeback)}")
        exit_code = 1
    else:
        print("✓ Colonnes de writeback présentes")

    records = client.list_records(args.table)
    print(f"\n{len(records)} lignes dans la table")
    for corpus in args.corpus or DEFAULT_CORPORA:
        result = validate_manifest_records(records, corpus)
        print(f"\n[{corpus}] valides: {len(result.valid)}, rejetées: {len(result.rejected)}")
        for row in result.valid:
            print(f"  ✓ {row.uid} — {row.titre[:60]} ({row.statut}, {row.cle_bucket})")
        for rejected in result.rejected:
            print(f"  ✗ record {rejected.record_id} (uid={rejected.uid}): {'; '.join(rejected.errors)}")

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
