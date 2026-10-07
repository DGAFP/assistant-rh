"""Public model catalogue and routing policy, without storage or transport."""

from assistant_rh_api.core.errors import MinistryForbidden, ModelNotFound
from assistant_rh_api.core.ministry_policy import MINISTRIES, validate_ministry_policy
from assistant_rh_api.core.models.auth import Delegation, Group
from assistant_rh_api.core.models.catalog import Model

MODEL_PREFIX = "assistant-rh-"


def model_ministry(model: str) -> str | None:
    """Ministry routed by an explicit public model ID; the alias is not a model."""
    if not model.startswith(MODEL_PREFIX) or model[len(MODEL_PREFIX) :] not in MINISTRIES:
        return None
    return model[len(MODEL_PREFIX) :]


def scope(principal: Group | Delegation) -> tuple[frozenset[str], str | None]:
    """Ministries granted to the principal and the one the alias routes to."""
    if isinstance(principal, Delegation):
        # Conversations fixes the ministry at conversation creation; an empty grant is legitimate.
        allowed = frozenset(principal.allowed_ministries)
        if principal.ministry is not None:
            allowed &= {principal.ministry}
        return allowed, principal.ministry
    validate_ministry_policy(principal)
    return frozenset(principal.allowed_ministries), principal.default_ministry


class ModelService:
    def list_models(self, principal: Group | Delegation) -> tuple[Model, ...]:
        allowed, _ = scope(principal)
        return tuple(Model(f"{MODEL_PREFIX}{ministry}", ministry) for ministry in sorted(allowed))

    def resolve(self, model: str, principal: Group | Delegation) -> Model:
        """Resolve the input alias or explicit model for future Chat Completions."""
        allowed, default = scope(principal)
        ministry = default if model == "assistant-rh" else model_ministry(model)
        if model != "assistant-rh" and ministry is None:
            raise ModelNotFound()
        # A model name is never an authorization: it must fall within the granted scope.
        if ministry is None or ministry not in allowed:
            raise MinistryForbidden()
        return Model(f"{MODEL_PREFIX}{ministry}", ministry)
