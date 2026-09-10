"""Public model catalogue and routing policy, without storage or transport."""

from assistant_rh_api.core.errors import MinistryForbidden, ModelNotFound
from assistant_rh_api.core.ministry_policy import MINISTRIES, validate_ministry_policy
from assistant_rh_api.core.models.auth import Group
from assistant_rh_api.core.models.catalog import Model


class ModelService:
    def list_models(self, group: Group) -> tuple[Model, ...]:
        validate_ministry_policy(group)
        return tuple(Model(f"assistant-rh-{ministry}", ministry) for ministry in sorted(set(group.allowed_ministries)))

    def resolve(self, model: str, group: Group) -> Model:
        """Resolve the input alias or explicit model for future Chat Completions."""
        validate_ministry_policy(group)
        if model == "assistant-rh":
            ministry = group.default_ministry
        else:
            prefix = "assistant-rh-"
            if not model.startswith(prefix) or model[len(prefix) :] not in MINISTRIES:
                raise ModelNotFound()
            ministry = model[len(prefix) :]
        if ministry not in group.allowed_ministries:
            raise MinistryForbidden()
        return Model(f"assistant-rh-{ministry}", ministry)
