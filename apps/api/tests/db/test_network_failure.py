"""Exercise psycopg's real socket/cancellation path through a local TCP proxy."""

import asyncio
from contextlib import asynccontextmanager

import psycopg
import pytest
from assistant_rh_api.core.errors import DatabaseUnavailable
from assistant_rh_api.db.dsn import DatabaseSettings
from assistant_rh_api.db.pool import Database


class ResponseDroppingProxy:
    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self.drop_responses = False
        self.response_dropped = asyncio.Event()
        self.writers: list[asyncio.StreamWriter] = []
        self.tasks: list[asyncio.Task] = []

    def accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.tasks.append(asyncio.create_task(self.bridge(reader, writer)))

    async def bridge(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.writers.append(writer)
        upstream_reader, upstream_writer = await asyncio.open_connection(self.host, self.port)
        self.writers.append(upstream_writer)

        async def relay(source, target, *, downstream: bool) -> None:
            try:
                while data := await source.read(65536):
                    if downstream and self.drop_responses:
                        self.response_dropped.set()
                    else:
                        target.write(data)
                        await target.drain()
            finally:
                target.close()

        await asyncio.gather(relay(reader, upstream_writer, downstream=False), relay(upstream_reader, writer, downstream=True))

    async def close(self) -> None:
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        for writer in self.writers:
            writer.close()
        await asyncio.gather(*(writer.wait_closed() for writer in self.writers), return_exceptions=True)


@asynccontextmanager
async def proxied_database(dsn: str, *, timeout_seconds: float):
    # The fixture has already checked and initialized the synthetic DB target.
    parameters = psycopg.conninfo.conninfo_to_dict(dsn)
    proxy = ResponseDroppingProxy(parameters["host"], int(parameters.get("port", "5432")))
    server = await asyncio.start_server(proxy.accept, "127.0.0.1", 0)
    parameters.update(host="127.0.0.1", port=str(server.sockets[0].getsockname()[1]))
    database = Database(DatabaseSettings(dsn=psycopg.conninfo.make_conninfo(**parameters), max_size=1, timeout_seconds=timeout_seconds))
    try:
        await database.open()
        yield database, proxy
    finally:
        await database.close()
        server.close()
        await server.wait_closed()
        await proxy.close()


@pytest.mark.anyio
@pytest.mark.parametrize("cancel_caller", [False, True], ids=["deadline", "caller-cancellation"])
async def test_lost_checkout_response_discards_lease_and_pool_recovers(synthetic_database_dsn: str, cancel_caller: bool) -> None:
    # Cancellation must be tested before the deadline; both must finish without
    # waiting for psycopg's network cancellation handshake or another response.
    timeout = 5.0 if cancel_caller else 0.25
    async with proxied_database(synthetic_database_dsn, timeout_seconds=timeout) as (database, proxy):
        async with database.transaction(read_only=True) as connection:
            original_pid = connection.info.backend_pid
        proxy.drop_responses = True
        reached_business_code = False

        async def caller() -> None:
            nonlocal reached_business_code
            async with database.transaction():
                reached_business_code = True

        task = asyncio.create_task(caller())
        try:
            await asyncio.wait_for(proxy.response_dropped.wait(), 1)
            if cancel_caller:
                task.cancel()
            done, _ = await asyncio.wait({task}, timeout=1)
            assert task in done, "checkout retained its lease after the deadline/cancellation"
            with pytest.raises(asyncio.CancelledError if cancel_caller else DatabaseUnavailable):
                await task
            assert not reached_business_code
            assert database.statistics()["returns_bad"] == 1
            proxy.drop_responses = False
            async with database.transaction(read_only=True) as connection:
                assert connection.info.backend_pid != original_pid
                cursor = await connection.execute("SELECT 1")
                assert await cursor.fetchone() == (1,)
        finally:
            # Also make an unfixed implementation terminate when this test fails.
            await proxy.close()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
