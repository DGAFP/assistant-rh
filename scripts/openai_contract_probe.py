"""Replay and probe the Assistant RH OpenAI-compatible transport contract.

The replay server deliberately contains no RAG logic.  It lets client integrations
exercise models, Chat Completions, SSE framing, errors, limits, and extensions before
``apps/api`` exists.  The probe uses the same OpenAI Python SDK as the application.

Examples:

    ASSISTANT_RH_CONTRACT_API_KEY="$(openssl rand -hex 24)" \
      uv run python scripts/openai_contract_probe.py serve

    ASSISTANT_RH_CONTRACT_API_KEY="..." \
      uv run python scripts/openai_contract_probe.py probe \
        --base-url http://127.0.0.1:8765/v1 --replay-errors

The API key is read only from an environment variable and is never printed or
included in the JSON report.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import threading
import time
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from http import HTTPStatus
from http.client import HTTPConnection, HTTPSConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import openai
from openai import APIError, APIStatusError, OpenAI

API_KEY_ENV = "ASSISTANT_RH_CONTRACT_API_KEY"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_MODEL = "assistant-rh-matte"
DEFAULT_MINISTRY = "matte"

MAX_HTTP_BODY_BYTES = 1_048_576
MAX_MESSAGES = 32
MAX_CONTENT_BYTES = 65_536
MAX_HISTORY_TURNS = 5

SOURCE_MARKER = "\n\n---\n**Sources :**\n"
STREAM_ERROR_SENTINEL = "__simulate_stream_error__"
DISCONNECT_SENTINEL = "__simulate_disconnect__"

MODEL_CATALOG = {
    "assistant-rh-matte": "matte",
    "assistant-rh-mso": "mso",
    "assistant-rh-mi": "mi",
}
AUTHORIZED_MODELS = ("assistant-rh-matte", "assistant-rh-mso")


class ProbeFailure(RuntimeError):
    """Raised when a response does not satisfy the selected contract."""


@dataclass
class ReplayState:
    """Sanitized observations from the replay server.

    Authorization headers and complete request bodies are intentionally never kept.
    """

    requests: list[dict[str, Any]] = field(default_factory=list)
    last_effective_history: list[dict[str, str]] = field(default_factory=list)
    stream_error_sent: threading.Event = field(default_factory=threading.Event)
    client_disconnected: threading.Event = field(default_factory=threading.Event)


class ReplayHTTPServer(ThreadingHTTPServer):
    """HTTP server carrying immutable replay configuration and sanitized state."""

    daemon_threads = True

    def __init__(self, server_address: tuple[str, int], api_key: str):
        super().__init__(server_address, ReplayRequestHandler)
        self.api_key = api_key
        self.state = ReplayState()
        self._turn_lock = threading.Lock()
        self._turn_number = 0

    def next_turn(self) -> tuple[str, str]:
        with self._turn_lock:
            self._turn_number += 1
            suffix = f"replay-{self._turn_number:04d}"
        return suffix, f"chatcmpl-{suffix}"


def _error_body(message: str, code: str, *, param: str | None = None, error_type: str = "invalid_request_error") -> dict[str, Any]:
    error: dict[str, Any] = {"message": message, "type": error_type, "code": code}
    if param is not None:
        error["param"] = param
    return {"error": error}


def _strip_sources(content: str) -> str:
    return content.split(SOURCE_MARKER, maxsplit=1)[0]


class ReplayRequestHandler(BaseHTTPRequestHandler):
    """Small OpenAI-compatible replay handler used by the A2 client spike."""

    server: ReplayHTTPServer
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        # The default access log has no header values, but disabling it entirely
        # makes the no-secret-in-artifacts property explicit and durable.
        return

    def _send_json(self, status: int, body: dict[str, Any]) -> None:
        payload = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(payload)
        self.close_connection = True

    def _authorized(self) -> bool:
        expected = f"Bearer {self.server.api_key}"
        if self.headers.get("Authorization") == expected:
            return True
        self._send_json(
            HTTPStatus.UNAUTHORIZED,
            _error_body("Invalid API key", "invalid_api_key"),
        )
        return False

    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/") != "/v1/models":
            self._send_json(HTTPStatus.NOT_FOUND, _error_body("Route not found", "not_found"))
            return
        if not self._authorized():
            return

        self.server.state.requests.append({"method": "GET", "path": "/v1/models"})
        self._send_json(
            HTTPStatus.OK,
            {
                "object": "list",
                "data": [
                    {"id": model, "object": "model", "created": 1_755_734_400, "owned_by": "assistant-rh"}
                    for model in AUTHORIZED_MODELS
                ],
            },
        )

    def do_POST(self) -> None:  # noqa: N802
        if self.path.rstrip("/") != "/v1/chat/completions":
            self._send_json(HTTPStatus.NOT_FOUND, _error_body("Route not found", "not_found"))
            return

        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send_json(HTTPStatus.BAD_REQUEST, _error_body("Invalid Content-Length", "invalid_content_length"))
            return
        if content_length > MAX_HTTP_BODY_BYTES:
            self._send_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, _error_body("Request body is too large", "request_too_large"))
            return
        if not self._authorized():
            return

        try:
            raw = self.rfile.read(content_length)
            payload = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json(HTTPStatus.UNPROCESSABLE_ENTITY, _error_body("Body must be valid JSON", "invalid_json"))
            return
        if not isinstance(payload, dict):
            self._send_json(HTTPStatus.UNPROCESSABLE_ENTITY, _error_body("Body must be an object", "invalid_body"))
            return

        validated = self._validate_completion(payload)
        if validated is None:
            return
        model, ministry, messages, question = validated
        stream = payload.get("stream") is True
        include_usage = bool((payload.get("stream_options") or {}).get("include_usage"))
        self.server.state.requests.append(
            {
                "method": "POST",
                "path": "/v1/chat/completions",
                "model": model,
                "stream": stream,
                "message_count": len(messages),
                "body_bytes": content_length,
                "request_fields": sorted(payload),
                "roles": [message["role"] for message in messages],
                "tool_count": len(payload.get("tools") or []),
                "include_usage": include_usage,
            }
        )

        turn_id, completion_id = self.server.next_turn()
        answer = self._answer(question)
        extension = {
            "turn_id": turn_id,
            "ministry": ministry,
            "sources": [
                {
                    "title": "Guide des congés de formation",
                    "url": "https://example.gouv.fr/guide-conges",
                    "publisher": "MATTE",
                    "doc_ref": "guide-conges",
                }
            ],
        }
        if stream:
            self._send_stream(
                payload=payload,
                answer=answer,
                completion_id=completion_id,
                model=model,
                extension=extension,
                include_usage=include_usage,
                question=question,
            )
        else:
            self._send_json(
                HTTPStatus.OK,
                {
                    "id": completion_id,
                    "object": "chat.completion",
                    "created": 1_755_734_400,
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": answer},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                    "x_assistant_rh": extension,
                },
            )

    def _validate_completion(self, payload: dict[str, Any]) -> tuple[str, str, list[dict[str, Any]], str] | None:
        requested_model = payload.get("model")
        if requested_model == "assistant-rh":
            requested_model = DEFAULT_MODEL
        if requested_model not in MODEL_CATALOG:
            self._send_json(HTTPStatus.NOT_FOUND, _error_body("Model not found", "model_not_found", param="model"))
            return None
        if requested_model not in AUTHORIZED_MODELS:
            self._send_json(HTTPStatus.FORBIDDEN, _error_body("Model is not allowed", "model_forbidden", param="model"))
            return None

        if payload.get("n", 1) != 1:
            self._send_json(HTTPStatus.UNPROCESSABLE_ENTITY, _error_body("Only n=1 is supported", "unsupported_n", param="n"))
            return None

        stream = payload.get("stream", False)
        if not isinstance(stream, bool):
            self._send_json(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                _error_body("stream must be a boolean", "invalid_stream", param="stream"),
            )
            return None
        stream_options = payload.get("stream_options")
        if stream_options is not None:
            if not stream or not isinstance(stream_options, dict):
                self._send_json(
                    HTTPStatus.UNPROCESSABLE_ENTITY,
                    _error_body(
                        "stream_options requires stream=true and must be an object",
                        "invalid_stream_options",
                        param="stream_options",
                    ),
                )
                return None
            unknown_stream_options = set(stream_options) - {"include_usage"}
            if unknown_stream_options:
                self._send_json(
                    HTTPStatus.UNPROCESSABLE_ENTITY,
                    _error_body(
                        "Unsupported stream option",
                        "unsupported_stream_option",
                        param=f"stream_options.{sorted(unknown_stream_options)[0]}",
                    ),
                )
                return None
            if not isinstance(stream_options.get("include_usage", False), bool):
                self._send_json(
                    HTTPStatus.UNPROCESSABLE_ENTITY,
                    _error_body(
                        "include_usage must be a boolean",
                        "invalid_stream_options",
                        param="stream_options.include_usage",
                    ),
                )
                return None

        messages = payload.get("messages")
        if not isinstance(messages, list) or not messages:
            self._send_json(HTTPStatus.UNPROCESSABLE_ENTITY, _error_body("messages must be a non-empty list", "invalid_messages"))
            return None
        if len(messages) > MAX_MESSAGES:
            self._send_json(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                _error_body(f"At most {MAX_MESSAGES} messages are accepted", "too_many_messages", param="messages"),
            )
            return None

        latest_user_index: int | None = None
        normalized_messages: list[dict[str, str]] = []
        for index, message in enumerate(messages):
            if not isinstance(message, dict):
                self._send_json(HTTPStatus.UNPROCESSABLE_ENTITY, _error_body("Each message must be an object", "invalid_message"))
                return None
            role = message.get("role")
            if role not in {"system", "developer", "user", "assistant"}:
                self._send_json(
                    HTTPStatus.UNPROCESSABLE_ENTITY,
                    _error_body("Unsupported message role", "unsupported_role", param=f"messages.{index}.role"),
                )
                return None
            content = self._normalize_text_content(message.get("content"))
            if content is None:
                self._send_json(
                    HTTPStatus.UNPROCESSABLE_ENTITY,
                    _error_body(
                        "Only string or text-part-array content is supported",
                        "unsupported_content",
                        param=f"messages.{index}.content",
                    ),
                )
                return None
            if len(content.encode("utf-8")) > MAX_CONTENT_BYTES:
                self._send_json(
                    HTTPStatus.UNPROCESSABLE_ENTITY,
                    _error_body(
                        f"Message content exceeds {MAX_CONTENT_BYTES} UTF-8 bytes",
                        "content_too_large",
                        param=f"messages.{index}.content",
                    ),
                )
                return None
            normalized_messages.append({"role": role, "content": content})
            if role == "user":
                latest_user_index = index

        if latest_user_index is None:
            self._send_json(HTTPStatus.UNPROCESSABLE_ENTITY, _error_body("A user message is required", "missing_user_message"))
            return None

        effective = []
        for message in normalized_messages[:latest_user_index]:
            if message["role"] not in {"user", "assistant"}:
                continue
            content = _strip_sources(message["content"]) if message["role"] == "assistant" else message["content"]
            effective.append({"role": message["role"], "content": content})
        self.server.state.last_effective_history = effective[-(MAX_HISTORY_TURNS * 2) :]

        return (
            str(requested_model),
            MODEL_CATALOG[str(requested_model)],
            normalized_messages,
            normalized_messages[latest_user_index]["content"],
        )

    @staticmethod
    def _normalize_text_content(content: Any) -> str | None:
        if isinstance(content, str):
            return content
        if not isinstance(content, list):
            return None
        text_parts: list[str] = []
        for part in content:
            if not isinstance(part, dict) or part.get("type") != "text" or not isinstance(part.get("text"), str):
                return None
            text_parts.append(part["text"])
        return "".join(text_parts)

    @staticmethod
    def _answer(question: str) -> str:
        summarized_question = question.replace("\n", " ")[:120]
        return (
            f"Réponse replay pour : {summarized_question}"
            f"{SOURCE_MARKER}1. [Guide des congés de formation](https://example.gouv.fr/guide-conges) — MATTE"
        )

    def _sse_headers(self) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()

    def _write_sse(self, value: dict[str, Any] | str) -> None:
        data = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
        self.wfile.flush()

    @staticmethod
    def _chunk(completion_id: str, model: str, choices: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
        return {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": 1_755_734_400,
            "model": model,
            "choices": choices,
            **extra,
        }

    def _send_stream(
        self,
        *,
        payload: dict[str, Any],
        answer: str,
        completion_id: str,
        model: str,
        extension: dict[str, Any],
        include_usage: bool,
        question: str,
    ) -> None:
        self._sse_headers()
        try:
            # Comments are legal SSE keep-alives and are ignored by both tested clients.
            self.wfile.write(b": ping\n\n")
            self.wfile.flush()
            self._write_sse(
                self._chunk(
                    completion_id,
                    model,
                    [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}],
                    usage=None if include_usage else None,
                )
            )

            if question == STREAM_ERROR_SENTINEL:
                self._write_sse(_error_body("Replay failure after response headers", "stream_error", error_type="server_error"))
                self.server.state.stream_error_sent.set()
                return

            if question == DISCONNECT_SENTINEL:
                for _ in range(128):
                    self._write_sse(
                        self._chunk(
                            completion_id,
                            model,
                            [{"index": 0, "delta": {"content": "x" * 16_384}, "finish_reason": None}],
                        )
                    )
                    time.sleep(0.01)
                return

            midpoint = max(1, len(answer) // 2)
            self._write_sse(
                self._chunk(
                    completion_id,
                    model,
                    [{"index": 0, "delta": {"content": answer[:midpoint]}, "finish_reason": None}],
                    usage=None if include_usage else None,
                )
            )
            self.wfile.write(b": ping\n\n")
            self.wfile.flush()
            self._write_sse(
                self._chunk(
                    completion_id,
                    model,
                    [{"index": 0, "delta": {"content": answer[midpoint:]}, "finish_reason": None}],
                    usage=None if include_usage else None,
                )
            )
            self._write_sse(
                self._chunk(
                    completion_id,
                    model,
                    [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    x_assistant_rh=extension,
                    usage=None if include_usage else None,
                )
            )
            if include_usage:
                self._write_sse(
                    self._chunk(
                        completion_id,
                        model,
                        [],
                        usage={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                    )
                )
            self._write_sse("[DONE]")
        except (BrokenPipeError, ConnectionResetError):
            self.server.state.client_disconnected.set()
        finally:
            self.close_connection = True


class RunningReplay(AbstractContextManager["RunningReplay"]):
    """Context manager running the replay server on a background thread."""

    def __init__(self, api_key: str, host: str = DEFAULT_HOST, port: int = 0):
        if not api_key:
            raise ValueError("A non-empty replay API key is required")
        self.server = ReplayHTTPServer((host, port), api_key)
        self.thread = threading.Thread(target=self.server.serve_forever, name="openai-contract-replay", daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}/v1"

    @property
    def state(self) -> ReplayState:
        return self.server.state

    def __enter__(self) -> "RunningReplay":
        self.thread.start()
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def _client(base_url: str, api_key: str) -> OpenAI:
    return OpenAI(base_url=base_url.rstrip("/"), api_key=api_key, max_retries=0, timeout=10)


def _extension(model: Any) -> dict[str, Any]:
    extra = model.model_extra or {}
    extension = extra.get("x_assistant_rh")
    if not isinstance(extension, dict):
        raise ProbeFailure("Missing x_assistant_rh response extension")
    return extension


def _probe_messages() -> list[dict[str, str]]:
    messages: list[dict[str, str]] = [{"role": "system", "content": "This client instruction must be ignored."}]
    for turn in range(1, 7):
        messages.extend(
            [
                {"role": "user", "content": f"Question historique {turn}"},
                {"role": "assistant", "content": f"Réponse historique {turn}{SOURCE_MARKER}ancienne source"},
            ]
        )
    messages.append({"role": "user", "content": "Comment demander un congé de formation ?"})
    return messages


def run_sdk_probe(*, base_url: str, api_key: str, model: str = DEFAULT_MODEL) -> dict[str, Any]:
    """Validate normal model, non-stream, and stream paths through the OpenAI SDK."""

    client = _client(base_url, api_key)
    model_ids = [item.id for item in client.models.list().data]
    if model not in model_ids:
        raise ProbeFailure(f"Expected model {model!r} in /v1/models")

    messages = _probe_messages()
    tools = [
        {
            "type": "function",
            "function": {
                "name": "self_documentation",
                "description": "Tool added by Conversations and ignored by Assistant RH",
                "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
            },
        }
    ]
    completion = client.chat.completions.create(
        model=model,
        messages=messages,
        metadata={"conversation_id": "contract-probe"},
        tools=tools,
        tool_choice="auto",
        parallel_tool_calls=True,
    )
    content = completion.choices[0].message.content or ""
    if SOURCE_MARKER not in content:
        raise ProbeFailure("Non-stream response has no interoperable markdown source block")
    non_stream_extension = _extension(completion)

    chunks = client.chat.completions.create(
        model=model,
        messages=messages,
        stream=True,
        stream_options={"include_usage": True},
        tools=tools,
        tool_choice="auto",
        parallel_tool_calls=True,
    )
    streamed_content: list[str] = []
    stream_extension: dict[str, Any] | None = None
    usage_seen = False
    finish_reason: str | None = None
    for chunk in chunks:
        if chunk.usage is not None:
            usage_seen = True
        if chunk.choices:
            choice = chunk.choices[0]
            if choice.delta.content:
                streamed_content.append(choice.delta.content)
            if choice.finish_reason:
                finish_reason = choice.finish_reason
        if (chunk.model_extra or {}).get("x_assistant_rh"):
            stream_extension = _extension(chunk)

    joined = "".join(streamed_content)
    if SOURCE_MARKER not in joined:
        raise ProbeFailure("Stream response has no interoperable markdown source block")
    if finish_reason != "stop":
        raise ProbeFailure(f"Unexpected stream finish reason: {finish_reason!r}")
    if stream_extension is None:
        raise ProbeFailure("Terminal stream chunk has no x_assistant_rh extension")
    if not usage_seen:
        raise ProbeFailure("stream_options.include_usage did not produce a usage chunk")

    return {
        "sdk": {"name": "openai", "version": openai.__version__},
        "base_url": base_url,
        "models": model_ids,
        "non_stream": {
            "completion_id_prefix": completion.id.split("-", maxsplit=1)[0],
            "finish_reason": completion.choices[0].finish_reason,
            "markdown_sources": True,
            "extension": {
                "ministry": non_stream_extension.get("ministry"),
                "source_count": len(non_stream_extension.get("sources") or []),
            },
        },
        "stream": {
            "finish_reason": finish_reason,
            "markdown_sources": True,
            "usage_chunk": usage_seen,
            "extension": {
                "ministry": stream_extension.get("ministry"),
                "source_count": len(stream_extension.get("sources") or []),
            },
        },
        "secret_material_recorded": False,
    }


def _capture_status(call: Any) -> dict[str, Any]:
    try:
        call()
    except APIStatusError as exc:
        body = exc.body if isinstance(exc.body, dict) else {}
        error = body.get("error", body) if isinstance(body, dict) else {}
        return {"status": exc.status_code, "code": error.get("code") if isinstance(error, dict) else None}
    raise ProbeFailure("Expected an HTTP error response")


def _raw_stream_lines(*, base_url: str, api_key: str, question: str) -> tuple[int, str, list[str]]:
    """Read an SSE response without an SDK so framing remains observable."""

    target = urlsplit(f"{base_url.rstrip('/')}/chat/completions")
    if target.scheme not in {"http", "https"} or not target.hostname:
        raise ProbeFailure(f"Unsupported base URL for the SSE framing probe: {base_url!r}")
    connection_type = HTTPSConnection if target.scheme == "https" else HTTPConnection
    connection = connection_type(target.hostname, target.port, timeout=10)
    body = json.dumps(
        {
            "model": DEFAULT_MODEL,
            "messages": [{"role": "user", "content": question}],
            "stream": True,
            "stream_options": {"include_usage": True},
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    path = target.path or "/"
    if target.query:
        path = f"{path}?{target.query}"
    try:
        connection.request(
            "POST",
            path,
            body=body,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Content-Length": str(len(body)),
                "Accept": "text/event-stream",
                "Connection": "close",
            },
        )
        response = connection.getresponse()
        content_type = response.getheader("Content-Type", "")
        lines = [raw.decode("utf-8").rstrip("\r\n") for raw in iter(response.readline, b"")]
        return response.status, content_type, lines
    finally:
        connection.close()


def probe_replay_sse_framing(*, base_url: str, api_key: str) -> dict[str, Any]:
    """Prove replay-only ping, usage, termination, and post-header error framing."""

    success_status, success_content_type, success_lines = _raw_stream_lines(
        base_url=base_url,
        api_key=api_key,
        question="Question de cadrage SSE",
    )
    success_data = [line.removeprefix("data: ") for line in success_lines if line.startswith("data: ")]
    success_payloads = [json.loads(value) for value in success_data if value != "[DONE]"]
    if success_status != HTTPStatus.OK or not success_content_type.startswith("text/event-stream"):
        raise ProbeFailure(f"Unexpected successful SSE response: {success_status} {success_content_type!r}")
    if success_data[-1:] != ["[DONE]"]:
        raise ProbeFailure("Successful SSE response did not terminate with [DONE]")
    if not any(payload.get("choices") == [] and payload.get("usage") is not None for payload in success_payloads):
        raise ProbeFailure("Successful SSE response did not include the requested usage chunk")
    success_ping_count = success_lines.count(": ping")
    if success_ping_count < 1:
        raise ProbeFailure("Successful SSE response contained no keep-alive comment")

    error_status, error_content_type, error_lines = _raw_stream_lines(
        base_url=base_url,
        api_key=api_key,
        question=STREAM_ERROR_SENTINEL,
    )
    error_data = [line.removeprefix("data: ") for line in error_lines if line.startswith("data: ")]
    error_payloads = [json.loads(value) for value in error_data if value != "[DONE]"]
    error_codes = [payload.get("error", {}).get("code") for payload in error_payloads if isinstance(payload, dict)]
    if error_status != HTTPStatus.OK or not error_content_type.startswith("text/event-stream"):
        raise ProbeFailure(f"Unexpected failing SSE response: {error_status} {error_content_type!r}")
    if "stream_error" not in error_codes:
        raise ProbeFailure("Post-header SSE response contained no stream_error event")
    if "[DONE]" in error_data:
        raise ProbeFailure("Post-header SSE error incorrectly terminated with [DONE]")

    return {
        "success": {
            "status": success_status,
            "ping_comments": success_ping_count,
            "usage_chunk": True,
            "done": True,
        },
        "post_header_error": {
            "status": error_status,
            "code": "stream_error",
            "done": False,
        },
        "secret_material_recorded": False,
    }


def run_replay_error_probe(*, base_url: str, api_key: str) -> dict[str, Any]:
    """Exercise deterministic replay-only status and size-limit scenarios."""

    valid = _client(base_url, api_key)
    invalid_key = hashlib.sha256(api_key.encode("utf-8")).hexdigest()
    invalid = _client(base_url, invalid_key)
    tiny_messages = [{"role": "user", "content": "test"}]

    results = {
        "invalid_bearer": _capture_status(lambda: invalid.models.list()),
        "forbidden_model": _capture_status(
            lambda: valid.chat.completions.create(model="assistant-rh-mi", messages=tiny_messages)
        ),
        "unknown_model": _capture_status(
            lambda: valid.chat.completions.create(model="assistant-rh-unknown", messages=tiny_messages)
        ),
        "n_greater_than_one": _capture_status(
            lambda: valid.chat.completions.create(model=DEFAULT_MODEL, messages=tiny_messages, n=2)
        ),
        "unsupported_stream_option": _capture_status(
            lambda: valid.chat.completions.create(
                model=DEFAULT_MODEL,
                messages=tiny_messages,
                stream=True,
                extra_body={"stream_options": {"include_obfuscation": True}},
            )
        ),
        "too_many_messages": _capture_status(
            lambda: valid.chat.completions.create(
                model=DEFAULT_MODEL,
                messages=[{"role": "user", "content": str(index)} for index in range(MAX_MESSAGES + 1)],
            )
        ),
        "content_too_large": _capture_status(
            lambda: valid.chat.completions.create(
                model=DEFAULT_MODEL,
                messages=[{"role": "user", "content": "é" * ((MAX_CONTENT_BYTES // 2) + 1)}],
            )
        ),
        "http_body_too_large": _capture_status(
            lambda: valid.chat.completions.create(
                model=DEFAULT_MODEL,
                messages=tiny_messages,
                extra_body={"contract_padding": "x" * MAX_HTTP_BODY_BYTES},
            )
        ),
    }

    expected = {
        "invalid_bearer": (401, "invalid_api_key"),
        "forbidden_model": (403, "model_forbidden"),
        "unknown_model": (404, "model_not_found"),
        "n_greater_than_one": (422, "unsupported_n"),
        "unsupported_stream_option": (422, "unsupported_stream_option"),
        "too_many_messages": (422, "too_many_messages"),
        "content_too_large": (422, "content_too_large"),
        "http_body_too_large": (413, "request_too_large"),
    }
    for name, (status, code) in expected.items():
        if results[name] != {"status": status, "code": code}:
            raise ProbeFailure(f"Unexpected {name} result: {results[name]!r}")
    return results


def probe_post_header_error(*, base_url: str, api_key: str) -> dict[str, Any]:
    """Verify the selected post-header error event raises the SDK's APIError."""

    client = _client(base_url, api_key)
    stream = client.chat.completions.create(
        model=DEFAULT_MODEL,
        messages=[{"role": "user", "content": STREAM_ERROR_SENTINEL}],
        stream=True,
    )
    first_content = None
    try:
        for chunk in stream:
            if chunk.choices and chunk.choices[0].delta.content is not None:
                first_content = chunk.choices[0].delta.content
    except APIError as exc:
        if exc.code != "stream_error":
            raise ProbeFailure(f"Unexpected post-header error code: {exc.code!r}") from exc
        return {"exception": type(exc).__name__, "code": exc.code, "initial_chunk_seen": first_content is not None}
    raise ProbeFailure("Post-header error stream ended without SDK APIError")


def run_conversations_provider_probe(*, base_url: str, api_key: str) -> dict[str, Any]:
    """Run the exact provider layer used by Conversations at the pinned A2 revision.

    ``pydantic-ai-slim[openai]==2.22.0`` and ``openai==2.52.0`` are optional
    spike dependencies.  They are deliberately not added to the production lock.
    """

    import asyncio
    from importlib.metadata import version

    try:
        from pydantic_ai import Agent
        from pydantic_ai.models.openai import OpenAIChatModel
        from pydantic_ai.providers.openai import OpenAIProvider
    except ImportError as exc:  # pragma: no cover - depends on the optional spike environment
        raise ProbeFailure(
            "Install the pinned Conversations provider dependencies: "
            "pydantic-ai-slim[openai]==2.22.0 and openai==2.52.0"
        ) from exc

    async def exercise() -> dict[str, Any]:
        provider = OpenAIProvider(base_url=base_url.rstrip("/"), api_key=api_key)
        model = OpenAIChatModel(model_name=DEFAULT_MODEL, provider=provider)
        agent = Agent(model=model, instructions=["Client instruction one.", "Client instruction two."])

        @agent.tool_plain
        def self_documentation() -> str:
            """Return client-side assistant documentation."""

            return "Client-side documentation"

        non_stream_result = await agent.run("Comment demander un congé de formation ?")
        if SOURCE_MARKER not in non_stream_result.output:
            raise ProbeFailure("Conversations provider client did not preserve non-stream markdown sources")

        async with agent.run_stream("Comment demander un congé de formation ?") as response:
            output = await response.get_output()
        if SOURCE_MARKER not in output:
            raise ProbeFailure("Conversations provider client did not preserve stream markdown sources")

        error_exception: str | None = None
        error_model = OpenAIChatModel(
            model_name=DEFAULT_MODEL,
            provider=OpenAIProvider(base_url=base_url.rstrip("/"), api_key=api_key),
        )
        error_agent = Agent(model=error_model, instructions="Ignored client instruction.")
        try:
            async with error_agent.run_stream(STREAM_ERROR_SENTINEL) as response:
                await response.get_output()
        except Exception as exc:  # noqa: BLE001 - the spike records the real third-party mapping
            error_exception = f"{type(exc).__module__}.{type(exc).__name__}"

        if error_exception is None:
            raise ProbeFailure("Conversations provider client hid the post-header stream error")
        return {
            "non_stream_markdown_sources": True,
            "stream_markdown_sources": True,
            "unknown_response_fields_tolerated": True,
            "post_header_error_exception": error_exception,
        }

    result = asyncio.run(exercise())
    return {
        "client": "suitenumerique/conversations provider layer",
        "conversations_revision": "1bba2f0e444ae9c2ddb3eae68c665b63ee4a195e",
        "pydantic_ai_version": version("pydantic-ai-slim"),
        "openai_version": openai.__version__,
        **result,
        "secret_material_recorded": False,
    }


def _require_api_key(env_name: str) -> str:
    value = os.environ.get(env_name)
    if not value:
        raise SystemExit(f"Set {env_name}; API keys are intentionally not accepted as command-line arguments.")
    return value


def _write_report(report: dict[str, Any], output: Path | None) -> None:
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(f"{rendered}\n", encoding="utf-8")
    print(rendered)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve = subparsers.add_parser("serve", help="Run the deterministic fake/replay endpoint.")
    serve.add_argument("--host", default=DEFAULT_HOST)
    serve.add_argument("--port", type=int, default=DEFAULT_PORT)
    serve.add_argument("--api-key-env", default=API_KEY_ENV)

    probe = subparsers.add_parser("probe", help="Probe an OpenAI-compatible endpoint with the OpenAI SDK.")
    probe.add_argument("--base-url", required=True, help="Base URL including /v1.")
    probe.add_argument("--model", default=DEFAULT_MODEL)
    probe.add_argument("--api-key-env", default=API_KEY_ENV)
    probe.add_argument("--replay-errors", action="store_true", help="Run deterministic scenarios only supported by this replay.")
    probe.add_argument(
        "--conversations-client",
        action="store_true",
        help="Also run Conversations' pinned Pydantic-AI provider layer (optional dependencies required).",
    )
    probe.add_argument("--output", type=Path)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    api_key = _require_api_key(args.api_key_env)

    if args.command == "serve":
        server = ReplayHTTPServer((args.host, args.port), api_key)
        host, port = server.server_address[:2]
        print(json.dumps({"base_url": f"http://{host}:{port}/v1", "api_key_env": args.api_key_env}))
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
        return 0

    report: dict[str, Any] = {"sdk_contract": run_sdk_probe(base_url=args.base_url, api_key=api_key, model=args.model)}
    if args.replay_errors:
        report["errors_and_limits"] = run_replay_error_probe(base_url=args.base_url, api_key=api_key)
        report["sse_framing"] = probe_replay_sse_framing(base_url=args.base_url, api_key=api_key)
        report["post_header_error"] = probe_post_header_error(base_url=args.base_url, api_key=api_key)
    if args.conversations_client:
        report["conversations_provider"] = run_conversations_provider_probe(base_url=args.base_url, api_key=api_key)
    _write_report(report, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
