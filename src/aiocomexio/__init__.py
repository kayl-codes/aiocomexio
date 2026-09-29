"""Asyncio-native Python client for the Comexio IO-Server."""

from importlib.metadata import version

from .client import ComexioClient, CreatedFunctionPlan, LiveStates, RawConfig, WebioBaseInfo
from .exceptions import (
    ComexioAuthenticationError,
    ComexioConnectionError,
    ComexioCreatedWithoutIdError,
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
    "ComexioCreatedWithoutIdError",
    "ComexioDataError",
    "ComexioError",
    "ComexioRequestRejectedError",
    "ComexioResponseError",
    "CreatedFunctionPlan",
    "LiveStates",
    "RawConfig",
    "WebioBaseInfo",
    "__version__",
    "is_local_address",
    "progress_trace_config",
    "session_kwargs",
]
