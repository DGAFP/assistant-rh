"""Explicit composition-root configuration; never read environment or connect."""

import math
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from assistant_rh_api.core.models.inference import Provider


@dataclass(frozen=True, slots=True)
class Endpoint:
    provider: Provider
    model: str
    base_url: str = field(repr=False)
    api_key: str = field(repr=False)

    def __post_init__(self) -> None:
        url = urlsplit(self.base_url)
        if (
            self.provider not in ("albert", "scaleway")
            or not self.model.strip()
            or not self.api_key.strip()
            or url.scheme != "https"
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise ValueError("invalid inference endpoint")


@dataclass(frozen=True, slots=True)
class RequestPolicy:
    # Per-provider total deadline includes all retries and (for rerank) batches.
    timeout: float = 10.0
    total_timeout: float = 30.0
    max_attempts: int = 2
    retry_delay: float = 0.1
    max_response_bytes: int = 8 * 1024 * 1024

    def __post_init__(self) -> None:
        if (
            not all(math.isfinite(value) and value > 0 for value in (self.timeout, self.total_timeout))
            or not math.isfinite(self.retry_delay)
            or self.retry_delay < 0
            or type(self.max_attempts) is not int
            or not 1 <= self.max_attempts <= 3
            or type(self.max_response_bytes) is not int
            or self.max_response_bytes <= 0
        ):
            raise ValueError("invalid inference request policy")


def validate_chain(primary: Endpoint, fallback: Endpoint | None) -> None:
    if fallback is not None and (primary.provider != "albert" or fallback.provider != "scaleway"):
        raise ValueError("fallback chain must be Albert then Scaleway")
