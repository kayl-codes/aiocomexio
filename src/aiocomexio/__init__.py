"""Asyncio-native Python client for the Comexio IO-Server."""

from importlib.metadata import version

from .client import (
    ComexioClient,
    CreatedFunctionPlan,
    IoAddress,
    LiveStates,
    RawConfig,
    SystemInfo,
    WebioBaseInfo,
    WebioCommandForm,
)
from .exceptions import (
    ComexioAuthenticationError,
    ComexioCertificateMismatchError,
    ComexioConnectionError,
    ComexioCreatedWithoutIdError,
    ComexioDataError,
    ComexioError,
    ComexioRequestRejectedError,
    ComexioResponseError,
)
from .scrape import TimeServerSettings
from .session import is_local_address, progress_trace_config, session_kwargs
from .tls import fetch_certificate_fingerprint, format_fingerprint, normalize_fingerprint

__version__ = version("aiocomexio")

__all__ = [
    "ComexioAuthenticationError",
    "ComexioCertificateMismatchError",
    "ComexioClient",
    "ComexioConnectionError",
    "ComexioCreatedWithoutIdError",
    "ComexioDataError",
    "ComexioError",
    "ComexioRequestRejectedError",
    "ComexioResponseError",
    "CreatedFunctionPlan",
    "IoAddress",
    "LiveStates",
    "RawConfig",
    "SystemInfo",
    "TimeServerSettings",
    "WebioBaseInfo",
    "WebioCommandForm",
    "__version__",
    "fetch_certificate_fingerprint",
    "format_fingerprint",
    "is_local_address",
    "normalize_fingerprint",
    "progress_trace_config",
    "session_kwargs",
]
