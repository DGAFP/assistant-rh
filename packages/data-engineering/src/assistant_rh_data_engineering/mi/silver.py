from __future__ import annotations

from typing import Any

from ..service_public.section_splitter import count_tokens, split_document_into_sections
from ..service_public.silver import SilverBundle, SilverRepository
from ..utils.helpers import sha256_text, stable_uuid_from_parts, utc_now_iso
from .bronze import MiBronzeAsset
from .config import DOC_SOURCE, MI_NAMESPACE, PUBLISHER, SilverConfig

__all__ = ["MiSilverBuilder", "SilverBundle", "SilverRepository"]


def mi_doc_uuid(short_id: str) -> str:
    return stable_uuid_from_parts(MI_NAMESPACE, DOC_SOURCE, short_id)


def mi_section_uuid(doc_id: str, section_index: int) -> str:
    return stable_uuid_from_parts(MI_NAMESPACE, doc_id, "section", section_index)


class MiSilverBuilder:
    """Silver MI: markdown OCR -> rag_documents/rag_sections.

    Sectionnement heading-based seedé sur le splitter Service-Public (le
    parsing par-ministère est la partie qui peut diverger: remplacer les
    imports service_public par un splitter local si les circulaires MI le
    demandent — c'est prévu par le design, issue #246).
    """

    def __init__(self, config: SilverConfig):
        self.config = config

    def build_bundle(self, asset: MiBronzeAsset) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        row = asset.row
        short_id = row.short_id
        doc_id = mi_doc_uuid(short_id)

        # Convention V3 du splitter: le titre du document est le heading ##.
        # Le prépendre garantit qu'aucun contenu OCR (préambule sans heading,
        # scans sans structure) n'échappe au sectionnement.
        doc_markdown = f"## {row.titre}\n\n{asset.ocr.markdown or ''}".strip()
        doc_text_hash = sha256_text(doc_markdown)

        sections = split_document_into_sections(
            doc_markdown=doc_markdown,
            doc_id=doc_id,
            doc_text_hash=doc_text_hash,
            min_section_chars=self.config.min_section_chars,
        )

        doc_record = {
            "doc_id": doc_id,
            "source": DOC_SOURCE,
            "source_url": str(row.fields.get("url") or "").strip() or None,
            "storage_path": row.cle_bucket,
            "title": row.titre,
            "full_title": row.titre,
            "short_id": short_id,
            "publisher": PUBLISHER,
            "doc_type": str(row.fields.get("type_document") or "").strip() or "Document PDF",
            "last_updated_date": None,
            "publication_date": row.date_publication,
            "page_count": asset.ocr.page_count or None,
            "lang": "fr",
            # checksum = sha256 du fichier d'origine (dropzone): c'est la clé
            # du delta de réconciliation (ignore_inchange), pas le hash texte.
            "checksum": asset.sha256,
            "parse_version": None,
            "parse_model": f"ocr_{asset.ocr.provider}_{asset.ocr.version}",
            "quality_flags": {
                "source_format": (row.cle_bucket.rsplit(".", 1)[-1].lower() if "." in row.cle_bucket else "pdf"),
                "ocr_provider": asset.ocr.provider,
                "ocr_from_cache": asset.ocr_from_cache,
            },
            "doc_markdown": doc_markdown,
            "doc_markdown_raw": asset.ocr.markdown,
            "doc_text_hash": doc_text_hash,
            "token_count": count_tokens(doc_markdown),
            "char_count": len(doc_markdown),
            "line_count": doc_markdown.count("\n") + (1 if doc_markdown else 0),
            "metadata": {
                "corpus": row.corpus,
                "grist_record_id": row.record_id,
                "cle_bucket": row.cle_bucket,
                "theme": str(row.fields.get("theme") or "").strip(),
            },
            "doc_structure": {
                "section_count": len(sections),
                "max_section_level": max((section.level for section in sections), default=0),
                "types": sorted({section.section_type for section in sections}),
            },
            "legacy_doc_id": None,
            "created_at": utc_now_iso(),
            "updated_at": utc_now_iso(),
        }

        section_records: list[dict[str, Any]] = []
        section_ids_by_index: dict[int, str] = {}
        for section in sections:
            section_id = mi_section_uuid(doc_id, section.section_index)
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
                    "doc_text_hash": doc_text_hash,
                    "is_indexable": section.is_indexable,
                }
            )

        for record, section in zip(section_records, sections, strict=True):
            if section.parent_index is not None:
                record["parent_section_id"] = section_ids_by_index.get(section.parent_index)

        return doc_record, section_records

    def persist_bundle(self, repository: SilverRepository, asset: MiBronzeAsset) -> SilverBundle:
        document, sections = self.build_bundle(asset)
        short_id = document["short_id"]
        return SilverBundle(
            document=document,
            sections=sections,
            document_path=repository.save_document(short_id, document),
            sections_path=repository.save_sections(short_id, sections),
        )
