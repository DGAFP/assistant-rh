"""Stable application errors; adapters must never include driver messages."""

from assistant_rh_api.core.models.inference import Attempt


class ApplicationError(Exception):
    code = "application_error"

    def __init__(self) -> None:
        super().__init__(self.code)


class DatabaseConfigurationError(ApplicationError):
    code = "database_configuration_error"


class RAGConfigurationError(ApplicationError):
    code = "rag_configuration_error"


class DatabaseUnavailable(ApplicationError):
    code = "database_unavailable"


class DatabaseConflict(ApplicationError):
    code = "database_conflict"


class DatabaseFailure(ApplicationError):
    code = "database_failure"


class MinistryConfigurationError(ApplicationError):
    code = "ministry_configuration_error"


class ModelNotFound(ApplicationError):
    code = "model_not_found"


class InferenceFailure(ApplicationError):
    """Safe diagnostics only; no URL, credentials, prompt or response body."""

    code = "inference_failure"

    def __init__(self, attempts: tuple[Attempt, ...], *, partial: bool = False) -> None:
        super().__init__()
        self.attempts = attempts
        self.partial = partial
