"""
Query pre-processing for the RAG V3 Clean pipeline.

Performs a **single LLM call** that handles in one shot:
  1. Intent classification (rag_query / chit_chat / out_of_scope / follow_up …)
  2. HR theme detection
  3. Query reformulation (for follow-up questions or acronym expansion)
  4. Legal-search flag

The unified prompt lives in ``prompts/intent.md`` (or DB *system_prompts*).

Dependencies (internal only):
  - config.get_acronym_dict, config.get_prompt_content, config.QueryProcessorConfig
  - llm_client.LLMClient
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from .config import QueryProcessorConfig
from .db_helpers import get_acronym_dict, load_prompt
from .llm_client import LLMClient
from .ministry_scope import MinistrySource, render_ministry_prompt

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Intent enum and direct responses
# ─────────────────────────────────────────────────────────────────────────────


class Intent(str, Enum):
    RAG_QUERY = "rag_query"
    CHIT_CHAT = "chit_chat"
    OUT_OF_SCOPE = "out_of_scope"
    CLARIFICATION = "clarification"
    FOLLOW_UP = "follow_up"
    DOCUMENT_REQUEST = "document_request"


_DIRECT_RESPONSES = {
    Intent.CHIT_CHAT: (
        "Bonjour, je suis l'Assistant RH specialise sur les questions liees "
        "aux contractuels de la fonction publique d'Etat (FPE). Comment puis-je vous aider ?"
    ),
    Intent.OUT_OF_SCOPE: (
        "Je suis specialise sur les questions liees aux contractuels de la fonction publique d'Etat (FPE). "
        "Puis-je vous aider sur un sujet RH (contrats, conges, remuneration, fin de contrat...) ?"
    ),
    Intent.CLARIFICATION: (
        "Je n'ai pas bien compris votre question. Pourriez-vous la preciser ? "
        "Par exemple : sur quel type de contrat, de conge, ou de situation vous souhaitez des informations ?"
    ),
    Intent.DOCUMENT_REQUEST: (
        "Je ne suis pas en mesure de vous donner directement acces aux documents. "
        "Posez-moi plutot une question RH et je pourrai vous guider vers les bonnes sources."
    ),
}

AVAILABLE_THEMES = [
    "recrutement",
    "typologie_contrats",
    "remuneration",
    "renouvellement_mobilite",
    "fin_contrat_licenciement",
    "temps_de_travail",
    "conges",
    "formation",
    "action_sociale",
    "psc",
    "sante_securite",
    "retraite",
    "apprentis",
    "deontologie",
    "autre",
]

BETA_EXCLUDED_THEMES = {"action_sociale", "psc", "retraite", "apprentis"}


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


# ─────────────────────────────────────────────────────────────────────────────
# Result dataclass
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class QueryProcessResult:
    original_query: str
    processed_query: str
    enriched_query: str = ""

    expanded_acronyms: List[str] = field(default_factory=list)
    detected_acronyms: Dict[str, str] = field(default_factory=dict)
    was_expanded: bool = False

    is_in_scope: bool = True
    intent: Intent = Intent.RAG_QUERY
    intent_confidence: float = 1.0
    intent_reason: Optional[str] = None
    needs_legal_search: bool = False
    # Preserved LLM-only value (None when classify failed or gating was off).
    # Lets observability/conformance compare the LLM against the post-heuristic
    # decision instead of seeing only the merged flag.
    needs_legal_search_llm: Optional[bool] = None

    theme: Optional[str] = None
    was_enriched: bool = False
    direct_response: Optional[str] = None

    # observability (kept lightweight)
    intent_raw_response: Optional[str] = None

    @property
    def should_proceed(self) -> bool:
        return self.is_in_scope

    @property
    def query_for_retrieval(self) -> str:
        return self.enriched_query or self.processed_query


# ─────────────────────────────────────────────────────────────────────────────
# Processor
# ─────────────────────────────────────────────────────────────────────────────


class QueryProcessor:
    """
    Pre-process a user query before retrieval.

    Steps:
      1. Detect known acronyms (case-sensitive, uppercase only).
      2. Single LLM call → intent + theme + reformulation + acronym validation.
      3. Return a ``QueryProcessResult``.
    """

    def __init__(self, config: QueryProcessorConfig, verbose: bool = False):
        self.config = config
        self.verbose = verbose
        self._acronyms: Dict[str, str] = {}
        if config.enable_acronym_expansion:
            self._acronyms = get_acronym_dict()
            if verbose:
                logger.info("Loaded %d acronyms from DB", len(self._acronyms))

    def process(
        self,
        query: str,
        conversation_history: Optional[List[Dict[str, str]]] = None,
        ministry: MinistrySource | None = None,
    ) -> QueryProcessResult:
        # Canonicalize the user input at the API boundary so the LLM, retriever,
        # and replay cache all see the same byte sequence regardless of whether
        # the client sent NFC (Chrome) or NFD (macOS clipboard).
        if query:
            query = unicodedata.normalize("NFC", query)
        processed = query
        detected = self._detect_acronyms(query)

        # --- intent gating (single LLM call) --------------------------------
        if self.config.enable_intent_gating:
            intent_data = self._classify(query, conversation_history, detected, ministry)
        else:
            intent_data = self._fallback_expand(query, detected)

        llm_needs_legal: Optional[bool] = intent_data.get("needs_legal")
        # Heuristic only runs when intent gating is on AND classify succeeded.
        # ``classify_ok`` flag is set on the success path; absent on the
        # exception fallback so we preserve the deterministic-False safe default
        # on LLM outage.
        heuristic_eligible = self.config.enable_intent_gating and intent_data.get("classify_ok", False)
        if heuristic_eligible:
            needs_legal = self._should_force_legal_search(
                query=query,
                processed_query=intent_data.get("query_for_retrieval") or query,
                intent_data=intent_data,
            )
        else:
            needs_legal = bool(llm_needs_legal)

        is_in_scope = intent_data["intent"] in (Intent.RAG_QUERY, Intent.FOLLOW_UP)
        qfr = intent_data.get("query_for_retrieval")
        if qfr:
            processed = qfr

        expanded = [a for a in detected if a in processed and detected[a] in processed]

        return QueryProcessResult(
            original_query=query,
            processed_query=processed,
            enriched_query=intent_data.get("enriched_query", ""),
            expanded_acronyms=expanded,
            detected_acronyms=detected,
            was_expanded=bool(expanded),
            is_in_scope=is_in_scope,
            intent=intent_data["intent"],
            intent_confidence=intent_data.get("confidence", 1.0),
            intent_reason=intent_data.get("reasoning"),
            needs_legal_search=needs_legal,
            needs_legal_search_llm=llm_needs_legal,
            theme=intent_data.get("theme"),
            was_enriched=bool(intent_data.get("enriched_query")),
            direct_response=intent_data.get("direct_response"),
            intent_raw_response=intent_data.get("raw"),
        )

    # -- internal helpers -----------------------------------------------------

    def _detect_acronyms(self, query: str) -> Dict[str, str]:
        out: Dict[str, str] = {}
        for acr, full in self._acronyms.items():
            if re.search(r"\b" + re.escape(acr) + r"\b", query):
                out[acr] = full
        return out

    def _classify(
        self,
        query: str,
        history: Optional[List[Dict[str, str]]],
        detected_acronyms: Dict[str, str],
        ministry: MinistrySource | None = None,
    ) -> Dict[str, Any]:
        """Run the unified intent prompt via a lightweight LLM call."""
        try:
            llm = LLMClient(provider="albert", model=self.config.intent_model, temperature=0.0)

            # format history
            if history and len(history) >= 2:
                parts = []
                for m in history[-8:]:
                    role = "Utilisateur" if m["role"] == "user" else "Assistant"
                    c = m["content"][:300] + "..." if len(m["content"]) > 300 else m["content"]
                    parts.append(f"{role}: {c}")
                history_text = "\n".join(parts)
            else:
                history_text = "(Pas d'historique de conversation)"

            if detected_acronyms:
                acr_section = "Les acronymes suivants ont ete detectes (en MAJUSCULES) :\n" + "\n".join(
                    f"- **{a}** = {f}" for a, f in detected_acronyms.items()
                )
            else:
                acr_section = "(Aucun acronyme detecte)"

            template = load_prompt(self.config.intent_prompt_name, "intent.md")
            if not template:
                raise FileNotFoundError("Intent prompt not found")

            # Resolve {ministere_*} before .format() fills history/query/acronyms.
            template = render_ministry_prompt(template, ministry)
            prompt = template.format(history=history_text, query=query, acronyms_section=acr_section)
            raw = llm.chat(prompt, system_prompt="")

            # parse JSON from LLM response
            text = raw.strip()
            m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
            text = m.group(1) if m else text
            if text.startswith("```"):
                text = text.split("```")[1]
            data = json.loads(text.strip().lstrip("json").strip())

            intent_str = data.get("intent", "rag_query")
            intent = Intent(intent_str) if intent_str in Intent._value2member_map_ else Intent.RAG_QUERY

            theme = data.get("theme")
            if theme and theme not in AVAILABLE_THEMES:
                theme = "autre"

            return {
                "intent": intent,
                "confidence": float(data.get("confidence", 0.8)),
                "reasoning": data.get("reasoning", ""),
                "needs_legal": bool(data.get("needs_legal_search", False)),
                "theme": theme,
                "enriched_query": data.get("reformulated_query") or "",
                "query_for_retrieval": data.get("query_for_retrieval"),
                "direct_response": _DIRECT_RESPONSES.get(intent),
                "raw": raw,
                "classify_ok": True,
            }

        except Exception as exc:
            logger.warning("Intent classification failed (%s), defaulting to rag_query", exc)
            return {"intent": Intent.RAG_QUERY, "confidence": 0.5, "reasoning": str(exc), "classify_ok": False}

    def _fallback_expand(self, query: str, detected: Dict[str, str]) -> Dict[str, Any]:
        """When intent gating is disabled, just expand acronyms inline."""
        expanded = query
        for acr, full in detected.items():
            expanded = re.sub(r"\b" + re.escape(acr) + r"\b", f"{acr} ({full})", expanded)
        return {
            "intent": Intent.RAG_QUERY,
            "confidence": 1.0,
            "query_for_retrieval": expanded if expanded != query else None,
        }

    def _should_force_legal_search(
        self,
        *,
        query: str,
        processed_query: str,
        intent_data: Dict[str, Any],
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
