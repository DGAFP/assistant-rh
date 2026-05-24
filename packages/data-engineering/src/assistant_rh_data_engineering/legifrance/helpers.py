from __future__ import annotations

import hashlib
import re
from typing import Any


def normalize_article_number(value: str) -> str:
    return re.sub(r"[.\s]+", "", (value or "").strip())


def slugify(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "_", (value or "").strip()).strip("_")
    return cleaned.lower() or "untitled"


def bounded_identifier(value: str, max_length: int = 64) -> str:
    text = str(value or "").strip()
    if not text:
        return "untitled"
    if len(text) <= max_length:
        return text

    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]
    head = text[: max_length - len(digest) - 1].rstrip("_-")
    return f"{head}_{digest}" if head else digest


def make_short_id(value: str, max_length: int = 64) -> str:
    return bounded_identifier(slugify(value), max_length=max_length)


def normalize_short_id(
    value: str | None,
    fallback: str,
    max_length: int = 64,
) -> str:
    candidate = str(value or "").strip()
    if candidate:
        return bounded_identifier(candidate, max_length=max_length)
    return make_short_id(fallback, max_length=max_length)


def build_legifrance_article_url(article_id: str, category: str | None = None) -> str:
    cleaned_category = str(category or "").upper()
    route = "codes" if cleaned_category == "CODE" else "loda"
    return f"https://www.legifrance.gouv.fr/{route}/article_lc/{article_id}"


def clean_nullable(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        raw = value.strip()
        if not raw or raw.upper() == "NULL":
            return None
        return raw
    return value


def count_links(value: Any) -> int:
    if isinstance(value, list):
        return len(value)
    if value is None:
        return 0
    return 1
