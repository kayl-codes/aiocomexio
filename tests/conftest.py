"""Fixtures for the client tests: a running fake Comexio server and a client wired to it."""

import gc
import sys
from collections.abc import AsyncIterator, Iterator

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


@pytest.fixture
def garbage_collected() -> Iterator[None]:
    """Collect now, at a shallow stack, and not at all while the test runs.

    A test that recurses up to the recursion limit uses it. A collection that ran in the middle of such a test would
    call the finalizers of earlier tests' garbage there, and pytest's unraisable-exception hook cannot even format what
    they raise at that depth: the test failed in CI (Python 3.13.18, with coverage) with "Failed to process unraisable
    exception". The cause of that garbage is not known; this takes the collection out of the recursion.
    """
    gc.collect()
    was_enabled = gc.isenabled()
    gc.disable()
    try:
        yield
    finally:
        if was_enabled:
            gc.enable()


@pytest.fixture
def int_max_str_digits() -> Iterator[None]:
    """Python's default int-to-str digit limit (4300), whatever PYTHONINTMAXSTRDIGITS sets for the run."""
    previous = sys.get_int_max_str_digits()
    sys.set_int_max_str_digits(4300)
    yield
    sys.set_int_max_str_digits(previous)
