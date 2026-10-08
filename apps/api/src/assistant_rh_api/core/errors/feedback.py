"""Individual feedback validation and access errors."""

from assistant_rh_api.core.errors.base import ApplicationError


class FeedbackInvalid(ApplicationError):
    code = "invalid_feedback"


class IndividualIdentityRequired(ApplicationError):
    code = "individual_identity_required"


class FeedbackNotFound(ApplicationError):
    code = "feedback_not_found"
