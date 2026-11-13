"""Asyncio-native Python client for the Comexio IO-Server."""

from importlib.metadata import version

from .api_settings import (
    ACCESS_FORBIDDEN,
    ACCESS_READ,
    ACCESS_READ_WRITE,
    ACCESS_SELECTION,
    WRITE_TARGET_IO,
    WRITE_TARGET_KNX,
    WRITE_TARGET_MARKER,
    ApiSettings,
)
from .client import (
    ComexioClient,
    CreatedFunctionPlan,
    IoAddress,
    LiveStates,
    PlacedModuleBlock,
    RawConfig,
    SystemInfo,
    WebioBaseInfo,
    WebioCommandForm,
    io_written_over_admin_session,
)
from .doorstation import Doorstation
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
from .extension import DOORSTATION_TYPE_ID, ExtensionInfo
from .onewire import OneWireSensor, OneWireStatus
from .scrape import TimeServerSettings
from .session import connection_trace_config, is_local_address, progress_trace_config, session_kwargs
from .tls import fetch_certificate_fingerprint, format_fingerprint, normalize_fingerprint

__version__ = version("aiocomexio")

__all__ = [
    "ACCESS_FORBIDDEN",
    "ACCESS_READ",
    "ACCESS_READ_WRITE",
    "ACCESS_SELECTION",
    "ApiSettings",
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
    "DOORSTATION_TYPE_ID",
    "Doorstation",
    "ExtensionInfo",
    "IoAddress",
    "LiveStates",
    "OneWireSensor",
    "OneWireStatus",
    "PlacedModuleBlock",
    "RawConfig",
    "SystemInfo",
    "TimeServerSettings",
    "WRITE_TARGET_IO",
    "WRITE_TARGET_KNX",
    "WRITE_TARGET_MARKER",
    "WebioBaseInfo",
    "WebioCommandForm",
    "__version__",
    "connection_trace_config",
    "fetch_certificate_fingerprint",
    "format_fingerprint",
    "io_written_over_admin_session",
    "is_local_address",
    "normalize_fingerprint",
    "progress_trace_config",
    "session_kwargs",
]
