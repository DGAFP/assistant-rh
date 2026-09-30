"""Individual feedback use cases; B4 is deliberately not an individual identity."""

import re

from assistant_rh_api.core.auth import AuthContext
from assistant_rh_api.core.errors import ApplicationError
from assistant_rh_api.core.models.conversations import FeedbackInput
from assistant_rh_api.core.ports.conversations import FeedbackStorePort
from assistant_rh_api.core.ports.system import ClockPort

POSITIVE_REASONS = ("Clair", "Utile", "Pertinent", "Complet", "Précis")
NEGATIVE_REASONS = ("Confus", "Éléments faux", "Non pertinent", "Incomplet", "Sources manquantes")


class FeedbackInvalid(ApplicationError):
    code = "invalid_feedback"


class IndividualIdentityRequired(ApplicationError):
    code = "individual_identity_required"


class FeedbackNotFound(ApplicationError):
    code = "feedback_not_found"


def turn_id(completion_id: str) -> str:
    value = completion_id.removeprefix("chatcmpl-")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        raise FeedbackInvalid()
    return value


def normalized(value: FeedbackInput) -> FeedbackInput:
    stars = value.stars
    positive, negative = value.reasons_positive, value.reasons_negative
    comment = value.comment.strip()
    if (
        stars is None
        or any(r not in POSITIVE_REASONS for r in positive)
        or any(r not in NEGATIVE_REASONS for r in negative)
        or (stars <= 2 and positive)
        or (stars == 5 and negative)
        or not (positive or negative or comment)
        or len(comment) > 4000
    ):
        raise FeedbackInvalid()
    try:
        comment.encode("utf-8")
    except UnicodeEncodeError:
        raise FeedbackInvalid() from None
    return FeedbackInput(
        turn_id(value.turn_id),
        stars,
        comment,
        tuple(r for r in POSITIVE_REASONS if r in positive),
        tuple(r for r in NEGATIVE_REASONS if r in negative),
        stars >= 3,
    )


class FeedbackService:
    def __init__(self, store: FeedbackStorePort, clock: ClockPort) -> None:
        self._store = store
        self._clock = clock

    @staticmethod
    def require_individual(auth: AuthContext) -> None:
        if auth.user_id is None:
            raise IndividualIdentityRequired()

    async def save(self, value: FeedbackInput, auth: AuthContext) -> None:
        self.require_individual(auth)
        # An audit pseudonym must be furnished by the verified auth boundary.
        if not re.fullmatch(r"[0-9a-f]{64}", auth.audit_session_hash):
            raise IndividualIdentityRequired()
        saved = await self._store.save(
            normalized(value),
            auth.group.slug,
            auth.audit_session_hash,
            self._clock.now(),
            user_id=auth.user_id,
            ministries=auth.group.allowed_ministries,
        )
        if saved is None:
            raise FeedbackNotFound()

    async def get(self, completion_id: str, auth: AuthContext) -> FeedbackInput:
        self.require_individual(auth)
        assert auth.user_id is not None
        saved = await self._store.get_owned(turn_id(completion_id), auth.user_id, auth.group.slug, auth.group.allowed_ministries)
        if saved is None:
            raise FeedbackNotFound()
        return saved.value
