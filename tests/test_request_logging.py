"""The debug line about a failed or late request (#289): idle time, connection, requests in flight."""

import asyncio
import logging
import re
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


def _expected_line(
    what: str,
    outcome: str,
    *,
    connection: str,
    idle: str = r"\d+\.\d s",
    others: int = 0,
    error: str | None = None,
) -> re.Pattern[str]:
    """The whole debug line; only the duration (and what the caller passes as a pattern) varies between runs.

    connection is literal text, idle and error are regular expressions.
    """
    tail = "" if error is None else f": {error}"
    return re.compile(
        rf"{re.escape(what)}: {outcome} after \d+\.\d{{3}} s; idle {idle} before it, "
        rf"connection {re.escape(connection)}, {others} other request\(s\) in flight{tail}"
    )


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
    # aiohttp itself tried the GET once more, on a new connection, which the server closed again.
    assert _expected_line(
        "Writing marker M5",
        "failed",
        connection="reused → new (aiohttp tried again)",
        error=r"ServerDisconnectedError\(.*\)",
    ).fullmatch(line)


async def test_the_first_request_has_no_idle_time_and_a_new_connection(
    api_client: ComexioClient, comexio: FakeComexio, caplog: pytest.LogCaptureFixture
) -> None:
    comexio.serve("GET", API_PATH, _close_connection)

    with caplog.at_level(logging.DEBUG, logger=LOGGER), pytest.raises(ComexioConnectionError):
        await api_client.set_marker_value(5, 0)

    (line,) = (r.getMessage() for r in caplog.records if "failed after" in r.getMessage())
    assert _expected_line(
        "Writing marker M5",
        "failed",
        idle=re.escape("n/a (first request)"),
        connection="new → new (aiohttp tried again)",
        error=r"ServerDisconnectedError\(.*\)",
    ).fullmatch(line)


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
    assert _expected_line("Writing marker M5", "answered", connection="reused").fullmatch(line)


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
    assert _expected_line(
        "Writing marker M5",
        "failed",
        idle=re.escape("n/a (first request)"),
        connection="unknown",
        error=r"ServerDisconnectedError\(.*\)",
    ).fullmatch(line)


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
    assert _expected_line(
        "Writing marker M5", "answered", connection="unknown (no connection trace on the session?)"
    ).fullmatch(line)


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
    assert _expected_line(
        "Writing marker M5",
        "failed",
        idle=re.escape("n/a (first request)"),
        connection="new",
        error=r"ClientConnectorError\(.*\)",
    ).fullmatch(line)


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
    assert _expected_line(
        "Writing marker M5",
        "failed",
        idle=re.escape("n/a (first request)"),
        connection="new → new (aiohttp tried again)",
        others=1,
        error=r"ServerDisconnectedError\(.*\)",
    ).fullmatch(line)


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
    assert _expected_line("Slow page", "failed", connection="reused", error="CancelledError").fullmatch(line)


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


@pytest.mark.parametrize(("idle_sec", "logged"), [(7.0, True), (4.0, False)])  # a slow runner may add a tenth
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
        assert _expected_line(
            "Writing marker M5", "answered", idle=rf"{idle_sec:.0f}\.[0-2] s", connection="reused"
        ).fullmatch(lines[0])


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
    assert _expected_line("Writing marker M5", "answered", connection="reused").fullmatch(line)


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
    assert re.fullmatch(
        r"Writing marker M5: failed after 0\.\d{3} s; idle 0\.0 s before it, connection new → new \(aiohttp tried "
        r"again\), 0 other request\(s\) in flight: ServerDisconnectedError\(.*\)",
        line,
    )


async def test_a_status_error_names_its_class_and_not_the_page(
    api_client: ComexioClient, comexio: FakeComexio, caplog: pytest.LogCaptureFixture
) -> None:
    async def server_error(_request: web.Request) -> web.StreamResponse:
        return web.Response(status=500, text="PAGE-EXCERPT-NOT-FOR-THE-LOG")

    comexio.serve("GET", API_PATH, server_error)

    with caplog.at_level(logging.DEBUG, logger=LOGGER), pytest.raises(ComexioResponseError):
        await api_client.set_marker_value(5, 0)

    (line,) = (r.getMessage() for r in caplog.records if "failed after" in r.getMessage())
    assert _expected_line(
        "Writing marker M5",
        "failed",
        idle=re.escape("n/a (first request)"),
        connection="new",
        error="ComexioResponseError",
    ).fullmatch(line)


async def test_a_response_error_names_its_class_and_status_and_not_the_url(
    comexio: FakeComexio, caplog: pytest.LogCaptureFixture
) -> None:
    async def server_error(_request: web.Request) -> web.StreamResponse:
        return web.Response(status=500)

    comexio.serve("GET", API_PATH, server_error)
    async with aiohttp.ClientSession(**session_kwargs(), raise_for_status=True) as strict_session:
        client = ComexioClient(comexio.host, USERNAME, PASSWORD, session=strict_session, api_username="api")

        with caplog.at_level(logging.DEBUG, logger=LOGGER), pytest.raises(ComexioConnectionError):
            await client.set_marker_value(5, 12345)

    (line,) = (r.getMessage() for r in caplog.records if "failed after" in r.getMessage())
    assert line.endswith(": ClientResponseError (status 500)")
    assert "12345" not in line


@pytest.mark.parametrize("extra", [-1, 0, 1])
async def test_a_transport_error_is_cut_above_the_limit_only(
    api_client: ComexioClient, caplog: pytest.LogCaptureFixture, extra: int
) -> None:
    limit = client_module._DIAGNOSTIC_ERROR_MAX_CHARS
    overhead = len(repr(aiohttp.ClientError("")))
    error = aiohttp.ClientError("x" * (limit - overhead + extra))
    assert len(repr(error)) == limit + extra

    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        api_client._log_request("Page", time.monotonic(), None, {}, error)

    (line,) = (r.getMessage() for r in caplog.records if "failed after" in r.getMessage())
    error_text = line.split(" in flight: ", 1)[1]
    assert error_text == (repr(error) if extra <= 0 else f"{repr(error)[:limit]}…")


async def test_a_request_cancelled_before_its_slot_has_no_line(
    api_client: ComexioClient, comexio: FakeComexio, caplog: pytest.LogCaptureFixture
) -> None:
    comexio.serve_text("GET", API_PATH, "1")
    gate_closed, entered = asyncio.Event(), asyncio.Event()

    @asynccontextmanager
    async def blocked_slot() -> AsyncIterator[None]:
        entered.set()
        await gate_closed.wait()  # a login that never lets a request start
        yield

    api_client._gate.request = blocked_slot  # type: ignore[method-assign]
    task = asyncio.create_task(api_client.set_marker_value(5, 1))
    await entered.wait()

    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert not [r for r in caplog.records if r.name == LOGGER]


async def test_a_redirect_that_fails_later_still_moved_the_answer_time(
    api_client: ComexioClient, comexio: FakeComexio
) -> None:
    async def redirect(_request: web.Request) -> web.StreamResponse:
        return web.Response(status=302, headers={"Location": MOVED_PATH})

    comexio.serve("GET", API_PATH, redirect)
    comexio.serve("GET", MOVED_PATH, _close_connection)

    with pytest.raises(ComexioConnectionError):
        await api_client.set_marker_value(5, 0)

    assert _answered_at(api_client) is not None


async def test_a_redirect_form_answer_moves_the_answer_time(
    api_client: ComexioClient, comexio: FakeComexio, caplog: pytest.LogCaptureFixture
) -> None:
    async def redirect(_request: web.Request) -> web.StreamResponse:
        return web.Response(status=302, headers={"Location": MOVED_PATH})

    comexio.serve("POST", SLOW_PATH, redirect)

    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        assert await api_client._request_redirect("POST", SLOW_PATH, what="Form") == MOVED_PATH

    assert _answered_at(api_client) is not None
    assert not [r for r in caplog.records if r.name == LOGGER]


async def test_the_requests_a_login_left_behind_are_still_counted(
    api_client: ComexioClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(client_module, "_LOGIN_WAIT_FOR_REQUESTS_SEC", 0.05)
    gate = api_client._gate
    release = asyncio.Event()

    async def hold() -> None:
        async with gate.request():
            await release.wait()

    task = asyncio.create_task(hold())
    await asyncio.sleep(0)
    assert gate.requests_in_flight == 1

    async with gate.login():  # gives up waiting for the held request after 0.05 s
        assert gate.requests_in_flight == 1

    release.set()
    await task
    assert gate.requests_in_flight == 0


async def test_the_connection_note_replaces_a_value_that_is_no_list(comexio: FakeComexio) -> None:
    comexio.serve_text("GET", API_PATH, "ok")
    url = f"http://{comexio.host}{API_PATH}"
    context: dict[str, object] = {CONNECTION_KEY: "not a list"}
    async with (
        aiohttp.ClientSession(trace_configs=[connection_trace_config()]) as session,
        session.get(url, trace_request_ctx=context) as resp,
    ):
        assert resp.status == 200

    assert context == {CONNECTION_KEY: [CONNECTION_NEW]}


async def test_session_kwargs_append_the_callers_trace_configs() -> None:
    own = aiohttp.TraceConfig()

    configs = session_kwargs(progress_log_interval=1.0, trace_configs=[own])["trace_configs"]

    assert len(configs) == 3
    assert configs[0].on_connection_reuseconn
    assert configs[1].on_request_start
    assert configs[2] is own
    assert session_kwargs(trace_configs=[own])["trace_configs"][1:] == [own]
    assert len(session_kwargs(trace_configs=None)["trace_configs"]) == 1
    async with aiohttp.ClientSession(**session_kwargs(trace_configs=[own])):
        pass  # no TypeError for a duplicate keyword


async def test_an_invalid_url_error_is_named_by_class_and_not_by_its_url(
    api_client: ComexioClient, caplog: pytest.LogCaptureFixture
) -> None:
    error = aiohttp.InvalidURL("http://host/api/?value=12345")

    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        api_client._log_request("Page", time.monotonic(), None, {}, error)

    (line,) = (r.getMessage() for r in caplog.records if "failed after" in r.getMessage())
    assert line.endswith(" in flight: InvalidURL")
    assert "12345" not in line
