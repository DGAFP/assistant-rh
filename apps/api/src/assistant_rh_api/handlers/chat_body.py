"""C1 bounded request validation, before model resolution or pipeline execution."""

import json

from fastapi import Request

from assistant_rh_api.core.chat import SOURCES_MARKER
from assistant_rh_api.core.models.chat import ChatInput
from assistant_rh_api.core.models.inference import Message

MAX_BODY = 1_048_576
MAX_CONTENT = 65_536


class ChatRequestError(Exception):
    def __init__(self, code: str, status: int = 422) -> None:
        self.code = code
        self.status = status


async def read_chat_body(request: Request) -> dict:
    try:
        lengths = request.headers.getlist("content-length")
        if lengths and (len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdigit()):
            raise ChatRequestError("invalid_request")
        if lengths and int(lengths[0]) > MAX_BODY:
            raise ChatRequestError("request_too_large", 413)
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > MAX_BODY:
                raise ChatRequestError("request_too_large", 413)
            body.extend(chunk)

        def invalid_constant(value: str) -> None:
            raise ValueError("invalid JSON constant")

        payload = json.loads(body.decode("utf-8"), parse_constant=invalid_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise ChatRequestError("invalid_json") from None
    if not isinstance(payload, dict):
        raise ChatRequestError("invalid_body")
    return payload


def validate_chat(payload: dict) -> ChatInput:
    model = payload.get("model")
    if not isinstance(model, str) or not model:
        raise ChatRequestError("invalid_request")
    stream = payload.get("stream", False)
    if type(stream) is not bool:
        raise ChatRequestError("invalid_stream")
    n = payload.get("n", 1)
    if type(n) is not int or n != 1:
        raise ChatRequestError("unsupported_n")
    options = payload.get("stream_options")
    if options is not None:
        if not stream or not isinstance(options, dict) or type(options.get("include_usage", False)) is not bool:
            raise ChatRequestError("invalid_stream_options")
        if set(options) - {"include_usage"}:
            raise ChatRequestError("unsupported_stream_option")
    metadata = payload.get("metadata")
    if metadata is not None and not isinstance(metadata, dict):
        raise ChatRequestError("invalid_request")
    correlation = (metadata or {}).get("conversation_id")
    if correlation is not None and not isinstance(correlation, str):
        raise ChatRequestError("invalid_request")
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        raise ChatRequestError("invalid_messages")
    if len(messages) > 32:
        raise ChatRequestError("too_many_messages")
    parsed = []
    for message in messages:
        if not isinstance(message, dict):
            raise ChatRequestError("invalid_message")
        role = message.get("role")
        if role not in ("user", "assistant", "system", "developer"):
            raise ChatRequestError("unsupported_role")
        content = message.get("content")
        if isinstance(content, list):
            if any(not isinstance(part, dict) or part.get("type") != "text" or not isinstance(part.get("text"), str) for part in content):
                raise ChatRequestError("unsupported_content")
            content = "".join(part["text"] for part in content)
        if not isinstance(content, str):
            raise ChatRequestError("unsupported_content")
        try:
            size = len(content.encode("utf-8"))
        except UnicodeError:
            raise ChatRequestError("unsupported_content") from None
        if size > MAX_CONTENT:
            raise ChatRequestError("content_too_large")
        parsed.append((role, content))
    last_user = next((index for index in range(len(parsed) - 1, -1, -1) if parsed[index][0] == "user"), None)
    if last_user is None:
        raise ChatRequestError("missing_user_message")
    history: list[Message] = []
    pending = None
    for role, content in parsed[:last_user]:
        if role == "user":
            pending = content
        elif role == "assistant" and pending is not None:
            history.extend((Message("user", pending), Message("assistant", content.split(SOURCES_MARKER, 1)[0])))
            pending = None
    # C7 will consume the same validated input. Until that transport exists,
    # reject stream=true explicitly rather than returning misleading JSON 200.
    if stream:
        raise ChatRequestError("invalid_stream")
    try:
        model.encode("utf-8")
        (correlation or "").encode("utf-8")
    except UnicodeError:
        raise ChatRequestError("invalid_request") from None
    return ChatInput(model, parsed[last_user][1], tuple(history[-10:]), correlation or "")
