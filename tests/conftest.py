"""Fixtures for the client tests: a running fake Comexio server and a client wired to it."""

from collections.abc import AsyncIterator

import aiohttp
import pytest

from aiocomexio import ComexioClient, session_kwargs
from tests.fake_comexio import PASSWORD, USERNAME, FakeComexio


@pytest.fixture
async def comexio() -> AsyncIterator[FakeComexio]:
    fake = FakeComexio()
    await fake.server.start_server()
    yield fake
    await fake.server.close()


@pytest.fixture
async def session(comexio: FakeComexio) -> AsyncIterator[aiohttp.ClientSession]:
    async with aiohttp.ClientSession(**session_kwargs()) as session:
        yield session


@pytest.fixture
def client(comexio: FakeComexio, session: aiohttp.ClientSession) -> ComexioClient:
    return ComexioClient(comexio.host, USERNAME, PASSWORD, session=session)


@pytest.fixture
async def logged_in(client: ComexioClient) -> ComexioClient:
    await client.login()
    return client
