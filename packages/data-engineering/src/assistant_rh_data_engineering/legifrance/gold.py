from __future__ import annotations

import hashlib
import re
from typing import Any

from ..utils.gold import GoldBundle, GoldRepository, build_embedders
from ..utils.helpers import utc_now_iso
from .config import EmbeddingConfig, GoldConfig

_LEGACY_PAGE_MARKER_RE = re.compile(r"^\[PAGE\s+\d+\]$", re.IGNORECASE)
_LEGACY_PAGE_COUNT_RE = re.compile(
    r"^\d+\s+sur\s+\d+(?:\s+\d{1,2}/\d{1,2}/\d{4},\s*\d{1,2}:\d{2})?$",
    re.IGNORECASE,
)
_LEGACY_LEGIFRANCE_URL_RE = re.compile(r"https?://www\.legifrance\.gouv\.fr/\S+", re.IGNORECASE)
_LEGAL_ARTICLE_HEADING_RE = re.compile(
    r"^(Article|Art\.)\s+"
    r"(?P<number>"
    r"(?:[A-Z]{1,4}\.?\s*)?\d+(?:[-‑–]\d+)*(?:\s*(?:bis|ter|quater|quinquies|sexies|septies|octies|nonies|decies))?"
    r"|1er"
    r")"
    r"(?:\s*\([^)]{1,80}\))?"
    r"\s*$",
    re.IGNORECASE,
)
_LEGAL_ARTICLE_NOTICE_RE = re.compile(
    r"^(?:"
    r"Modifi(?:é|ée)s?\s+par|"
    r"Abrog(?:é|ée)s?\s+par|"
    r"Cré(?:é|ée)s?\s+par|"
    r"Création\s+|"
    r"Rétabli(?:e)?\s+par|"
    r"Transféré(?:e)?\s+par|"
    r"Version\s+en\s+vigueur|"
    r"NOTA\s*:)"
    r".*",
    re.IGNORECASE,
)
# A structural heading is a kind keyword followed by a numbering token (roman/arabic/ordinal),
# a bare keyword on its own line (e.g. "Annexe"), or a colon-introduced title. The numbering must
# end the line or be followed by a separator, otherwise ordinary sentences that merely start with a
# structural word ("Section syndicale dans l'entreprise…", "Chapitre premier du présent règlement…")
# would be misread as headings and their text dropped. The roman class stays uppercase-only so that
# lowercase words containing roman letters ("Livre ouvert…", "Chapitre divers…") cannot match.
_LEGAL_CONTEXT_HEADING_RE = re.compile(
    r"^(?:#{1,6}\s*)?"
    r"(?i:(?P<kind>Livre|Titre|Chapitre|Sous-section|Section|Paragraphe|Annexe))"
    r"(?:"
    r"\s+(?:[IVXLCDM]+(?i:er|re|e|ème|nd|nde)?|\d+(?i:er|re|e|ème|nd|nde)?|(?i:premier|première|unique|préliminaire|liminaire))"
    r"(?:\s+(?i:bis|ter|quater|quinquies|sexies|septies|octies|nonies|decies))?"
    r"(?=\s*$|\s*[:.)\-–—])"
    r"|\s*:"
    r"|\s*$"
    r")",
)
_LEGAL_CONTEXT_LEVELS = {
    "livre": 1,
    "titre": 2,
    "chapitre": 3,
    "section": 4,
    "sous-section": 5,
    "paragraphe": 6,
    "annexe": 3,
}


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


def _normalize_legacy_heading(line: str) -> str:
    return re.sub(r"^#{1,6}\s+", "", line).strip()


def _is_legacy_export_residue(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    if _LEGACY_PAGE_MARKER_RE.match(stripped):
        return True
    if _LEGACY_PAGE_COUNT_RE.match(stripped):
        return True
    if stripped.lower() == "légifrance":
        return True
    return bool(_LEGACY_LEGIFRANCE_URL_RE.search(stripped))


def clean_legacy_legal_text(text: str) -> str:
    lines: list[str] = []
    for raw_line in (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw_line.strip()
        if _is_legacy_export_residue(line):
            continue
        lines.append(line)
    cleaned = "\n".join(lines).strip()
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned


def _legal_context_level(line: str) -> int | None:
    match = _LEGAL_CONTEXT_HEADING_RE.match(line)
    if not match:
        return None
    return _LEGAL_CONTEXT_LEVELS.get(match.group("kind").lower())


def _build_legacy_section_path(source_title: str, context: list[tuple[int, str]], heading: str | None) -> str:
    parts = [source_title.strip()]
    parts.extend(title for _, title in context if title)
    if heading:
        parts.append(heading)
    return " > ".join(dict.fromkeys(part for part in parts if part))


def _take_trailing_article_notices(lines: list[str]) -> list[str]:
    notices_reversed: list[str] = []
    while lines:
        line = lines[-1]
        if not line:
            if notices_reversed:
                notices_reversed.append(lines.pop())
                continue
            break
        if _LEGAL_ARTICLE_NOTICE_RE.match(line):
            notices_reversed.append(lines.pop())
            continue
        break
    return list(reversed(notices_reversed))


def _split_legacy_legal_blocks(text: str, source_title: str) -> list[dict[str, Any]]:
    context: list[tuple[int, str]] = []
    pending_lines: list[str] = []
    current_heading: str | None = None
    current_lines: list[str] = []
    blocks: list[dict[str, Any]] = []

    def flush_current() -> None:
        nonlocal current_heading, current_lines
        block_text = "\n".join(line for line in current_lines if line is not None).strip()
        if block_text:
            blocks.append(
                {
                    "heading": current_heading,
                    "section_path": _build_legacy_section_path(source_title, context, current_heading),
                    "text": block_text,
                }
            )
        current_heading = None
        current_lines = []

    def flush_pending() -> None:
        nonlocal pending_lines
        pending_text = "\n".join(line for line in pending_lines if line is not None).strip()
        if pending_text:
            blocks.append(
                {
                    "heading": None,
                    "section_path": _build_legacy_section_path(source_title, context, None),
                    "text": pending_text,
                }
            )
        pending_lines = []

    for raw_line in text.split("\n"):
        line = raw_line.strip()
        if not line:
            if current_lines and current_lines[-1] != "":
                current_lines.append("")
            elif pending_lines and pending_lines[-1] != "":
                pending_lines.append("")
            continue

        article_match = _LEGAL_ARTICLE_HEADING_RE.match(line)
        if article_match:
            flush_current()
            article_notices = _take_trailing_article_notices(pending_lines)
            flush_pending()
            current_heading = _normalize_legacy_heading(line)
            current_lines = [current_heading, *article_notices]
            continue

        context_level = _legal_context_level(line)
        if context_level is not None:
            flush_current()
            # Body seen before this heading belongs to the context it appeared under, not the next
            # one — emit it as a LEGAL_TEXT block instead of discarding it.
            flush_pending()
            heading = _normalize_legacy_heading(line)
            context = [(level, title) for level, title in context if level < context_level]
            context.append((context_level, heading))
            continue

        if current_heading is None:
            pending_lines.append(line)
        else:
            current_lines.append(line)

    flush_current()
    flush_pending()
    return blocks


def chunk_legacy_legal_text(
    text: str,
    source_name: str,
    thematique: str = "",
    max_chars: int = 1200,
    min_chars: int = 350,
) -> list[dict[str, Any]]:
    source_title = source_name.replace(" - Légifrance.txt", "").replace(" - Légifrance.txt", "").replace(".txt", "")
    cleaned = clean_legacy_legal_text(text)
    blocks = _split_legacy_legal_blocks(cleaned, source_title or source_name)
    rows: list[dict[str, Any]] = []
    for block in blocks:
        heading = block.get("heading")
        role = "LEGAL_ARTICLE" if heading else "LEGAL_TEXT"
        qa_id = hashlib.sha1(f"{source_name}|{block['section_path']}|{heading or ''}".encode("utf-8")).hexdigest()
        for part_index, part in enumerate(split_legal_chunks(block["text"], max_chars=max_chars, min_chars=min_chars)):
            chunk_text = part.strip()
            if heading and not chunk_text.startswith(str(heading)):
                contextualized = f"{heading}\n\n{chunk_text}"
                if len(contextualized) <= max_chars:
                    chunk_text = contextualized
            rows.append(
                {
                    "qa_id": qa_id,
                    "parent_qa_id": qa_id if part_index else None,
                    "role": role,
                    "section_path": block["section_path"],
                    "chunk_index": len(rows),
                    "text": chunk_text,
                    "source_name": source_name,
                    "lang": "fr",
                    "thematique": thematique,
                }
            )
    return rows


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
        article_id = link.get("id") if str(link.get("id") or "").startswith("LEGIARTI") else None
        return {
            "textCid": link.get("cidtexte"),
            "linkType": link.get("typelien"),
            "numTexte": link.get("numtexte"),
            "articleId": article_id,
            "dateDebut": None,
            "datePubli": None,
            "parentCid": None,
            "textTitle": link.get("label"),
            "articleNum": link.get("num"),
            "natureText": link.get("naturetexte"),
            "linkOrientation": link.get("sens"),
        }

    @classmethod
    def _normalize_legacy_links(cls, value: Any) -> list[dict[str, Any]] | None:
        if not isinstance(value, list):
            return None
        normalized = [
            cls._normalize_legacy_link_item(link)
            for link in value
            if isinstance(link, dict) and str(link.get("typelien") or "").upper() != "TXT_SOURCE"
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

        raw_chunks = chunk_legacy_legal_text(
            raw_text,
            source_name=source_name,
            thematique=thematique,
            max_chars=self.gold_config.max_chunk_chars,
            min_chars=self.gold_config.min_chunk_chars,
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
                        "url": document.get("source_url") or "",
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
