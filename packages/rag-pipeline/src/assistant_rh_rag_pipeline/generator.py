"""
Streaming LLM generator for the RAG V3 Clean pipeline.

Builds the user prompt from context items and streams the response token
by token using ``FallbackLLMClient``.

Dependencies (internal only):
  - config (GenerationConfig)
  - db_helpers (load_prompt)
  - llm_client (FallbackLLMClient)
  - models (ContextItem)
  - context_builder (ContextBuilder – only the static formatter)
"""

from __future__ import annotations

import logging
from typing import Dict, Generator, List

from .config import GenerationConfig
from .context_builder import ContextBuilder
from .db_helpers import load_prompt
from .llm_client import FallbackLLMClient
from .ministry_scope import MinistrySource, render_ministry_prompt
from .models import ContextItem

logger = logging.getLogger(__name__)

# Dernière instruction lue par le modèle : elle doit porter la même voix que le
# prompt système V7 (persona gestionnaire RH), sinon elle le contredit par
# récence. Rendue par ministère via render_ministry_prompt AVANT .format()
# ({ministere_sigle} est remplacé par str.replace, {context}/{question} restent
# pour .format()).
USER_PROMPT_TEMPLATE = """Voici le contexte documentaire pour répondre à la question :

{context}

---

**Question du gestionnaire RH :** {question}

---

En vous appuyant uniquement sur les sources ci-dessus, répondez au gestionnaire RH de {ministere_sigle} de manière opérationnelle :
- nommez explicitement l'acteur de chaque action (le gestionnaire, le service RH, l'autorité compétente, l'agent)
  et ne vous adressez jamais à l'agent à la deuxième personne ;
- conservez le détail utile à l'instruction du dossier : conditions, étapes, délais, montants,
  contrôles et exceptions présents dans les sources ;
- écartez les cas particuliers de corps spécifiques (enseignants, Police nationale…) sauf si la question les vise.
Si les sources ne permettent pas de répondre, dites-le explicitement et n'inventez pas."""


class StreamingGenerator:
    """
    Generate an answer by streaming tokens from the LLM.

    Usage::

        gen = StreamingGenerator(config)
        for token in gen.stream(query, context_items, history):
            print(token, end="")
    """

    def __init__(self, config: GenerationConfig):
        self.config = config
        self._llm: FallbackLLMClient | None = None
        self._base_prompt: str | None = None
        self.last_full_prompt: str = ""
        self.last_system_prompt: str = ""

    @property
    def llm(self) -> FallbackLLMClient:
        if self._llm is None:
            # Baked default renders the generic (no-ministry) wording; every
            # request overrides it per-call with the selected ministry so a
            # single cached client serves all tenants safely.
            self._llm = FallbackLLMClient(
                primary_provider=self.config.provider.value,
                primary_model=self.config.model,
                fallback_provider=self.config.fallback_provider.value,
                fallback_model=self.config.fallback_model,
                temperature=self.config.temperature,
                system_prompt=self._system_prompt_for(None),
            )
        return self._llm

    def stream(
        self,
        query: str,
        context_items: List[ContextItem],
        history: list[Dict[str, str]] | None = None,
        ministry: MinistrySource | None = None,
    ) -> Generator[str, None, None]:
        """Yield tokens one by one."""
        user_prompt = self._user_prompt_for(query, context_items, ministry)
        self.last_full_prompt = user_prompt
        system_prompt = self._system_prompt_for(ministry)
        self.last_system_prompt = system_prompt
        yield from self.llm.chat_stream(user_prompt, system_prompt=system_prompt, history=history)

    def generate(
        self,
        query: str,
        context_items: List[ContextItem],
        ministry: MinistrySource | None = None,
    ) -> str:
        """Non-streaming variant (useful for evaluation)."""
        user_prompt = self._user_prompt_for(query, context_items, ministry)
        self.last_full_prompt = user_prompt
        system_prompt = self._system_prompt_for(ministry)
        self.last_system_prompt = system_prompt
        return self.llm.chat(user_prompt, system_prompt=system_prompt)

    def _user_prompt_for(
        self,
        query: str,
        context_items: List[ContextItem],
        ministry: MinistrySource | None,
    ) -> str:
        """Assemble the per-request prompt, ministry-rendered like the system prompt."""
        context_text = ContextBuilder.format_for_prompt(context_items)
        template = render_ministry_prompt(USER_PROMPT_TEMPLATE, ministry)
        return template.format(context=context_text, question=query)

    def _base_system_prompt(self) -> str:
        """Load the (unrendered) system prompt template, cached per instance."""
        if self._base_prompt is None:
            _DEFAULT = (
                "Tu assistes les gestionnaires RH en SGCD pour {ministere_label}. "
                "Reformule les sources pour le gestionnaire, nomme explicitement l'acteur de chaque action "
                "et ne t'adresse jamais à l'agent à la deuxième personne."
            )
            self._base_prompt = load_prompt(self.config.system_prompt_name, "generator.md", default=_DEFAULT)
        return self._base_prompt

    def _system_prompt_for(self, ministry: MinistrySource | None) -> str:
        """Render the system prompt for *ministry* (generic wording if *None*)."""
        return render_ministry_prompt(self._base_system_prompt(), ministry)
