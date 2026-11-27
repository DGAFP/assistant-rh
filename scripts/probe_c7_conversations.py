"""Run the pinned Conversations Django client against the real API with synthetic ports.

Requires the existing conversations:backend-development image and a disposable,
localhost PostgreSQL server for the client's Django test database. No live RAG
providers, repository .env, or production/staging database are used.
"""

import argparse
import asyncio
import os
import socket
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import uvicorn
from assistant_rh_api.core.errors import InferenceFailure
from assistant_rh_api.core.models.inference import StreamCompleted, TextDelta
from assistant_rh_api.handlers.app import create_app

from apps.api.tests.auth_fakes import service
from apps.api.tests.chat_fakes import LLM, Runtime

ROOT = Path(__file__).resolve().parents[1]


class ProbeLLM(LLM):
    @asynccontextmanager
    async def stream(self, request):
        result = await self.complete(request)

        async def events():
            yield TextDelta(result.text)
            if "__simulate_stream_error__" in request.messages[-1].content:
                raise InferenceFailure((), partial=True)
            yield StreamCompleted(result.provider, result.model, result.attempts, result.finish_reason, result.usage)

        yield events()


async def probe(args):
    auth = service()
    issued = await auth.login("beta", "password", "local")
    runtime = Runtime()
    runtime.llm = ProbeLLM()
    app = create_app(auth_service=auth, chat_service=runtime.service, environ={})
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", timeout_graceful_shutdown=2))
    serving = asyncio.create_task(server.serve(sockets=[sock]))
    process = None
    container_name = "assistant-rh-c7-client-" + uuid4().hex
    try:
        async with asyncio.timeout(5):
            while not server.started:
                if serving.done():
                    serving.result()
                await asyncio.sleep(0.01)
        # Only the disposable bearer is passed to the test client; never print it.
        environment = {
            **os.environ,
            "ASSISTANT_RH_CONVERSATIONS_API_KEY": issued.access_token,
            "ASSISTANT_RH_CONVERSATIONS_BASE_URL": f"http://127.0.0.1:{sock.getsockname()[1]}/v1",
        }
        command = [
            "docker",
            "run",
            "--rm",
            "--name",
            container_name,
            "--network",
            "host",
            "--entrypoint",
            "python",
            "--env-file",
            str(args.checkout / "env.d/test"),
        ]
        for setting in (
            "DJANGO_CONFIGURATION=Test",
            "DJANGO_SETTINGS_MODULE=conversations.settings",
            "DB_HOST=127.0.0.1",
            f"DB_PORT={args.db_port}",
            "LLM_CONFIGURATION_FILE_PATH=/run/assistant-rh/config.json",
            "LLM_DEFAULT_MODEL_HRID=assistant-rh-matte",
            "LLM_SUMMARIZATION_MODEL_HRID=assistant-rh-summarization",
            "ASSISTANT_RH_CONVERSATIONS_API_KEY",
            "ASSISTANT_RH_CONVERSATIONS_BASE_URL",
        ):
            command += ["-e", setting]
        command += [
            "-v",
            f"{ROOT}/tests/openai-contract/conversations-llm.issue443.json:/run/assistant-rh/config.json:ro",
            "-v",
            f"{ROOT}/tests/openai-contract/conversations-c7-test.py:/app/chat/tests/views/chat/conversations/test_issue464_live.py:ro",
            args.image,
            "-m",
            "pytest",
            "/app/chat/tests/views/chat/conversations/test_issue464_live.py",
            "-q",
            "--no-cov",
        ]
        process = await asyncio.create_subprocess_exec(*command, env=environment)
        async with asyncio.timeout(180):
            status = await process.wait()
        if status:
            raise RuntimeError(f"Conversations tests failed with exit status {status}")
        statuses = sorted(run.status for run in runtime.runs.rows.values())
        if statuses != ["completed", "failed"] or app.state.stream_workers.active:
            raise RuntimeError(f"Unexpected API finalization: {statuses}")
        print("Real API: completed and failed runs finalized; no active streaming worker.")
    finally:
        if process is not None and process.returncode is None:
            cleanup = await asyncio.create_subprocess_exec("docker", "rm", "--force", container_name)
            await cleanup.wait()
            await process.wait()
        server.should_exit = True
        await serving
        sock.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", type=Path, required=True, help="Pinned A2 Conversations checkout (1bba2f0)")
    parser.add_argument("--db-port", type=int, default=55465)
    parser.add_argument("--image", default="conversations:backend-development")
    asyncio.run(probe(parser.parse_args()))
