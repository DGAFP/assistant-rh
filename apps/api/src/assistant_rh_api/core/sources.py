"""Final source identities, public links and Markdown presentation."""

import re
from hashlib import sha256
from urllib.parse import urlsplit
from uuid import UUID

from assistant_rh_api.core.models.context import ContextItem
from assistant_rh_api.core.models.conversations import RunSource

SOURCES_MARKER = "\n\n---\n**Sources :**\n"
_ANSWER_URL = re.compile(
    r"(?:(?<![\w+.-])[a-z][a-z0-9+.-]*://|(?<![\w:])//"
    # Bare hosts need a path/query/fragment, so filenames and versions stay text.
    r"|(?<![\w@.-])(?:[a-z0-9-]+\.)+[a-z0-9-]+(?::[0-9]+)?(?=[/?#]))"
    # Stop between adjacent Markdown links without splitting parentheses inside URLs.
    r"(?:(?!\)[.,;:!?]*\[)[^\s<>\"'`])+",
    re.IGNORECASE,
)
_ANSWER_BOUNDARY = re.compile(r"""[\s<>"'`]""")


class AnswerStream:
    """Apply answer policy before delivery, retaining unfinished URLs/source markers.

    The provider byte limit also bounds a token with no delimiter. Such a token
    cannot be released early: a later fragment may turn it into a private URL.
    """

    def __init__(self) -> None:
        self._pending = ""
        self._carriage_return = ""
        self._started = False
        self._stopped = False

    def feed(self, text: str, *, final: bool = False) -> str:
        if self._stopped:
            return ""
        text = self._carriage_return + text
        self._carriage_return = "\r" if not final and text.endswith("\r") else ""
        if self._carriage_return:
            text = text[:-1]
        self._pending += text.replace("\r\n", "\n").replace("\r", "\n")
        leading_marker = SOURCES_MARKER.lstrip("\n")
        marker = self._pending.find(SOURCES_MARKER)
        if not self._started and self._pending.startswith(leading_marker):
            marker = 0
        if marker >= 0:
            self._pending = self._pending[:marker]
            final = True
        if final:
            self._stopped = True
            result = redact_private_urls(self._pending)
            self._pending = ""
            return result
        if not self._started and leading_marker.startswith(self._pending):
            return ""
        limit = len(self._pending)
        for size in range(min(limit, len(SOURCES_MARKER) - 1), 0, -1):
            if self._pending.endswith(SOURCES_MARKER[:size]):
                limit -= size
                break
        # URL matching cannot cross these delimiters. Keep the last unfinished
        # token and any reserved marker prefix until their meaning is known.
        boundary = 0
        for match in _ANSWER_BOUNDARY.finditer(self._pending, 0, limit):
            boundary = match.end()
        ready, self._pending = self._pending[:boundary], self._pending[boundary:]
        self._started = self._started or bool(ready)
        return redact_private_urls(ready)


def redact_private_urls(text: str, *, replacement: str = "[lien privé retiré]") -> str:
    """Apply the source-link allowlist to URLs echoed in generated Markdown/HTML."""

    def redact(match: re.Match[str]) -> str:
        token = match.group()
        # Keep Markdown closers and prose punctuation outside the URL.
        url = token.rstrip(".,;:!?)]}")
        suffix = token[len(url) :]
        # An allowed hostname must not authorize another URL embedded in its path.
        if public_source_url(url) and not _ANSWER_URL.search(urlsplit(url).path):
            return token
        return replacement + suffix

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
                title=redact_private_urls(title),
                url=public_url,
                document_id=document_id,
                publisher=redact_private_urls(item.publisher or ""),
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
    # This reserved block is owned by served context, never by generated text.
    answer = answer.replace("\r\n", "\n").replace("\r", "\n")
    # The provider gateway strips leading whitespace from completions.
    if answer.startswith(SOURCES_MARKER.lstrip("\n")):
        answer = ""
    answer = redact_private_urls(answer.split(SOURCES_MARKER, 1)[0])
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
