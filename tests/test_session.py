"""Session helpers: local-address detection, session settings, slow-request logging."""

import asyncio
import logging

import aiohttp
import pytest
from aiohttp import web
from yarl import URL

from aiocomexio import is_local_address, session_kwargs
from tests.fake_comexio import ADMIN_PATH, FakeComexio


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("192.168.0.5", True),
        ("192.168.0.5:8080", True),
        ("10.0.0.1", True),
        ("127.0.0.1", True),
        ("[fe80::1]:80", True),
        ("[fd00::1]", True),
        ("comexio.local", True),
        ("comexio.lan.", True),
        ("localhost", True),
        (" comexio.home ", True),
        ("8.8.8.8", False),
        ("comexio.example.com", False),
        ("[fd00::1", False),
        ("", False),
    ],
)
def test_is_local_address(host: str, expected: bool) -> None:
    assert is_local_address(host) is expected


async def test_session_kwargs_with_progress_logging() -> None:
    kwargs = session_kwargs(timeout=30, progress_log_interval=10)

    assert kwargs["timeout"].total == 30
    assert len(kwargs["trace_configs"]) == 1


@pytest.mark.parametrize("interval", [0, -1, float("nan")])
async def test_session_kwargs_rejects_non_positive_progress_interval(interval: float) -> None:
    with pytest.raises(ValueError, match="positive"):
        session_kwargs(progress_log_interval=interval)


async def test_session_kwargs_logs_nothing_by_default() -> None:
    assert set(session_kwargs()) == {"timeout", "cookie_jar"}


@pytest.mark.parametrize("ip", ["192.168.0.5", "203.0.113.7"])
async def test_session_cookie_jar_keeps_cookies_from_private_and_public_ips(ip: str) -> None:
    # aiohttp's default jar silently drops cookies from any IP host, which would lose the login.
    async with aiohttp.ClientSession(**session_kwargs()) as session:
        session.cookie_jar.update_cookies({"PHPSESSID": "x"}, URL(f"http://{ip}/"))

        assert session.cookie_jar.filter_cookies(URL(f"http://{ip}/admin/"))["PHPSESSID"].value == "x"


async def test_progress_logging_stops_after_the_request(comexio: FakeComexio, caplog: pytest.LogCaptureFixture) -> None:
    async def slow(_request: web.Request) -> web.Response:
        await asyncio.sleep(0.25)
        return web.Response(text="done")

    comexio.serve("GET", ADMIN_PATH, slow)

    with caplog.at_level(logging.INFO, logger="aiocomexio.session"):
        async with aiohttp.ClientSession(**session_kwargs(progress_log_interval=0.1)) as session:
            async with session.get(f"http://{comexio.host}{ADMIN_PATH}") as resp:
                assert await resp.text() == "done"
            # A fast request after the slow one must not keep logging.
            caplog.clear()
            async with session.get(f"http://{comexio.host}/missing") as resp:
                assert resp.status == 404
            await asyncio.sleep(0.15)

    assert "Still waiting" not in caplog.text


async def test_slow_request_progress_line(comexio: FakeComexio, caplog: pytest.LogCaptureFixture) -> None:
    async def slow(_request: web.Request) -> web.Response:
        await asyncio.sleep(0.25)
        return web.Response(text="done")

    comexio.serve("GET", ADMIN_PATH, slow)

    with caplog.at_level(logging.INFO, logger="aiocomexio.session"):
        async with aiohttp.ClientSession(**session_kwargs(progress_log_interval=0.1)) as session:
            async with session.get(f"http://{comexio.host}{ADMIN_PATH}") as resp:
                await resp.text()

    assert f"Still waiting for Comexio: GET {ADMIN_PATH}" in caplog.text


async def test_progress_logging_stops_on_request_exception(
    comexio: FakeComexio, caplog: pytest.LogCaptureFixture
) -> None:
    host = comexio.host
    await comexio.server.close()

    with caplog.at_level(logging.INFO, logger="aiocomexio.session"):
        async with aiohttp.ClientSession(**session_kwargs(progress_log_interval=0.05)) as session:
            with pytest.raises(aiohttp.ClientConnectionError):
                await session.get(f"http://{host}{ADMIN_PATH}")
            # Refused connections can take seconds on Windows, logging while they wait — fine.
            # What must not happen is logging on after the request failed.
            caplog.clear()
            await asyncio.sleep(0.15)

    assert "Still waiting" not in caplog.text
