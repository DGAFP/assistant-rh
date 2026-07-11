#!/usr/bin/env python3
"""Régénère ``config/legifrance_article_cids.json`` depuis l'API Légifrance (E2.2/E2.3-b, #289).

Follow-live des **textes suivis en Grist** (modèle v2, 11/07/2026) : chaque
ligne Légifrance active du référentiel (``legifrance_code`` → LEGITEXT via
``legi/tableMatieres`` ; ``legifrance_texte`` → JORFTEXT via ``lawDecree``)
fournit sa TOC ; le cache est l'**union** des articles en vigueur.

Chaque article porte DEUX identifiants LEGIARTI : le ``cid`` **chronique**
(stable — l'identité corpus) et l'``id`` de **version** (change à chaque
modification — c'est le nom du fichier dans le dump DILA). Le cache émet les
deux : ``article_cids`` (identité) et ``article_version_ids`` (extraction du
bulk). ``--dry-run`` imprime le diff sans écrire.

Usage::

    uv run python scripts/generate_legifrance_cids.py --dry-run
    uv run python scripts/generate_legifrance_cids.py

Lecture : Grist (référentiel) + API Légifrance (PISTE, OAuth). Écrit uniquement
le fichier config.
"""

from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_SRC = REPO_ROOT / "packages" / "data-engineering" / "src"
if str(PACKAGE_SRC) not in sys.path:
    sys.path.insert(0, str(PACKAGE_SRC))

DEFAULT_CONFIG = REPO_ROOT / "config" / "legifrance_article_cids.json"


def date_to_millis(date: datetime.date) -> int:
    return int(datetime.datetime(date.year, date.month, date.day, tzinfo=datetime.timezone.utc).timestamp() * 1000)


def current_cids(config_path: Path) -> list[str]:
    if not config_path.exists():
        return []
    return list(json.loads(config_path.read_text(encoding="utf-8")).get("article_cids") or [])


def render_config(
    texts: list[dict],
    cids: list[str],
    version_ids: list[str],
    generated_at: str,
) -> dict:
    return {
        "dataset": "rag_chunks_dgafp",
        "source": "legifrance_api:follow-live",
        "generated_at": generated_at,
        "texts": texts,
        "article_cids": cids,
        "article_version_ids": version_ids,
    }


def main(argv: list[str] | None = None) -> int:
    from dotenv import load_dotenv

    load_dotenv(REPO_ROOT / ".env")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--date", default=None, help="YYYY-MM-DD (défaut: aujourd'hui)")
    parser.add_argument("--dry-run", action="store_true", help="imprime le diff sans écrire")
    args = parser.parse_args(argv)

    from assistant_rh_data_engineering.legifrance.piste import PisteClient
    from assistant_rh_data_engineering.legifrance.reconcile import VIGUEUR, select_legifrance_rows
    from assistant_rh_data_engineering.utils.grist import GristClient

    date = datetime.date.fromisoformat(args.date) if args.date else datetime.date.today()
    millis = date_to_millis(date)

    selection = select_legifrance_rows(GristClient().list_records())
    followed = [row for row in selection.followed_rows if row.active]
    if not followed:
        print("Aucun texte Légifrance actif dans le référentiel Grist — refus d'écrire un cache vide.", file=sys.stderr)
        return 1

    client = PisteClient()
    texts: list[dict] = []
    cids: set[str] = set()
    version_ids: set[str] = set()
    for row in followed:
        articles = client.text_articles(row.uid, millis, kind=row.kind)
        en_vigueur = [a for a in articles if str(a.etat).strip().upper() == VIGUEUR]
        if not en_vigueur:
            print(f"[warn] TOC vide pour le texte actif {row.uid} ({row.kind}) — texte ignoré du cache.", file=sys.stderr)
            continue
        cids.update(a.cid for a in en_vigueur)
        version_ids.update(a.version_id or a.cid for a in en_vigueur)
        texts.append(
            {
                "uid": row.uid,
                "kind": row.kind,
                "titre": str(row.fields.get("titre_document") or "")[:120],
                "articles_en_vigueur": len(en_vigueur),
            }
        )
        print(f"{row.uid} ({row.kind}) @ {date} : {len(en_vigueur)} articles en vigueur")

    if not cids:
        print("Aucun article en vigueur collecté — refus d'écrire un cache vide.", file=sys.stderr)
        return 1

    sorted_cids = sorted(cids)
    old = set(current_cids(args.config))
    added = sorted(cids - old)
    removed = sorted(old - cids)
    print(f"\nTOTAL {len(followed)} textes suivis : {len(old)} -> {len(sorted_cids)} CIDs en vigueur")
    print(f"  + {len(added)} ajoutés")
    print(f"  - {len(removed)} retirés (abrogés / recodifiés / hors périmètre)")

    if args.dry_run:
        print("\n[dry-run] cache non modifié.")
        return 0
    generated_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    payload = render_config(texts, sorted_cids, sorted(version_ids), generated_at)
    args.config.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"\n✓ {args.config.relative_to(REPO_ROOT)} généré ({len(sorted_cids)} CIDs, {len(version_ids)} version_ids).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
