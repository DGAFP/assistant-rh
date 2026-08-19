from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Any

from ..utils.helpers import (
    stable_doc_uuid,
    stable_section_uuid,
    utc_now_iso,
)
from ..utils.silver import SilverBundle, SilverRepository
from .config import SilverConfig
from .section_splitter import split_document_into_sections
from .xml_parser import parse_fiche_xml_from_bytes

__all__ = ["ServicePublicSilverBuilder", "SilverBundle", "SilverRepository"]


class ServicePublicSilverBuilder:
    def __init__(self, config: SilverConfig):
        self.config = config

    def _filter_xml_by_situation(self, xml_bytes: bytes) -> bytes:
        if not self.config.situation_filter:
            return xml_bytes

        root = ET.fromstring(xml_bytes.decode("utf-8"))
        liste_situations = root.find(".//ListeSituations")
        if liste_situations is None:
            return xml_bytes

        wanted = self.config.situation_filter.strip().upper()
        situations = liste_situations.findall("Situation")
        kept = []
        for situation in situations:
            titre = situation.find("Titre")
            if titre is not None and (titre.text or "").strip().upper() == wanted:
                kept.append(situation)

        if not kept:
            return xml_bytes

        for situation in list(situations):
            liste_situations.remove(situation)
        for situation in kept:
            liste_situations.append(situation)

        return ET.tostring(root, encoding="utf-8")

    def build_bundle(
        self,
        fiche_id: str,
        xml_bytes: bytes,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        filtered_xml = self._filter_xml_by_situation(xml_bytes)
        parsed = parse_fiche_xml_from_bytes(filtered_xml, fiche_id)
        if not parsed:
            raise RuntimeError(f"Impossible de parser la fiche XML {fiche_id}.")

        doc_id = stable_doc_uuid(parsed["short_id"], parsed["source_url"])
        sections = split_document_into_sections(
            doc_markdown=parsed["doc_markdown"],
            doc_id=doc_id,
            doc_text_hash=parsed["doc_text_hash"],
            min_section_chars=self.config.min_section_chars,
            doc_references=parsed["metadata"].get("references_juridiques", []),
        )

        doc_record = {
            "doc_id": doc_id,
            "source": "service_public",
            "source_url": parsed["source_url"],
            "storage_path": None,
            "title": parsed["title"],
            "full_title": parsed["title"],
            "short_id": parsed["short_id"],
            "publisher": "Service-Public",
            "doc_type": parsed["metadata"].get("category") or "Fiche pratique",
            "last_updated_date": parsed.get("last_updated_date"),
            "publication_date": None,
            "page_count": None,
            "lang": "fr",
            "checksum": parsed["doc_text_hash"],
            "parse_version": parsed["metadata"].get("parser_version"),
            "parse_model": "xml_parser_v3",
            "quality_flags": {"source_format": "xml", "official_feed": True},
            "doc_markdown": parsed["doc_markdown"],
            "doc_markdown_raw": parsed["doc_markdown"],
            "doc_text_hash": parsed["doc_text_hash"],
            "token_count": parsed["token_count"],
            "char_count": parsed["char_count"],
            "line_count": parsed["doc_markdown"].count("\n") + (1 if parsed["doc_markdown"] else 0),
            "metadata": parsed["metadata"],
            "doc_structure": {
                "section_count": len(sections),
                "max_section_level": max(
                    (section.level for section in sections),
                    default=0,
                ),
                "types": sorted({section.section_type for section in sections}),
            },
            "legacy_doc_id": None,
            "created_at": utc_now_iso(),
            "updated_at": utc_now_iso(),
        }

        section_records: list[dict[str, Any]] = []
        section_ids_by_index: dict[int, str] = {}
        for section in sections:
            section_id = stable_section_uuid(doc_id, section.section_index)
            section_ids_by_index[section.section_index] = section_id
            section_records.append(
                {
                    "section_id": section_id,
                    "doc_id": doc_id,
                    "heading": section.heading,
                    "heading_path": section.heading_path,
                    "section_markdown": section.section_markdown,
                    "markdown_content": section.section_markdown,
                    "section_index": section.section_index,
                    "parent_section_id": None,
                    "references_juridiques": section.references_juridiques,
                    "section_type": section.section_type,
                    "level": section.level,
                    "page_start": section.page_start,
                    "page_end": section.page_end,
                    "token_count": section.token_count,
                    "char_count": section.char_count,
                    "text_hash": section.text_hash,
                    "doc_text_hash": parsed["doc_text_hash"],
                    "is_indexable": section.is_indexable,
                }
            )

        for record in section_records:
            source_section = next(s for s in sections if s.section_index == record["section_index"])
            if source_section.parent_index is not None:
                record["parent_section_id"] = section_ids_by_index.get(source_section.parent_index)

        return doc_record, section_records

    def persist_bundles(
        self,
        repository: SilverRepository,
        bronze_assets: list[Any],
    ) -> list[SilverBundle]:
        bundles: list[SilverBundle] = []
        for asset in bronze_assets:
            document, sections = self.build_bundle(asset.fiche_id, asset.xml_bytes)
            document_path = repository.save_document(asset.fiche_id, document)
            sections_path = repository.save_sections(asset.fiche_id, sections)
            bundles.append(
                SilverBundle(
                    document=document,
                    sections=sections,
                    document_path=document_path,
                    sections_path=sections_path,
                )
            )

        repository.save_manifest(
            {
                "run_id": utc_now_iso().replace(":", "").replace(".", ""),
                "created_at": utc_now_iso(),
                "document_count": len(bundles),
                "section_count": sum(len(bundle.sections) for bundle in bundles),
                "short_ids": [bundle.document["short_id"] for bundle in bundles],
            }
        )
        return bundles
