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
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from .config import QueryProcessorConfig
from .db_helpers import get_acronym_dict, load_prompt
from .llm_client import LLMClient

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

_LEGAL_SEARCH_HINT_PATTERNS = (
    re.compile(r"\barticle\s+[a-z]?\s*[\.\-]?\d", re.IGNORECASE),
    re.compile(r"\b(?:cgfp|code général de la fonction publique|code de la sécurité sociale|code du travail)\b", re.IGNORECASE),
    re.compile(r"\b(?:décret|arrete|arrêté|circulaire|loi|ordonnance|jurisprudence)\b", re.IGNORECASE),
    re.compile(r"\b(?:fondement juridique|base légale|selon quel texte|c['’]est écrit où|preuve réglementaire)\b", re.IGNORECASE),
)

_LEGAL_SEARCH_RH_TOPIC_PATTERNS = (
    re.compile(r"\bagent contractuel\b", re.IGNORECASE),
    re.compile(r"\bcontrat de projet\b", re.IGNORECASE),
    re.compile(r"\bemploi permanent\b", re.IGNORECASE),
    re.compile(r"\bcongé parental\b", re.IGNORECASE),
    re.compile(r"\bsubrog\w*\b", re.IGNORECASE),
    re.compile(r"\bindemnités? journalières?\b", re.IGNORECASE),
    re.compile(r"\bprestations? en espèces\b", re.IGNORECASE),
    re.compile(r"\bpensions? de vieillesse\b", re.IGNORECASE),
    re.compile(r"\bcasier judiciaire\b", re.IGNORECASE),
    re.compile(r"\bservice national\b", re.IGNORECASE),
    re.compile(r"\bdroit au séjour\b", re.IGNORECASE),
    re.compile(r"\brupture anticipée\b", re.IGNORECASE),
    re.compile(r"\brenouvel(?:er|lement)\b", re.IGNORECASE),
)

_LEGAL_SEARCH_RULE_PATTERN = re.compile(
    r"\b(?:dans quels cas|à partir de quand|quelles informations|"
    r"quelles vérifications|quel montant|quel délai|quelles clauses|"
    r"dans quelles conditions)\b",
    re.IGNORECASE,
)

_LEGAL_SEARCH_HIGH_SIGNAL_TOPIC_PATTERN = re.compile(
    r"\b(?:contrat de projet|congé parental|emploi permanent|subrog\w*|"
    r"prestations? en espèces|pensions? de vieillesse|casier judiciaire|"
    r"service national|droit au séjour|rupture anticipée|"
    r"renouvel(?:er|lement))\b",
    re.IGNORECASE,
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
    ) -> QueryProcessResult:
        processed = query
        detected = self._detect_acronyms(query)

        # --- intent gating (single LLM call) --------------------------------
        if self.config.enable_intent_gating:
            intent_data = self._classify(query, conversation_history, detected)
        else:
            intent_data = self._fallback_expand(query, detected)

        intent_data["needs_legal"] = self._should_force_legal_search(
            query=query,
            processed_query=intent_data.get("query_for_retrieval") or query,
            intent_data=intent_data,
        )

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
            needs_legal_search=intent_data.get("needs_legal", False),
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
                "needs_legal": data.get("needs_legal_search", False),
                "theme": theme,
                "enriched_query": data.get("reformulated_query") or "",
                "query_for_retrieval": data.get("query_for_retrieval"),
                "direct_response": _DIRECT_RESPONSES.get(intent),
                "raw": raw,
            }

        except Exception as exc:
            logger.warning("Intent classification failed (%s), defaulting to rag_query", exc)
            return {"intent": Intent.RAG_QUERY, "confidence": 0.5, "reasoning": str(exc)}

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

        DGAFP is completely excluded from retrieval unless ``needs_legal_search`` is
        true. A narrow prompt-only definition is not robust enough for legal RH
        questions that mention the rule directly without explicitly asking for the
        article or decree. The heuristic stays conservative:
        - always preserve explicit LLM ``true``
        - force legal search for obvious legal markers
        - force legal search for legal-ish RH rule questions when at least two
          domain signals are present
        """
        llm_decision = bool(intent_data.get("needs_legal", False))
        if llm_decision:
            return True

        intent = intent_data.get("intent", Intent.RAG_QUERY)
        if intent not in (Intent.RAG_QUERY, Intent.FOLLOW_UP):
            return False

        haystack = f"{query}\n{processed_query}".strip()
        if any(pattern.search(haystack) for pattern in _LEGAL_SEARCH_HINT_PATTERNS):
            return True

        topic_hits = sum(1 for pattern in _LEGAL_SEARCH_RH_TOPIC_PATTERNS if pattern.search(haystack))
        asks_for_rule = bool(_LEGAL_SEARCH_RULE_PATTERN.search(haystack))
        high_signal_topic = bool(_LEGAL_SEARCH_HIGH_SIGNAL_TOPIC_PATTERN.search(haystack))
        return asks_for_rule and (topic_hits >= 2 or high_signal_topic)
