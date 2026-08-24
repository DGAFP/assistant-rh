from __future__ import annotations

from typing import Any

from ..utils.helpers import LEGIFRANCE_NAMESPACE, sha256_text, stable_uuid_from_parts, utc_now_iso
from ..utils.silver import SilverBundle, SilverRepository
from .config import SilverConfig
from .helpers import normalize_short_id

__all__ = ["LegifranceSilverBuilder", "SilverBundle", "SilverRepository"]


class LegifranceSilverBuilder:
    def __init__(self, config: SilverConfig):
        self.config = config

    @staticmethod
    def _token_count(text: str) -> int:
        return max(1, len(text) // 4) if text else 0

    @staticmethod
    def _clean_legacy_title(source_name: str) -> str:
        return source_name.replace(" - Légifrance.txt", "").replace(" - Légifrance.txt", "").replace(".txt", "")

    def _build_legacy_text_bundle(self, asset: Any) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        payload = asset.payload
        source_name = str(payload.get("source_name") or asset.short_id).strip()
        source_url = str(payload.get("source_url") or "").strip()
        short_id = normalize_short_id(payload.get("short_id"), source_name)
        doc_markdown = str(payload.get("text") or "").strip()
        checksum = sha256_text(doc_markdown)
        created_at = utc_now_iso()
        doc_id = stable_uuid_from_parts(LEGIFRANCE_NAMESPACE, "legacy_text", source_name, source_url or short_id)
        section_id = stable_uuid_from_parts(LEGIFRANCE_NAMESPACE, doc_id, "section", 0)
        title = self._clean_legacy_title(source_name)
        metadata = {
            "legacy_qna_source_name": source_name,
            "legacy_qna_source_path": payload.get("source_path"),
            "legacy_qna_raw_source_path": payload.get("raw_source_path"),
            "thematique": payload.get("thematique") or self.config.default_thematique,
        }
        document = {
            "doc_id": doc_id,
            "source": "legifrance",
            "source_url": source_url,
            "storage_path": payload.get("raw_source_path") or payload.get("source_path"),
            "title": title,
            "full_title": source_name.replace(".txt", ""),
            "short_id": short_id,
            "publisher": "Légifrance",
            "doc_type": "LegalText",
            "last_updated_date": None,
            "publication_date": None,
            "page_count": None,
            "lang": "fr",
            "checksum": checksum,
            "parse_version": "legifrance_silver_v4",
            "parse_model": "legifrance_legacy_text_raw",
            "quality_flags": {"source_format": "txt", "legacy_qna_source": True},
            "doc_markdown": doc_markdown,
            "doc_markdown_raw": doc_markdown,
            "doc_text_hash": checksum,
            "token_count": self._token_count(doc_markdown),
            "char_count": len(doc_markdown),
            "line_count": doc_markdown.count("\n") + 1 if doc_markdown else 0,
            "metadata": metadata,
            "doc_structure": {"section_count": 1, "max_section_level": 1, "types": ["legacy_text"]},
            "legacy_doc_id": None,
            "created_at": created_at,
            "updated_at": created_at,
        }
        sections = [
            {
                "section_id": section_id,
                "doc_id": doc_id,
                "heading": source_name,
                "heading_path": source_name,
                "section_markdown": doc_markdown,
                "markdown_content": doc_markdown,
                "section_index": 0,
                "parent_section_id": None,
                "references_juridiques": [],
                "section_type": "legacy_text",
                "level": 1,
                "page_start": None,
                "page_end": None,
                "token_count": self._token_count(doc_markdown),
                "char_count": len(doc_markdown),
                "text_hash": checksum,
                "doc_text_hash": checksum,
                "is_indexable": True,
            }
        ]
        return document, sections

    def _build_article_bundle(self, asset: Any) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        payload = asset.payload
        num_article = str(payload["num_article"]).strip()
        full_title = str(payload.get("full_title") or payload.get("title") or f"Article {num_article}").strip()
        subtitles = str(payload.get("full_sections_title") or payload.get("subtitles") or "").strip()
        body = str(payload.get("text") or "").strip()
        markdown_parts = [f"# {full_title}"]
        if subtitles:
            markdown_parts.append(subtitles)
        markdown_parts.append(f"## Article {num_article}")
        markdown_parts.append(body)
        doc_markdown = "\n\n".join(part for part in markdown_parts if part)
        checksum = sha256_text(doc_markdown)
        created_at = utc_now_iso()
        doc_id = stable_uuid_from_parts(
            LEGIFRANCE_NAMESPACE,
            "article",
            payload.get("cid") or payload.get("article_id"),
            num_article,
            payload.get("source_url") or "",
        )
        section_id = stable_uuid_from_parts(LEGIFRANCE_NAMESPACE, doc_id, "section", 0)
        metadata = {
            "article_id": payload.get("article_id"),
            "cid": payload.get("cid"),
            "num_article": num_article,
            "num_norm": payload.get("num_norm"),
            "subtitles": payload.get("subtitles"),
            "full_sections_title": payload.get("full_sections_title") or payload.get("subtitles"),
            "category": payload.get("category"),
            "status": payload.get("status"),
            "source_name": payload.get("source_name"),
            "section_parent_cid": payload.get("section_parent_cid"),
            "section_parent_titre": payload.get("section_parent_titre"),
            "code_id": payload.get("code_id"),
            "state": payload.get("state"),
            "date_version": payload.get("date_version"),
            "start_date": payload.get("start_date"),
            "end_date": payload.get("end_date"),
            "ministry": payload.get("ministry"),
            "nota": payload.get("nota"),
            "lien_citations": payload.get("lien_citations"),
            "lien_citations_count": payload.get("lien_citations_count"),
            "lien_modifications": payload.get("lien_modifications"),
            "lien_modifications_count": payload.get("lien_modifications_count"),
            "lien_concordes": payload.get("lien_concordes"),
            "lien_concordes_count": payload.get("lien_concordes_count"),
            "comporte_liens_sp": payload.get("comporte_liens_sp"),
            "origin": payload.get("origin"),
        }
        parse_model = "legifrance_bulk_xml" if str(payload.get("origin") or "").startswith("legi_bulk") else "legifrance_article_raw_json"
        short_id = normalize_short_id(
            payload.get("short_id"),
            str(payload.get("cid") or payload.get("article_id") or num_article),
        )
        document = {
            "doc_id": doc_id,
            "source": "legifrance",
            "source_url": payload.get("source_url") or "",
            "storage_path": None,
            "title": str(payload.get("title") or full_title).strip(),
            "full_title": full_title,
            "short_id": short_id,
            "publisher": "Légifrance",
            "doc_type": "Article",
            "last_updated_date": payload.get("date_version"),
            "publication_date": None,
            "page_count": None,
            "lang": "fr",
            "checksum": checksum,
            "parse_version": "legifrance_silver_v4",
            "parse_model": parse_model,
            "quality_flags": {"source_format": "bulk/xml" if parse_model == "legifrance_bulk_xml" else "raw/json", "official_feed": True},
            "doc_markdown": doc_markdown,
            "doc_markdown_raw": doc_markdown,
            "doc_text_hash": checksum,
            "token_count": self._token_count(doc_markdown),
            "char_count": len(doc_markdown),
            "line_count": doc_markdown.count("\n") + 1 if doc_markdown else 0,
            "metadata": metadata,
            "doc_structure": {"section_count": 1, "max_section_level": 2, "types": ["article"]},
            "legacy_doc_id": None,
            "created_at": created_at,
            "updated_at": created_at,
        }
        heading = f"Article {num_article}"
        sections = [
            {
                "section_id": section_id,
                "doc_id": doc_id,
                "heading": heading,
                "heading_path": f"{subtitles} > {heading}".strip(" >") if subtitles else heading,
                "section_markdown": body,
                "markdown_content": body,
                "section_index": 0,
                "parent_section_id": None,
                "references_juridiques": [],
                "section_type": "article",
                "level": 2,
                "page_start": None,
                "page_end": None,
                "token_count": self._token_count(body),
                "char_count": len(body),
                "text_hash": sha256_text(body),
                "doc_text_hash": checksum,
                "is_indexable": True,
            }
        ]
        return document, sections

    def build_bundle(self, asset: Any) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        if asset.asset_type == "legacy_text":
            return self._build_legacy_text_bundle(asset)
        return self._build_article_bundle(asset)

    def persist_bundles(
        self,
        repository: SilverRepository,
        bronze_assets: list[Any],
    ) -> list[SilverBundle]:
        bundles: list[SilverBundle] = []
        for asset in bronze_assets:
            document, sections = self.build_bundle(asset)
            document_path = repository.save_document(document["short_id"], document)
            sections_path = repository.save_sections(document["short_id"], sections)
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
