from __future__ import annotations

import re
from collections import Counter
from math import ceil
from typing import Any

from ..service_public.section_splitter import count_tokens, split_document_into_sections
from ..service_public.silver import SilverBundle, SilverRepository
from ..utils.helpers import sha256_text, stable_uuid_from_parts, utc_now_iso
from ..utils.ocr import OcrResult
from .bronze import MasaBronzeAsset
from .config import DOC_SOURCE, MASA_NAMESPACE, PUBLISHER, SilverConfig

__all__ = ["MasaSilverBuilder", "SilverBundle", "SilverRepository"]

_HEADING_RE = re.compile(r"^(#{1,5})(\s)", re.MULTILINE)
# "Page 12", "- 12 -", "2 de 3", "Page 4 sur 9", "12/33"…
_PAGE_NUMBER_LINE_RE = re.compile(
    r"^[-–—\s]*(page\s+)?\d+(\s*(de|sur|/)\s*\d+)?[-–—\s]*$",
    re.IGNORECASE,
)


def masa_doc_uuid(short_id: str) -> str:
    return stable_uuid_from_parts(MASA_NAMESPACE, DOC_SOURCE, short_id)


def masa_section_uuid(doc_id: str, section_index: int) -> str:
    return stable_uuid_from_parts(MASA_NAMESPACE, doc_id, "section", section_index)


def _page_markdowns(ocr: OcrResult) -> list[str]:
    pages = [str(page.get("markdown") or "") for page in ocr.pages]
    if any(page.strip() for page in pages):
        return pages
    return [ocr.markdown or ""]


def _page_edge_boilerplate(pages: list[str]) -> set[str]:
    """Lignes d'en-tête/pied de page répétées (audit 2026-07-04: « Direction
    des ressources humaines ministérielle » sur 49 des 60 pages d'un guide).

    Seules les lignes courtes en bord de page, revues sur >= 40 % des pages,
    sont retirées — le contenu légitimement répétitif (cellules de formulaires,
    tableaux) n'est jamais en bord de page sur autant de pages.
    """
    if len(pages) < 3:
        return set()
    counter: Counter[str] = Counter()
    for page in pages:
        lines = [line.strip() for line in page.splitlines() if line.strip()]
        for line in set(lines[:3] + lines[-3:]):
            if 3 <= len(line) <= 100 and not line.startswith(("#", "|")):
                counter[line] += 1
    threshold = max(3, ceil(len(pages) * 0.4))
    return {line for line, count in counter.items() if count >= threshold}


def _own_section_markdowns(doc_markdown: str, sections: list[Any]) -> dict[int, str]:
    """Contenu PROPRE de chaque section: sa plage moins celles de ses enfants.

    Le splitter donne aux sections parentes un contenu qui inclut leurs
    descendants; chunker chaque section telle quelle dupliquerait le texte
    2-3x dans l'index (constat d'audit 2026-07-04 sur le corpus MI réel).
    Le contenu propre s'arrête au premier heading enfant: les sections
    forment alors une partition du document.
    """
    first_child_start: dict[int, int] = {}
    for section in sections:
        parent = section.parent_index
        if parent is None:
            continue
        current = first_child_start.get(parent)
        first_child_start[parent] = section.char_start if current is None else min(current, section.char_start)

    own: dict[int, str] = {}
    for section in sections:
        end = first_child_start.get(section.section_index, section.char_end)
        own[section.section_index] = doc_markdown[section.char_start : end].strip()
    return own


def normalize_ocr_markdown(ocr: OcrResult, titre: str) -> str:
    """Markdown OCR -> markdown sectionnable (constats de l'audit sur corpus réel).

    - Marqueurs <!-- PAGE: n --> entre pages (page_start/page_end des sections).
    - En-têtes/pieds de page répétés et lignes de folio retirés.
    - Headings rétrogradés d'un niveau: mistral-ocr émet ses titres de section
      en # (H1) que le splitter ignore par convention (## = titre du document).
      Après rétrogradation, tout heading OCR est détecté sous le titre prépendu.
    """
    pages = _page_markdowns(ocr)
    boilerplate = _page_edge_boilerplate(pages)

    cleaned_pages: list[str] = []
    for number, page in enumerate(pages, start=1):
        lines = page.splitlines()
        # Les lignes de folio ne sont retirées qu'en bord de page: une ligne
        # numérique en pleine page (année, montant, réponse d'un formulaire)
        # est du contenu, pas un numéro de page.
        content_indexes = [index for index, line in enumerate(lines) if line.strip()]
        edge_indexes = set(content_indexes[:3] + content_indexes[-3:])
        kept = [
            line
            for index, line in enumerate(lines)
            if line.strip() not in boilerplate and not (index in edge_indexes and _PAGE_NUMBER_LINE_RE.match(line.strip()))
        ]
        text = "\n".join(kept).strip()
        cleaned_pages.append(f"<!-- PAGE: {number} -->\n{text}" if len(pages) > 1 else text)

    body = _HEADING_RE.sub(r"#\1\2", "\n\n".join(cleaned_pages))
    return f"## {titre}\n\n{body}".strip()


class MasaSilverBuilder:
    """Silver MASA: markdown OCR -> rag_documents/rag_sections.

    Sectionnement heading-based seedé sur le splitter Service-Public (le
    parsing par-ministère est la partie qui peut diverger: remplacer les
    imports service_public par un splitter local si les documents MASA le
    demandent — c'est prévu par le design, issue #247).
    """

    def __init__(self, config: SilverConfig):
        self.config = config

    def build_bundle(self, asset: MasaBronzeAsset) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        row = asset.row
        short_id = row.short_id
        doc_id = masa_doc_uuid(short_id)

        # Convention V3 du splitter: le titre du document est le heading ##.
        # Le prépendre garantit qu'aucun contenu OCR (préambule sans heading,
        # scans sans structure) n'échappe au sectionnement.
        doc_markdown = normalize_ocr_markdown(asset.ocr, row.titre)
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

        own_markdowns = _own_section_markdowns(doc_markdown, sections)

        section_records: list[dict[str, Any]] = []
        section_ids_by_index: dict[int, str] = {}
        for section in sections:
            section_id = masa_section_uuid(doc_id, section.section_index)
            section_ids_by_index[section.section_index] = section_id
            own_markdown = own_markdowns.get(section.section_index, section.section_markdown)
            section_records.append(
                {
                    "section_id": section_id,
                    "doc_id": doc_id,
                    "heading": section.heading,
                    "heading_path": section.heading_path,
                    "section_markdown": own_markdown,
                    "markdown_content": own_markdown,
                    "section_index": section.section_index,
                    "parent_section_id": None,
                    "references_juridiques": section.references_juridiques,
                    "section_type": section.section_type,
                    "level": section.level,
                    "page_start": section.page_start,
                    "page_end": section.page_end,
                    "token_count": count_tokens(own_markdown),
                    "char_count": len(own_markdown),
                    "text_hash": sha256_text(own_markdown),
                    "doc_text_hash": doc_text_hash,
                    "is_indexable": section.is_indexable and len(own_markdown) >= self.config.min_section_chars,
                }
            )

        for record, section in zip(section_records, sections, strict=True):
            if section.parent_index is not None:
                record["parent_section_id"] = section_ids_by_index.get(section.parent_index)

        return doc_record, section_records

    def persist_bundle(self, repository: SilverRepository, asset: MasaBronzeAsset) -> SilverBundle:
        document, sections = self.build_bundle(asset)
        short_id = document["short_id"]
        return SilverBundle(
            document=document,
            sections=sections,
            document_path=repository.save_document(short_id, document),
            sections_path=repository.save_sections(short_id, sections),
        )
