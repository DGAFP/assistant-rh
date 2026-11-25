"""Pure prompt and trace projections; no adapter or generator dependencies."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any, Dict, List, Optional

from assistant_rh_api.core.models.context import AggregatedSection, ContextItem
from assistant_rh_api.core.models.retrieval import RetrievedChunk


def standalone_legal_label(metadata: Mapping[str, Any]) -> str:
    """Build a canonical article label for standalone legal chunks (DGAFP / Légifrance)."""
    raw_title = str(metadata.get("full_title") or metadata.get("title") or "").strip()
    raw_number = re.sub(r"^Article\b\s*", "", str(metadata.get("number") or "").strip(), flags=re.IGNORECASE).strip()
    if raw_number:
        legal_title = re.sub(
            r"^(?:Article\s+)?" + re.escape(raw_number) + r"(?!\w)(?:\s*[—–-]\s*|\s*$)",
            "",
            raw_title,
            flags=re.IGNORECASE,
        ).strip()
        return f"Article {raw_number} - {legal_title}" if legal_title else f"Article {raw_number}"
    return raw_title


def section_context_label(doc_title: str | None, heading: str | None, heading_path: str | None = None) -> str:
    """Combine document title and section breadcrumb without redundant prefixes."""
    title = (doc_title or "").strip()
    leaf = (heading or "").strip()
    path = (heading_path or "").strip()
    if path:
        section_label = f"{path} > {leaf}" if leaf and leaf.lower() not in path.lower() else path
    else:
        section_label = leaf
    if title and section_label:
        if section_label.lower() == title.lower():
            return title
        if section_label.lower().startswith(title.lower() + " >"):
            return section_label
        return f"{title} — {section_label}"
    return title or section_label


def format_for_prompt(items: Sequence[ContextItem]) -> str:
    parts = []
    for i, item in enumerate(items, 1):
        heading_path = str(item.metadata.get("heading_path") or "") if item.metadata else ""
        label = section_context_label(item.document_title, item.heading, heading_path or None)
        header = f"[Source {i}] {label}"
        if item.publisher:
            header += f" ({item.publisher})"
        parts.append(f"### {header}\n\n```markdown\n{item.content}\n```\n\n---\n")
    return "\n".join(parts)


CHUNK_LOG_MARKDOWN_PREVIEW = 300


def _first_metadata_value(*sources: Mapping[str, Any], keys: tuple[str, ...]) -> Any:
    for source in sources:
        if not source:
            continue
        for key in keys:
            value = source.get(key)
            if value not in (None, ""):
                return value
    return ""


def _round_score(score: Any) -> float | None:
    if score is None:
        return None
    return round(float(score), 6)


_DOC_ID_METADATA_KEYS = ("doc_id", "doc_short_id", "document_id", "source_document_id", "short_id", "cid")


def _chunk_log_dict(
    chunk: "RetrievedChunk",
    *,
    section: "Optional[AggregatedSection]" = None,
    rerank_score: Optional[float] = None,
) -> Dict[str, Any]:
    """Serialize one chunk to the 9-key shape used by chat_runs trace columns."""
    meta = chunk.metadata if isinstance(chunk.metadata, Mapping) else {}
    section_meta = section.metadata if section is not None and isinstance(section.metadata, Mapping) else {}

    if section is not None:
        doc_id = section.document_id or _first_metadata_value(
            section_meta,
            meta,
            keys=_DOC_ID_METADATA_KEYS,
        )
        doc_title = _first_metadata_value(
            section_meta,
            meta,
            keys=("doc_title", "source_name", "full_title", "title", "number"),
        )
        doc_publisher = section.publisher or _first_metadata_value(section_meta, meta, keys=("doc_publisher", "publisher", "source"))
        section_heading = (
            section.heading
            or section.heading_path
            or _first_metadata_value(
                section_meta,
                meta,
                keys=(
                    "heading",
                    "section_heading",
                    "matched_heading",
                    "matched_heading_path",
                    "heading_path",
                    "section_path",
                    "doc_title",
                    "full_title",
                    "title",
                    "number",
                ),
            )
        )
    else:
        doc_id = _first_metadata_value(meta, keys=("doc_id", "doc_short_id", "document_id", "source_document_id", "short_id", "cid"))
        doc_title = _first_metadata_value(meta, keys=("doc_title", "source_name", "full_title", "title", "number"))
        doc_publisher = _first_metadata_value(meta, keys=("doc_publisher", "publisher", "source")) or chunk.table_source
        section_heading = _first_metadata_value(
            meta,
            keys=(
                "heading",
                "section_heading",
                "matched_heading",
                "matched_heading_path",
                "heading_path",
                "section_path",
                "doc_title",
                "source_name",
                "full_title",
                "title",
                "number",
            ),
        )

    return {
        "doc_id": str(doc_id) if doc_id else "",
        "chunk_id": str(chunk.chunk_id),
        "doc_title": str(doc_title) if doc_title else "",
        "section_id": str(chunk.section_id) if chunk.section_id else "",
        "final_score": _round_score(chunk.score),
        "rerank_score": _round_score(rerank_score),
        "doc_publisher": str(doc_publisher or chunk.table_source or ""),
        "chunk_markdown": (chunk.text or "")[:CHUNK_LOG_MARKDOWN_PREVIEW],
        "section_heading": str(section_heading) if section_heading else "",
    }


def serialize_section_chunks(
    sections: Sequence[AggregatedSection],
    *,
    include_rerank_score: bool = False,
) -> List[Dict[str, Any]]:
    """Flatten chunks grouped under aggregated sections, preserving section order."""
    out: List[Dict[str, Any]] = []
    for section in sections:
        rerank_score = section.score if include_rerank_score else None
        for chunk in section.chunks:
            out.append(_chunk_log_dict(chunk, section=section, rerank_score=rerank_score))
    return out
