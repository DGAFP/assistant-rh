import asyncio
import json
from dataclasses import replace
from datetime import timedelta

import httpx
import openai
import pytest
from assistant_rh_api.core.errors import DatabaseFailure, DatabaseUnavailable
from assistant_rh_api.core.sources import SOURCES_MARKER
from assistant_rh_api.db.run_store import json_data
from assistant_rh_api.handlers.app import create_app
from assistant_rh_api.handlers.chat_body import MAX_BODY, MAX_CONTENT

from apps.api.tests.auth_fakes import service
from apps.api.tests.chat_fakes import Runtime

pytestmark = pytest.mark.anyio
BODY = {"model": "assistant-rh", "messages": [{"role": "user", "content": "Question RH"}]}


@pytest.fixture
async def chat():
    auth = service()
    issued = await auth.login("beta", "password", "local")
    runtime = Runtime()
    app = create_app(auth_service=auth, chat_service=runtime.service)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", headers={"Authorization": "Bearer " + issued.access_token}
    ) as client:
        yield client, runtime, auth, issued


async def test_openai_sdk_consumes_real_completion_and_only_server_parameters(chat):
    client, runtime, _, issued = chat
    async with openai.AsyncOpenAI(
        api_key=issued.access_token, base_url="http://test/v1", http_client=client, max_retries=0, _strict_response_validation=True
    ) as sdk:
        completion = await sdk.chat.completions.create(
            model="assistant-rh",
            messages=[{"role": "system", "content": "CLIENT_SECRET"}, *BODY["messages"]],
            temperature=0.9,
            max_tokens=1,
            user="external-identity",
            extra_body={
                "tools": {"arbitrary": "CLIENT_SECRET"},
                "tool_choice": "required",
                "max_completion_tokens": 1,
                "response_format": {"type": "json_object"},
                "metadata": {"conversation_id": "same-client-id", "ministry": "masa"},
            },
        )
        assert completion.object == "chat.completion" and completion.model == "assistant-rh-matte"
        assert completion.choices[0].finish_reason == "stop" and completion.choices[0].message.role == "assistant"
        assert not completion.choices[0].message.tool_calls
        extension = completion.model_extra["x_assistant_rh"]
        assert completion.id == "chatcmpl-" + extension["turn_id"]
        run = runtime.runs.rows[extension["turn_id"]]
        assert run.answer == completion.choices[0].message.content
        assert completion.created == int(run.timestamp.timestamp())
        assert len(extension["turn_id"]) == 32
        assert extension["sources"] == [
            {"doc_ref": "guide-matte", "title": "Guide matte", "publisher": "MATTE", "url": None, "access": "authenticated"}
        ]
        assert run.conversation_id == "same-client-id" and run.selected_ministry == "matte"
        assert completion.usage.total_tokens == 14
        assert "CLIENT_SECRET" not in str(runtime.llm.calls) and all(r.temperature == 0 for r in runtime.llm.calls)


async def test_history_validation_pairing_and_text_parts(chat):
    client, runtime, *_ = chat
    messages = [{"role": "assistant", "content": "orphan"}, {"role": "user", "content": "replaced"}]
    for number in range(7):
        messages += [
            {"role": "user", "content": "U" + str(number)},
            {"role": "developer", "content": "IGNORED"},
            {"role": "assistant", "content": "A" + str(number) + SOURCES_MARKER + "old sources"},
        ]
    messages += [
        {"role": "user", "content": [{"type": "text", "text": "Et "}, {"type": "text", "text": "alors ?"}]},
        {"role": "assistant", "content": "after question"},
    ]
    response = await client.post("/v1/chat/completions", json={"model": "assistant-rh", "messages": messages})
    assert response.status_code == 200
    run = next(iter(runtime.runs.rows.values()))
    assert run.question == "Et alors ?"
    prompt = runtime.llm.calls[0].messages[0].content
    # C1 retains U2/A2..U6/A6; the preserved C2 prompt policy reads its last 8 messages.
    assert "U3" in prompt and "A6" in prompt
    assert not any(word in prompt for word in ["orphan", "replaced", "IGNORED", "old sources", "after question", "U1"])
    assert len(runtime.llm.calls[-1].messages) == 2  # C5 non-stream policy is unchanged.


@pytest.mark.parametrize(
    "change,code",
    [
        ({"model": None}, "invalid_request"),
        ({"model": ""}, "invalid_request"),
        ({"model": 42}, "invalid_request"),
        ({"model": "assistant-rh\x00"}, "invalid_request"),
        ({"messages": []}, "invalid_messages"),
        ({"messages": None}, "invalid_messages"),
        ({"messages": {}}, "invalid_messages"),
        ({"messages": [None]}, "invalid_message"),
        ({"messages": [{"content": "x"}]}, "unsupported_role"),
        *[({"messages": [{"role": role, "content": "x"}]}, "unsupported_role") for role in ["tool", "function", {}, None, "unknown"]],
        *[
            ({"messages": [{"role": "user", "content": content}]}, "unsupported_content")
            for content in [None, 2, {}, [{"type": "image_url", "image_url": "x"}], [{"type": "text", "text": 2}], ["x"], "\ud800"]
        ],
        ({"messages": [{"role": "user"}]}, "unsupported_content"),
        ({"messages": [{"role": "assistant", "content": "x"}]}, "missing_user_message"),
        *[({"stream": value}, "invalid_stream") for value in [None, 0, 1, "false", True]],
        *[({"n": value}, "unsupported_n") for value in [0, -1, 2, None, "1", True, 1.0]],
        *[({"stream_options": value}, "invalid_stream_options") for value in [{}, [], "x", 1]],
        ({"stream": True, "stream_options": {"extra": True}}, "unsupported_stream_option"),
        ({"stream": True, "stream_options": {"include_usage": "true"}}, "invalid_stream_options"),
        ({"metadata": []}, "invalid_request"),
        ({"metadata": {"conversation_id": 3}}, "invalid_request"),
        ({"metadata": {"conversation_id": "correlation\x00"}}, "invalid_request"),
        *[
            ({"messages": [{"role": "user", "content": content}]}, "empty_user_message")
            for content in ["", " \t\n\u00a0", [], [{"type": "text", "text": " "}, {"type": "text", "text": "\n"}]]
        ],
        *[
            ({"messages": [{"role": role, "content": content}, *BODY["messages"]]}, "unsupported_content")
            for role in ["user", "assistant", "system", "developer"]
            for content in ["text\x00", [{"type": "text", "text": "text\x00"}]]
        ],
        ({"messages": BODY["messages"] * 33}, "too_many_messages"),
        ({"messages": [{"role": "system", "content": "é" * (MAX_CONTENT // 2 + 1)}, *BODY["messages"]]}, "content_too_large"),
        (
            {"messages": [{"role": "user", "content": [{"type": "text", "text": "x" * MAX_CONTENT}, {"type": "text", "text": "x"}]}]},
            "content_too_large",
        ),
    ],
)
async def test_validation_matrix_never_executes_pipeline(chat, change, code):
    client, runtime, *_ = chat
    response = await client.post("/v1/chat/completions", content=json.dumps({**BODY, **change}).encode())
    assert response.status_code == 422 and response.json()["error"]["code"] == code
    assert response.headers["cache-control"] == "no-store"
    assert not runtime.llm.calls and runtime.config.calls == 0 and not runtime.runs.calls


@pytest.mark.parametrize(
    "content,code",
    [(b"{", "invalid_json"), (b"[]", "invalid_body"), (b"null", "invalid_body"), (b'{"n":NaN}', "invalid_json"), (b"\xff", "invalid_json")],
)
async def test_invalid_json_is_safe(chat, content, code):
    client, runtime, *_ = chat
    response = await client.post("/v1/chat/completions", content=content)
    assert response.status_code == 422 and response.json()["error"]["code"] == code
    assert not runtime.runs.calls


async def test_content_and_body_limits_are_inclusive_with_or_without_length(chat):
    client, runtime, *_ = chat
    for content in ["é" * (MAX_CONTENT // 2), [{"type": "text", "text": "é" * (MAX_CONTENT // 2)}]]:
        response = await client.post("/v1/chat/completions", json={**BODY, "messages": [{"role": "user", "content": content}]})
        assert response.status_code == 200
    data = json.dumps(BODY).encode()
    exact = data + b" " * (MAX_BODY - len(data))
    for overflow in [False, True]:

        async def chunks():
            yield exact[:17]
            yield exact[17:]
            if overflow:
                yield b" "

        count = len(runtime.runs.calls)
        response = await client.post("/v1/chat/completions", content=chunks())
        assert response.status_code == (413 if overflow else 200)
        assert len(runtime.runs.calls) == count + (0 if overflow else 1)
    response = await client.post("/v1/chat/completions", content=exact, headers={"Content-Length": str(MAX_BODY + 1)})
    assert response.status_code == 413


async def test_blank_last_question_does_not_fall_back_to_a_previous_question(chat):
    client, runtime, *_ = chat
    response = await client.post(
        "/v1/chat/completions", json={**BODY, "messages": [*BODY["messages"], {"role": "user", "content": " "}]}
    )
    assert response.status_code == 422 and response.json()["error"]["code"] == "empty_user_message"
    assert not runtime.llm.calls and not runtime.runs.calls and runtime.config.calls == 0


@pytest.mark.parametrize(
    "private_url",
    [
        "https://storage.invalid/private.pdf?X-Amz-Signature=PRIVATE_CAPABILITY",
        "storage.invalid/private.pdf?X-Amz-Signature=PRIVATE_CAPABILITY",
        "s3://private/PRIVATE_CAPABILITY",
        "//storage.invalid/PRIVATE_CAPABILITY",
    ],
)
async def test_generated_private_url_is_redacted_in_http_response_and_persisted_answer(chat, monkeypatch, private_url):
    client, runtime, *_ = chat
    complete = runtime.llm.complete
    public_url = "https://www.legifrance.gouv.fr/codes/article_lc/LEGIARTI123"

    async def echo_link(request):
        result = await complete(request)
        if request.messages[0].content.startswith("GENERATE"):
            return replace(result, text=f"Consulter [le guide]({private_url})[la loi]({public_url}).")
        return result

    monkeypatch.setattr(runtime.llm, "complete", echo_link)
    response = await client.post("/v1/chat/completions", json=BODY)
    assert response.status_code == 200
    answer = response.json()["choices"][0]["message"]["content"]
    run = next(iter(runtime.runs.rows.values()))
    assert run.answer == answer and public_url in answer
    assert "PRIVATE_CAPABILITY" not in response.text and "storage.invalid" not in run.answer
    assert "lien privé retiré" in answer
    assert "PRIVATE_CAPABILITY" not in json.dumps(json_data(run))


async def test_private_url_in_retrieved_title_is_redacted_in_http_and_persisted_run(chat, monkeypatch):
    client, runtime, *_ = chat
    search = runtime.search.search

    async def title_with_private_url(request):
        chunks = await search(request)
        return tuple(
            replace(
                chunk,
                metadata={**chunk.metadata, "source_name": "Guide https://storage.invalid/file?X-Amz-Signature=PRIVATECAPABILITY"},
            )
            for chunk in chunks
        )

    monkeypatch.setattr(runtime.search, "search", title_with_private_url)
    response = await client.post("/v1/chat/completions", json=BODY)
    assert response.status_code == 200
    body = response.json()
    assert body["x_assistant_rh"]["sources"][0]["title"] == "Guide [lien privé retiré]"
    assert body["x_assistant_rh"]["sources"][0]["doc_ref"] == "guide-matte"
    run = runtime.runs.rows[body["x_assistant_rh"]["turn_id"]]
    assert run.answer == body["choices"][0]["message"]["content"]
    assert "PRIVATECAPABILITY" not in response.text and "PRIVATECAPABILITY" not in json.dumps(json_data(run))


async def test_announced_oversize_is_rejected_without_consuming_body(chat):
    client, runtime, *_ = chat

    async def unreadable():
        raise AssertionError("body must not be read")
        yield b""

    response = await client.post("/v1/chat/completions", content=unreadable(), headers={"Content-Length": str(MAX_BODY + 1)})
    assert response.status_code == 413 and not runtime.runs.calls


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
async def test_generated_sources_cannot_override_authoritative_http_or_persisted_sources(chat, monkeypatch, newline):
    client, runtime, *_ = chat
    complete = runtime.llm.complete

    async def invent_source(request):
        result = await complete(request)
        if request.messages[0].content.startswith("GENERATE"):
            marker = SOURCES_MARKER.replace("\n", newline)
            return replace(result, text="Réponse" + marker + "1. [Invented](https://www.legifrance.gouv.fr/fake)")
        return result

    monkeypatch.setattr(runtime.llm, "complete", invent_source)
    response = await client.post("/v1/chat/completions", json=BODY)
    assert response.status_code == 200
    body = response.json()
    answer = body["choices"][0]["message"]["content"]
    run = runtime.runs.rows[body["x_assistant_rh"]["turn_id"]]
    assert answer == run.answer == "Réponse" + SOURCES_MARKER + "1. Guide matte — MATTE"
    assert [source["doc_ref"] for source in body["x_assistant_rh"]["sources"]] == [source.doc_ref for source in run.sources] == ["guide-matte"]


@pytest.mark.parametrize(
    "heading_error,chunks_error,status,code",
    [
        (DatabaseUnavailable, DatabaseUnavailable, 503, "service_unavailable"),
        (DatabaseUnavailable, None, 503, "service_unavailable"),
        (DatabaseUnavailable, DatabaseFailure, 500, "internal_error"),
        (DatabaseFailure, DatabaseFailure, 500, "internal_error"),
    ],
)
async def test_scoped_retrieval_outages_are_503_and_internal_errors_remain_500(chat, monkeypatch, heading_error, chunks_error, status, code):
    client, runtime, *_ = chat
    search = runtime.search.search

    async def fail_search(request):
        error_type = heading_error if request.mode == "heading" else chunks_error
        if error_type is not None:
            error = error_type()
            error.args = ("postgresql://private-user:SECRET@private-host/db",)
            raise error
        return await search(request)

    monkeypatch.setattr(runtime.search, "search", fail_search)
    response = await client.post("/v1/chat/completions", json=BODY)
    assert response.status_code == status and response.json()["error"]["code"] == code
    assert "SECRET" not in response.text and "private-host" not in response.text
    run = next(iter(runtime.runs.rows.values()))
    assert run.status == "failed" and run.answer == "" and run.sources == ()
    assert not any(call.messages[0].content.startswith("GENERATE") for call in runtime.llm.calls)


@pytest.mark.parametrize("failure_at", ["configuration", "retrieval", "finalization"])
@pytest.mark.parametrize(
    "persistence_error,status,code", [(DatabaseUnavailable, 503, "service_unavailable"), (DatabaseFailure, 500, "internal_error")]
)
async def test_failed_finalization_preserves_database_outages(chat, monkeypatch, caplog, failure_at, persistence_error, status, code):
    client, runtime, *_ = chat
    secret = "postgresql://private-user:SECRET@private-host/db"
    error = persistence_error()
    error.args = (secret,)
    runtime.runs.failure = error

    async def unavailable(*args):
        raise DatabaseUnavailable()

    if failure_at == "configuration":
        monkeypatch.setattr(runtime.config, "load", unavailable)
    elif failure_at == "retrieval":
        monkeypatch.setattr(runtime.search, "search", unavailable)

    response = await client.post("/v1/chat/completions", json=BODY)
    assert response.status_code == status and response.json()["error"]["code"] == code
    assert response.headers["cache-control"] == "no-store"
    assert "SECRET" not in response.text and secret not in caplog.text
    assert not runtime.runs.rows
    # Configuration outages retain the existing default-config fallback.
    assert [run.status for run in runtime.runs.calls] == (["failed"] if failure_at == "retrieval" else ["completed", "failed"])
    failed = runtime.runs.calls[-1]
    assert failed.answer == "" and failed.sources == ()


@pytest.mark.parametrize(
    "headers",
    [
        {"Authorization": ""},
        {"Authorization": "Basic secret"},
        {"Authorization": "Bearer invalid"},
        [("Authorization", "Bearer one"), ("Authorization", "Bearer two")],
    ],
)
async def test_auth_precedes_execution(chat, headers):
    client, runtime, *_ = chat
    response = await client.post("/v1/chat/completions", json=BODY, headers=headers)
    assert response.status_code == 401 and response.json()["error"]["code"] == "invalid_api_key"
    assert response.headers["www-authenticate"] == "Bearer" and not runtime.runs.calls


@pytest.mark.parametrize("change", ["expired", "revoked", "revision"])
async def test_unusable_sessions_are_rejected(chat, change):
    client, runtime, auth, issued = chat
    if change == "expired":
        auth.clock.value += timedelta(hours=8)
    elif change == "revoked":
        await auth.logout(issued.context)
    else:
        auth.groups.rows["beta"] = replace(auth.groups.rows["beta"], credential_revision=9)
    response = await client.post("/v1/chat/completions", json=BODY)
    assert response.status_code == 401 and not runtime.runs.calls


@pytest.mark.parametrize("model,status,code", [("unknown", 404, "model_not_found"), ("assistant-rh-masa", 403, "ministry_forbidden")])
async def test_sdk_model_errors_on_real_route(chat, model, status, code):
    client, runtime, _, issued = chat
    async with openai.AsyncOpenAI(api_key=issued.access_token, base_url="http://test/v1", http_client=client, max_retries=0) as sdk:
        with pytest.raises(openai.APIStatusError) as error:
            await sdk.chat.completions.create(model=model, messages=BODY["messages"])
        assert error.value.status_code == status and error.value.code == code
    assert not runtime.runs.calls and runtime.config.calls == 0


async def test_corrupt_policy_and_internal_failures_are_safe(chat):
    client, runtime, auth, _ = chat
    group = auth.groups.rows["beta"]
    auth.groups.rows["beta"] = replace(group, default_ministry="")
    response = await client.post("/v1/chat/completions", json=BODY)
    assert response.status_code == 500 and response.json()["error"]["code"] == "ministry_configuration_error"
    assert not runtime.runs.calls
    auth.groups.rows["beta"] = group
    runtime.llm.failure = RuntimeError("SECRET postgres://password")
    response = await client.post("/v1/chat/completions", json=BODY)
    assert response.status_code == 500 and response.json()["error"]["code"] == "internal_error"
    assert "SECRET" not in response.text and "password" not in response.text
    assert next(iter(runtime.runs.rows.values())).status == "failed"


async def test_http_waits_for_commit_and_persistence_failure_is_never_success(chat):
    client, runtime, *_ = chat
    runtime.runs.entered, runtime.runs.release = asyncio.Event(), asyncio.Event()
    pending = asyncio.create_task(client.post("/v1/chat/completions", json=BODY))
    await runtime.runs.entered.wait()
    assert not pending.done() and not runtime.runs.rows
    runtime.runs.release.set()
    assert (await pending).status_code == 200
    runtime.runs.failure = DatabaseFailure()
    response = await client.post("/v1/chat/completions", json=BODY)
    assert response.status_code == 500 and response.json()["error"]["code"] == "internal_error"


async def test_explicit_authorized_model_and_no_answer(chat):
    client, runtime, *_ = chat
    response = await client.post("/v1/chat/completions", json={**BODY, "model": "assistant-rh-mi"})
    assert response.status_code == 200 and response.json()["model"] == "assistant-rh-mi"
    assert {r.source for r in runtime.search.calls} == {"mi", "service_public", "dgafp"}
    runtime.llm.selector_responses = ['{"selected_ids":[]}', '{"selected_ids":[]}']
    response = await client.post("/v1/chat/completions", json=BODY)
    assert response.status_code == 200 and response.json()["x_assistant_rh"]["sources"] == []
    assert SOURCES_MARKER not in response.json()["choices"][0]["message"]["content"]


async def test_five_complete_pairs_and_orphans_follow_c1_exactly():
    from assistant_rh_api.handlers.chat_body import validate_chat

    messages = []
    for index in range(7):
        messages.extend([{"role": "user", "content": f"U{index}"}, {"role": "assistant", "content": f"A{index}"}])
    messages.append({"role": "user", "content": "Question"})
    parsed = validate_chat({**BODY, "messages": messages})
    assert [message.content for message in parsed.history] == [f"{role}{index}" for index in range(2, 7) for role in ("U", "A")]
    parsed = validate_chat(
        {
            **BODY,
            "messages": [
                {"role": "system", "content": "S"},
                {"role": "assistant", "content": "A0"},
                {"role": "user", "content": "U1"},
                {"role": "user", "content": "U2"},
                {"role": "developer", "content": "D"},
                {"role": "assistant", "content": "A2"},
                {"role": "assistant", "content": "A3"},
                {"role": "user", "content": "U3"},
                {"role": "user", "content": "U4"},
                {"role": "assistant", "content": "A4"},
            ],
        }
    )
    assert parsed.question == "U4" and [message.content for message in parsed.history] == ["U2", "A2"]


async def test_exact_message_count_and_declared_body_size_are_accepted(chat):
    client, *_ = chat
    messages = [{"role": "system", "content": "ignored"}] * 31 + BODY["messages"]
    payload = json.dumps({**BODY, "messages": messages, "metadata": None, "stream_options": None}).encode()
    payload += b" " * (MAX_BODY - len(payload))
    response = await client.post("/v1/chat/completions", content=payload)
    assert response.status_code == 200


async def test_missing_bearer_and_unexpected_error_use_safe_envelopes():
    auth = service()
    runtime = Runtime()
    app = create_app(auth_service=auth, chat_service=runtime.service)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test") as client:
        response = await client.post("/v1/chat/completions", json=BODY)
        assert response.status_code == 401 and not runtime.runs.calls
        issued = await auth.login("beta", "password", "source")

        class BrokenModels:
            def resolve(self, *args):
                raise RuntimeError("SECRET")

        app.state.model_service = BrokenModels()
        response = await client.post("/v1/chat/completions", json=BODY, headers={"Authorization": "Bearer " + issued.access_token})
        assert response.status_code == 500 and response.json()["error"]["code"] == "internal_error"
        assert "SECRET" not in response.text and response.headers["cache-control"] == "no-store"
