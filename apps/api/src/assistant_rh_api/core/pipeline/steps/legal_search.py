"""Deterministic legal hints extracted without quality changes."""

import re
import unicodedata
from typing import Any

from assistant_rh_api.core.models.query_processing import Intent

# NFKD leaves typographic dashes intact; normalize them for citations like L‑132-1.
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
    """Make matching insensitive to case, accents and Unicode dash variants."""
    if not text:
        return ""
    text = text.translate(_DASH_TRANSLATION)
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(c for c in nfkd if not unicodedata.combining(c)).lower()


_LEGAL_SEARCH_HINT_PATTERNS = (
    # Restrict spaced letters to [lrd] to reject "cet article a 5 ans";
    # reject bare four-digit years such as "article 2025 du blog".
    re.compile(r"\barticles?\s+(?:[a-z]\.\s*\d|[a-z]\s*-\s*\d|[a-z]\d|[lrd]\s+\d|\d+-\d|\d{1,3}(?!\d))"),
    re.compile(r"\b(?:cgfp|code general de la fonction publique|code de la securite sociale|code du travail)\b"),
    re.compile(r"\b(?:decret|circulaire|ordonnance|jurisprudence)\b"),
    # After folding, distinguish "arrêté" from "arrête" by qualifier or determiner.
    re.compile(r"\barretes?\s+(?:n[°o]\s*\d|du\s+\d|ministeriel|prefectoral|interministeriel|royal|conjoint)"),
    re.compile(r"\b(?:un|une|cet|cette|quels?|quelles?|du|des|aux|nouvel|nouvelle)\s+arretes?\b"),
    # Qualify "loi" to exclude idioms; require digits after n°/no, not "loi nouvelle".
    re.compile(r"\bloi\s+(?:n[°o]\s*\d|du\s+\d|organique|de\s+finances?|de\s+\d)"),
    re.compile(r"\b(?:fondement juridique|base legale|selon quel texte|c'est ecrit ou|preuve reglementaire)\b"),
)

# (pattern, high_signal): a rule question needs two topics, or one high-signal topic.
_LEGAL_SEARCH_TOPIC_PATTERNS: tuple = (
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
    """Preserve LLM true; otherwise supplement in-scope queries with legal hints.

    This flag feeds diagnostics, not retrieval: configured DGAFP is always searched.
    """
    llm_decision = bool(intent_data.get("needs_legal", False))
    if llm_decision:
        return True

    intent = intent_data.get("intent", Intent.RAG_QUERY)
    if intent not in (Intent.RAG_QUERY, Intent.FOLLOW_UP):
        return False

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
                break
    return high_signal_topic or topic_hits >= 2
