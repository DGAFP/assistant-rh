"""
Chatbot Sources – Source pills rendering & citation matching helpers.

Extracted from ``01_Chatbot.py`` to keep that page focused on orchestration.
"""

import json
import re
from datetime import datetime
from typing import TYPE_CHECKING, Dict, List, Optional

import streamlit as st
from assistant_rh_rag_pipeline.citation_extractor import match_refs_with_response_v3

from src.ui.document_url_helper import get_document_url

if TYPE_CHECKING:
    from assistant_rh_rag_pipeline.models import Chunk, ContextItem


# ═══════════════════════════════════════════════════════════════════════════════
# RESPONSE CLASSIFICATION
# ═══════════════════════════════════════════════════════════════════════════════


def is_negative_response(answer: str) -> bool:
    """Return True when the LLM response doesn't warrant sources/feedback."""
    if not answer:
        return False
    return False


def should_hide_sources(answer: str) -> bool:
    """Return True when sources should be hidden (clear negative answers)."""
    if not answer:
        return True

    answer_lower = answer.lower().strip()

    negative_starts = [
        "je n'ai pas trouvé la réponse à cette question",
        "je n'ai pas trouvé d'information",
        "je ne comprends pas la question",
        "pourriez-vous reformuler",
        "je ne dispose pas d'information",
    ]
    for pattern in negative_starts:
        if answer_lower.startswith(pattern):
            return True

    if len(answer) < 150:
        short_negative_patterns = [
            "je n'ai pas trouvé",
            "aucune information disponible",
            "je ne peux pas répondre",
        ]
        for pattern in short_negative_patterns:
            if pattern in answer_lower:
                return True

    return False


# ═══════════════════════════════════════════════════════════════════════════════
# DATE FORMATTING
# ═══════════════════════════════════════════════════════════════════════════════


def format_source_date(date_str: str) -> str:
    """Format a date as MM/YY for discrete pill display. Returns '' on failure."""
    if not date_str:
        return ""
    try:
        date_str = str(date_str).strip()
        formats_to_try = [
            "%Y-%m-%d",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d %H:%M:%S",
            "%d/%m/%Y",
            "%d-%m-%Y",
            "%Y%m%d",
        ]
        parsed_date = None
        for fmt in formats_to_try:
            try:
                test_str = date_str[: len("2024-03-15T00:00:00")]
                expected_len = len(
                    fmt.replace("%", "")
                    .replace("Y", "YYYY")
                    .replace("m", "MM")
                    .replace("d", "DD")
                    .replace("H", "HH")
                    .replace("M", "MM")
                    .replace("S", "SS")
                )
                parsed_date = datetime.strptime(test_str[:expected_len], fmt)
                break
            except ValueError:
                continue

        if not parsed_date:
            m = re.search(r"(\d{4})-(\d{2})", date_str)
            if m:
                return f"{m.group(2)}/{m.group(1)[2:]}"
            m = re.search(r"(\d{2})/(\d{4})", date_str)
            if m:
                return f"{m.group(1)}/{m.group(2)[2:]}"

        return parsed_date.strftime("%m/%y") if parsed_date else ""
    except Exception:
        return ""


# ═══════════════════════════════════════════════════════════════════════════════
# V3 CONTEXT → V1 CHUNKS (for render_sources compatibility)
# ═══════════════════════════════════════════════════════════════════════════════


def context_items_to_v1_chunks(
    context_items: List["ContextItem"],
    Chunk: type,
) -> List["Chunk"]:
    """Convert V3-clean ``ContextItem`` objects to legacy ``Chunk`` for pill rendering."""
    from assistant_rh_rag_pipeline.models import (
        Chunk as _Chunk,  # noqa: F401  # avoid circular at module level
    )

    v1_chunks: List[_Chunk] = []
    for item in context_items:
        publisher_lower = (item.publisher or "").lower()
        is_sp = "service" in publisher_lower and "public" in publisher_lower

        meta = item.metadata or {}
        v1_meta: Dict = {
            "source_name": item.document_title or item.heading or "",
            "url": item.document_url or meta.get("doc_url") or "",
            "source": item.publisher or "",
            "table_source": "v3_clean",
            "section_heading": item.heading or "",
            "title": item.document_title or meta.get("full_title") or "",
            "full_title": meta.get("full_title", ""),
            "source_document_id": meta.get("doc_id") or "",
            "is_rag_doc": True,
            "date_derniere_modif": meta.get("doc_date", ""),
            "number": meta.get("number", ""),
            "cid": meta.get("cid", ""),
        }
        if is_sp:
            v1_meta["sid"] = meta.get("doc_short_id") or item.section_id or "sp"

        v1_chunks.append(
            Chunk(
                id=item.section_id or item.heading or "",
                text=item.content[:500] if item.content else "",
                score=item.score,
                metadata=v1_meta,
            )
        )
    return v1_chunks


# ═══════════════════════════════════════════════════════════════════════════════
# CITATION EXTRACTION HELPER
# ═══════════════════════════════════════════════════════════════════════════════


def extract_legal_refs_for_display(
    response: str,
    context_items: List["ContextItem"],
) -> List[Dict]:
    """
    Collect ``references_juridiques`` from context items, match them against
    article/decree mentions in *response*, and return display-ready dicts.
    """
    all_refs: List[Dict] = []
    for item in context_items:
        if not item.references_juridiques:
            continue
        refs = item.references_juridiques
        if isinstance(refs, str):
            try:
                refs = json.loads(refs)
            except Exception:
                refs = []
        if isinstance(refs, list):
            all_refs.extend(refs)
        elif isinstance(refs, dict):
            all_refs.append(refs)

    matched = match_refs_with_response_v3(response, all_refs)
    return [
        {
            "cid": r.cid,
            "number": r.number,
            "title": r.title,
            "url": r.url,
            "is_decree": r.is_decree,
            "display_title": r.display_title,
        }
        for r in matched
    ], matched


# ═══════════════════════════════════════════════════════════════════════════════
# RENDER SOURCES (pills)
# ═══════════════════════════════════════════════════════════════════════════════

_SOURCE_PILLS_CSS = """
<style>
.sources-container {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    margin: 12px 0;
}
.source-pill {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 6px 16px;
    border-radius: 20px;
    font-size: 0.875rem;
    font-weight: 400;
    text-decoration: none !important;
    transition: all 0.2s ease;
    cursor: pointer;
    border: 1px solid;
    background-color: rgb(240, 242, 246);
    border-color: rgb(240, 242, 246);
    color: rgb(49, 51, 63) !important;
}
.source-pill:hover {
    background-color: rgb(230, 234, 241);
    border-color: rgb(220, 224, 231);
    text-decoration: none !important;
    color: rgb(49, 51, 63) !important;
}
.source-pill:visited { color: rgb(49, 51, 63) !important; text-decoration: none !important; }
.source-pill:active  { color: rgb(49, 51, 63) !important; text-decoration: none !important; }
.source-pill:focus   { outline: none; color: rgb(49, 51, 63) !important; text-decoration: none !important; }
.source-pill-no-link { cursor: default; opacity: 0.7; }
.source-pill-no-link:hover { background-color: rgb(240, 242, 246); border-color: rgb(240, 242, 246); }
.source-icon { font-family: 'Material Symbols Outlined'; font-size: 1.1em; line-height: 1; display: inline-flex; align-items: center; }
.source-date { color: #888; font-size: 0.75em; margin-left: 4px; opacity: 0.8; }
</style>
"""


def render_sources(
    chunks: List["Chunk"],
    key_suffix: str = "",
    legal_refs: Optional[List] = None,
) -> None:
    """Render unique source pills from chunks + optional legal references."""
    if not chunks and not legal_refs:
        return

    sources: List[Dict] = []
    seen_sids: set = set()
    seen_dgafp_numbers: set = set()
    seen_matte_doc_ids: set = set()

    for chunk in chunks:
        meta = chunk.metadata or {}
        cid = meta.get("cid", "")
        number = meta.get("number", "")
        sid = meta.get("sid", "")
        source_document_id = meta.get("source_document_id", "")
        source_name = meta.get("source_name", "").lower()
        url = meta.get("url") or ""

        if number and number in seen_dgafp_numbers:
            continue
        if sid and sid in seen_sids:
            continue
        if source_document_id and source_document_id in seen_matte_doc_ids:
            continue

        is_service_public = sid or "service-public" in url or "service-public" in source_name or (number and not cid and "service" in source_name)
        is_internal_pdf = False

        if source_document_id and not url:
            is_rag_doc = meta.get("is_rag_doc", False)
            url = get_document_url(source_document_id, relative=True, pdf_only=True, is_rag_doc=is_rag_doc)
            is_internal_pdf = True

        if cid and not url:
            url = f"https://www.legifrance.gouv.fr/codes/id/{cid}/"

        # ------ Title ------ #
        if cid:
            article_number = meta.get("number", "")
            title = (meta.get("full_title") or meta.get("title") or "").strip()
            if not title and article_number:
                title = f"{meta.get('nature', 'Article')} {article_number}"
            if not title:
                title = "Code Général FP"
            if article_number and article_number not in title:
                title = f"Art. {article_number} - {title}"
        elif is_service_public:
            base_title = (meta.get("title") or "").strip()
            if not base_title:
                base_title = f"Fiche {sid or number or 'Service Public'}"
            title = f"Service Public: {base_title}"
        else:
            title = (meta.get("source_name") or meta.get("source") or "").strip()
            if not title:
                title = chunk.preview(30) if hasattr(chunk, "preview") else "Source"
            if not title or title == "…":
                if url:
                    url_match = re.search(r"/([A-Z]\d+)$", url)
                    if url_match:
                        title = f"Fiche {url_match.group(1)}"
                    elif "legifrance" in url:
                        title = "Légifrance"
                    else:
                        title = "Source externe"
                else:
                    title = "Source"

        if len(title) > 40:
            title = title[:37] + "..."

        # ------ Date ------ #
        source_date = ""
        if is_internal_pdf:
            source_date = "07/24"
        elif is_service_public:
            raw_date = meta.get("date_derniere_modif", "") or meta.get("date_modification", "")
            source_date = format_source_date(raw_date)

        if url:
            sources.append({"title": title, "url": url, "is_internal_pdf": is_internal_pdf, "date": source_date})
            if sid:
                seen_sids.add(sid)
            if number:
                seen_dgafp_numbers.add(number)
            if source_document_id:
                seen_matte_doc_ids.add(source_document_id)

    # ------ Legal references from citation extraction ------ #
    if legal_refs:
        seen_legal_cids: set = set()
        for ref in legal_refs:
            if isinstance(ref, dict):
                cid = ref.get("cid", "") or ref.get("reference", "")
                number = ref.get("number", "")
                title = ref.get("title", "Code général de la fonction publique")
                is_decree = ref.get("is_decree", False)
                if ref.get("display_title"):
                    display_title = ref["display_title"]
                elif is_decree:
                    display_title = title
                elif number:
                    t_short = title[:30] + "..." if len(title) > 30 else title
                    display_title = f"Art. {number} - {t_short}"
                else:
                    display_title = title or ref.get("article", "Référence juridique")
                url = ref.get("url", "")
                if not url and cid and not cid.startswith("DECREE-"):
                    url = f"https://www.legifrance.gouv.fr/codes/id/{cid}/"
            else:
                cid = ref.cid
                display_title = ref.display_title
                url = ref.url
                is_decree = getattr(ref, "is_decree", False)

            if cid and cid not in seen_legal_cids:
                seen_legal_cids.add(cid)
                sources.append(
                    {
                        "title": display_title,
                        "url": url,
                        "is_internal_pdf": False,
                        "is_legal_ref": True,
                        "is_decree": is_decree,
                    }
                )

    if not sources:
        return

    # ------ HTML rendering ------ #
    st.markdown(_SOURCE_PILLS_CSS, unsafe_allow_html=True)

    pills_html = '<div class="sources-container">'
    for source in sources:
        if source.get("is_decree"):
            icon = "gavel"
        elif source.get("is_legal_ref"):
            icon = "balance"
        elif source.get("is_internal_pdf"):
            icon = "import_contacts"
        else:
            icon = "globe"

        date_html = f'<span class="source-date">({source.get("date", "")})</span>' if source.get("date") else ""
        pills_html += (
            f'<a href="{source["url"]}" target="_blank" rel="noopener noreferrer" '
            f'class="source-pill" title="Cliquer pour ouvrir la source">'
            f'<span class="source-icon material-symbols-outlined">{icon}</span>'
            f"<span>{source['title']}</span>{date_html}</a>"
        )
    pills_html += "</div>"
    st.markdown(pills_html, unsafe_allow_html=True)


# ═══════════════════════════════════════════════════════════════════════════════
# UTILITY
# ═══════════════════════════════════════════════════════════════════════════════


def detect_source_type(chunk: "Chunk") -> str:
    """Detect the origin type of a chunk for badges."""
    meta = chunk.metadata or {}
    publisher = str(meta.get("source") or meta.get("publisher") or "").lower()
    if publisher == "mso":
        return "MSO"
    if publisher == "matte":
        return "MATTE"
    if meta.get("cid"):
        return "DGAFP"
    if meta.get("sid") or "service-public" in str(meta.get("url", "")).lower():
        return "Service Public"
    if meta.get("source_document_id"):
        return "MATTE"
    if "legifrance" in str(meta.get("url", "")).lower():
        return "Légifrance"
    return "Autre"


def serialize_retrieved(chunks: List["Chunk"]) -> str:
    """Serialize chunks to compact JSON for CSV/DB storage."""
    mini = [
        {
            "id": c.id,
            "score": round(c.score, 4),
            "source": c.metadata.get("source") if c.metadata else None,
            "source_name": c.metadata.get("source_name") if c.metadata else None,
            "doc_title": c.metadata.get("doc_title") if c.metadata else None,
            "page": c.metadata.get("page") if c.metadata else None,
            "url": c.metadata.get("url") if c.metadata else None,
        }
        for c in chunks
    ]
    return json.dumps(mini, ensure_ascii=False)
