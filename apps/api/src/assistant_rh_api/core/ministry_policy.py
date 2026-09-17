"""Canonical API ministry catalogue and fail-closed group policy validation."""

from assistant_rh_api.core.errors import MinistryConfigurationError
from assistant_rh_api.core.models.auth import Group

MINISTRIES = frozenset(("matte", "mso", "mi", "masa"))


def valid_ministry_policy(group: Group) -> bool:
    return bool(group.allowed_ministries and set(group.allowed_ministries) <= MINISTRIES and group.default_ministry in group.allowed_ministries)


def validate_ministry_policy(group: Group) -> None:
    if not valid_ministry_policy(group):
        raise MinistryConfigurationError()
