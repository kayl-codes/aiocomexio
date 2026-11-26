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
from datetime import datetime, timedelta
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
    "same_authority",
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
# A certificate from the network is parsed only up to this size: a real one is a few KiB, and parsing every
# extension of a huge one costs the caller CPU and memory after the handshake.
_MAX_PARSED_CERTIFICATE_BYTES = 64 * 1024


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
    """The certificate a host serves over HTTPS: its pinnable fingerprint, its validity period and its issuer.

    fingerprint is the SHA-256 of the DER certificate (64 lowercase hex digits), the value ComexioClient
    pins. not_valid_before and not_valid_after are the certificate's dates as certificate_info reads them
    (timezone-aware, UTC), or both None when they cannot be read: the certificate does not parse as
    X.509, or cryptography warns about it and the process runs with warnings as errors ("-W error";
    the library leaves the warning filters alone, so even a parseable certificate then has no dates).
    A certificate over 64 KiB is not parsed at all, so it has no dates, issuer or authority key identifier.
    None means unknown, never unlimited: a caller must not read a missing date as a certificate that
    does not expire. The fingerprint is what the handshake presented either way and stays pinnable.

    issuer is the issuer name as an RFC 4514 string (for example "CN=Comexio LAN CA"). authority_key_id
    is the key identifier of the CA that signed the certificate, as lowercase hex without separators
    (the Authority Key Identifier extension). Both are None when they cannot be read, which includes a
    certificate that does not parse at all (the dates are then unknown too). authority_key_id is also None
    when the certificate carries no key identifier, or when any other extension is malformed (cryptography
    parses them all together; the debug log names the cause). None means unknown, not an empty value: two
    certificates whose authority_key_id is None do not share an authority.
    Both fields are copied from the certificate itself, so anyone can copy them into a certificate of
    their own. Equal values say the fields match, not that the same CA signed both certificates.
    same_authority compares two certificates on these fields and answers False whenever either one is unknown.

    Constructing it with only one of the two dates, a date that is not timezone-aware UTC, a fingerprint in
    any other spelling than 64 lowercase hex digits, an issuer that is not a string or is empty (None is the
    unknown issuer), or an authority_key_id that is not lowercase hex with an even number of digits raises
    ValueError, so a stored pin is compared after normalize_fingerprint.
    """

    fingerprint: str
    not_valid_before: datetime | None
    not_valid_after: datetime | None
    issuer: str | None = None
    authority_key_id: str | None = None

    def __post_init__(self) -> None:
        if (self.not_valid_before is None) != (self.not_valid_after is None):
            raise ValueError("not_valid_before and not_valid_after are either both known or both None")
        for date in (self.not_valid_before, self.not_valid_after):
            if date is not None and date.utcoffset() != timedelta(0):
                raise ValueError(f"{date!r} is not a timezone-aware UTC date")
        if normalize_fingerprint(self.fingerprint) != self.fingerprint:
            raise ValueError(f"{self.fingerprint!r} is not a fingerprint in 64 lowercase hex digits")
        # A non-string issuer would compare equal to an equal non-string one in same_authority.
        if self.issuer is not None and not isinstance(self.issuer, str):
            raise ValueError(f"{self.issuer!r} is not an issuer name; None means an unknown issuer")
        # certificate_info maps an empty issuer to None; a stored "" would compare equal to another empty one.
        if self.issuer == "":
            raise ValueError("issuer is an empty string; None means an unknown issuer")
        # Checked before the regex: fullmatch raises TypeError for a value that is not a string.
        if self.authority_key_id is not None and (
            not isinstance(self.authority_key_id, str)
            or _HEX_RE.fullmatch(self.authority_key_id) is None
            or len(self.authority_key_id) % 2
        ):
            raise ValueError(f"{self.authority_key_id!r} is not an authority key identifier in lowercase hex digits")


def same_authority(first: CertificateInfo, second: CertificateInfo) -> bool:
    """Whether both certificates name the same issuer and authority key identifier, as far as both are known.

    False when either certificate has an unknown issuer or authority key identifier: two unknowns do not
    make a match. A True answer is only as strong as the copied fields (see CertificateInfo), so it is a
    rule for a caller to apply, not proof that the same CA signed both.
    """
    if first.issuer is None or first.authority_key_id is None:
        return False
    return first.issuer == second.issuer and first.authority_key_id == second.authority_key_id


def _issuer(parsed: x509.Certificate, fingerprint: str) -> str | None:
    """The issuer of parsed as an RFC 4514 string; None when it cannot be read or is empty."""
    try:
        # An empty name (no attributes at all) is no issuer to compare: None, so it never equals another empty one.
        return parsed.issuer.rfc4514_string() or None
    # ValueError: an issuer cryptography cannot decode, e.g. invalid UTF-8. TypeError: a BitString attribute under an
    # OID other than X500_UNIQUE_IDENTIFIER. Warning: one warned about under "-W error".
    except (TypeError, ValueError, Warning) as err:
        _LOGGER.debug("Certificate %s: issuer cannot be read: %r", format_fingerprint(fingerprint), err)
        return None


def _authority_key_id(parsed: x509.Certificate, fingerprint: str) -> str | None:
    """The key identifier of the CA that signed parsed, as lowercase hex; None when it has none or cannot be read."""
    try:
        extension = parsed.extensions.get_extension_for_class(x509.AuthorityKeyIdentifier)
        key_id = extension.value.key_identifier
    # Silent on purpose: a certificate without an Authority Key Identifier is the normal case, not a failure.
    except x509.ExtensionNotFound:
        return None
    # DuplicateExtension, UnsupportedGeneralNameType: the extension is malformed. ValueError, Warning: the same
    # when cryptography raises one, or warns about it under "-W error". parsed.extensions parses all extensions
    # at once, so one malformed extension of any kind lands here too.
    except (x509.DuplicateExtension, x509.UnsupportedGeneralNameType, ValueError, Warning) as err:
        _LOGGER.debug(
            "Certificate %s: authority key identifier cannot be read: %r", format_fingerprint(fingerprint), err
        )
        return None
    # An empty key identifier is no identifier: CertificateInfo refuses it, so it counts as none.
    return key_id.hex() if key_id else None


def _read_certificate(certificate: bytes) -> tuple[CertificateInfo, ValueError | Warning | x509.InvalidVersion | None]:
    """certificate_info and, when the dates cannot be read, why not (None when they can)."""
    fingerprint = hashlib.sha256(certificate).hexdigest()
    # cryptography warns about odd but readable certificates (a negative serial number). The warning
    # goes out as it is: changing the warning filters here would change them for every thread of the
    # process. Under "-W error" it is raised instead and the validity counts as unknown, with the cause.
    if len(certificate) > _MAX_PARSED_CERTIFICATE_BYTES:
        return CertificateInfo(fingerprint, None, None), ValueError(
            f"larger than {_MAX_PARSED_CERTIFICATE_BYTES} bytes, not parsed"
        )
    try:
        parsed = x509.load_der_x509_certificate(certificate)
        not_valid_before, not_valid_after = parsed.not_valid_before_utc, parsed.not_valid_after_utc
    # ValueError: anything cryptography cannot parse. InvalidVersion: a version field outside the X.509 versions,
    # an Exception but no ValueError. Warning: a warning raised as an error.
    except (ValueError, Warning, x509.InvalidVersion) as err:
        return CertificateInfo(fingerprint, None, None), err
    info = CertificateInfo(
        fingerprint,
        not_valid_before,
        not_valid_after,
        issuer=_issuer(parsed, fingerprint),
        authority_key_id=_authority_key_id(parsed, fingerprint),
    )
    return info, None


def certificate_info(certificate: bytes) -> CertificateInfo:
    """Fingerprint and validity period of a DER certificate (as ssl's getpeercert(binary_form=True) gives it).

    Bytes whose dates cannot be read (no X.509 certificate, or a cryptography warning raised as an
    error, see CertificateInfo) still get their fingerprint, with both dates None (logged at DEBUG
    only; fetch_certificate logs a warning).
    """
    info, cause = _read_certificate(certificate)
    if cause is not None:
        _LOGGER.debug("Certificate %s has no readable validity period: %r", format_fingerprint(info.fingerprint), cause)
    return info


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

    A certificate whose dates cannot be read (it does not parse as X.509, or cryptography's warning
    about it is raised as an error under "-W error") is no error: it comes back with both dates None
    (see CertificateInfo) and a warning naming the cause is logged, on every call (a caller that
    fetches periodically sees one per fetch). Handshake and closing share one deadline of
    FINGERPRINT_FETCH_TIMEOUT_SEC. Raises ComexioConnectionError if host cannot be reached over TLS
    by then (no HTTPS on that port included), ComexioDataError if the handshake completed without a
    certificate. A host that does not form a URL raises ValueError.
    """
    info, cause = _read_certificate(await _fetch_peer_certificate(host))
    if info.not_valid_after is None:  # exactly when there is a cause
        _LOGGER.warning(
            "%r serves a certificate (SHA-256 %s) whose validity period cannot be read: %r",
            host,
            format_fingerprint(info.fingerprint),
            cause,
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

    Pass it to ComexioClient as fingerprint. Trust on first use: fetch it once, at setup, and store it
    (see fetch_certificate). Same handshake, deadline and errors as fetch_certificate, a host that does
    not form a URL raising ValueError included, but the certificate is only hashed, never parsed: what
    the pin depends on stays independent of the X.509 parser.
    """
    fingerprint = hashlib.sha256(await _fetch_peer_certificate(host)).hexdigest()
    _LOGGER.debug("%r serves a certificate with SHA-256 fingerprint %s", host, format_fingerprint(fingerprint))
    return fingerprint
