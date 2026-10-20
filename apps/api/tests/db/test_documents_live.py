"""Actual TCP/backend transfer proof, with a synthetic legacy PDF and identity."""

import asyncio
import hashlib
import socket

import httpx
import pytest
import uvicorn
from assistant_rh_api.core.models.conversations import RunSource
from assistant_rh_api.db.run_store import ChatRunStore
from assistant_rh_api.handlers.app import create_app

from apps.api.tests.auth_fakes import Signer, delegated_service, individual_context
from apps.api.tests.db.test_documents import persist
from apps.api.tests.db.test_run_feedback_stores import make_run

pytestmark = pytest.mark.anyio


async def test_legacy_pdf_over_real_backend_socket_and_client_disconnect(repository_db):
    data = b"%PDF-1.7\n" + b"synthetic-pdf-content\n" * 500000
    doc_id, _ = await persist(repository_db, data=data)
    run = make_run(author_user_id=individual_context().user_id, sources=(RunSource("final", "PDF", "", str(doc_id)),))
    await ChatRunStore(repository_db).finalize(run)
    signer = Signer()
    app = create_app(database=repository_db, environ={}, auth_service=delegated_service(signer))
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(app, log_config=None, log_level="error", ws="none", timeout_graceful_shutdown=3))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(5):
            while not server.started:
                if task.done():
                    task.result()
                await asyncio.sleep(0.01)
        base = "http://127.0.0.1:" + str(sock.getsockname()[1])
        path = "/v1/documents/final/content?completion_id=chatcmpl-" + run.turn_id
        async with httpx.AsyncClient(base_url=base, trust_env=False) as client:
            digest = hashlib.sha256()
            async with client.stream("GET", path, headers={"Authorization": "Bearer " + signer.sign()}) as response:
                assert response.status_code == 200 and "location" not in response.headers
                async for chunk in response.aiter_bytes(65536):
                    digest.update(chunk)
            assert digest.digest() == hashlib.sha256(data).digest()
            # Closing the socket after one chunk must release the snapshot/lease.
            async with client.stream("GET", path, headers={"Authorization": "Bearer " + signer.sign()}) as response:
                assert (await anext(response.aiter_bytes(1024))).startswith(b"%PDF-")
            pool = app.state.document_service.documents.database
            async with asyncio.timeout(3):
                while pool.statistics()["pool_available"] != pool.statistics()["pool_size"]:
                    await asyncio.sleep(0.01)
            assert (await client.get(path, headers={"Authorization": "Bearer " + signer.sign(signer.claims(models=[]))})).status_code == 404
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 5)
        sock.close()
