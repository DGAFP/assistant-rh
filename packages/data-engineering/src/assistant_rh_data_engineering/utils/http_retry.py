from __future__ import annotations

import time
from collections.abc import Callable

import requests

RETRYABLE_HTTP_STATUS_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})


def request_with_retry(
    send: Callable[[], requests.Response],
    *,
    attempts: int = 4,
    backoff_seconds: float = 1.0,
) -> requests.Response:
    """Send a safe HTTP request with bounded retries for transient failures.

    The caller decides whether the operation is safe to replay. Responses with
    non-transient status codes are returned immediately so existing error
    handling can preserve its domain-specific message.
    """
    if attempts < 1:
        raise ValueError("attempts must be at least 1")

    for attempt in range(attempts):
        response: requests.Response | None = None
        try:
            response = send()
        except (requests.ConnectionError, requests.Timeout):
            if attempt == attempts - 1:
                raise
        else:
            if response.status_code not in RETRYABLE_HTTP_STATUS_CODES or attempt == attempts - 1:
                return response

        delay = backoff_seconds * (2**attempt)
        retry_after = (getattr(response, "headers", {}) or {}).get("Retry-After") if response is not None else None
        if retry_after:
            try:
                delay = max(delay, float(retry_after))
            except ValueError:
                pass
        time.sleep(delay)

    raise AssertionError("unreachable")
