"""Immutable configuration values and revisioned store snapshots."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

type JsonValue = None | bool | int | float | str | tuple[JsonValue, ...] | Mapping[str, JsonValue]
type ConfigValues = Mapping[str, JsonValue]


@dataclass(frozen=True, slots=True)
class Snapshot[T]:
    """An immutable value and its content revision, captured together by a store.

    T must be deeply immutable, including nested config values. Revisions are
    opaque equality tokens, not ordered versions or database write/CAS tokens.
    """

    value: T
    revision: str
    origin: Literal["database", "packaged", "default"]


@dataclass(frozen=True, slots=True)
class Prompt:
    name: str
    content: str


@dataclass(frozen=True, slots=True)
class Acronym:
    short: str
    expansion: str
