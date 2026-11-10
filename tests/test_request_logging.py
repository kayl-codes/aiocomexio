"""The debug line about a failed or late request (#289): idle time, connection, requests in flight."""

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace

import aiohttp
import pytest
from aiohttp import web

from aiocomexio import (
    ComexioClient,
    ComexioConnectionError,
    ComexioResponseError,
    connection_trace_config,
    session_kwargs,
)
from aiocomexio import client as client_module
from aiocomexio.session import CONNECTION_KEY, CONNECTION_NEW, CONNECTION_REUSED
from tests.fake_comexio import PASSWORD, USERNAME, FakeComexio

API_PATH = "/api/"
SLOW_PATH = "/slow"
MOVED_PATH = "/moved"
LOGGER = "aiocomexio.client"


@pytest.fixture
def api_client(comexio: FakeComexio, session: aiohttp.ClientSession) -> ComexioClient:
    return ComexioClient(comexio.host, USERNAME, PASSWORD, session=session, api_username="api", api_password="pw")


def _answered_at(client: ComexioClient) -> float | None:
    return client._last_response_at


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
    # A failure may have happened before the connector was reached: no hint about the trace then.
    assert "connection unknown," in line
    assert "no connection trace" not in line


async def test_an_answered_request_without_the_connection_note_points_at_the_missing_trace(
    comexio: FakeComexio, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    comexio.serve_text("GET", API_PATH, "1")
    monkeypatch.setattr(client_module, "_IDLE_LOG_MIN_SEC", 0.0)
    async with aiohttp.ClientSession() as bare_session:
        client = ComexioClient(comexio.host, USERNAME, PASSWORD, session=bare_session, api_username="api")
        await client.set_marker_value(5, 1)

        with caplog.at_level(logging.DEBUG, logger=LOGGER):
            await client.set_marker_value(5, 0)

    (line,) = (r.getMessage() for r in caplog.records if "answered after" in r.getMessage())
    assert "connection unknown (no connection trace on the session?)," in line


async def test_the_connection_note_fills_the_context_dict_and_ignores_other_contexts(comexio: FakeComexio) -> None:
    comexio.serve_text("GET", API_PATH, "ok")
    url = f"http://{comexio.host}{API_PATH}"
    first: dict[str, list[str]] = {}
    second: dict[str, list[str]] = {}
    async with aiohttp.ClientSession(trace_configs=[connection_trace_config()]) as session:
        async with session.get(url, trace_request_ctx=first) as resp:
            assert await resp.text() == "ok"
        async with session.get(url, trace_request_ctx=second) as resp:
            assert await resp.text() == "ok"
        async with session.get(url) as resp:  # no context at all
            assert resp.status == 200
        other = SimpleNamespace()
        async with session.get(url, trace_request_ctx=other) as resp:  # a context that is no dict
            assert resp.status == 200

    assert first == {CONNECTION_KEY: [CONNECTION_NEW]}
    assert second == {CONNECTION_KEY: [CONNECTION_REUSED]}
    assert not vars(other)


def test_the_connection_trace_config_hooks_both_signals() -> None:
    trace_config = connection_trace_config()

    assert len(trace_config.on_connection_reuseconn) == 1
    assert len(trace_config.on_connection_create_start) == 1


async def test_session_kwargs_carry_the_connection_note() -> None:
    (trace_config,) = session_kwargs()["trace_configs"]
    assert trace_config.on_connection_reuseconn


async def test_a_connect_that_fails_is_still_a_new_connection(caplog: pytest.LogCaptureFixture) -> None:
    async with aiohttp.ClientSession(**session_kwargs()) as session:
        client = ComexioClient("127.0.0.1:1", USERNAME, PASSWORD, session=session, api_username="api")

        with caplog.at_level(logging.DEBUG, logger=LOGGER), pytest.raises(ComexioConnectionError):
            await client.set_marker_value(5, 0)

    (line,) = (r.getMessage() for r in caplog.records if "failed after" in r.getMessage())
    assert "connection new," in line


async def _hold_request(api_client: ComexioClient, comexio: FakeComexio) -> tuple["asyncio.Task[str]", asyncio.Event]:
    """Start a request the server keeps open until the returned event is set."""
    reached, release = asyncio.Event(), asyncio.Event()

    async def slow(_request: web.Request) -> web.StreamResponse:
        reached.set()
        await release.wait()
        return web.Response(text="ok")

    comexio.serve("GET", SLOW_PATH, slow)
    task = asyncio.create_task(api_client._request_text("GET", SLOW_PATH, what="Slow page"))
    await reached.wait()
    return task, release


async def test_another_request_in_flight_is_counted(
    api_client: ComexioClient, comexio: FakeComexio, caplog: pytest.LogCaptureFixture
) -> None:
    task, release = await _hold_request(api_client, comexio)
    comexio.serve("GET", API_PATH, _close_connection)

    with caplog.at_level(logging.DEBUG, logger=LOGGER), pytest.raises(ComexioConnectionError):
        await api_client.set_marker_value(5, 0)
    release.set()
    await task

    (line,) = (r.getMessage() for r in caplog.records if "Writing marker M5: failed after" in r.getMessage())
    assert "1 other request(s) in flight" in line


async def test_a_cancelled_request_is_no_answer(
    api_client: ComexioClient, comexio: FakeComexio, caplog: pytest.LogCaptureFixture
) -> None:
    comexio.serve_text("GET", API_PATH, "1")
    await api_client.set_marker_value(5, 1)
    answered_at = api_client._last_response_at
    assert answered_at is not None
    task, release = await _hold_request(api_client, comexio)

    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    release.set()

    assert api_client._last_response_at == answered_at
    (line,) = (r.getMessage() for r in caplog.records if "Slow page: failed after" in r.getMessage())
    assert "CancelledError" in line


async def test_the_answer_time_is_set_by_an_answer_and_not_by_a_failure(
    api_client: ComexioClient, comexio: FakeComexio
) -> None:
    comexio.serve("GET", API_PATH, _close_connection)
    with pytest.raises(ComexioConnectionError):
        await api_client.set_marker_value(5, 0)
    assert _answered_at(api_client) is None

    comexio.serve_text("GET", API_PATH, "1")
    await api_client.set_marker_value(5, 1)
    answered_at = _answered_at(api_client)
    assert answered_at is not None

    comexio.serve("GET", API_PATH, _close_connection)
    with pytest.raises(ComexioConnectionError):
        await api_client.set_marker_value(5, 0)
    assert _answered_at(api_client) == answered_at


@pytest.mark.parametrize(("idle_sec", "logged"), [(7.0, True), (4.0, False)])
async def test_the_idle_time_is_the_time_since_the_last_answer(
    api_client: ComexioClient,
    comexio: FakeComexio,
    caplog: pytest.LogCaptureFixture,
    idle_sec: float,
    logged: bool,
) -> None:
    comexio.serve_text("GET", API_PATH, "1")
    await api_client.set_marker_value(5, 1)
    api_client._last_response_at = time.monotonic() - idle_sec

    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        await api_client.set_marker_value(5, 0)

    lines = [r.getMessage() for r in caplog.records if "answered after" in r.getMessage()]
    assert bool(lines) is logged
    if logged:
        assert f"idle {idle_sec:.1f} s before it" in lines[0]


async def test_a_followed_redirect_is_not_a_second_try_of_aiohttp(
    api_client: ComexioClient,
    comexio: FakeComexio,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def redirect(_request: web.Request) -> web.StreamResponse:
        return web.Response(status=302, headers={"Location": MOVED_PATH})

    comexio.serve("GET", API_PATH, redirect)
    comexio.serve_text("GET", MOVED_PATH, "1")
    monkeypatch.setattr(client_module, "_IDLE_LOG_MIN_SEC", 0.0)
    await api_client.set_marker_value(5, 1)  # the first request has no idle time and logs nothing

    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        await api_client.set_marker_value(5, 0)

    (line,) = (r.getMessage() for r in caplog.records if "answered after" in r.getMessage())
    assert "connection reused," in line
    assert "aiohttp tried again" not in line


async def test_a_failing_debug_line_never_replaces_the_requests_own_exception(
    api_client: ComexioClient,
    comexio: FakeComexio,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(*_args: object) -> None:
        raise RuntimeError("the line itself broke")

    monkeypatch.setattr(api_client, "_log_request", broken)
    comexio.serve("GET", API_PATH, _close_connection)

    with caplog.at_level(logging.DEBUG, logger=LOGGER), pytest.raises(ComexioConnectionError):
        await api_client.set_marker_value(5, 0)

    assert "Request diagnostics for Writing marker M5 failed" in caplog.text


async def test_a_failing_debug_line_does_not_replace_a_successful_answer(
    api_client: ComexioClient,
    comexio: FakeComexio,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(*_args: object) -> None:
        raise RuntimeError("the line itself broke")

    monkeypatch.setattr(api_client, "_log_request", broken)
    comexio.serve_text("GET", API_PATH, "1")

    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        await api_client.set_marker_value(5, 1)

    (record,) = (r for r in caplog.records if "Request diagnostics" in r.getMessage())
    assert record.exc_info is not None


async def test_the_wait_at_the_login_gate_is_not_idle_time(
    api_client: ComexioClient,
    comexio: FakeComexio,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Another request answers while this one waits for its slot: the client was not idle then.
    real_request = api_client._gate.request

    @asynccontextmanager
    async def slot_after_another_answer() -> AsyncIterator[object]:
        await asyncio.sleep(0.05)
        api_client._last_response_at = time.monotonic()
        async with real_request() as slot:
            yield slot

    monkeypatch.setattr(api_client._gate, "request", slot_after_another_answer)
    api_client._last_response_at = time.monotonic() - 30
    comexio.serve("GET", API_PATH, _close_connection)

    with caplog.at_level(logging.DEBUG, logger=LOGGER), pytest.raises(ComexioConnectionError):
        await api_client.set_marker_value(5, 0)

    (line,) = (r.getMessage() for r in caplog.records if "failed after" in r.getMessage())
    assert "idle 0.0 s before it" in line
    assert "failed after 0.0" in line


async def test_a_status_error_names_its_class_and_not_the_page(
    api_client: ComexioClient, comexio: FakeComexio, caplog: pytest.LogCaptureFixture
) -> None:
    async def server_error(_request: web.Request) -> web.StreamResponse:
        return web.Response(status=500, text="PAGE-EXCERPT-NOT-FOR-THE-LOG")

    comexio.serve("GET", API_PATH, server_error)

    with caplog.at_level(logging.DEBUG, logger=LOGGER), pytest.raises(ComexioResponseError):
        await api_client.set_marker_value(5, 0)

    (line,) = (r.getMessage() for r in caplog.records if "failed after" in r.getMessage())
    assert line.endswith(": ComexioResponseError")
    assert "PAGE-EXCERPT" not in line
    assert "no connection trace" not in line
