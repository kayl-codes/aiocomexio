"""Asyncio-native Python client for the Comexio IO-Server."""

from importlib.metadata import version

from .client import ComexioClient, LiveStates, RawConfig, WebioBaseInfo
from .exceptions import (
    ComexioAuthenticationError,
    ComexioConnectionError,
    ComexioDataError,
    ComexioError,
    ComexioRequestRejectedError,
    ComexioResponseError,
)
from .session import is_local_address, progress_trace_config, session_kwargs

__version__ = version("aiocomexio")

__all__ = [
    "ComexioAuthenticationError",
    "ComexioClient",
    "ComexioConnectionError",
    "ComexioDataError",
    "ComexioError",
    "ComexioRequestRejectedError",
    "ComexioResponseError",
    "LiveStates",
    "RawConfig",
    "WebioBaseInfo",
    "__version__",
    "is_local_address",
    "progress_trace_config",
    "session_kwargs",
]
