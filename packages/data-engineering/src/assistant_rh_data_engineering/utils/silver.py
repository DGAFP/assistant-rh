"""Artefacts silver partagés (miroir de utils/gold.py).

SilverBundle + SilverRepository sont corpus-agnostiques: persistance JSON des
documents/sections/manifests, aucune logique de parsing. Chaque corpus
(service_public, legifrance, pdf_ministry) les réimporte au lieu d'en garder
une copie — un changement de layout lake est ainsi centralisé, et importer un
socle ne tire plus le parseur d'un module frère.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .helpers import ensure_dir, write_json, write_jsonl


@dataclass
class SilverBundle:
    document: dict[str, Any]
    sections: list[dict[str, Any]]
    document_path: Path
    sections_path: Path


class SilverRepository:
    def __init__(self, silver_dir: Path):
        self.root = ensure_dir(silver_dir)
        self.documents_dir = ensure_dir(self.root / "documents")
        self.sections_dir = ensure_dir(self.root / "sections")
        self.manifest_dir = ensure_dir(self.root / "manifests")

    def save_document(self, short_id: str, document: dict[str, Any]) -> Path:
        path = self.documents_dir / f"{short_id}.document.json"
        write_json(path, document)
        return path

    def save_sections(self, short_id: str, sections: list[dict[str, Any]]) -> Path:
        path = self.sections_dir / f"{short_id}.sections.jsonl"
        write_jsonl(path, sections)
        return path

    def save_manifest(self, manifest: dict[str, Any]) -> Path:
        path = self.manifest_dir / f"silver_manifest_{manifest['run_id']}.json"
        write_json(path, manifest)
        return path
