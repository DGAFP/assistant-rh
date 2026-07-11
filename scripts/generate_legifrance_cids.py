#!/usr/bin/env python3
"""Régénère ``config/legifrance_article_cids.json`` depuis l'API Légifrance (E2.2, #289).

Follow-live du CGFP : ``tableMatieres(LEGITEXT, date)`` via PISTE → les articles
**en vigueur** → la liste de CIDs. Le fichier devient un **cache généré** (plus
une liste figée). ``--dry-run`` imprime le diff sans écrire.

Usage::

    uv run python scripts/generate_legifrance_cids.py --dry-run
    uv run python scripts/generate_legifrance_cids.py

Lecture : API Légifrance (PISTE, OAuth). Écrit uniquement le fichier config.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = REPO_ROOT / "config" / "legifrance_article_cids.json"
CGFP_LEGITEXT = "LEGITEXT000044416551"


def date_to_millis(date: datetime.date) -> int:
    return int(datetime.datetime(date.year, date.month, date.day, tzinfo=datetime.timezone.utc).timestamp() * 1000)


def current_cids(config_path: Path) -> list[str]:
    if not config_path.exists():
        return []
    return list(json.loads(config_path.read_text(encoding="utf-8")).get("article_cids") or [])


def render_config(legitext: str, cids: list[str], generated_at: str) -> dict:
    return {
        "dataset": "rag_chunks_dgafp",
        "source": f"legifrance_api:tableMatieres:{legitext}",
        "generated_at": generated_at,
        "article_cids": cids,
    }


def main(argv: list[str] | None = None) -> int:
    from dotenv import load_dotenv

    # Avant le parser : le défaut --legitext lit LEGIFRANCE_CODE_ID de l'env.
    load_dotenv(REPO_ROOT / ".env")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--legitext", default=os.getenv("LEGIFRANCE_CODE_ID") or CGFP_LEGITEXT)
    parser.add_argument("--date", default=None, help="YYYY-MM-DD (défaut: aujourd'hui)")
    parser.add_argument("--dry-run", action="store_true", help="imprime le diff sans écrire")
    args = parser.parse_args(argv)

    from assistant_rh_data_engineering.legifrance.piste import PisteClient

    date = datetime.date.fromisoformat(args.date) if args.date else datetime.date.today()
    cids = PisteClient().code_articles_en_vigueur(args.legitext, date_to_millis(date))
    if not cids:
        print("Aucun article en vigueur renvoyé par l'API — refus d'écrire un cache vide.", file=sys.stderr)
        return 1

    current = current_cids(args.config)
    added = sorted(set(cids) - set(current))
    removed = sorted(set(current) - set(cids))
    print(f"CGFP {args.legitext} @ {date} : {len(current)} -> {len(cids)} CIDs en vigueur")
    print(f"  + {len(added)} ajoutés")
    print(f"  - {len(removed)} retirés (abrogés / recodifiés depuis la liste figée)")

    if args.dry_run:
        print("\n[dry-run] cache non modifié.")
        return 0
    generated_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    args.config.write_text(json.dumps(render_config(args.legitext, cids, generated_at), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"\n✓ {args.config.relative_to(REPO_ROOT)} généré ({len(cids)} CIDs).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
