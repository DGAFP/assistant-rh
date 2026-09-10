"""Stable public error imports; domain modules define each class exactly once."""

from assistant_rh_api.core.errors.access import MinistryConfigurationError, ModelNotFound
from assistant_rh_api.core.errors.base import ApplicationError
from assistant_rh_api.core.errors.inference import InferenceFailure
from assistant_rh_api.core.errors.rag import ClassificationFailure, RAGConfigurationError
from assistant_rh_api.core.errors.storage import DatabaseConfigurationError, DatabaseConflict, DatabaseFailure, DatabaseUnavailable

__all__ = [
    "ApplicationError",
    "ClassificationFailure",
    "DatabaseConfigurationError",
    "DatabaseConflict",
    "DatabaseFailure",
    "DatabaseUnavailable",
    "InferenceFailure",
    "MinistryConfigurationError",
    "ModelNotFound",
    "RAGConfigurationError",
]
