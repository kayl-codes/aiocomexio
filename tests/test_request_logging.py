"""The debug line about a failed or late request (#289): idle time, connection, requests in flight."""

import logging

import aiohttp
import pytest
from aiohttp import web

from aiocomexio import ComexioClient, ComexioConnectionError, connection_trace_config, session_kwargs
from aiocomexio import client as client_module
from tests.fake_comexio import PASSWORD, USERNAME, FakeComexio

API_PATH = "/api/"
LOGGER = "aiocomexio.client"


@pytest.fixture
def api_client(comexio: FakeComexio, session: aiohttp.ClientSession) -> ComexioClient:
    return ComexioClient(comexio.host, USERNAME, PASSWORD, session=session, api_username="api", api_password="pw")


async def _close_connection(request: web.Request) -> web.StreamResponse:
    assert request.transport is not None
    request.transport.close()
    return web.Response()


async def test_failed_write_on_a_reused_connection_logs_why(
    api_client: ComexioClient, comexio: FakeComexio, caplog: pytest.LogCaptureFixture
) -> None:
    comexio.serve_text("GET", API_PATH, "1")
    await api_client.set_marker_value(5, 1)  # opens the connection the next request reuses
    comexio.serve("GET", API_PATH, _close_connection)

    with caplog.at_level(logging.DEBUG, logger=LOGGER), pytest.raises(ComexioConnectionError):
        await api_client.set_marker_value(5, 0)

    (line,) = (r.getMessage() for r in caplog.records if "failed after" in r.getMessage())
    assert "Writing marker M5: failed after" in line
    # aiohttp itself tried the GET once more, on a new connection, which the server closed again.
    assert "connection reused → new (aiohttp tried again)" in line
    assert "0 other request(s) in flight" in line
    assert "ServerDisconnectedError" in line
    assert "idle " in line
    assert "n/a" not in line


async def test_the_first_request_has_no_idle_time_and_a_new_connection(
    api_client: ComexioClient, comexio: FakeComexio, caplog: pytest.LogCaptureFixture
) -> None:
    comexio.serve("GET", API_PATH, _close_connection)

    with caplog.at_level(logging.DEBUG, logger=LOGGER), pytest.raises(ComexioConnectionError):
        await api_client.set_marker_value(5, 0)

    (line,) = (r.getMessage() for r in caplog.records if "failed after" in r.getMessage())
    assert "n/a (first request)" in line
    assert "connection new → new (aiohttp tried again)" in line


async def test_a_request_after_a_pause_logs_even_when_it_succeeds(
    api_client: ComexioClient,
    comexio: FakeComexio,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    comexio.serve_text("GET", API_PATH, "1")
    monkeypatch.setattr(client_module, "_IDLE_LOG_MIN_SEC", 0.0)
    await api_client.set_marker_value(5, 1)

    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        await api_client.set_marker_value(5, 0)

    (line,) = (r.getMessage() for r in caplog.records if "answered after" in r.getMessage())
    assert "connection reused," in line
    assert "aiohttp tried again" not in line


async def test_a_quick_successful_request_logs_nothing(
    api_client: ComexioClient, comexio: FakeComexio, caplog: pytest.LogCaptureFixture
) -> None:
    comexio.serve_text("GET", API_PATH, "1")
    await api_client.set_marker_value(5, 1)

    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        await api_client.set_marker_value(5, 0)

    assert not [r for r in caplog.records if r.name == LOGGER and "after" in r.getMessage()]


async def test_a_failure_logs_nothing_above_debug_level(
    api_client: ComexioClient, comexio: FakeComexio, caplog: pytest.LogCaptureFixture
) -> None:
    comexio.serve("GET", API_PATH, _close_connection)

    with caplog.at_level(logging.INFO, logger=LOGGER), pytest.raises(ComexioConnectionError):
        await api_client.set_marker_value(5, 0)

    assert not [r for r in caplog.records if r.name == LOGGER]


async def test_a_session_without_the_connection_note_says_unknown(
    comexio: FakeComexio, caplog: pytest.LogCaptureFixture
) -> None:
    comexio.serve("GET", API_PATH, _close_connection)
    async with aiohttp.ClientSession() as bare_session:
        client = ComexioClient(comexio.host, USERNAME, PASSWORD, session=bare_session, api_username="api")

        with caplog.at_level(logging.DEBUG, logger=LOGGER), pytest.raises(ComexioConnectionError):
            await client.set_marker_value(5, 0)

    (line,) = (r.getMessage() for r in caplog.records if "failed after" in r.getMessage())
    assert "connection unknown," in line


async def test_the_connection_note_ignores_a_request_without_a_context_dict(comexio: FakeComexio) -> None:
    comexio.serve_text("GET", API_PATH, "ok")
    async with aiohttp.ClientSession(trace_configs=[connection_trace_config()]) as session:
        async with session.get(f"http://{comexio.host}{API_PATH}") as resp:
            assert await resp.text() == "ok"
        async with session.get(f"http://{comexio.host}{API_PATH}", trace_request_ctx={}) as resp:
            assert resp.status == 200


async def test_session_kwargs_carry_the_connection_note() -> None:
    assert len(session_kwargs()["trace_configs"]) == 1
