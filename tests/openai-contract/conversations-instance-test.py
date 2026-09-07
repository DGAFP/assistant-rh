"""Mounted into the pinned Conversations image for the assistant-rh#443 proof.

This file is intentionally not named ``test_*.py`` so the Assistant RH pytest
suite does not collect it.  The spike runbook mounts it into Conversations'
own test tree, where that project's fixtures and Django application are present.
"""

from __future__ import annotations

import json
import os

import pytest
from chat.factories import ChatConversationFactory
from chat.llm_configuration import load_llm_configuration
from chat.tests.utils import assert_data_stream_response
from openai import APIError

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def issue443_settings(settings):
    settings.LLM_CONFIGURATIONS = load_llm_configuration(os.environ["LLM_CONFIGURATION_FILE_PATH"])
    settings.LLM_DEFAULT_MODEL_HRID = "assistant-rh-matte"
    settings.LLM_SUMMARIZATION_MODEL_HRID = "assistant-rh-summarization"
    settings.AUTO_TITLE_AFTER_USER_MESSAGES = None
    settings.LANGFUSE_ENABLED = False
    return settings


def test_instance_lists_models_and_consumes_completion(api_client, hello_conversation_data):
    conversation = ChatConversationFactory(owner__language="fr-fr")
    api_client.force_authenticate(user=conversation.owner)
    bearer = os.environ["ASSISTANT_RH_CONVERSATIONS_API_KEY"]

    models_response = api_client.get("/api/v1.0/llm-configuration/")
    assert models_response.status_code == 200
    assert bearer.encode() not in models_response.content
    models = {item["hrid"]: item for item in models_response.json()["models"]}
    assert models["assistant-rh-matte"]["is_default"] is True
    assert models["assistant-rh-mso"]["supports_image"] is False

    url = f"/api/v1.0/chats/{conversation.pk}/conversation/?protocol=data&model_hrid=assistant-rh-matte"
    response = api_client.post(url, hello_conversation_data, format="json")
    assert_data_stream_response(response)
    wire = b"".join(response.streaming_content).decode("utf-8")
    assert "Sources" in wire
    assert "guide-conges" in wire
    assert bearer not in wire

    conversation.refresh_from_db()
    assert "**Sources :**" in conversation.messages[-1].content
    assert bearer not in conversation.messages[-1].content
    assert bearer not in json.dumps(conversation.pydantic_messages, ensure_ascii=False)
    provider_responses = [message for message in conversation.pydantic_messages if message["kind"] == "response"]
    assert provider_responses[-1]["provider_response_id"].startswith("chatcmpl-replay-")
    assert provider_responses[-1]["provider_name"] == "openai"


def test_instance_currently_propagates_post_header_openai_error(api_client, hello_conversation_data):
    conversation = ChatConversationFactory(owner__language="fr-fr")
    api_client.force_authenticate(user=conversation.owner)
    user_message = hello_conversation_data["messages"][0]
    user_message["content"] = "__simulate_stream_error__"
    user_message["parts"] = [{"text": "__simulate_stream_error__", "type": "text"}]

    url = f"/api/v1.0/chats/{conversation.pk}/conversation/?protocol=data&model_hrid=assistant-rh-matte"
    response = api_client.post(url, hello_conversation_data, format="json")
    assert_data_stream_response(response)
    with pytest.raises(APIError, match="Replay failure after response headers") as caught:
        b"".join(response.streaming_content)
    assert caught.value.code == "stream_error"
