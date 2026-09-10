from dataclasses import FrozenInstanceError, replace
from itertools import permutations

import pytest
from assistant_rh_api.core.catalog import ModelService
from assistant_rh_api.core.errors import MinistryConfigurationError, MinistryForbidden, ModelNotFound

from apps.api.tests.auth_fakes import Groups


def test_catalogue_has_stable_order_unique_ids_and_immutable_values():
    group = Groups().rows["beta"]
    service = ModelService()
    for ministries in permutations(("mso", "matte", "mi", "masa", "matte")):
        models = service.list_models(replace(group, allowed_ministries=ministries))
        assert [model.id for model in models] == ["assistant-rh-masa", "assistant-rh-matte", "assistant-rh-mi", "assistant-rh-mso"]
        assert {model.created for model in models} == {1755734400}
    with pytest.raises(FrozenInstanceError):
        models[0].ministry = "private"


@pytest.mark.parametrize("ministries,default", [(("mi",), "mi"), (("mso", "matte"), "mso")])
def test_alias_resolves_only_to_configured_default_and_explicit_models_roundtrip(ministries, default):
    group = replace(Groups().rows["beta"], allowed_ministries=ministries, default_ministry=default)
    service = ModelService()
    assert service.resolve("assistant-rh", group).ministry == default
    assert [model.ministry for model in service.list_models(group)] == sorted(ministries)
    for model in service.list_models(group):
        assert service.resolve(model.id, group) == model
    assert "assistant-rh" not in [model.id for model in service.list_models(group)]


@pytest.mark.parametrize("model", ["unknown", "", "assistant-rh-", "assistant-rh-unknown", "assistant-rh-MI", "assistant-rh-mi ", "other-mi"])
def test_unknown_models_do_not_fallback(model):
    with pytest.raises(ModelNotFound):
        ModelService().resolve(model, Groups().rows["beta"])


def test_known_but_unauthorized_model_is_forbidden():
    with pytest.raises(MinistryForbidden):
        ModelService().resolve("assistant-rh-masa", Groups().rows["beta"])


@pytest.mark.parametrize(
    "change",
    [
        {"allowed_ministries": ()},
        {"default_ministry": ""},
        {"default_ministry": None},
        {"default_ministry": "masa"},
        {"default_ministry": "unknown"},
        {"allowed_ministries": ("matte", "unknown")},
    ],
)
def test_invalid_policy_is_explicit_configuration_error_for_listing_and_resolution(change):
    group = replace(Groups().rows["beta"], **change)
    service = ModelService()
    with pytest.raises(MinistryConfigurationError, match="^ministry_configuration_error$"):
        service.list_models(group)
    for model in ("assistant-rh", "assistant-rh-matte"):
        with pytest.raises(MinistryConfigurationError):
            service.resolve(model, group)
