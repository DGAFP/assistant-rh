"""RAG configuration and query classification errors."""

from typing import Literal

from assistant_rh_api.core.errors.base import ApplicationError


class RAGConfigurationError(ApplicationError):
    code = "rag_configuration_error"


class ClassificationFailure(ApplicationError):
    """Expected classification failure, chained to its original cause internally."""

    code = "classification_failure"

    def __init__(self, reason: Literal["provider_failure", "invalid_response"]) -> None:
        super().__init__()
        self.reason = reason
