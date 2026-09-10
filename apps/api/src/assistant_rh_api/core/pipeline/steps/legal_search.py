"""Deterministic legal hints extracted without quality changes."""

import re
import unicodedata
from typing import Any

from assistant_rh_api.core.models.query_processing import Intent

# Word/PDF/Légifrance pastes often carry typographic dashes (U+2010..U+2015,
# U+2212) that NFKD does NOT decompose to ASCII `-`. Normalize them explicitly
# so the article regex doesn't miss `article L‑132-1`.
_DASH_TRANSLATION = str.maketrans(
    {
        "‐": "-",  # hyphen
        "‑": "-",  # non-breaking hyphen
        "‒": "-",  # figure dash
        "–": "-",  # en dash
        "—": "-",  # em dash
        "―": "-",  # horizontal bar
        "−": "-",  # minus sign
    }
)


def _fold(text: str) -> str:
    """Lowercase + dash-normalize + NFKD-decompose + strip combining marks.

    Why: regex patterns target French legal vocabulary. Inputs reach us in mixed
    forms (NFC from browsers, NFD from macOS clipboards, ASCII-only from mobile
    autocorrect). Folding once at matching time lets patterns stay accent-free.
    """
    if not text:
        return ""
    text = text.translate(_DASH_TRANSLATION)
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(c for c in nfkd if not unicodedata.combining(c)).lower()


# Patterns operate on the folded haystack (lowercase, no diacritics, ASCII dashes).
_LEGAL_SEARCH_HINT_PATTERNS = (
    # Canonical Légifrance article citations: "article L. 132-1", "article L 132",
    # "articles R123-4", "article 3-2". The section letter may be glued to a
    # `.`/`-`/digit, OR separated by a space — but the spaced form is restricted
    # to the real code letters [lrd] so the French verb/preposition "a" in
    # "cet article a 5 ans" is not mistaken for a citation. A bare number must be
    # hyphenated ("3-2") or short ("4") — a 4-digit run ("article 2025 du blog")
    # is rejected as a year, not an article.
    re.compile(r"\barticles?\s+(?:[a-z]\.\s*\d|[a-z]\s*-\s*\d|[a-z]\d|[lrd]\s+\d|\d+-\d|\d{1,3}(?!\d))"),
    # Specific legal code names (CGFP, etc.).
    re.compile(r"\b(?:cgfp|code general de la fonction publique|code de la securite sociale|code du travail)\b"),
    # Decree/circular/jurisprudence keywords. After accent folding, the verb
    # `arrête` collapses to the same `arrete` as the noun `arrêté`, so the
    # decree noun is matched two disambiguated ways instead of as a bare word:
    #   (a) followed by a qualifier (`n°`, `du <date>`, ministériel, …), or
    #   (b) preceded by a determiner that cannot precede the finite verb
    #       (`quel/un/cet/des arrêté` is the noun; `il/les arrête` is the verb).
    re.compile(r"\b(?:decret|circulaire|ordonnance|jurisprudence)\b"),
    re.compile(r"\barretes?\s+(?:n[°o]\s*\d|du\s+\d|ministeriel|prefectoral|interministeriel|royal|conjoint)"),
    re.compile(r"\b(?:un|une|cet|cette|quels?|quelles?|du|des|aux|nouvel|nouvelle)\s+arretes?\b"),
    # `loi` is excluded as a bare word (matches idioms like "la loi du plus
    # fort"). The qualifier must include an actual number after `n°`/`no` to
    # avoid `loi nouvelle/normale/notre/nous` collapsing to `loi n…`.
    re.compile(r"\bloi\s+(?:n[°o]\s*\d|du\s+\d|organique|de\s+finances?|de\s+\d)"),
    # Explicit asks for the legal basis.
    re.compile(r"\b(?:fondement juridique|base legale|selon quel texte|c'est ecrit ou|preuve reglementaire)\b"),
)

# RH topic vocabulary. ``is_high_signal=True`` items short-circuit the
# two-hit-required rule so a single match suffices when combined with
# ``_LEGAL_SEARCH_RULE_PATTERN``.
_LEGAL_SEARCH_TOPIC_PATTERNS: tuple = (
    # (pattern, is_high_signal)
    (re.compile(r"\bagents?\s+contractuels?\b"), False),
    (re.compile(r"\bcontrats?\s+de\s+projet\b"), True),
    (re.compile(r"\bemplois?\s+permanents?\b"), True),
    (re.compile(r"\bconges?\s+parent(?:al|aux|ale|ales)\b"), True),
    (re.compile(r"\bsubrog\w*\b"), True),
    (re.compile(r"\bindemnites?\s+journalieres?\b"), False),
    (re.compile(r"\bprestations?\s+en\s+especes\b"), True),
    (re.compile(r"\bpensions?\s+de\s+vieillesse\b"), True),
    (re.compile(r"\bcasiers?\s+judiciaires?\b"), False),
    (re.compile(r"\bservice\s+national\b"), False),
    (re.compile(r"\bdroit\s+au\s+sejour\b"), True),
    (re.compile(r"\bruptures?\s+anticipees?\b"), True),
    (re.compile(r"\brenouvel\w*\b"), True),
)

_LEGAL_SEARCH_RULE_PATTERN = re.compile(
    r"\b(?:(?:dans|sous|a|au|pour)\s+)?"
    r"(?:quels?\s+cas|a\s+partir\s+de\s+quand|quelles?\s+informations?|"
    r"quelles?\s+verifications?|quels?\s+montants?|quels?\s+delais?|quelles?\s+clauses?|"
    r"quelles?\s+conditions?)\b",
)


def should_force_legal_search(
    *,
    query: str,
    processed_query: str,
    intent_data: dict[str, Any],
) -> bool:
    """Apply deterministic guardrails when the LLM under-classifies legal queries.

    ``needs_legal_search`` no longer gates whether DGAFP is retrieved: as of the
    always-on retrieval change, DGAFP is searched whenever it is in the configured
    tables, regardless of this flag. The flag now only feeds logging and
    conformance metadata, but a robust classification still matters there because a
    narrow prompt-only definition under-counts legal RH questions that mention the
    rule directly without explicitly asking for the article or decree. The
    heuristic stays conservative:
    - always preserve explicit LLM ``true``
    - force legal search for obvious legal markers
    - force legal search for legal-ish RH rule questions when at least two
      domain signals are present (or one high-signal topic)
    """
    llm_decision = bool(intent_data.get("needs_legal", False))
    if llm_decision:
        return True

    intent = intent_data.get("intent", Intent.RAG_QUERY)
    if intent not in (Intent.RAG_QUERY, Intent.FOLLOW_UP):
        return False

    # Fold once: accent-strip + lowercase so patterns are NFC/NFD/ASCII agnostic
    # and `re.IGNORECASE` is no longer needed (already lowercase).
    raw = query if processed_query == query else f"{query}\n{processed_query}"
    haystack = _fold(raw)
    if any(pattern.search(haystack) for pattern in _LEGAL_SEARCH_HINT_PATTERNS):
        return True

    if not _LEGAL_SEARCH_RULE_PATTERN.search(haystack):
        return False

    topic_hits = 0
    high_signal_topic = False
    for pattern, is_high in _LEGAL_SEARCH_TOPIC_PATTERNS:
        if pattern.search(haystack):
            topic_hits += 1
            if is_high:
                high_signal_topic = True
                break  # one high-signal topic is enough
    return high_signal_topic or topic_hits >= 2
