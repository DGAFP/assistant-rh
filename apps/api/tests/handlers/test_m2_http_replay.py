import httpx
import pytest
from assistant_rh_api.core.models.chat import ChatInput
from assistant_rh_api.core.models.inference import Message

from apps.api.tests.auth_fakes import service
from apps.api.tests.chat_fakes import Runtime
from scripts.conformance import m2_goldset_http, m2_http_replay

pytestmark = pytest.mark.anyio


async def test_http_replay_uses_real_core_and_preserves_result_after_followup():
    history = (Message("user", "Question RH"), Message("assistant", "Réponse initiale"))
    request = ChatInput("assistant-rh-matte", "Et pour les contractuels ?", history)
    auth_service = service()
    issued = await auth_service.login("beta", "password", "local")
    auth = await auth_service.resolve(issued.access_token)
    direct = Runtime()
    expected_run, expected_result = await direct.service.complete(request, auth)

    delegated = Runtime()
    actual_run, actual_result = await m2_http_replay.complete_over_http(delegated.service, request, auth)

    assert actual_result == expected_result
    assert actual_run.sources == expected_run.sources
    assert actual_run.author_user_id is not None
    assert delegated.llm.calls == direct.llm.calls
    assert delegated.runs.rows[actual_run.turn_id] == actual_run


def test_live_report_keeps_transport_failures_out_of_quality_and_omits_private_fields():
    row = {"id": 1, "model": "assistant-rh-mi", "question": "Question synthétique", "expected_doc_refs": ["guide"]}
    turn_id = "a" * 32
    payload = {
        "id": f"chatcmpl-{turn_id}",
        "object": "chat.completion",
        "model": row["model"],
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "Réponse synthétique"}, "finish_reason": "stop"}],
        "x_assistant_rh": {
            "turn_id": turn_id,
            "ministry": "mi",
            "sources": [{"doc_ref": "guide", "access": "authenticated", "url": None}],
        },
    }
    responses = iter((httpx.Response(503, text="private details"), httpx.Response(200, json=payload)))
    with httpx.Client(base_url="http://127.0.0.1/v1/", transport=httpx.MockTransport(lambda request: next(responses))) as client:
        failed, missing = m2_goldset_http.complete(client, row, "test.jwt.placeholder")
        success, (quality, private) = m2_goldset_http.complete(client, row, "fresh.jwt.placeholder")

    assert missing is None and failed["outcome"] == "http_error" and failed["status"] == 503
    assert success["outcome"] == "completed"
    assert quality == {"id": 1, "gold_sources": 1, "gold_sources_served": 1}
    assert private["turn_id"] == turn_id
    assert "private details" not in str(failed) and "Réponse" not in str((success, quality))
