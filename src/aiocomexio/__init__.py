"""Asyncio-native Python client for the Comexio IO-Server."""

from importlib.metadata import version

from .api_settings import (
    ACCESS_FORBIDDEN,
    ACCESS_READ,
    ACCESS_READ_WRITE,
    ACCESS_SELECTION,
    RIGHT_READ,
    RIGHT_WRITE,
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
    WebioTlsSettings,
    io_written_over_admin_session,
)
from .doorstation import Doorstation
from .doorstation_pictures import DoorstationPicture, DoorstationPictureKind, doorstation_picture_file_path
from .exceptions import (
    ComexioAuthenticationError,
    ComexioCertificateMismatchError,
    ComexioConnectionError,
    ComexioCreatedWithoutIdError,
    ComexioDataError,
    ComexioError,
    ComexioInsecureTransportError,
    ComexioRequestRejectedError,
    ComexioResponseError,
)
from .extension import DOORSTATION_TYPE_ID, ExtensionInfo
from .onewire import OneWireSensor, OneWireStatus
from .scrape import TimeServerSettings
from .session import (
    DEFAULT_KEEPALIVE_TIMEOUT_SEC,
    connection_trace_config,
    connector_kwargs,
    is_local_address,
    progress_trace_config,
    session_kwargs,
)
from .tls import (
    CertificateInfo,
    certificate_info,
    fetch_certificate,
    fetch_certificate_fingerprint,
    format_fingerprint,
    normalize_fingerprint,
    same_authority,
)

__version__ = version("aiocomexio")

__all__ = [
    "ACCESS_FORBIDDEN",
    "ACCESS_READ",
    "ACCESS_READ_WRITE",
    "ACCESS_SELECTION",
    "RIGHT_READ",
    "RIGHT_WRITE",
    "ApiSettings",
    "CertificateInfo",
    "ComexioAuthenticationError",
    "ComexioCertificateMismatchError",
    "ComexioClient",
    "ComexioConnectionError",
    "ComexioCreatedWithoutIdError",
    "ComexioDataError",
    "ComexioError",
    "ComexioInsecureTransportError",
    "ComexioRequestRejectedError",
    "ComexioResponseError",
    "CreatedFunctionPlan",
    "DEFAULT_KEEPALIVE_TIMEOUT_SEC",
    "DOORSTATION_TYPE_ID",
    "Doorstation",
    "DoorstationPicture",
    "DoorstationPictureKind",
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
    "WebioTlsSettings",
    "__version__",
    "certificate_info",
    "connection_trace_config",
    "connector_kwargs",
    "doorstation_picture_file_path",
    "fetch_certificate",
    "fetch_certificate_fingerprint",
    "format_fingerprint",
    "io_written_over_admin_session",
    "is_local_address",
    "normalize_fingerprint",
    "progress_trace_config",
    "same_authority",
    "session_kwargs",
]
