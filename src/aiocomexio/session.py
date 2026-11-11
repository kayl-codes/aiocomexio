"""Helpers for the aiohttp session a consumer creates and hands to ComexioClient.

The client never creates or closes a session itself. These helpers give the settings
a Comexio session needs: a request timeout, a cookie jar that accepts cookies from a bare
IP address, and optional "still waiting" logging for slow requests.
"""

import asyncio
import ipaddress
import logging
import math
import re
from collections.abc import Iterable
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

# Where connection_trace_config() leaves its findings in a request's trace_request_ctx dict (a list, one entry per
# connection the request was tried on), and the two values.
CONNECTION_KEY = "connection"
CONNECTION_REUSED = "reused"
CONNECTION_NEW = "new"

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
    *,
    timeout: float = DEFAULT_TIMEOUT_SEC,
    progress_log_interval: float | None = None,
    trace_configs: Iterable[aiohttp.TraceConfig] | None = None,
) -> dict[str, Any]:
    """Keyword arguments for the aiohttp.ClientSession a ComexioClient should use.

    Comexio's admin login is cookie-based. aiohttp's default cookie jar drops cookies from a bare
    IP address (private or public), so the jar is always created with unsafe=True; for a host
    name that flag changes nothing. Each session needs its own cookie jar — two clients sharing
    one would share (and overwrite) one login, and the client clears the jar on every login. Pass
    progress_log_interval (e.g. DEFAULT_PROGRESS_LOG_INTERVAL_SEC) to log slow requests.

    The result always carries "trace_configs" (connection_trace_config() for the client's debug line about a failed
    request, plus the progress one if asked, then the caller's own trace_configs): a caller passes its own to this
    function and not to aiohttp.ClientSession as well, which would get the keyword twice (TypeError).
    """
    configs = [connection_trace_config()]
    if progress_log_interval is not None:
        configs.append(progress_trace_config(progress_log_interval))
    configs.extend(trace_configs or ())
    return {
        "timeout": aiohttp.ClientTimeout(total=timeout),
        "cookie_jar": aiohttp.CookieJar(unsafe=True),
        "trace_configs": configs,
    }


def connection_trace_config() -> aiohttp.TraceConfig:
    """TraceConfig that notes on which connections a request was tried: a reused one, a newly opened one.

    It logs nothing itself: it appends CONNECTION_REUSED or CONNECTION_NEW to the list under CONNECTION_KEY in the
    dict a caller passes as trace_request_ctx, and the client reads it back for its debug line about a failed
    request. Two entries mean aiohttp tried again on its own (it does for an idempotent method, GET included, whose
    connection the server dropped). That is what tells a closed idle connection from a server that refuses a
    request. A request without such a dict is left alone.
    """

    # aiohttp requires async trace callbacks even when they never await (python:S7503).
    async def on_reuse(_session: aiohttp.ClientSession, ctx: SimpleNamespace, _params: Any) -> None:  # NOSONAR
        _note_connection(ctx, CONNECTION_REUSED)

    # On the start, not the end: a connect that fails (refused, timed out, TLS) never ends, but is still noted.
    async def on_create(_session: aiohttp.ClientSession, ctx: SimpleNamespace, _params: Any) -> None:  # NOSONAR
        _note_connection(ctx, CONNECTION_NEW)

    trace_config = aiohttp.TraceConfig()
    trace_config.on_connection_reuseconn.append(on_reuse)
    trace_config.on_connection_create_start.append(on_create)
    return trace_config


def _note_connection(ctx: SimpleNamespace, kind: str) -> None:
    request_ctx = getattr(ctx, "trace_request_ctx", None)
    if isinstance(request_ctx, dict):
        # A callback that raises would fail the request: a value under the key that is no list is replaced.
        noted = request_ctx.get(CONNECTION_KEY)
        if not isinstance(noted, list):
            noted = request_ctx[CONNECTION_KEY] = []
        noted.append(kind)


def progress_trace_config(interval: float = DEFAULT_PROGRESS_LOG_INTERVAL_SEC) -> aiohttp.TraceConfig:
    """TraceConfig that logs an INFO line every interval seconds until a request's response headers arrive.

    A fast request never logs anything. A long, otherwise silent wait becomes visible instead of
    looking hung; a body download that stalls after the headers is not covered (the session
    timeout still ends it). aiohttp gives every request its own trace context, so one instance can serve a
    whole session with concurrent requests. Raises ValueError for a non-positive interval (it
    would turn the logging into a busy loop).
    """
    if interval <= 0 or math.isnan(interval):
        raise ValueError(f"progress log interval must be positive, got {interval}")

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
