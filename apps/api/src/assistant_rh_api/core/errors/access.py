"""Authentication, ministry access policy and public model resolution errors."""

from assistant_rh_api.core.errors.base import ApplicationError


class MinistryConfigurationError(ApplicationError):
    code = "ministry_configuration_error"


class ModelNotFound(ApplicationError):
    code = "model_not_found"


class InvalidCredentials(ApplicationError):
    code = "invalid_api_key"


class MinistryForbidden(ApplicationError):
    code = "ministry_forbidden"


class LoginRateLimited(ApplicationError):
    code = "rate_limit_exceeded"

    def __init__(self, retry_after: int) -> None:
        super().__init__()
        self.retry_after = retry_after
