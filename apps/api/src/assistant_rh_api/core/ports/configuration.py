"""Configuration boundaries; adapters own I/O and lifecycle state."""

from typing import Protocol

from assistant_rh_api.core.models.configuration import Acronym, ConfigValues, Prompt, Snapshot


class ConfigStorePort(Protocol):
    async def load(self) -> Snapshot[ConfigValues] | None: ...


class PromptStorePort(Protocol):
    async def get(self, name: str) -> Snapshot[Prompt] | None: ...


class AcronymStorePort(Protocol):
    async def load(self) -> Snapshot[tuple[Acronym, ...]]: ...
