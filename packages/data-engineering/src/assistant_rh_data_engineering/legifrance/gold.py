from __future__ import annotations

import hashlib
import re
from typing import Any

from ..service_public.qna_chunking import chunk_markdown_like_notebook
from ..utils.gold import GoldBundle, GoldRepository, build_embedders
from ..utils.helpers import utc_now_iso
from .config import EmbeddingConfig, GoldConfig
from .helpers import build_legifrance_article_url


def _hard_wrap(text: str, max_chars: int) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    parts: list[str] = []
    remaining = text
    while len(remaining) > max_chars:
        window = remaining[:max_chars]
        split_at = max(window.rfind("\n\n"), window.rfind(". "), window.rfind("; "), window.rfind(", "))
        if split_at < max_chars // 2:
            split_at = max_chars
        parts.append(remaining[:split_at].strip())
        remaining = remaining[split_at:].strip()
    if remaining:
        parts.append(remaining)
    return [part for part in parts if part]


def split_legal_chunks(text: str, max_chars: int, min_chars: int) -> list[str]:
    cleaned = re.sub(r"\n{3,}", "\n\n", (text or "").strip())
    if not cleaned:
        return []
    if len(cleaned) <= max_chars:
        return [cleaned]

    paragraphs = [paragraph.strip() for paragraph in re.split(r"\n{2,}", cleaned) if paragraph.strip()]
    if not paragraphs:
        return _hard_wrap(cleaned, max_chars)

    chunks: list[str] = []
    buffer = ""
    for paragraph in paragraphs:
        candidate = f"{buffer}\n\n{paragraph}".strip() if buffer else paragraph
        if len(candidate) <= max_chars:
            buffer = candidate
            continue
        if buffer:
            chunks.append(buffer)
            buffer = ""
        if len(paragraph) <= max_chars:
            buffer = paragraph
        else:
            chunks.extend(_hard_wrap(paragraph, max_chars))
    if buffer:
        chunks.append(buffer)

    merged: list[str] = []
    for chunk in chunks:
        if merged and len(chunk) < min_chars and len(merged[-1]) + 2 + len(chunk) <= max_chars:
            merged[-1] = f"{merged[-1]}\n\n{chunk}"
        else:
            merged.append(chunk)
    return merged


class LegifranceGoldBuilder:
    def __init__(self, embedding_config: EmbeddingConfig, gold_config: GoldConfig):
        self.embedding_config = embedding_config
        self.gold_config = gold_config

    @staticmethod
    def _embed_chunks(embedding_config: EmbeddingConfig, chunks: list[dict[str, Any]]) -> None:
        embedders = build_embedders(embedding_config)
        if not embedders or not chunks:
            return
        texts = [str(row.get("chunk_text") or "") for row in chunks]
        for embedder in embedders:
            vectors = embedder.embed_texts(texts)
            for row, vector in zip(chunks, vectors, strict=True):
                row[embedder.column_name] = vector

    @staticmethod
    def _build_article_chunk_id(article_id: str, chunk_index: int) -> str:
        return f"{article_id}_{chunk_index}"

    @staticmethod
    def _build_legacy_chunk_id(source_name: str, qa_id: str | None, role: str | None, chunk_index: int, text: str) -> str:
        key = "|".join([source_name, qa_id or "", role or "", str(chunk_index), text[:256]])
        return hashlib.sha1(key.encode("utf-8")).hexdigest()

    @staticmethod
    def _build_article_chunk_text(document: dict[str, Any], metadata: dict[str, Any], body: str) -> str:
        parts = [str(document.get("full_title") or document.get("title") or "").strip()]
        context = str(metadata.get("full_sections_title") or metadata.get("section_parent_titre") or "").strip()
        category = str(metadata.get("category") or "").upper()
        status = str(metadata.get("status") or "").strip()
        if context:
            parts.append(context if category in {"", "CODE", "DECRET"} else f"Contexte: {context}")
        parts.append(f"Article {metadata.get('num_article') or document.get('short_id')}")
        if category != "CODE" and not context and status:
            parts.append(f"Statut: {status}")
        header = "\n".join(part for part in parts if part).strip()
        return f"{header}\n\n{body}".strip() if body else header

    @staticmethod
    def _normalize_open_end_date(value: Any) -> Any:
        return None if value == "2999-01-01" else value

    @staticmethod
    def _normalize_legacy_link_item(link: dict[str, Any]) -> dict[str, Any]:
        raw_article_id = link.get("articleId") or link.get("id")
        article_id = raw_article_id if str(raw_article_id or "").startswith("LEGIARTI") else None
        return {
            "textCid": link.get("textCid") or link.get("cidtexte"),
            "linkType": link.get("linkType") or link.get("typelien"),
            "numTexte": link.get("numTexte") or link.get("numtexte"),
            "articleId": article_id,
            "dateDebut": link.get("dateDebut"),
            "datePubli": link.get("datePubli"),
            "parentCid": link.get("parentCid"),
            "textTitle": link.get("textTitle") or link.get("label"),
            "articleNum": link.get("articleNum") or link.get("num"),
            "natureText": link.get("natureText") or link.get("naturetexte"),
            "linkOrientation": link.get("linkOrientation") or link.get("sens"),
        }

    @classmethod
    def _normalize_legacy_links(cls, value: Any) -> list[dict[str, Any]] | None:
        if not isinstance(value, list):
            return None
        normalized = [
            cls._normalize_legacy_link_item(link)
            for link in value
            if isinstance(link, dict) and str(link.get("linkType") or link.get("typelien") or "").upper() != "TXT_SOURCE"
        ]
        return normalized or None

    def _build_article_chunk_bodies(self, section: dict[str, Any]) -> list[str]:
        section_body = str(section.get("section_markdown") or "").strip()
        if not section_body:
            return []
        if self.gold_config.single_chunk_per_article:
            return [section_body]
        return split_legal_chunks(
            section_body,
            max_chars=self.gold_config.max_chunk_chars,
            min_chars=self.gold_config.min_chunk_chars,
        )

    def _build_legacy_text_chunks(self, document: dict[str, Any], sections: list[dict[str, Any]]) -> list[dict[str, Any]]:
        metadata = document.get("metadata", {})
        source_name = str(metadata.get("legacy_qna_source_name") or document.get("full_title") or "").strip()
        raw_text = str(document.get("doc_markdown_raw") or document.get("doc_markdown") or "").strip()
        thematique = str(metadata.get("thematique") or "legifrance")
        if not source_name or not raw_text:
            return []

        raw_chunks = chunk_markdown_like_notebook(
            raw_text,
            source_name=source_name,
            thematique=thematique,
            max_chars=self.gold_config.max_chunk_chars,
            overlap=max(200, self.gold_config.chunk_overlap_chars),
        )
        section_id = sections[0]["section_id"] if len(sections) == 1 else None
        created_at = utc_now_iso()
        chunks: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for raw_chunk in raw_chunks:
            chunk_text = str(raw_chunk.get("text") or "").strip()
            chunk_id = self._build_legacy_chunk_id(
                source_name,
                raw_chunk.get("qa_id"),
                raw_chunk.get("role"),
                int(raw_chunk.get("chunk_index", 0)),
                chunk_text,
            )
            if chunk_id in seen_ids:
                continue
            seen_ids.add(chunk_id)
            chunks.append(
                {
                    "hash_id": chunk_id,
                    "chunk_id": chunk_id,
                    "qa_id": raw_chunk.get("qa_id"),
                    "parent_qa_id": raw_chunk.get("parent_qa_id"),
                    "source_name": raw_chunk.get("source_name"),
                    "section_path": raw_chunk.get("section_path") or "",
                    "role": raw_chunk.get("role"),
                    "chunk_index": int(raw_chunk.get("chunk_index", 0)),
                    "chunk_number": int(raw_chunk.get("chunk_index", 0)),
                    "text": chunk_text,
                    "chunk_text": chunk_text,
                    "title": document.get("title") or document.get("full_title") or "",
                    "full_title": document.get("full_title") or document.get("title") or "",
                    "number": None,
                    "category": None,
                    "url": document.get("source_url") or "",
                    "cid": None,
                    "status": None,
                    "subtitles": None,
                    "nota": None,
                    "ministry": None,
                    "section_parent_cid": None,
                    "section_parent_titre": None,
                    "lien_citations": None,
                    "lien_citations_count": 0,
                    "lien_modifications": None,
                    "lien_modifications_count": 0,
                    "lien_concordes": None,
                    "lien_concordes_count": 0,
                    "comporte_liens_sp": False,
                    "thematique": raw_chunk.get("thematique") or thematique,
                    "lang": raw_chunk.get("lang") or document.get("lang", "fr"),
                    "references_juridiques": [],
                    "source_document_id": document["doc_id"],
                    "section_id": section_id,
                    "short_id": document["short_id"],
                    "source": "LEGIFRANCE",
                    "created_at": created_at,
                    "updated_at": created_at,
                    "_targets": ["modern"],
                }
            )

        self._embed_chunks(self.embedding_config, chunks)
        return chunks

    def _build_article_chunks(self, document: dict[str, Any], sections: list[dict[str, Any]]) -> list[dict[str, Any]]:
        metadata = document.get("metadata", {})
        short_id = document["short_id"]
        thematique = metadata.get("category") or metadata.get("thematique") or ""
        article_id = str(metadata.get("cid") or metadata.get("article_id") or short_id)
        # URL d'affichage (#350) : la route article_lc n'accepte que des ids de
        # VERSION — construite sur metadata.article_id (META_COMMUN/ID du dump
        # DILA), jamais sur le cid chronique (404 pour ~43 % du CGFP, ancienne
        # version sinon). L'identité corpus (chunk_id, cid, doc_id silver)
        # reste keyée chronique (acquis revue #307). Sans article_id : repli
        # sur source_url (chronique), comportement historique.
        version_id = str(metadata.get("article_id") or "").strip()
        display_url = build_legifrance_article_url(version_id, metadata.get("category")) if version_id else str(document.get("source_url") or "")
        full_sections_title = str(
            metadata.get("full_sections_title") or metadata.get("subtitles") or metadata.get("section_parent_titre") or ""
        ).strip()
        created_at = utc_now_iso()
        chunks: list[dict[str, Any]] = []

        for section in sections:
            for index, body in enumerate(self._build_article_chunk_bodies(section)):
                chunk_text = self._build_article_chunk_text(document, metadata, body)
                chunk_id = self._build_article_chunk_id(article_id, index)
                qa_id = hashlib.sha1(f"{short_id}|{section.get('heading_path') or section.get('heading') or ''}|{index}".encode("utf-8")).hexdigest()
                link_citations = self._normalize_legacy_links(metadata.get("lien_citations"))
                link_modifications = self._normalize_legacy_links(metadata.get("lien_modifications"))
                link_concordes = self._normalize_legacy_links(metadata.get("lien_concordes"))
                chunks.append(
                    {
                        "chunk_id": chunk_id,
                        "hash_id": chunk_id,
                        "qa_id": qa_id,
                        "parent_qa_id": None,
                        "source_name": metadata.get("source_name"),
                        "section_path": full_sections_title or section.get("heading_path") or "",
                        "role": section.get("section_type") or "article",
                        "chunk_index": index,
                        "chunk_number": index,
                        "text": body,
                        "chunk_text": chunk_text,
                        "title": document.get("title") or document.get("full_title") or "",
                        "full_title": document.get("full_title") or document.get("title") or "",
                        "number": metadata.get("num_article") or short_id,
                        "category": metadata.get("category") or "",
                        "url": display_url,
                        "cid": article_id,
                        "status": metadata.get("status") or "",
                        "subtitles": metadata.get("full_sections_title") or metadata.get("subtitles") or "",
                        "ministry": metadata.get("ministry") if str(metadata.get("category") or "").upper() == "CODE" else None,
                        "start_date": metadata.get("start_date"),
                        "end_date": self._normalize_open_end_date(metadata.get("end_date")),
                        "nota": metadata.get("nota") or "",
                        "section_parent_cid": metadata.get("section_parent_cid"),
                        "section_parent_titre": metadata.get("section_parent_titre"),
                        "lien_citations": link_citations,
                        "lien_citations_count": len(link_citations) if isinstance(link_citations, list) else 0,
                        "lien_modifications": link_modifications,
                        "lien_modifications_count": len(link_modifications) if isinstance(link_modifications, list) else 0,
                        "lien_concordes": link_concordes,
                        "lien_concordes_count": len(link_concordes) if isinstance(link_concordes, list) else 0,
                        "comporte_liens_sp": bool(metadata.get("comporte_liens_sp") or False),
                        "thematique": thematique,
                        "lang": document.get("lang", "fr"),
                        "references_juridiques": section.get("references_juridiques") or [],
                        "source_document_id": document["doc_id"],
                        "section_id": section.get("section_id"),
                        "short_id": short_id,
                        "source": "LEGIFRANCE",
                        "created_at": created_at,
                        "updated_at": created_at,
                        "_targets": ["legacy"],
                    }
                )

        self._embed_chunks(self.embedding_config, chunks)
        return chunks

    def build_chunks(self, document: dict[str, Any], sections: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if document.get("metadata", {}).get("legacy_qna_source_name"):
            return self._build_legacy_text_chunks(document, sections)
        return self._build_article_chunks(document, sections)

    def persist_bundles(self, repository: GoldRepository, silver_bundles: list[Any]) -> list[GoldBundle]:
        bundles: list[GoldBundle] = []
        for silver_bundle in silver_bundles:
            chunks = self.build_chunks(silver_bundle.document, silver_bundle.sections)
            short_id = silver_bundle.document["short_id"]
            chunks_path = repository.save_chunks_jsonl(short_id, chunks)
            parquet_path = repository.save_parquet(short_id, chunks) if self.gold_config.export_parquet else None
            npy_path = repository.save_npy(short_id, chunks, "embedding_m3") if self.gold_config.export_npy else None
            bundles.append(
                GoldBundle(
                    document=silver_bundle.document,
                    chunks=chunks,
                    chunks_path=chunks_path,
                    parquet_path=parquet_path,
                    npy_path=npy_path,
                )
            )

        repository.save_manifest(
            {
                "run_id": utc_now_iso().replace(":", "").replace(".", ""),
                "document_count": len(bundles),
                "chunk_count": sum(len(bundle.chunks) for bundle in bundles),
                "documents": [bundle.document["short_id"] for bundle in bundles],
            }
        )
        return bundles
