import json
from dataclasses import replace

import pytest
from assistant_rh_api.core.errors import ApplicationError
from assistant_rh_api.core.models.chat import ChatInput
from assistant_rh_api.core.trace_values import MAX_TRACE_BYTES, MAX_TRACE_ITEMS, MAX_TRACE_TEXT, PRIVATE_URL, TRUNCATED, trace_payload
from assistant_rh_api.db.run_store import json_data

from apps.api.tests.auth_fakes import service
from apps.api.tests.chat_fakes import Runtime

SIGNED_URL = "https://storage.invalid/guide?X-Amz-Signature=synthetic-secret"
PUBLIC_URL = "https://www.service-public.gouv.fr/particuliers/vosdroits/F1"


@pytest.mark.anyio
@pytest.mark.parametrize("fail_generation", [False, True])
@pytest.mark.parametrize("url", [SIGNED_URL, "s3://private/synthetic-secret", "//storage.invalid/synthetic-secret", SIGNED_URL.removeprefix("https://")])
async def test_persisted_run_redacts_urls_without_changing_provider_input(fail_generation, url):
    runtime = Runtime()
    search = runtime.search.search
    complete = runtime.llm.complete
    content = f"Texte du guide : [document]({url})\n" + "x" * 10_000 + " FIN_DU_DOCUMENT"

    async def documents(request):
        chunks = await search(request)
        return tuple(
            replace(chunk, text=content, metadata={**chunk.metadata, "doc_url": url, "future_secret": "DO_NOT_CAPTURE"}) for chunk in chunks
        )

    async def generate(request):
        if fail_generation and request.messages[0].content.startswith("GENERATE"):
            raise RuntimeError("provider details must not be logged")
        result = await complete(request)
        return replace(result, text=f"Consulter {url}.") if request.messages[0].content.startswith("GENERATE") else result

    runtime.search.search = documents
    runtime.llm.complete = generate
    auth = (await service().login("beta", "password", "local")).context
    if fail_generation:
        with pytest.raises(ApplicationError):
            await runtime.service.complete(ChatInput("assistant-rh", "Question"), auth)
    else:
        await runtime.service.complete(ChatInput("assistant-rh", "Question"), auth)
        generation_prompt = runtime.llm.calls[-1].messages[-1].content
        assert url in generation_prompt and "FIN_DU_DOCUMENT" in generation_prompt

    run = next(iter(runtime.runs.rows.values()))
    persisted = json.dumps(json_data(run))
    assert "synthetic-secret" not in persisted and "X-Amz-Signature" not in persisted
    assert "DO_NOT_CAPTURE" not in persisted and "provider details" not in persisted
    assert PRIVATE_URL in persisted and TRUNCATED in persisted
    for event in run.events:
        assert len(json.dumps(json_data(event.output_ref)).encode()) <= MAX_TRACE_BYTES


@pytest.mark.parametrize("url", [SIGNED_URL, "https://user:synthetic-secret@example.org/file", PUBLIC_URL + "?token=synthetic-secret"])
def test_trace_redacts_private_urls_in_nested_text_and_preserves_canonical_links(url):
    original = {"content": f"Lien {url}", "nested": ({"public": PUBLIC_URL, "metadata": {"doc_url": url}},)}
    trace = trace_payload(original)
    assert trace["nested"][0]["public"] == PUBLIC_URL
    assert trace["nested"][0]["metadata"]["doc_url"] == PRIVATE_URL
    assert "synthetic-secret" not in json.dumps(json_data(trace))
    assert original["nested"][0]["metadata"]["doc_url"] == url


def test_trace_limits_utf8_size_collections_and_depth_with_visible_truncation():
    text = trace_payload("é" * (MAX_TRACE_TEXT + 1))
    assert text.endswith(TRUNCATED)
    many = trace_payload(tuple(range(MAX_TRACE_ITEMS + 1)))
    assert many[-1] == TRUNCATED
    large = trace_payload({str(index): "é" * MAX_TRACE_TEXT for index in range(MAX_TRACE_ITEMS)})
    assert large["truncated"] is True and large["reason"] == "trace_size_limit"
    assert len(json.dumps(json_data(large)).encode()) <= MAX_TRACE_BYTES
    nested = "leaf"
    for _ in range(20):
        nested = {"child": nested}
    assert TRUNCATED in json.dumps(json_data(trace_payload(nested)))


@pytest.mark.anyio
async def test_run_traces_keep_provider_diagnostics_without_embedding_vectors_or_duplicate_documents():
    runtime = Runtime()
    auth = (await service().login("beta", "password", "local")).context
    run, _ = await runtime.service.complete(ChatInput("assistant-rh", "Question"), auth)
    retrieval = next(e.output_ref for e in run.events if e.stage == "retriever")
    assert retrieval["embedding"]["dimensions"] == 1
    assert "vector" not in retrieval["embedding"]
    assert retrieval["chunks"][0]["chunk_id"] == "matte-chunk"
    assert "text" not in retrieval["chunks"][0]
    generation = run.events[-1].output_ref["diagnostics"]
    assert generation["outcome"]["provider"] == "albert"
    assert generation["outcome"]["usage"]["total_tokens"] == 14
    assert generation["request"]["messages"][-1]["role"] == "user"
