"""Document existence and availability stay distinct, without storage details."""

from assistant_rh_api.core.errors.base import ApplicationError


class DocumentNotFound(ApplicationError):
    code = "document_not_found"


class DocumentUnavailable(ApplicationError):
    code = "document_unavailable"
