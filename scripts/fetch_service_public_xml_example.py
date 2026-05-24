#!/usr/bin/env python3
"""
Exemple minimal de récupération d'une fiche Service-Public depuis le flux officiel XML.

Flux:
1. Interroger l'API data.gouv.fr pour récupérer les métadonnées du dataset.
2. Trouver l'URL du ZIP `vosdroits-latest.zip`.
3. Extraire une fiche XML précise (par défaut: F12391).
4. Convertir le XML en contenu texte/markdown via le parser existant du projet.

Usage:
    python scripts/fetch_service_public_xml_example.py
    python scripts/fetch_service_public_xml_example.py --fiche-id F12391 --save-markdown tmp/F12391.md
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import zipfile
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_SOURCE_PATHS = [
    REPO_ROOT,
    REPO_ROOT / "packages" / "data-engineering" / "src",
]
for source_path in reversed(PACKAGE_SOURCE_PATHS):
    path_str = str(source_path)
    if path_str not in sys.path:
        sys.path.insert(0, path_str)

from assistant_rh_data_engineering.service_public.xml_parser import parse_fiche_xml_from_bytes  # noqa: E402

DATA_GOUV_API_ROOT = "https://www.data.gouv.fr/api/1"
DATASET_SLUG = "service-public-fr-guide-vos-droits-et-demarches-particuliers"
DEFAULT_FICHE_ID = "F12391"
DEFAULT_DIRECT_ZIP_URL = (
    "https://lecomarquage.service-public.gouv.fr/vdd/3.4/part/zip/vosdroits-latest.zip"
)


def _http_get(url: str) -> bytes:
    request = Request(
        url,
        headers={
            "User-Agent": "assistant-rh/1.0 (+https://www.data.gouv.fr/)",
            "Accept": "*/*",
        },
    )
    with urlopen(request, timeout=60) as response:
        return response.read()


def fetch_dataset_metadata() -> dict[str, Any]:
    api_url = f"{DATA_GOUV_API_ROOT}/datasets/{DATASET_SLUG}/"
    return json.loads(_http_get(api_url).decode("utf-8"))


def select_zip_url(dataset: dict[str, Any]) -> str:
    resources = dataset.get("resources", [])
    candidates: list[str] = []

    for resource in resources:
        for key in ("url", "latest", "original_url"):
            value = resource.get(key)
            if isinstance(value, str) and value:
                candidates.append(value)

    for url in candidates:
        lowered = url.lower()
        if lowered.endswith("vosdroits-latest.zip") or "/zip/" in lowered:
            return url

    for url in candidates:
        if url.lower().endswith(".zip"):
            return url

    raise RuntimeError("Impossible de trouver une ressource ZIP dans les métadonnées data.gouv.fr.")


def load_fiche_xml_from_zip(zip_bytes: bytes, fiche_id: str) -> bytes:
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
        expected_suffixes = (
            f"/{fiche_id}.xml",
            f"\\{fiche_id}.xml",
            f"{fiche_id}.xml",
        )
        for member in archive.namelist():
            if member.endswith(expected_suffixes):
                return archive.read(member)

    raise FileNotFoundError(f"Fiche {fiche_id}.xml introuvable dans le ZIP.")


def render_preview(parsed: dict[str, Any], max_chars: int = 1800) -> str:
    title = parsed.get("title", "")
    source_url = parsed.get("source_url", "")
    metadata = parsed.get("metadata", {})
    verification_date = metadata.get("date_verification") or parsed.get("last_updated_date")
    doc_markdown = parsed.get("doc_markdown", "").strip()
    excerpt = doc_markdown[:max_chars].rstrip()

    parts = [
        f"Titre: {title}",
        f"URL source: {source_url}",
        f"Date de verification: {verification_date}",
        "",
        "Apercu du contenu:",
        excerpt,
    ]
    return "\n".join(parts)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fiche-id", default=DEFAULT_FICHE_ID, help="Identifiant de fiche Service-Public (ex: F12391)")
    parser.add_argument(
        "--save-markdown",
        type=Path,
        help="Chemin de sortie pour sauvegarder le markdown complet extrait du XML",
    )
    args = parser.parse_args()

    fiche_id = args.fiche_id.upper().strip()

    try:
        dataset = fetch_dataset_metadata()
        zip_url = select_zip_url(dataset)
    except (HTTPError, URLError, TimeoutError, RuntimeError) as exc:
        print(
            f"[WARN] Impossible d'utiliser l'API data.gouv.fr ({exc}). "
            f"Fallback vers l'URL directe du flux DILA: {DEFAULT_DIRECT_ZIP_URL}",
            file=sys.stderr,
        )
        zip_url = DEFAULT_DIRECT_ZIP_URL

    zip_bytes = _http_get(zip_url)
    xml_bytes = load_fiche_xml_from_zip(zip_bytes, fiche_id)
    parsed = parse_fiche_xml_from_bytes(xml_bytes, fiche_id)
    if not parsed:
        raise RuntimeError(f"Le parser XML a échoué pour la fiche {fiche_id}.")

    if args.save_markdown:
        args.save_markdown.parent.mkdir(parents=True, exist_ok=True)
        args.save_markdown.write_text(parsed["doc_markdown"], encoding="utf-8")

    print(f"ZIP utilise: {zip_url}")
    if args.save_markdown:
        print(f"Markdown sauvegarde: {args.save_markdown}")
    print()
    print(render_preview(parsed))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
