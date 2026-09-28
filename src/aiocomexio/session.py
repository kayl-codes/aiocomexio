"""Helpers for the aiohttp session a consumer creates and hands to ComexioClient.

The client never creates or closes a session itself. These helpers give the settings
a Comexio session needs: a request timeout, a cookie jar that accepts cookies from a bare
IP address, and optional "still waiting" logging for slow requests.
"""

import asyncio
import ipaddress
import logging
import re
from contextlib import suppress
from types import SimpleNamespace
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)

# Comexio serializes requests server-side; right after a heavy write batch a single admin call
# can take well over a minute. Without any timeout a stalled response (e.g. mid-firmware-update)
# would hang the caller forever.
DEFAULT_TIMEOUT_SEC = 120
DEFAULT_PROGRESS_LOG_INTERVAL_SEC = 10

_LOCAL_HOSTNAME_RE = re.compile(r"^(?:localhost|[a-zA-Z0-9_-]+\.local|[a-zA-Z0-9_-]+\.lan|[a-zA-Z0-9_-]+\.home)\.?$")


def is_local_address(host: str) -> bool:
    """True if host (optionally with :port, IPv6 in brackets) is a private/loopback/link-local IP or local name."""
    if not host:
        return False

    host = host.strip()
    if host.startswith("["):
        closing = host.find("]")
        if closing == -1:
            return False
        host = host[1:closing]
    elif ":" in host:
        host, _, _ = host.partition(":")

    with suppress(ValueError):
        ip = ipaddress.ip_address(host)
        if ip.is_private or ip.is_loopback or ip.is_link_local:
            return True
    return bool(_LOCAL_HOSTNAME_RE.match(host))


def session_kwargs(
    host: str,
    *,
    timeout: float = DEFAULT_TIMEOUT_SEC,
    progress_log_interval: float | None = None,
) -> dict[str, Any]:
    """Keyword arguments for the aiohttp.ClientSession a ComexioClient for host should use.

    Comexio's admin login is cookie-based. aiohttp's default cookie jar drops cookies from a bare
    IP address, so for a local host the jar is created with unsafe=True. For a non-local host the
    default jar is kept. Each session needs its own cookie jar — two clients sharing one would
    share (and overwrite) one login, and the client clears the jar on every login. Pass
    progress_log_interval (e.g. DEFAULT_PROGRESS_LOG_INTERVAL_SEC) to log slow requests.
    """
    kwargs: dict[str, Any] = {"timeout": aiohttp.ClientTimeout(total=timeout)}
    if is_local_address(host):
        kwargs["cookie_jar"] = aiohttp.CookieJar(unsafe=True)
    if progress_log_interval is not None:
        kwargs["trace_configs"] = [progress_trace_config(progress_log_interval)]
    return kwargs


def progress_trace_config(interval: float = DEFAULT_PROGRESS_LOG_INTERVAL_SEC) -> aiohttp.TraceConfig:
    """TraceConfig that logs an INFO line every interval seconds until a request's response headers arrive.

    A fast request never logs anything. A long, otherwise silent wait becomes visible instead of
    looking hung; a body download that stalls after the headers is not covered (the session
    timeout still ends it). aiohttp gives every request its own trace context, so one instance can serve a
    whole session with concurrent requests.
    """

    # aiohttp requires async trace callbacks even when they never await (python:S7503).
    async def on_start(  # NOSONAR
        _session: aiohttp.ClientSession, ctx: SimpleNamespace, params: aiohttp.TraceRequestStartParams
    ) -> None:
        ctx.progress_task = asyncio.ensure_future(_log_while_waiting(params.method, params.url.path, interval))

    async def on_done(_session: aiohttp.ClientSession, ctx: SimpleNamespace, _params: Any) -> None:
        task: asyncio.Future[None] | None = getattr(ctx, "progress_task", None)
        if task is None:
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    trace_config = aiohttp.TraceConfig()
    trace_config.on_request_start.append(on_start)
    trace_config.on_request_end.append(on_done)
    trace_config.on_request_exception.append(on_done)
    return trace_config


async def _log_while_waiting(method: str, path: str, interval: float) -> None:
    """Log a 'still waiting' line every interval seconds until cancelled."""
    elapsed = 0.0
    while True:
        await asyncio.sleep(interval)
        elapsed += interval
        _LOGGER.info("Still waiting for Comexio: %s %s (%.0fs)", method, path, elapsed)
