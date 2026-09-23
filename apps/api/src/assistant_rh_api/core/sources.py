"""Final source identities, public links and Markdown presentation."""

import re
from hashlib import sha256
from urllib.parse import urlsplit
from uuid import UUID

from assistant_rh_api.core.models.context import ContextItem
from assistant_rh_api.core.models.conversations import RunSource

SOURCES_MARKER = "\n\n---\n**Sources :**\n"
_ANSWER_URL = re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s<>\"'`]+|(?<![\w:])//[^\s<>\"'`]+", re.IGNORECASE)


def redact_private_urls(text: str) -> str:
    """Apply the source-link allowlist to URLs echoed in generated Markdown/HTML."""

    def redact(match: re.Match[str]) -> str:
        token = match.group()
        # Keep Markdown closers and prose punctuation outside the URL.
        url = token.rstrip(".,;:!?)]}")
        suffix = token[len(url) :]
        # Adjacent Markdown links can form one token; the first hostname must
        # not grant permission to a second URL embedded in that token.
        if public_source_url(url) and not _ANSWER_URL.search(url, url.index("://") + 3):
            return token
        return "[lien privé retiré]" + suffix

    return _ANSWER_URL.sub(redact, text)


def public_source_url(raw_url: str) -> str:
    try:
        url = urlsplit(raw_url)
        public = (
            url.scheme == "https"
            and url.hostname in {"www.service-public.fr", "www.service-public.gouv.fr", "www.legifrance.gouv.fr", "legifrance.gouv.fr"}
            and not (url.username or url.password or url.query or url.fragment)
            and url.port in (None, 443)
        )
    except ValueError:
        return ""
    return raw_url if public else ""


def final_sources(items: tuple[ContextItem, ...]) -> tuple[RunSource, ...]:
    """Only served context grants source authority; internal URLs never escape."""
    sources: list[RunSource] = []
    seen: set[str] = set()
    for item in items:
        metadata = item.metadata
        raw_document_id = str(metadata.get("doc_id") or "")
        try:
            document_id = str(UUID(raw_document_id))
        except ValueError:
            document_id = None
        title = item.document_title or str(metadata.get("full_title") or metadata.get("title") or item.heading or "Document")
        reference = str(document_id or metadata.get("doc_short_id") or raw_document_id or metadata.get("cid") or "")
        if not reference:
            reference = "source-" + sha256(f"{item.publisher}\n{item.section_id}\n{title}\n{item.document_url}".encode()).hexdigest()
        if reference in seen:
            continue
        seen.add(reference)
        public_url = public_source_url(item.document_url or "")
        sources.append(
            RunSource(
                doc_ref=reference,
                title=title,
                url=public_url,
                document_id=document_id,
                publisher=item.publisher or "",
                access="public" if public_url else "authenticated",
            )
        )
    return tuple(sources)


def source_text(value: str) -> str:
    # Metadata remains plain text even if a document title contains Markdown/HTML.
    value = " ".join(value.split()).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    for character in "\\`*_{}[]()#!|":
        value = value.replace(character, "\\" + character)
    return value


def with_sources(answer: str, sources: tuple[RunSource, ...]) -> str:
    answer = redact_private_urls(answer)
    if not sources:
        return answer
    lines = []
    for index, source in enumerate(sources, 1):
        title = source_text(source.title)
        if source.url:
            safe_url = source.url.replace("(", "%28").replace(")", "%29").replace(" ", "%20").replace("<", "%3C").replace(">", "%3E")
            title = f"[{title}]({safe_url})"
        publisher = f" — {source_text(source.publisher)}" if source.publisher else ""
        lines.append(f"{index}. {title}{publisher}")
    return answer + SOURCES_MARKER + "\n".join(lines)
