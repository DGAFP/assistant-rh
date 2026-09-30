import json
from dataclasses import replace

import httpx
import pytest
from assistant_rh_api.core.feedback import FeedbackService
from assistant_rh_api.handlers.app import create_app
from assistant_rh_api.handlers.auth import resolve_bearer

from apps.api.tests.auth_fakes import Clock, individual_context

pytestmark = pytest.mark.anyio


async def individual():
    return individual_context()


class Store:
    async def save(self, *args, **kwargs):
        raise AssertionError("Invalid inputs must not reach the database")


@pytest.mark.parametrize(
    "changes",
    [
        {"stars": None},
        {"stars": 0},
        {"stars": 6},
        {"stars": True},
        {"stars": "3"},
        {"rating": 3},
        {"user_id": "forged"},
        {"group_slug": "forged"},
        {"helpful": True},
        {"reasons_positive": ["unknown"]},
        {"reasons_negative": ["Clair"]},
        {"stars": 1, "reasons_positive": ["Clair"]},
        {"stars": 5, "reasons_negative": ["Confus"]},
        {"comment": "   "},
        {"comment": "x" * 4001},
        {"comment": "\ud800"},
        {"completion_id": ""},
        {"completion_id": "bad/id"},
        {"completion_id": "chatcmpl-"},
        {"reasons_positive": "Clair"},
        {"reasons_positive": ["Clair"] * 33},
    ],
)
async def test_invalid_feedback_is_safe_422(changes):
    app = create_app(feedback_service=FeedbackService(Store(), Clock()))
    app.dependency_overrides[resolve_bearer] = individual
    body = {"completion_id": "synthetic-id", "stars": 3, "comment": "Test", **changes}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/v1/feedback", content=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        assert response.status_code == 422
        assert "Test" not in response.text and "forged" not in response.text and "input" not in response.text
        assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("chunked", [False, True])
async def test_feedback_body_bounded_before_json(chunked):
    app = create_app(feedback_service=FeedbackService(Store(), Clock()))
    app.dependency_overrides[resolve_bearer] = individual
    payload = b"x" * 17000

    async def chunks():
        yield payload[:100]
        yield payload[100:]

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.post("/v1/feedback", content=chunks() if chunked else payload)).status_code == 413


async def test_missing_audit_pseudonym_fails_closed():
    app = create_app(feedback_service=FeedbackService(Store(), Clock()))
    app.dependency_overrides[resolve_bearer] = lambda: replace(individual_context(), audit_session_hash="")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.post("/v1/feedback", json={"completion_id": "run", "stars": 3, "comment": "Test"})).status_code == 403
