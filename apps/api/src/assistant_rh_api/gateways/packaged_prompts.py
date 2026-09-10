"""Packaged prompt fallback; resource I/O remains outside the core."""

from hashlib import sha256
from importlib.resources import files

from assistant_rh_api.core.models.configuration import Prompt, Snapshot


class PackagedPromptStore:
    async def get(self, name: str) -> Snapshot[Prompt] | None:
        # Only resources explicitly shipped with this API can be requested.
        if name != "intent.md":
            return None
        content = files("assistant_rh_api.prompts").joinpath(name).read_text(encoding="utf-8")
        return Snapshot(Prompt(name, content), sha256(content.encode()).hexdigest(), "packaged")
