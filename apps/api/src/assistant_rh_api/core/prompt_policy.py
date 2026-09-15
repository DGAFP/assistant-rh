"""C5 prompt decisions, preserving the retained runtime's exact wording.

Stores own DB/resource I/O. The caller supplies an already authorized ministry;
rendering's generic fallback is not an authorization decision.
"""

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from hashlib import sha256

from assistant_rh_api.core.errors import DatabaseConflict, DatabaseFailure, DatabaseUnavailable
from assistant_rh_api.core.ministry_policy import MINISTRIES
from assistant_rh_api.core.models.configuration import Prompt, Snapshot
from assistant_rh_api.core.models.context import AggregatedSection, ContextItem
from assistant_rh_api.core.models.inference import ChatRequest, Message
from assistant_rh_api.core.pipeline.steps.context_formatting import format_for_prompt
from assistant_rh_api.core.ports.configuration import PromptStorePort

USER_PROMPT_TEMPLATE = """Voici le contexte documentaire pour repondre a la question :

{context}

---

**Question de l'utilisateur :** {question}

---

En vous appuyant uniquement sur les sources ci-dessus, repondez de maniere claire, precise et proportionnee a la question.
Pour une question sur les regles, exposez le cadre documente sans ajouter de rubrique "En pratique" ni de procedure pas-a-pas.
N'inferez pas la frequence d'une decision, qui choisit une modalite, ni une demarche locale si les sources ne le disent pas explicitement.
Si les sources ne permettent pas de repondre, dites-le explicitement et n'inventez pas."""

COMPLEMENTARY_SOURCE_COVERAGE = """## Couverture des sources complémentaires

Lorsque le contexte contient à la fois un texte juridique et une source
ministérielle directement pertinents, utilisez les deux : présentez d'abord le
cadre légal général, puis distinguez clairement sa mise en œuvre ministérielle.
N'écartez pas une disposition juridique non redondante au seul motif qu'une
fiche pratique est disponible, et n'ajoutez aucune consigne opérationnelle qui
n'est pas étayée par les sources. N'en déduisez pas de procédure locale (jour
imposé, démarche auprès du service RH ou circuit de validation) si elle n'est
pas explicitement décrite. Une question sur les règles ne demande pas une
procédure pas-à-pas : n'ajoutez une marche à suivre que si l'utilisateur la
demande explicitement. Nommez les articles juridiques pertinents lorsque leur
numéro figure dans le contexte et ne désignez jamais les documents par des
numéros techniques comme « source 1 » ou « document 2 ». N'inférez jamais la
fréquence d'une décision ni qui choisit une modalité lorsque les sources ne le
précisent pas."""

COMPLEMENTARY_SOURCE_SELECTION_RULE = """

## Redondance et complémentarité

Avant d'éliminer une section comme redondante, identifie l'information précise
qu'elle apporte. Deux sections sont redondantes uniquement si elles donnent la
même règle, la même condition ou la même modalité sans apport supplémentaire.

Elles sont complémentaires si chacune apporte un élément distinct utile à la
réponse : champ d'application, conditions, modalités, autorité compétente,
consultation requise, texte de mise en œuvre ou déclinaison ministérielle.
Le fait de traiter du même sujet, ou qu'une source soit prioritaire, ne suffit
jamais à rendre une autre source redondante.

Applique le même test de pertinence à tous les éditeurs. La hiérarchie des
sources sert uniquement à départager deux passages réellement équivalents.
Garde toutes les sections directement pertinentes dont l'apport est distinct.
"""

DEFAULT_SELECTOR_PROMPT = """Tu es un expert en selection de contexte pour un assistant RH.

**Question :** {query}

**Sections disponibles :**
{context}

Selectionne les sections pertinentes pour repondre a la question.

Reponds UNIQUEMENT avec un JSON :
```json
{{
  "selected_ids": [0, 2, 5],
  "reason": "Explication courte"
}}
```
"""

DEFAULT_GENERATOR_PROMPT = (
    """Tu es un assistant RH expert pour {ministere_label}. Reponds aux questions des agents publics sur les ressources humaines."""
)

NO_ANSWER = (
    "Je n'ai pas trouvé d'informations suffisamment pertinentes dans ma base de connaissances "
    "pour répondre à cette question. N'hésitez pas à reformuler votre question ou à contacter "
    "votre service RH pour obtenir une réponse précise."
)


@dataclass(frozen=True, slots=True)
class LoadedPrompt:
    snapshot: Snapshot[Prompt]
    store_errors: tuple[str, ...] = ()


async def load_prompt(
    prompts: PromptStorePort,
    packaged_prompts: PromptStorePort,
    name: str,
    fallback_name: str,
    default: str,
) -> LoadedPrompt:
    errors = []
    for candidate in (name, fallback_name):
        try:
            snapshot = await prompts.get(candidate)
        except (DatabaseConflict, DatabaseFailure, DatabaseUnavailable) as exc:
            errors.append(exc.code)
            snapshot = None
        # Empty DB content skips this name, as in get_prompt_content/load_prompt.
        if snapshot is None:
            snapshot = await packaged_prompts.get(candidate)
        if snapshot is not None and snapshot.value.content:
            return LoadedPrompt(snapshot, tuple(errors))
    return LoadedPrompt(Snapshot(Prompt(fallback_name, default), sha256(default.encode()).hexdigest(), "default"), tuple(errors))


def render_ministry_prompt(template: str, ministry: str | None) -> str:
    key = (ministry or "").strip().lower()
    label = key.upper() if key in MINISTRIES else "votre ministère"
    return template.replace("{ministere_label}", label).replace("{ministere_sigle}", label)


def selector_prompt(template: str, query: str, sections: Sequence[AggregatedSection], ministry: str | None, *, today: str) -> str:
    if "## Redondance et complémentarité" not in template:
        template = f"{template.rstrip()}\n{COMPLEMENTARY_SOURCE_SELECTION_RULE}"
    template = render_ministry_prompt(template.replace("{today}", today), ministry)
    context = "\n\n---\n\n".join(f"[{i}] {sec.heading} ({sec.publisher or 'unknown'})\n{sec.markdown}" for i, sec in enumerate(sections))
    try:
        return template.format_map(defaultdict(str, query=query, context=context, theme=""))
    except (ValueError, KeyError, IndexError, AttributeError, TypeError):
        return template.replace("{query}", query).replace("{context}", context).replace("{theme}", "")


def generation_request(
    template: str,
    query: str,
    items: Sequence[ContextItem],
    ministry: str | None,
    temperature: float,
    history: tuple[Message, ...] = (),
    *,
    today: str,
) -> ChatRequest:
    if "## Couverture des sources complémentaires" not in template:
        template = f"{template.rstrip()}\n\n{COMPLEMENTARY_SOURCE_COVERAGE}"
    system = render_ministry_prompt(template.replace("{today}", today), ministry)
    user = USER_PROMPT_TEMPLATE.format(context=format_for_prompt(items), question=query)
    return ChatRequest((Message("system", system), *history, Message("user", user)), temperature)
