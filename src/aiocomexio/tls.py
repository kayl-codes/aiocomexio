"""TLS certificate pinning for the Comexio IO-Server.

Comexio serves HTTPS with a certificate it signs itself ("Comexio LAN CA"), issued to
comexio.local, so neither the CA nor the host name can be verified. Instead the client pins the
SHA-256 fingerprint of the certificate (ComexioClient's fingerprint argument).
fetch_certificate_fingerprint reads that fingerprint from a bare TLS handshake, before any
credentials go out; Comexio's own admin page lists the same value under its certificate hashes.
fetch_certificate reads the same handshake and adds the certificate's validity period, so a
caller can see when Comexio will replace the certificate it pinned.
"""

import asyncio
import hashlib
import logging
import re
import ssl
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlsplit

from cryptography import x509

from .exceptions import ComexioConnectionError, ComexioDataError

__all__ = [
    "FINGERPRINT_FETCH_TIMEOUT_SEC",
    "CertificateInfo",
    "certificate_info",
    "fetch_certificate",
    "fetch_certificate_fingerprint",
    "format_fingerprint",
    "https_address",
    "normalize_fingerprint",
]

_LOGGER = logging.getLogger(__name__)

# A TLS handshake on the local network takes milliseconds; a host that does not answer within
# this time does not serve HTTPS in any useful way.
FINGERPRINT_FETCH_TIMEOUT_SEC = 10
_HTTPS_DEFAULT_PORT = 443
_SHA256_HEX_LENGTH = 64
# Separators people copy along with a fingerprint: Comexio shows "33:60:55:...", others use spaces or dashes.
_FINGERPRINT_SEPARATORS_RE = re.compile(r"[\s:-]")
_HEX_RE = re.compile(r"[0-9a-f]+", re.ASCII)


def normalize_fingerprint(value: str) -> str:
    """A SHA-256 certificate fingerprint as 64 lowercase hex digits.

    Accepts upper or lower case with ":", "-" or whitespace between the bytes. Any other string, a
    SHA-1 or MD5 fingerprint included, raises ValueError; a non-str raises TypeError.
    """
    if not isinstance(value, str):
        raise TypeError(f"fingerprint must be a str, not {type(value).__name__}")
    digits = _FINGERPRINT_SEPARATORS_RE.sub("", value).lower()
    if len(digits) != _SHA256_HEX_LENGTH or not _HEX_RE.fullmatch(digits):
        raise ValueError(f"{value!r} is not a SHA-256 fingerprint (64 hex digits)")
    return digits


def format_fingerprint(value: str) -> str:
    """A SHA-256 fingerprint the way Comexio displays it: uppercase byte pairs joined by ":"."""
    digits = normalize_fingerprint(value).upper()
    return ":".join(digits[i : i + 2] for i in range(0, len(digits), 2))


def https_address(host: str) -> tuple[str, int]:
    """Host name and port an https:// URL for host reaches: the port in host if any, else 443.

    host is what ComexioClient takes ("hostname", "ip" or either with ":port", IPv6 in brackets).
    Raises ValueError for anything else: a host that does not form a URL, or one that carries a
    path, query, fragment or user info, which would point the URL somewhere other than host.
    """
    message = f"{host!r} is not a host name or address"
    parts = urlsplit(f"https://{host}")
    try:
        port = parts.port
    except ValueError as err:
        raise ValueError(message) from err
    # netloc != host: a path, query or fragment followed; "@": user info precedes the real host.
    if not parts.hostname or parts.netloc != host or "@" in host:
        raise ValueError(message)
    return parts.hostname, _HTTPS_DEFAULT_PORT if port is None else port


@dataclass(frozen=True, slots=True)
class CertificateInfo:
    """The certificate a host serves over HTTPS: its pinnable fingerprint and its validity period.

    fingerprint is the SHA-256 of the DER certificate (64 lowercase hex digits), the value ComexioClient
    pins. not_valid_before and not_valid_after are timezone-aware UTC datetimes, or both None when the
    certificate cannot be parsed as X.509. None means unknown, never unlimited: a caller must not read a
    missing date as a certificate that does not expire. The fingerprint is what the handshake presented
    either way and stays pinnable. Constructing it with only one of the two dates raises ValueError.
    """

    fingerprint: str
    not_valid_before: datetime | None
    not_valid_after: datetime | None

    def __post_init__(self) -> None:
        if (self.not_valid_before is None) != (self.not_valid_after is None):
            raise ValueError("not_valid_before and not_valid_after are either both known or both None")


def certificate_info(certificate: bytes) -> CertificateInfo:
    """Fingerprint and validity period of a DER certificate (as ssl's getpeercert(binary_form=True) gives it).

    Bytes that do not parse as an X.509 certificate still get their fingerprint, with both dates None;
    a caller that wants to report that checks not_valid_after (fetch_certificate logs a warning).
    """
    fingerprint = hashlib.sha256(certificate).hexdigest()
    try:
        parsed = x509.load_der_x509_certificate(certificate)
        not_valid_before, not_valid_after = parsed.not_valid_before_utc, parsed.not_valid_after_utc
    # ValueError: anything cryptography cannot parse. Warning: a deprecation warning cryptography emits
    # for an odd certificate (a negative serial number), raised as an error under "-W error".
    except (ValueError, Warning) as err:
        _LOGGER.debug("Certificate %s has no readable validity period: %r", format_fingerprint(fingerprint), err)
        return CertificateInfo(fingerprint, None, None)
    return CertificateInfo(fingerprint, not_valid_before, not_valid_after)


async def _fetch_peer_certificate(host: str) -> bytes:
    """The DER certificate host presents in a bare TLS handshake (see fetch_certificate for the contract)."""
    hostname, port = https_address(host)
    # Self-signed certificate issued to comexio.local: neither host name nor CA can be verified,
    # the fingerprint read here is what gets pinned (trust on first use, see fetch_certificate).
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)  # NOSONAR
    context.check_hostname = False  # NOSONAR
    context.verify_mode = ssl.CERT_NONE  # NOSONAR
    deadline = asyncio.get_running_loop().time() + FINGERPRINT_FETCH_TIMEOUT_SEC
    try:
        async with asyncio.timeout_at(deadline):
            _reader, writer = await asyncio.open_connection(hostname, port, ssl=context)
    except OSError as err:  # TimeoutError included
        raise ComexioConnectionError(f"TLS handshake with {host!r} failed: {err!r}") from err
    try:
        ssl_object = writer.get_extra_info("ssl_object")
        certificate: bytes | None = ssl_object.getpeercert(binary_form=True) if ssl_object is not None else None
    finally:
        writer.close()
        # The close_notify exchange is a courtesy; a peer that does not answer it must not stall the caller.
        with suppress(OSError):  # TimeoutError included
            async with asyncio.timeout_at(deadline):
                await writer.wait_closed()
    if not certificate:
        raise ComexioDataError(f"{host!r} completed the TLS handshake without presenting a certificate")
    return certificate


async def fetch_certificate(host: str) -> CertificateInfo:
    """Fingerprint and validity period of the certificate host serves over HTTPS.

    Only the TLS handshake runs: no HTTP request and no credentials are sent, and the certificate
    is deliberately not verified — reading it is the point. Pass the fingerprint to ComexioClient as
    fingerprint so every later request checks the server presents this very certificate. This is
    trust on first use: whoever answers this one handshake gets pinned, so fetch once (at setup)
    and store the fingerprint rather than fetching it anew on every start. Fetching again later to
    read the validity period is fine as long as the result is compared with the stored pin and not
    pinned itself: a validity period belongs to the stored pin only when the fingerprints match.

    A certificate that does not parse as X.509 is no error: it comes back with both dates None (see
    CertificateInfo) and a warning is logged. Handshake and closing share one deadline of
    FINGERPRINT_FETCH_TIMEOUT_SEC. Raises ComexioConnectionError if host cannot be reached over TLS
    by then (no HTTPS on that port included), ComexioDataError if the handshake completed without a
    certificate. A host that does not form a URL raises ValueError.
    """
    info = certificate_info(await _fetch_peer_certificate(host))
    if info.not_valid_after is None:
        _LOGGER.warning(
            "%r serves a certificate (SHA-256 %s) whose validity period cannot be read",
            host,
            format_fingerprint(info.fingerprint),
        )
    else:
        _LOGGER.debug(
            "%r serves a certificate with SHA-256 fingerprint %s, valid until %s",
            host,
            format_fingerprint(info.fingerprint),
            info.not_valid_after.isoformat(),
        )
    return info


async def fetch_certificate_fingerprint(host: str) -> str:
    """The SHA-256 fingerprint (64 lowercase hex digits) of the certificate host serves over HTTPS.

    Same handshake, deadline and errors as fetch_certificate, but the certificate is only hashed, never
    parsed: what the pin depends on stays independent of the X.509 parser.
    """
    fingerprint = hashlib.sha256(await _fetch_peer_certificate(host)).hexdigest()
    _LOGGER.debug("%r serves a certificate with SHA-256 fingerprint %s", host, format_fingerprint(fingerprint))
    return fingerprint
