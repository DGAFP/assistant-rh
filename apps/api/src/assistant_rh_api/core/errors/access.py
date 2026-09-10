"""Ministry access policy and public model resolution errors."""

from assistant_rh_api.core.errors.base import ApplicationError


class MinistryConfigurationError(ApplicationError):
    code = "ministry_configuration_error"


class ModelNotFound(ApplicationError):
    code = "model_not_found"
