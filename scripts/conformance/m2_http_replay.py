"""Replay the production JSON route with recorded I/O and an ephemeral test issuer."""

from unittest.mock import patch

import httpx
from assistant_rh_api.handlers.app import create_app

from apps.api.tests.auth_fakes import Signer, delegated_service
from scripts.conformance.m0b_values import assert_equal, plain


async def complete_over_http(service, request, auth):
    ministry = auth.group.default_ministry
    signer = Signer()
    claims = signer.claims(models=[request.model], ministry=ministry)
    token = signer.sign(claims)
    app = create_app(auth_service=delegated_service(signer), chat_service=service)
    messages = [*plain(request.history), {"role": "user", "content": request.question}]

    completed = []
    complete = service.complete

    async def capture(*args, **kwargs):
        outcome = await complete(*args, **kwargs)
        completed.append(outcome)
        return outcome

    with patch.object(service, "complete", capture):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://replay.invalid", trust_env=False) as client:
            response = await client.post(
                "/v1/chat/completions",
                json={"model": request.model, "messages": messages, "stream": False},
                headers={"Authorization": "Bearer " + token},
            )
    assert_equal(response.status_code, 200, "HTTP status")
    assert_equal(len(completed), 1, "HTTP completions")
    run, result = completed[0]
    assert_equal(str(run.author_user_id), claims["sub"], "HTTP run author")
    assert_equal(run.selected_ministry, ministry, "HTTP run ministry")
    assert_equal(
        response.json(),
        {
            "id": f"chatcmpl-{run.turn_id}",
            "object": "chat.completion",
            "created": int(run.timestamp.timestamp()),
            "model": run.model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": run.answer}, "finish_reason": "stop"}],
            "usage": plain(result.usage),
            "x_assistant_rh": {
                "turn_id": run.turn_id,
                "ministry": run.selected_ministry,
                "sources": [
                    {
                        "doc_ref": source.doc_ref,
                        "publisher": source.publisher,
                        "title": source.title,
                        "access": source.access,
                        "url": source.url or None,
                    }
                    for source in run.sources
                ],
            },
        },
        "HTTP JSON",
    )
    return run, result
