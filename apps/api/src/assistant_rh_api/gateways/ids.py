"""Full UUIDs for completion IDs; generation is outside the pure core."""

from uuid import uuid4

from assistant_rh_api.core.ports.system import IdGeneratorPort


class CompletionIds(IdGeneratorPort):
    def new_id(self) -> str:
        return "chatcmpl-" + uuid4().hex
