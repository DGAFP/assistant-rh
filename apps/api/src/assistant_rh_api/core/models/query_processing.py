"""Immutable query output and request-local evidence for later engine composition."""

from dataclasses import dataclass
from enum import Enum

from assistant_rh_api.core.models.configuration import Acronym, Prompt, Snapshot
from assistant_rh_api.core.models.inference import Attempt, Completion


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

AVAILABLE_THEMES = (
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
)

BETA_EXCLUDED_THEMES = frozenset({"action_sociale", "psc", "retraite", "apprentis"})


@dataclass(frozen=True, slots=True)
class QueryProcessResult:
    original_query: str
    processed_query: str
    enriched_query: str = ""

    expanded_acronyms: tuple[str, ...] = ()
    detected_acronyms: tuple[Acronym, ...] = ()
    was_expanded: bool = False

    is_in_scope: bool = True
    intent: Intent = Intent.RAG_QUERY
    intent_confidence: float = 1.0
    intent_reason: str | None = None
    needs_legal_search: bool = False
    # Preserved LLM-only value (None when classify failed or gating was off).
    # Lets observability/conformance compare the LLM against the post-heuristic
    # decision instead of seeing only the merged flag.
    needs_legal_search_llm: bool | None = None

    theme: str | None = None
    was_enriched: bool = False
    direct_response: str | None = None

    # observability (kept lightweight)
    intent_raw_response: str | None = None

    @property
    def should_proceed(self) -> bool:
        return self.is_in_scope

    @property
    def query_for_retrieval(self) -> str:
        return self.enriched_query or self.processed_query


@dataclass(frozen=True, slots=True)
class QueryDiagnostics:
    acronyms: Snapshot[tuple[Acronym, ...]] | None = None
    prompt: Snapshot[Prompt] | None = None
    completion: Completion | None = None
    failed_attempts: tuple[Attempt, ...] = ()
    store_errors: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class QueryProcessing:
    result: QueryProcessResult
    diagnostics: QueryDiagnostics
