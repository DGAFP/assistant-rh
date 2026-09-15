"""Provider inference errors with safe per-attempt diagnostics."""

from assistant_rh_api.core.errors.base import ApplicationError
from assistant_rh_api.core.models.inference import Attempt


class InferenceFailure(ApplicationError):
    """Safe diagnostics only; no URL, credentials, prompt or response body."""

    code = "inference_failure"

    def __init__(self, attempts: tuple[Attempt, ...], *, partial: bool = False) -> None:
        super().__init__()
        self.attempts = attempts
        self.partial = partial
