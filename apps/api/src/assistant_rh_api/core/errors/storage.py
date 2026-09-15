"""Storage configuration, availability and transaction errors."""

from assistant_rh_api.core.errors.base import ApplicationError


class DatabaseConfigurationError(ApplicationError):
    code = "database_configuration_error"


class DatabaseUnavailable(ApplicationError):
    code = "database_unavailable"


class DatabaseConflict(ApplicationError):
    code = "database_conflict"


class DatabaseFailure(ApplicationError):
    code = "database_failure"
