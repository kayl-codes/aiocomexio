"""The debug line about a failed or late request (#289): idle time, connection, requests in flight."""

import ast
import asyncio
import logging
import pathlib
import re
import socket
import time
import traceback
import warnings
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager, suppress
from types import SimpleNamespace
from typing import TypeIs

import aiohttp
import pytest
from aiohttp import http_exceptions, web
from aiohttp.client_reqrep import ConnectionKey
from multidict import CIMultiDictProxy
from yarl import URL

from aiocomexio import (
    ComexioClient,
    ComexioConnectionError,
    ComexioResponseError,
    connection_trace_config,
    session_kwargs,
)
from aiocomexio import client as client_module
from aiocomexio.client import _IDLE_LOG_MIN_SEC
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


def _stripped_cause(error: BaseException) -> BaseException | None:
    """What _raise_from_stripped raises a ComexioError from: error, stripped in place, or None if stripping failed."""
    with pytest.raises(ComexioConnectionError) as raised:
        client_module._raise_from_stripped(ComexioConnectionError("Page failed"), error)
    return raised.value.__cause__


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

    # Only the client's own line about the value it read: any diagnostic line, whatever its wording, would be a second.
    assert [r.getMessage() for r in caplog.records if r.name == LOGGER] == ["Writing marker M5: '1'"]


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


@contextmanager
def _refused_port() -> Iterator[int]:
    """A local port that refuses every connection while the block runs.

    Bound but never listening, and held open: no other process can be given the port in between, and a fixed one
    (such as 1) would be answered by whatever happens to listen there.
    """
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        yield probe.getsockname()[1]


async def test_a_connect_that_fails_is_still_a_new_connection(caplog: pytest.LogCaptureFixture) -> None:
    async with aiohttp.ClientSession(**session_kwargs()) as session:
        with _refused_port() as port:
            client = ComexioClient(f"127.0.0.1:{port}", USERNAME, PASSWORD, session=session, api_username="api")

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


async def test_the_idle_time_is_the_time_since_the_last_answer(
    api_client: ComexioClient, comexio: FakeComexio, caplog: pytest.LogCaptureFixture
) -> None:
    idle_sec = _IDLE_LOG_MIN_SEC + 2
    comexio.serve_text("GET", API_PATH, "1")
    await api_client.set_marker_value(5, 1)
    stamped_at = time.monotonic()
    api_client._last_response_at = stamped_at - idle_sec

    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        await api_client.set_marker_value(5, 0)
    elapsed = time.monotonic() - stamped_at

    (line,) = (r.getMessage() for r in caplog.records if "answered after" in r.getMessage())
    match = _expected_line("Writing marker M5", "answered", idle=r"(\d+\.\d) s", connection="reused").fullmatch(line)
    assert match is not None
    # The idle time grows by what passed between the stamp and the request, at most by the time the test measured
    # (the line shows one decimal, hence the 0.05).
    assert idle_sec <= float(match[1]) <= idle_sec + elapsed + 0.05


@pytest.mark.parametrize(
    ("idle_sec", "logged"),
    [(_IDLE_LOG_MIN_SEC + 0.5, True), (_IDLE_LOG_MIN_SEC, True), (_IDLE_LOG_MIN_SEC - 0.5, False)],
)
async def test_an_answered_request_is_logged_from_the_idle_threshold_on(
    api_client: ComexioClient,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    idle_sec: float,
    logged: bool,
) -> None:
    started = 1000.0  # fixed: the idle time is `started` minus the last answer, so no clock and no rounding is in it
    # The client's clock only (the event loop keeps its own): the request took 0.25 s.
    monkeypatch.setattr(client_module, "time", SimpleNamespace(monotonic=lambda: started + 0.25))
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        api_client._log_request("Page", started, started - idle_sec, {CONNECTION_KEY: [CONNECTION_REUSED]}, None)

    line = (
        f"Page: answered after 0.250 s; idle {idle_sec:.1f} s before it, connection {CONNECTION_REUSED}, "
        "0 other request(s) in flight"
    )
    assert [r.getMessage() for r in caplog.records if r.name == LOGGER] == ([line] if logged else [])


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

    with caplog.at_level(logging.DEBUG, logger=LOGGER), pytest.raises(ComexioConnectionError) as raised:
        await api_client.set_marker_value(5, 0)

    _assert_the_dropped_write_of_m5(raised.value)
    (message,) = (r.getMessage() for r in caplog.records if "Request diagnostics" in r.getMessage())
    assert message == "Request diagnostics for Writing marker M5 failed"


def _assert_the_dropped_write_of_m5(error: ComexioConnectionError) -> None:
    """The request's own error, not one a failing log call put in its place: its text and its cause name the drop."""
    assert str(error).startswith("Writing marker M5 failed: ServerDisconnectedError")
    assert isinstance(error.__cause__, aiohttp.ServerDisconnectedError)
    assert error.__context__ is error.__cause__


class _RaisingLogger:
    """A logger whose handlers raise on every record of the request diagnostics (the line and its fallback)."""

    def __init__(self) -> None:
        self.attempted: list[str] = []

    def isEnabledFor(self, _level: int) -> bool:
        return True

    def __getattr__(self, _name: str) -> Callable[..., None]:
        return lambda *_args, **_kwargs: None  # info, warning, ...: nothing to raise

    def debug(self, message: str, *_args: object, **_kwargs: object) -> None:
        # Other debug records of the client (the answer's body) are not the diagnostics.
        if " after " in message or message.startswith("Request diagnostics"):
            self.attempted.append(message)
            raise RuntimeError("the handler broke")


async def test_a_logger_that_raises_on_both_records_changes_no_result(
    api_client: ComexioClient, comexio: FakeComexio, monkeypatch: pytest.MonkeyPatch
) -> None:
    logger = _RaisingLogger()
    monkeypatch.setattr(client_module, "_LOGGER", logger)
    comexio.serve("GET", API_PATH, _close_connection)
    with pytest.raises(ComexioConnectionError) as raised:
        await api_client.set_marker_value(5, 0)
    _assert_the_dropped_write_of_m5(raised.value)
    # The line and then its fallback were tried, and both raised.
    assert [m.split(" ")[0] for m in logger.attempted] == ["%s:", "Request"]

    # A successful answer after a pause is logged too: its line and the fallback were tried.
    comexio.serve_text("GET", API_PATH, "1")
    api_client._last_response_at = time.monotonic() - 10.0
    await api_client.set_marker_value(5, 1)
    assert len(logger.attempted) == 4


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
    sent: list[str | None] = []

    async def server_error(request: web.Request) -> web.StreamResponse:
        sent.append(request.headers.get("Authorization"))
        return web.Response(status=500, headers={"Set-Cookie": "session=secret123"})

    comexio.serve("GET", API_PATH, server_error)
    async with aiohttp.ClientSession(**session_kwargs(), raise_for_status=True) as strict_session:
        client = ComexioClient(
            comexio.host, USERNAME, PASSWORD, session=strict_session, api_username="api", api_password="pw"
        )

        with caplog.at_level(logging.DEBUG, logger=LOGGER), pytest.raises(ComexioConnectionError) as raised:
            await client.set_marker_value(5, 12345)

    (line,) = (r.getMessage() for r in caplog.records if "failed after" in r.getMessage())
    assert _expected_line(
        "Writing marker M5",
        "failed",
        idle=re.escape("n/a (first request)"),
        connection="new",
        error=re.escape("ClientResponseError (status 500)"),
    ).fullmatch(line)
    assert "12345" not in line  # the value an API write carries stays out of the line
    # So does the error the caller gets: its request, URL and Authorization header are not in the message.
    assert str(raised.value) == "Writing marker M5 failed: ClientResponseError (status 500)"
    # Nor in its cause, which a traceback prints and a caller may re-raise: the request without its query and headers.
    cause = raised.value.__cause__
    assert isinstance(cause, aiohttp.ClientResponseError)
    assert (cause.status, cause.request_info.real_url.path, cause.request_info.real_url.query_string) == (
        500,
        API_PATH,
        "",
    )
    # The request did carry the credentials (base64 of "api:pw"); the cause keeps no request or answer header.
    assert sent == ["Basic YXBpOnB3"]
    assert (dict(cause.request_info.headers), cause.headers, cause.history) == ({}, None, ())
    assert (type(cause.request_info.headers), cause.message) == (CIMultiDictProxy, "Internal Server Error")
    # What a traceback prints of the chain (its source lines show the test's own call, value included), and the repr.
    shown = "".join(traceback.format_exception_only(cause)) + repr(cause) + repr(cause.args)
    assert "12345" not in shown
    assert "YXBpOnB3" not in shown
    assert "secret123" not in shown


def test_a_response_error_without_a_request_info_is_still_stripped_of_the_answer(api_client: ComexioClient) -> None:
    # Not a RequestInfo (a test double, as some callers build): there is no request to strip, the answer still goes.
    error = aiohttp.ClientResponseError(
        SimpleNamespace(),  # type: ignore[arg-type]  # deliberately not a RequestInfo
        (),
        status=500,
        headers={"Set-Cookie": "session=secret123"},  # type: ignore[arg-type]  # any mapping
    )

    raised = api_client._transport_error("Page", error)

    assert str(raised) == "Page failed: ClientResponseError (status 500)"
    assert _stripped_cause(error) is error
    assert (error.status, error.headers, error.history) == (500, None, ())


_SENT_REASON = "token=12345 " + "x" * client_module._DIAGNOSTIC_ERROR_MAX_CHARS


@pytest.mark.parametrize(
    ("error_class", "status", "kept"),
    [
        # A reason phrase as the server or a proxy sent it: the standard one of the status instead.
        (aiohttp.ClientResponseError, 502, "Bad Gateway"),
        (aiohttp.ClientHttpProxyError, 407, "Proxy Authentication Required"),
        (aiohttp.ClientResponseError, 599, ""),  # a status without a standard phrase
        # aiohttp's own message (it names the content type the server sent): kept, cut like the debug line.
        (aiohttp.ContentTypeError, 200, f"{_SENT_REASON[: client_module._DIAGNOSTIC_ERROR_MAX_CHARS]}…"),
    ],
    ids=["status", "proxy", "unknown-status", "aiohttp-message"],
)
def test_the_message_of_a_response_error_is_bounded_before_it_becomes_the_cause(
    error_class: type[aiohttp.ClientResponseError], status: int, kept: str
) -> None:
    error = error_class(
        SimpleNamespace(),  # type: ignore[arg-type]  # no request needed here
        (),
        status=status,
        message=_SENT_REASON,
    )

    assert _stripped_cause(error) is error
    assert error.message == kept


def _connection_key(*, through_proxy: bool = True) -> ConnectionKey:
    """The connection key of a request, through a proxy that needs credentials or direct (whatever fields it has)."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)  # BasicAuth, deprecated in newer aiohttp
        auth = aiohttp.BasicAuth("proxyuser", "12345")
    key = ConnectionKey._make([None] * len(ConnectionKey._fields))._replace(
        host="comexio", port=443, is_ssl=True, ssl=True
    )
    return key._replace(proxy=URL("http://proxyuser:12345@proxy:3128"), proxy_auth=auth) if through_proxy else key


_REFUSED = OSError(111, "Connection refused")


@pytest.mark.parametrize(
    ("error", "proxy"),
    [
        (aiohttp.ClientConnectorError(_connection_key(), _REFUSED), URL("http://proxy:3128")),
        (aiohttp.ClientConnectorSSLError(_connection_key(), OSError(1, "handshake failed")), URL("http://proxy:3128")),
        (aiohttp.ClientConnectorCertificateError(_connection_key(), ValueError("bad cert")), URL("http://proxy:3128")),
        (aiohttp.ClientConnectorDNSError(_connection_key(), OSError(-2, "Name unknown")), URL("http://proxy:3128")),
        (aiohttp.ClientProxyConnectionError(_connection_key(), _REFUSED), URL("http://proxy:3128")),
        (aiohttp.ClientConnectorError(_connection_key(through_proxy=False), _REFUSED), None),
    ],
    ids=["connect", "tls", "certificate", "dns", "proxy", "no-proxy"],
)
def test_a_connection_error_through_a_proxy_does_not_show_its_credentials(
    api_client: ComexioClient, error: aiohttp.ClientConnectorError, proxy: URL | None
) -> None:
    raised = api_client._transport_error("Page", error)
    diagnostic = client_module._diagnostic_error_text(error)

    assert _stripped_cause(error) is error
    # The repr of the error, with the key as the cause now carries it.
    assert str(raised) == f"Page failed: {type(error).__name__}({error.args[0]!r}, {error.args[1]!r})"
    assert str(raised).startswith(f"Page failed: {type(error).__name__}(ConnectionKey(host='comexio', port=443")
    # The error keeps what tells where it failed; the proxy stays, without its user info.
    assert (error.host, error.port, error.args[0].proxy, error.args[0].proxy_auth) == ("comexio", 443, proxy, None)
    shown = str(raised) + diagnostic + "".join(traceback.format_exception_only(error)) + repr(error)
    assert "12345" not in shown
    assert "proxyuser" not in shown


async def test_a_connection_error_of_aiohttp_carries_the_key_that_is_stripped() -> None:
    # The tests above build the key themselves: aiohttp's own connector has to raise with that class, else the key
    # would be left as it is without a sign.
    with _refused_port() as port:
        async with aiohttp.ClientSession() as session:
            with pytest.raises(aiohttp.ClientConnectorError) as raised:
                await session.get(f"http://127.0.0.1:{port}/api/?value=12345")

    assert isinstance(raised.value.args[0], ConnectionKey)
    assert _stripped_cause(raised.value) is raised.value
    assert (raised.value.host, raised.value.port, raised.value.args[0].proxy_auth) == ("127.0.0.1", port, None)


def _is_transport_error_call(node: ast.expr) -> TypeIs[ast.Call]:
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "_transport_error"


def test_every_transport_error_is_raised_from_its_stripped_cause() -> None:
    # Stripping is not part of _transport_error (a consumer overrides it): each raise has to ask for it itself.
    calls = [
        node
        for node in ast.walk(ast.parse(pathlib.Path(client_module.__file__).read_text(encoding="utf-8")))
        if isinstance(node, ast.Call)
    ]
    built = [call for call in calls if _is_transport_error_call(call)]
    raised = [
        (ast.unparse(call.args[0].args[1]), ast.unparse(call.args[1]))
        for call in calls
        if isinstance(call.func, ast.Name)
        and call.func.id == "_raise_from_stripped"
        and _is_transport_error_call(call.args[0])
    ]

    assert built
    # Each one built goes to _raise_from_stripped, as the error raised from the same error it was built from.
    assert [cause for _err, cause in raised] == [err for err, _cause in raised]
    assert len(raised) == len(built)


async def test_an_error_status_the_session_raises_still_moves_the_answer_time(
    comexio: FakeComexio,
) -> None:
    async def server_error(_request: web.Request) -> web.StreamResponse:
        return web.Response(status=500)

    comexio.serve("GET", API_PATH, server_error)
    comexio.serve("POST", SLOW_PATH, server_error)
    async with aiohttp.ClientSession(**session_kwargs(), raise_for_status=True) as strict_session:
        client = ComexioClient(comexio.host, USERNAME, PASSWORD, session=strict_session, api_username="api")

        with pytest.raises(ComexioConnectionError):
            await client.set_marker_value(5, 0)
        assert _answered_at(client) is not None

        # An old value, so the move shows on a coarse clock too.
        client._last_response_at = 1.0
        with pytest.raises(ComexioConnectionError):
            await client._request_redirect("POST", SLOW_PATH, what="Form")
        assert _answered_at(client) != 1.0


async def test_a_form_request_that_fails_below_http_is_no_answer(
    api_client: ComexioClient, comexio: FakeComexio
) -> None:
    comexio.serve("POST", SLOW_PATH, _close_connection)

    with pytest.raises(ComexioConnectionError):
        await api_client._request_redirect("POST", SLOW_PATH, what="Form")

    assert _answered_at(api_client) is None


async def test_a_proxy_refusal_is_no_answer_of_the_server(api_client: ComexioClient) -> None:
    info = SimpleNamespace(real_url="http://proxy")
    api_client._note_status_answer(aiohttp.ClientHttpProxyError(info, (), status=407))  # type: ignore[arg-type]
    assert _answered_at(api_client) is None

    api_client._note_status_answer(aiohttp.ClientResponseError(info, (), status=500))  # type: ignore[arg-type]
    assert _answered_at(api_client) is not None


async def test_the_line_reports_duration_and_idle_time_from_the_clock(
    api_client: ComexioClient, caplog: pytest.LogCaptureFixture
) -> None:
    started = time.monotonic() - 2.0
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        api_client._log_request("Page", started, started - 30.0, {CONNECTION_KEY: [CONNECTION_REUSED]}, None)
    ceiling = time.monotonic() - started  # the line cannot show more than the time the test measured

    (line,) = (r.getMessage() for r in caplog.records if "answered after" in r.getMessage())
    match = re.fullmatch(
        r"Page: answered after (\d+\.\d{3}) s; idle 30\.0 s before it, connection reused, "
        r"0 other request\(s\) in flight",
        line,
    )
    assert match is not None
    # The runner may be slow, never fast: the duration counts from `started`.
    assert 2.0 <= float(match[1]) <= ceiling + 0.001  # the line is rounded to milliseconds


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
    shown = repr(error) if extra <= 0 else f"{repr(error)[:limit]}…"
    assert _expected_line(
        "Page", "failed", idle=re.escape("n/a (first request)"), connection="unknown", error=re.escape(shown)
    ).fullmatch(line)


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


async def test_a_request_of_the_login_itself_is_counted_while_it_runs(
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

    try:
        async with gate.login():  # gives up waiting for the held request
            assert gate.requests_in_flight == 1
            async with gate.request():  # the login's own request, sent from its task
                assert gate.requests_in_flight == 2
            assert gate.requests_in_flight == 1
    finally:
        release.set()  # a failed assertion above must not leave the held request pending
        await task
    assert gate.requests_in_flight == 0


async def test_a_failing_request_of_the_login_itself_is_no_longer_counted(api_client: ComexioClient) -> None:
    gate = api_client._gate

    async def fail_in_request() -> None:
        async with gate.request():
            raise RuntimeError("the request failed")

    async with gate.login():
        with pytest.raises(RuntimeError):
            await fail_in_request()
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
    assert _expected_line(
        "Page", "failed", idle=re.escape("n/a (first request)"), connection="unknown", error="InvalidURL"
    ).fullmatch(line)
    assert "12345" not in line


@pytest.mark.parametrize(
    ("url", "description", "kept"),
    [
        ("http://host/api/?value=12345", None, "http://host/api/"),
        ("http://host/api/?value=12345", "no host", "http://host/api/ - no host"),
        ("http://api:12345@host/api/#12345", None, "http://host/api/"),
        ("http://api:pw@12345@host/api/", None, "http://host/api/"),  # an "@" in the password
        ("api:12345@host:80/api/", None, "host:80/api/"),  # no scheme
        ("/api/?value=12345#x", None, "/api/"),
        (URL("http://api:12345@host/api/?value=12345#x"), None, "http://host/api/"),
        (URL("/api/?value=12345"), None, "/api/"),
    ],
)
def test_an_invalid_url_error_loses_its_query_before_it_becomes_the_cause(
    api_client: ComexioClient, url: str | URL, description: str | None, kept: str
) -> None:
    error = aiohttp.InvalidURL(url, description)  # built here: the client changes it
    # As aiohttp raises it: from yarl's error, while handling it (so it is both cause and context).
    try:
        raise ValueError("yarl's message: http://api:12345@host/api/?value=12345")
    except ValueError as yarl_error:
        with suppress(aiohttp.InvalidURL):
            raise error from yarl_error

    raised = api_client._transport_error("Page", error)

    assert str(raised) == "Page failed: InvalidURL"
    assert _stripped_cause(error) is error
    # The cause keeps its type and URL type, without query, fragment, user info and the error it came from.
    assert (type(error), type(error.url), str(error)) == (aiohttp.InvalidURL, type(url), kept)
    assert "12345" not in "".join(traceback.format_exception_only(error)) + repr(error.args)
    assert (error.__cause__, error.__context__, error.__suppress_context__) == (None, None, True)


async def _bad_status_line(request: web.Request) -> web.StreamResponse:
    assert request.transport is not None
    request.transport.write(b"HTTP/1.1 2x0 secret123\r\n\r\n")
    request.transport.close()
    return web.Response()


async def test_an_answer_that_cannot_be_parsed_leaves_no_raw_byte_in_the_cause(
    api_client: ComexioClient, comexio: FakeComexio
) -> None:
    comexio.serve("GET", API_PATH, _bad_status_line)

    with pytest.raises(ComexioConnectionError) as raised:
        await api_client.set_marker_value(5, 0)

    cause = raised.value.__cause__
    # aiohttp puts the raw status line into the message and raises from the parser's error, which holds it too.
    assert isinstance(cause, aiohttp.ClientResponseError)
    # The message names the parser's error instead (which one depends on aiohttp's C or Python parser).
    assert issubclass(getattr(http_exceptions, cause.message), http_exceptions.HttpProcessingError)
    assert (cause.status, cause.__cause__, cause.__context__) == (400, None, None)
    assert "secret123" not in "".join(traceback.format_exception(raised.value))


async def test_a_failing_debug_line_does_not_log_the_unstripped_error_of_the_request(
    api_client: ComexioClient,
    comexio: FakeComexio,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(*_args: object) -> None:
        raise RuntimeError("the line itself broke")

    monkeypatch.setattr(api_client, "_log_request", broken)
    comexio.serve("GET", API_PATH, _bad_status_line)

    with caplog.at_level(logging.DEBUG, logger=LOGGER), pytest.raises(ComexioConnectionError):
        await api_client.set_marker_value(5, 0)

    (record,) = (r for r in caplog.records if "Request diagnostics" in r.getMessage())
    # The record carries the traceback of the line's own failure, without the request's error it was raised under.
    assert record.exc_info is not None
    logged = "".join(traceback.format_exception(record.exc_info[1]))
    assert "the line itself broke" in logged
    assert "secret123" not in logged + caplog.text


def _broken_strip(_error: BaseException) -> None:
    raise TypeError("http://host/api/?value=12345")


def test_a_failure_while_stripping_keeps_the_requests_own_error(
    api_client: ComexioClient, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(client_module, "_strip_invalid_url", _broken_strip)
    error = aiohttp.InvalidURL("http://host/api/?value=12345")

    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        built = api_client._transport_error("Page", error)
        # Raised where it would be: in the block that handles the request's error.
        try:
            raise error
        except aiohttp.InvalidURL as handled:
            with pytest.raises(ComexioConnectionError) as raised:
                client_module._raise_from_stripped(built, handled)

    assert (raised.value, str(built)) == (built, "Page failed: InvalidURL")
    # Neither its cause nor its context: following the chain does not reach the unstripped error.
    assert (built.__cause__, built.__context__, built.__suppress_context__) == (None, None, True)
    # Logged by the classes only: the failure's own message may hold what was to go.
    assert [(r.levelno, r.getMessage()) for r in caplog.records if r.name == LOGGER] == [
        (
            logging.WARNING,
            "Could not strip the request from InvalidURL (TypeError): raised without it as the cause or context",
        )
    ]


def test_a_warning_that_raises_while_stripping_failed_keeps_the_requests_own_error(
    api_client: ComexioClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempted: list[object] = []

    def broken(*args: object, **_kwargs: object) -> None:
        attempted.append(args[0])
        raise RuntimeError("the record itself broke")

    monkeypatch.setattr(client_module, "_strip_invalid_url", _broken_strip)
    monkeypatch.setattr(client_module._LOGGER, "warning", broken)
    error = aiohttp.InvalidURL("http://host/api/?value=12345")
    built = api_client._transport_error("Page", error)

    try:
        raise error
    except aiohttp.InvalidURL as handled:
        with pytest.raises(ComexioConnectionError) as raised:
            client_module._raise_from_stripped(built, handled)

    assert raised.value is built
    assert len(attempted) == 1
    # What the warning raised is gone with the unstripped error: neither is reachable from the request's error.
    assert (built.__cause__, built.__context__, built.__suppress_context__) == (None, None, True)


async def test_an_error_that_could_not_be_stripped_is_not_chained(
    api_client: ComexioClient, comexio: FakeComexio, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(client_module, "_strip_response_error", _broken_strip)
    comexio.serve("GET", API_PATH, _bad_status_line)

    with pytest.raises(ComexioConnectionError) as raised:
        await api_client.set_marker_value(5, 0)

    assert (
        str(raised.value),
        raised.value.__cause__,
        raised.value.__context__,
        raised.value.__suppress_context__,
    ) == ("Writing marker M5 failed: ClientResponseError (status 400)", None, None, True)
    # A traceback does not show the unstripped error (its message is the raw status line).
    assert "secret123" not in "".join(traceback.format_exception(raised.value))


async def _cut_off_in_the_headers(request: web.Request) -> web.StreamResponse:
    assert request.transport is not None
    request.transport.write(b"HTTP/1.1 200 OK\r\nSet-Cookie: session=secret123\r\n")
    request.transport.close()
    return web.Response()


async def test_a_disconnect_in_the_middle_of_an_answer_names_its_status_and_not_its_headers(
    api_client: ComexioClient, comexio: FakeComexio, caplog: pytest.LogCaptureFixture
) -> None:
    comexio.serve("GET", API_PATH, _cut_off_in_the_headers)

    with caplog.at_level(logging.DEBUG, logger=LOGGER), pytest.raises(ComexioConnectionError) as raised:
        await api_client.set_marker_value(5, 0)

    (line,) = (r.getMessage() for r in caplog.records if "failed after" in r.getMessage())
    shown = "ServerDisconnectedError (in the middle of an answer, status 200)"
    assert _expected_line(
        "Writing marker M5",
        "failed",
        idle=re.escape("n/a (first request)"),
        connection="new → new (aiohttp tried again)",
        error=re.escape(shown),
    ).fullmatch(line)
    # The error the caller gets names it the same way, and a traceback of it (its cause included) shows no header.
    assert str(raised.value) == f"Writing marker M5 failed: {shown}"
    cause = raised.value.__cause__
    assert isinstance(cause, aiohttp.ServerDisconnectedError)
    # The parsed answer in it is replaced by the text: a plain message, which _carries_an_answer no longer matches.
    assert (cause.message, cause.args) == (shown, (shown,))
    assert "secret123" not in "".join(traceback.format_exception(raised.value))


@pytest.mark.parametrize(
    ("error", "shown"),
    [
        (aiohttp.ServerDisconnectedError(), "ServerDisconnectedError('Server disconnected')"),
        (aiohttp.ServerDisconnectedError("closed by peer"), "ServerDisconnectedError('closed by peer')"),
        (
            aiohttp.ServerDisconnectedError(object()),  # type: ignore[arg-type]  # a message without a status code
            "ServerDisconnectedError (in the middle of an answer, status ?)",
        ),
        (aiohttp.InvalidURL("http://host/api/?value=12345"), "InvalidURL"),
    ],
)
def test_a_transport_error_is_shown_in_full_unless_it_carries_the_request_or_an_answer(
    error: aiohttp.ClientError, shown: str
) -> None:
    assert client_module._transport_error_text(error) == shown


async def test_a_missing_note_is_reported_and_not_shown_as_a_first_request(
    api_client: ComexioClient, caplog: pytest.LogCaptureFixture
) -> None:
    notes = {client_module._STARTED_KEY: time.monotonic()}  # without the previous answer time

    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        api_client._log_request_safely("Page", notes, aiohttp.ClientError("x"))

    (record,) = (r for r in caplog.records if r.name == LOGGER)
    assert record.getMessage() == "Request diagnostics for Page failed"
    assert record.exc_info is not None
    assert record.exc_info[0] is KeyError
    assert record.exc_info[1] is not None
    assert record.exc_info[1].args == (client_module._PREVIOUS_RESPONSE_KEY,)
