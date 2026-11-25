"""Certificate pinning: fingerprint helpers, the bare-handshake fetch and the pinned HTTPS client."""

import asyncio
import base64
import datetime
import hashlib
import logging
import ssl
import warnings
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any, NoReturn

import aiohttp
import pytest
from aiohttp import web
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.utils import CryptographyDeprecationWarning
from cryptography.x509.oid import NameOID

import aiocomexio.tls
from aiocomexio import (
    CertificateInfo,
    ComexioCertificateMismatchError,
    ComexioClient,
    ComexioConnectionError,
    ComexioDataError,
    ComexioError,
    ComexioResponseError,
    certificate_info,
    fetch_certificate,
    fetch_certificate_fingerprint,
    format_fingerprint,
    normalize_fingerprint,
    session_kwargs,
)
from aiocomexio.client import _origin, _same_origin_or_https
from aiocomexio.tls import https_address
from tests.common import load_fixture
from tests.fake_comexio import ADMIN_PATH, LOGIN_PATH, PASSWORD, USERNAME, FakeComexio

# Synthetic: a fingerprint no test certificate has.
FOREIGN_FINGERPRINT = "ab" * 32
API_USER = "api"
API_PASSWORD = "api-secret"  # nosec B105
_API_PATH = "/api/"
_MOVED_PATH = "/admin/moved"
_PLAN_SAVE_PATH = "/admin/function_function_module/save_fub"
_SAVING_PLAN = "Saving function plan 1"
_SYNTHETIC_HOST = "192.168.0.5"


class SelfSignedCertificate:
    """A self-signed certificate made for the test run, the way Comexio signs its own."""

    def __init__(self, directory: Path) -> None:
        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "comexio.test")])
        # Whole seconds: X.509 keeps no fractions, so these are the dates the certificate carries.
        now = datetime.datetime.now(datetime.UTC).replace(microsecond=0)
        self.not_valid_before = now - datetime.timedelta(days=1)
        self.not_valid_after = now + datetime.timedelta(days=1)
        certificate = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(self.not_valid_before)
            .not_valid_after(self.not_valid_after)
            .sign(key, hashes.SHA256())
        )
        self.der = certificate.public_bytes(serialization.Encoding.DER)
        self.fingerprint = hashlib.sha256(certificate.public_bytes(serialization.Encoding.DER)).hexdigest()
        self.issuer = name.rfc4514_string()  # self-signed: the issuer is the subject
        self.cert_path = directory / "cert.pem"
        self.key_path = directory / "key.pem"
        self.cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
        self.key_path.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
            )
        )

    def server_context(self) -> ssl.SSLContext:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(self.cert_path, self.key_path)
        return context


@pytest.fixture(scope="session")
def certificate(tmp_path_factory: pytest.TempPathFactory) -> SelfSignedCertificate:
    return SelfSignedCertificate(tmp_path_factory.mktemp("tls"))


@pytest.fixture
async def tls_comexio(certificate: SelfSignedCertificate) -> AsyncIterator[FakeComexio]:
    fake = FakeComexio()
    # HTTPS only, like a Comexio reached through its certificate.
    await fake.server.start_server(ssl=certificate.server_context())
    yield fake
    await fake.server.close()


@pytest.fixture
async def tls_session(tls_comexio: FakeComexio) -> AsyncIterator[aiohttp.ClientSession]:
    async with aiohttp.ClientSession(**session_kwargs()) as session:
        yield session


def _pinned_client(comexio: FakeComexio, session: aiohttp.ClientSession, fingerprint: str) -> ComexioClient:
    return ComexioClient(
        comexio.host,
        USERNAME,
        PASSWORD,
        session=session,
        api_username=API_USER,
        api_password=API_PASSWORD,
        fingerprint=fingerprint,
    )


def _refused_redirect_message(what: str, location: str) -> str:
    return f"{what} refused: Comexio redirected to {location!r}, outside the pinned HTTPS connection"


# --- fingerprint helpers -------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "33:60:55:7F:C1:47:A6:46:B7:7C:A8:F0:13:C4:55:E5:A7:DC:A8:A0:C2:1A:AF:31:53:79:A1:91:02:C9:01:B6",
        "3360557fc147a646b77ca8f013c455e5a7dca8a0c21aaf315379a19102c901b6",
        " 33 60 55 7f c1 47 a6 46 b7 7c a8 f0 13 c4 55 e5 a7 dc a8 a0 c2 1a af 31 53 79 a1 91 02 c9 01 b6\n",
        "33-60-55-7F-C1-47-A6-46-B7-7C-A8-F0-13-C4-55-E5-A7-DC-A8-A0-C2-1A-AF-31-53-79-A1-91-02-C9-01-B6",
    ],
)
def test_normalize_fingerprint_accepts_the_usual_spellings(value: str) -> None:
    assert normalize_fingerprint(value) == "3360557fc147a646b77ca8f013c455e5a7dca8a0c21aaf315379a19102c901b6"


@pytest.mark.parametrize(
    "value",
    [
        "",
        "34:54:BE:0E:EE:95:2A:89:39:74:14:03:58:AA:5D:B7:9C:99:4C:4C",  # SHA-1
        "E0:C8:5B:26:8E:70:21:4A:4C:42:FD:01:CC:96:C9:D0",  # MD5
        "g" * 64,
        "ab" * 33,
        "٣" * 64,  # non-ASCII digits
        "1ieZ8u1IJ/ZDDpZN8dkDSeF2/cd3wslvDLrx8k+GtrU=",  # Comexio's base64 public-key pin
    ],
)
def test_normalize_fingerprint_rejects_anything_but_sha256(value: str) -> None:
    with pytest.raises(ValueError) as excinfo:
        normalize_fingerprint(value)
    assert str(excinfo.value) == f"{value!r} is not a SHA-256 fingerprint (64 hex digits)"


def test_normalize_fingerprint_rejects_non_strings() -> None:
    value: Any = bytes(32)
    with pytest.raises(TypeError, match="^fingerprint must be a str, not bytes$"):
        normalize_fingerprint(value)


def test_format_fingerprint_matches_comexios_display() -> None:
    assert format_fingerprint("3360557fc147a646b77ca8f013c455e5a7dca8a0c21aaf315379a19102c901b6") == (
        "33:60:55:7F:C1:47:A6:46:B7:7C:A8:F0:13:C4:55:E5:A7:DC:A8:A0:C2:1A:AF:31:53:79:A1:91:02:C9:01:B6"
    )


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("192.168.0.250", ("192.168.0.250", 443)),
        ("Comexio.Local", ("comexio.local", 443)),
        ("comexio.local:8443", ("comexio.local", 8443)),
        ("[fe80::1]:444", ("fe80::1", 444)),
        ("[fe80::1]", ("fe80::1", 443)),
        ("comexio.local:0", ("comexio.local", 0)),  # an explicit port is kept, even 0
    ],
)
def test_https_address_uses_the_port_in_host_or_443(host: str, expected: tuple[str, int]) -> None:
    assert https_address(host) == expected


@pytest.mark.parametrize(
    "host",
    [
        "",
        ":443",
        "comexio:notaport",
        "comexio:99999",
        "comexio.local/admin",
        "comexio.local?x=1",
        "comexio.local#top",
        "attacker.example@comexio.local",
    ],
)
def test_https_address_rejects_malformed_hosts(host: str) -> None:
    with pytest.raises(ValueError) as excinfo:
        https_address(host)
    assert str(excinfo.value) == f"{host!r} is not a host name or address"


# --- fetch_certificate_fingerprint ---------------------------------------------------------


async def test_fetch_certificate_fingerprint_reads_the_served_certificate_without_a_request(
    tls_comexio: FakeComexio, certificate: SelfSignedCertificate
) -> None:
    assert await fetch_certificate_fingerprint(tls_comexio.host) == certificate.fingerprint
    assert tls_comexio.requests == []


async def test_fetch_certificate_fingerprint_of_a_plain_http_server_is_a_connection_error(
    comexio: FakeComexio,
) -> None:
    with pytest.raises(ComexioConnectionError, match=r"^TLS handshake with '127\.0\.0\.1:\d+' failed: "):
        await fetch_certificate_fingerprint(comexio.host)


async def test_fetch_certificate_fingerprint_of_a_closed_port_is_a_connection_error(
    unused_tcp_port: int,
) -> None:
    with pytest.raises(ComexioConnectionError, match=r"^TLS handshake with '127\.0\.0\.1:\d+' failed: "):
        await fetch_certificate_fingerprint(f"127.0.0.1:{unused_tcp_port}")


async def test_fetch_certificate_fingerprint_gives_up_on_a_silent_server(monkeypatch: pytest.MonkeyPatch) -> None:
    async def silent(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await asyncio.sleep(5)  # never answers the ClientHello
        writer.close()

    server = await asyncio.start_server(silent, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    monkeypatch.setattr(aiocomexio.tls, "FINGERPRINT_FETCH_TIMEOUT_SEC", 0.2)
    try:
        with pytest.raises(ComexioConnectionError) as excinfo:
            await fetch_certificate_fingerprint(f"127.0.0.1:{port}")
    finally:
        server.close()
    assert str(excinfo.value) == f"TLS handshake with '127.0.0.1:{port}' failed: TimeoutError()"


class _SslObject:
    def __init__(self, certificate: bytes | None) -> None:
        self._certificate = certificate

    def getpeercert(self, binary_form: bool = False) -> bytes | None:
        assert binary_form
        return self._certificate


class _FakeWriter:
    """What asyncio.open_connection hands back: a chosen ssl_object and a close_notify that fails."""

    def __init__(self, ssl_object: _SslObject | None) -> None:
        self._ssl_object = ssl_object
        self.closed = False

    def get_extra_info(self, name: str) -> _SslObject | None:
        return self._ssl_object if name == "ssl_object" else None

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        raise ConnectionResetError("peer skipped close_notify")


class _StallingWriter(_FakeWriter):
    """A peer that never answers close_notify."""

    async def wait_closed(self) -> None:
        await asyncio.sleep(60)


def _open_connection_returning(
    writer: _FakeWriter, delay: float = 0
) -> Callable[..., Awaitable[tuple[None, _FakeWriter]]]:
    async def open_connection(*_args: Any, **_kwargs: Any) -> tuple[None, _FakeWriter]:
        await asyncio.sleep(delay)
        return None, writer

    return open_connection


@pytest.mark.parametrize("ssl_object", [None, _SslObject(None), _SslObject(b"")], ids=["no TLS", "none", "empty"])
async def test_fetch_certificate_fingerprint_without_a_certificate_is_a_data_error(
    monkeypatch: pytest.MonkeyPatch, ssl_object: _SslObject | None
) -> None:
    writer = _FakeWriter(ssl_object)
    monkeypatch.setattr(asyncio, "open_connection", _open_connection_returning(writer))

    with pytest.raises(ComexioDataError) as excinfo:
        await fetch_certificate_fingerprint(_SYNTHETIC_HOST)

    assert str(excinfo.value) == f"{_SYNTHETIC_HOST!r} completed the TLS handshake without presenting a certificate"
    assert writer.closed


async def test_fetch_certificate_fingerprint_survives_a_failing_close(monkeypatch: pytest.MonkeyPatch) -> None:
    writer = _FakeWriter(_SslObject(b"synthetic certificate"))
    monkeypatch.setattr(asyncio, "open_connection", _open_connection_returning(writer))

    fingerprint = await fetch_certificate_fingerprint(_SYNTHETIC_HOST)

    assert fingerprint == hashlib.sha256(b"synthetic certificate").hexdigest()
    assert writer.closed


async def test_fetch_certificate_fingerprint_keeps_one_deadline_for_handshake_and_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writer = _StallingWriter(_SslObject(b"synthetic certificate"))
    monkeypatch.setattr(asyncio, "open_connection", _open_connection_returning(writer))
    monkeypatch.setattr(aiocomexio.tls, "FINGERPRINT_FETCH_TIMEOUT_SEC", 0.2)
    timeout_at = asyncio.timeout_at
    deadlines: list[float | None] = []

    def recording_timeout_at(when: float | None) -> asyncio.Timeout:
        deadlines.append(when)
        return timeout_at(when)

    monkeypatch.setattr(asyncio, "timeout_at", recording_timeout_at)
    started = asyncio.get_running_loop().time()

    fingerprint = await fetch_certificate_fingerprint(_SYNTHETIC_HOST)

    assert fingerprint == hashlib.sha256(b"synthetic certificate").hexdigest()
    assert writer.closed
    # No wall-clock bound: handshake and the stalled close are cut off by the very same deadline.
    (handshake, close) = deadlines
    assert handshake == close == pytest.approx(started + 0.2, abs=0.1)


# --- fetch_certificate / certificate_info --------------------------------------------------

_UNPARSABLE = b"synthetic certificate"
_UNPARSABLE_FINGERPRINT = hashlib.sha256(_UNPARSABLE).hexdigest()
# The two date fields of CertificateInfo, as keyword names for the tests that build one from a dict.
_BEFORE = "not_valid_before"
_AFTER = "not_valid_after"
_DATE_FIELDS = (_BEFORE, _AFTER)


def _parser_error(raw: bytes) -> ValueError:
    """The error cryptography's parser itself raises for raw, so a test can tell the logged cause from a stand-in."""
    with pytest.raises(ValueError) as excinfo:
        x509.load_der_x509_certificate(raw)
    return excinfo.value


def _assert_utc(info: CertificateInfo) -> None:
    assert info.not_valid_before is not None
    assert info.not_valid_after is not None
    assert info.not_valid_before.tzinfo == datetime.UTC
    assert info.not_valid_after.tzinfo == datetime.UTC


async def test_fetch_certificate_reads_fingerprint_and_validity_without_a_request(
    tls_comexio: FakeComexio, certificate: SelfSignedCertificate, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG, logger="aiocomexio.tls"):
        info = await fetch_certificate(tls_comexio.host)

    assert info == CertificateInfo(
        certificate.fingerprint, certificate.not_valid_before, certificate.not_valid_after, issuer=certificate.issuer
    )
    _assert_utc(info)
    assert tls_comexio.requests == []
    assert [(r.levelno, r.getMessage()) for r in caplog.records] == [
        (
            logging.DEBUG,
            f"{tls_comexio.host!r} serves a certificate with SHA-256 fingerprint "
            f"{format_fingerprint(certificate.fingerprint)}, valid until {certificate.not_valid_after.isoformat()}",
        )
    ]


async def test_fetch_certificate_of_a_plain_http_server_is_a_connection_error(comexio: FakeComexio) -> None:
    with pytest.raises(ComexioConnectionError, match=r"^TLS handshake with '127\.0\.0\.1:\d+' failed: "):
        await fetch_certificate(comexio.host)


async def test_fetch_certificate_without_a_certificate_is_a_data_error(monkeypatch: pytest.MonkeyPatch) -> None:
    writer = _FakeWriter(_SslObject(None))
    monkeypatch.setattr(asyncio, "open_connection", _open_connection_returning(writer))

    with pytest.raises(ComexioDataError) as excinfo:
        await fetch_certificate(_SYNTHETIC_HOST)

    assert str(excinfo.value) == f"{_SYNTHETIC_HOST!r} completed the TLS handshake without presenting a certificate"
    assert writer.closed


async def test_fetch_certificate_keeps_the_fingerprint_of_an_unparsable_certificate_and_warns(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    writer = _FakeWriter(_SslObject(_UNPARSABLE))
    monkeypatch.setattr(asyncio, "open_connection", _open_connection_returning(writer))

    with caplog.at_level(logging.WARNING, logger="aiocomexio.tls"):
        info = await fetch_certificate(_SYNTHETIC_HOST)

    assert info == CertificateInfo(_UNPARSABLE_FINGERPRINT, None, None)
    assert writer.closed
    (record,) = caplog.records
    assert record.levelno == logging.WARNING
    # The cause is the error cryptography's parser raises for these bytes (its wording is not part of this contract).
    assert record.getMessage() == (
        f"{_SYNTHETIC_HOST!r} serves a certificate (SHA-256 {format_fingerprint(_UNPARSABLE_FINGERPRINT)}) "
        f"whose validity period cannot be read: {_parser_error(_UNPARSABLE)!r}"
    )


async def test_fetch_certificate_fingerprint_never_parses_the_certificate(
    monkeypatch: pytest.MonkeyPatch, certificate: SelfSignedCertificate
) -> None:
    parsed: list[bytes] = []

    def broken_parser(data: bytes) -> x509.Certificate:
        parsed.append(data)
        raise RuntimeError("parser must not run for the pin")

    monkeypatch.setattr(x509, "load_der_x509_certificate", broken_parser)
    monkeypatch.setattr(
        asyncio, "open_connection", _open_connection_returning(_FakeWriter(_SslObject(certificate.der)))
    )

    assert await fetch_certificate_fingerprint(_SYNTHETIC_HOST) == certificate.fingerprint
    assert not parsed  # not even a parse whose error is swallowed


def test_certificate_info_reads_the_dates_of_a_der_certificate(certificate: SelfSignedCertificate) -> None:
    info = certificate_info(certificate.der)

    assert info == CertificateInfo(
        certificate.fingerprint, certificate.not_valid_before, certificate.not_valid_after, issuer=certificate.issuer
    )
    _assert_utc(info)


_COMEXIO_KEY_ID = "9f216351622d90f115942e8bbe954ac0899369cc"


def _certificate_signed_by(
    issuer_cn: str, key_id: bytes | None, *, extension: x509.AuthorityKeyIdentifier | None = None
) -> bytes:
    """A DER certificate whose issuer is issuer_cn and whose Authority Key Identifier is key_id (none when None).

    extension, when given, is the Authority Key Identifier itself and replaces key_id.
    """
    if extension is None and key_id is not None:
        extension = x509.AuthorityKeyIdentifier(key_id, None, None)
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.datetime.now(datetime.UTC).replace(microsecond=0)
    builder = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "comexio.test")]))
        .issuer_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, issuer_cn)]))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
    )
    if extension is not None:
        builder = builder.add_extension(extension, critical=False)
    return builder.sign(key, hashes.SHA256()).public_bytes(serialization.Encoding.DER)


def test_certificate_info_reads_the_issuer_and_the_authority_key_identifier() -> None:
    info = certificate_info(_certificate_signed_by("Comexio LAN CA", bytes.fromhex(_COMEXIO_KEY_ID)))

    assert info.issuer == "CN=Comexio LAN CA"
    assert info.authority_key_id == _COMEXIO_KEY_ID
    _assert_utc(info)


def test_certificate_info_without_an_authority_key_identifier_keeps_the_dates() -> None:
    info = certificate_info(_certificate_signed_by("Comexio LAN CA", None))

    assert info.issuer == "CN=Comexio LAN CA"
    assert info.authority_key_id is None
    _assert_utc(info)


@pytest.mark.parametrize(
    "error",
    [
        ValueError("malformed extension"),
        x509.DuplicateExtension("duplicate extension", x509.AuthorityKeyIdentifier.oid),
        UserWarning("warned about"),
    ],
    ids=["value error", "duplicate extension", "warning"],
)
def test_certificate_info_keeps_the_dates_when_the_extensions_cannot_be_read(
    monkeypatch: pytest.MonkeyPatch, certificate: SelfSignedCertificate, error: Exception
) -> None:
    parsed = x509.load_der_x509_certificate(certificate.der)

    class _MalformedExtensions:
        not_valid_before_utc = parsed.not_valid_before_utc
        not_valid_after_utc = parsed.not_valid_after_utc
        issuer = parsed.issuer

        @property
        def extensions(self) -> NoReturn:
            raise error

    monkeypatch.setattr(x509, "load_der_x509_certificate", lambda _data: _MalformedExtensions())

    info = certificate_info(certificate.der)

    assert info == CertificateInfo(
        certificate.fingerprint, certificate.not_valid_before, certificate.not_valid_after, issuer=certificate.issuer
    )


@pytest.mark.parametrize(
    ("error", "in_lookup"),
    [
        (x509.UnsupportedGeneralNameType("unsupported general name", 99), False),
        (ValueError("malformed key identifier"), False),
        (x509.DuplicateExtension("duplicate extension", x509.AuthorityKeyIdentifier.oid), True),
    ],
    ids=["unsupported name from value", "value error from value", "duplicate from lookup"],
)
def test_certificate_info_keeps_the_dates_when_the_authority_key_identifier_cannot_be_read(
    monkeypatch: pytest.MonkeyPatch, certificate: SelfSignedCertificate, error: Exception, in_lookup: bool
) -> None:
    parsed = x509.load_der_x509_certificate(certificate.der)

    class _Extension:
        @property
        def value(self) -> NoReturn:
            raise error

    class _Extensions:
        def get_extension_for_class(self, _cls: type) -> _Extension:
            if in_lookup:
                raise error
            return _Extension()

    class _Parsed:
        not_valid_before_utc = parsed.not_valid_before_utc
        not_valid_after_utc = parsed.not_valid_after_utc
        issuer = parsed.issuer
        extensions = _Extensions()

    monkeypatch.setattr(x509, "load_der_x509_certificate", lambda _data: _Parsed())

    info = certificate_info(certificate.der)

    assert info == CertificateInfo(
        certificate.fingerprint, certificate.not_valid_before, certificate.not_valid_after, issuer=certificate.issuer
    )


def test_certificate_info_keeps_the_dates_when_the_issuer_warning_is_raised_as_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    der = _certificate_signed_by("Comexio LAN CA", bytes.fromhex(_COMEXIO_KEY_ID))
    parsed = x509.load_der_x509_certificate(der)

    class _WarnedIssuer:
        not_valid_before_utc = parsed.not_valid_before_utc
        not_valid_after_utc = parsed.not_valid_after_utc
        extensions = parsed.extensions

        @property
        def issuer(self) -> NoReturn:
            raise UserWarning("issuer warned about")

    monkeypatch.setattr(x509, "load_der_x509_certificate", lambda _data: _WarnedIssuer())

    info = certificate_info(der)

    assert info == CertificateInfo(
        hashlib.sha256(der).hexdigest(),
        parsed.not_valid_before_utc,
        parsed.not_valid_after_utc,
        authority_key_id=_COMEXIO_KEY_ID,
    )


def test_certificate_info_keeps_the_dates_when_the_authority_key_identifier_has_no_key_identifier() -> None:
    der = _certificate_signed_by(
        "Comexio LAN CA", None, extension=x509.AuthorityKeyIdentifier(None, [x509.DNSName("ca")], 1)
    )

    info = certificate_info(der)

    assert info.issuer == "CN=Comexio LAN CA"
    assert info.authority_key_id is None
    _assert_utc(info)


def test_certificate_info_keeps_the_dates_and_the_authority_key_identifier_when_the_issuer_cannot_be_decoded(
    caplog: pytest.LogCaptureFixture,
) -> None:
    der = bytearray(_certificate_signed_by("Comexio LAN CA", bytes.fromhex(_COMEXIO_KEY_ID)))
    der[bytes(der).index(b"LAN CA")] = 0xFF  # not UTF-8 in the issuer's common name
    parsed = x509.load_der_x509_certificate(bytes(der))

    with caplog.at_level(logging.DEBUG, logger="aiocomexio.tls"):
        info = certificate_info(bytes(der))

    assert info.issuer is None
    assert info.authority_key_id == _COMEXIO_KEY_ID
    assert (info.not_valid_before, info.not_valid_after) == (parsed.not_valid_before_utc, parsed.not_valid_after_utc)
    fingerprint = format_fingerprint(hashlib.sha256(bytes(der)).hexdigest())
    messages = [record.getMessage() for record in caplog.records if record.name == "aiocomexio.tls"]
    assert len(messages) == 1
    assert messages[0].startswith(f"Certificate {fingerprint}: issuer cannot be read: ")


def test_certificate_info_keeps_the_dates_when_an_issuer_attribute_has_the_wrong_type() -> None:
    # The common name re-tagged as BIT STRING under an OID that is no X500_UNIQUE_IDENTIFIER: cryptography parses
    # the certificate, but rfc4514_string raises TypeError. Same length, so the DER lengths around it stay valid.
    der = bytearray(_certificate_signed_by("Comexio LAN CA", None))
    utf8_common_name = b"\x0c\x0eComexio LAN CA"
    start = bytes(der).index(utf8_common_name)
    der[start : start + len(utf8_common_name)] = b"\x03\x0e\x00omexio LAN CA"

    info = certificate_info(bytes(der))

    assert info.issuer is None
    _assert_utc(info)


def test_certificate_info_reads_an_empty_issuer_as_unknown() -> None:
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.datetime.now(datetime.UTC).replace(microsecond=0)
    der = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "comexio.test")]))
        .issuer_name(x509.Name([]))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .sign(key, hashes.SHA256())
        .public_bytes(serialization.Encoding.DER)
    )

    info = certificate_info(der)

    assert info.issuer is None
    _assert_utc(info)


def test_certificate_info_keeps_the_fingerprint_of_a_certificate_with_an_invalid_version() -> None:
    # The version field is the INTEGER after "[0] EXPLICIT"; 29 is no X.509 version. cryptography raises
    # InvalidVersion for it, an Exception but not a ValueError.
    der = bytearray(_certificate_signed_by("Comexio LAN CA", None))
    version_field = bytes([0xA0, 0x03, 0x02, 0x01, 0x02])
    start = bytes(der).index(version_field)
    der[start + 4] = 29

    info = certificate_info(bytes(der))

    assert info == CertificateInfo(hashlib.sha256(der).hexdigest(), None, None)


def test_certificate_info_counts_an_empty_authority_key_identifier_as_none() -> None:
    info = certificate_info(_certificate_signed_by("Comexio LAN CA", b""))

    assert info.authority_key_id is None
    _assert_utc(info)


@pytest.mark.parametrize("key_id", ["9F21", "9f2", "", "zz"], ids=["uppercase", "odd length", "empty", "not hex"])
def test_certificate_info_refuses_an_authority_key_identifier_in_another_spelling(key_id: str) -> None:
    with pytest.raises(ValueError, match=r"is not an authority key identifier in lowercase hex digits$"):
        CertificateInfo(FOREIGN_FINGERPRINT, None, None, authority_key_id=key_id)


@pytest.mark.parametrize(
    "data",
    [lambda _c: _UNPARSABLE, lambda c: c.der[:-5], lambda _c: b""],
    ids=["junk", "truncated certificate", "empty"],
)
def test_certificate_info_keeps_the_fingerprint_of_bytes_that_are_no_certificate(
    caplog: pytest.LogCaptureFixture,
    certificate: SelfSignedCertificate,
    data: Callable[[SelfSignedCertificate], bytes],
) -> None:
    raw = data(certificate)
    with caplog.at_level(logging.DEBUG, logger="aiocomexio.tls"):
        info = certificate_info(raw)

    fingerprint = hashlib.sha256(raw).hexdigest()
    assert info == CertificateInfo(fingerprint, None, None)
    (record,) = caplog.records
    assert record.levelno == logging.DEBUG
    assert record.getMessage() == (
        f"Certificate {format_fingerprint(fingerprint)} has no readable validity period: {_parser_error(raw)!r}"
    )


def test_certificate_info_treats_a_warning_raised_as_error_like_an_unparsable_certificate(
    monkeypatch: pytest.MonkeyPatch, certificate: SelfSignedCertificate
) -> None:
    def warning_as_error(_data: bytes) -> x509.Certificate:
        raise UserWarning("negative serial number")  # what "-W error" makes of cryptography's warning

    monkeypatch.setattr(x509, "load_der_x509_certificate", warning_as_error)

    assert certificate_info(certificate.der) == CertificateInfo(certificate.fingerprint, None, None)


def test_certificate_info_keeps_the_dates_of_a_certificate_cryptography_only_warns_about(
    monkeypatch: pytest.MonkeyPatch, certificate: SelfSignedCertificate
) -> None:
    real_parser = x509.load_der_x509_certificate
    filters_while_parsing: list[list[tuple[object, ...]]] = []

    def warning_parser(data: bytes) -> x509.Certificate:
        # Looked at while the parser runs and, below, right after the call, both inside pytest.warns:
        # it restores its own filters on exit, so a change certificate_info made would be gone by the
        # time the test could look outside the block.
        filters_while_parsing.append(list(warnings.filters))
        warnings.warn("negative serial number", CryptographyDeprecationWarning, stacklevel=2)
        return real_parser(data)

    monkeypatch.setattr(x509, "load_der_x509_certificate", warning_parser)

    with pytest.warns(CryptographyDeprecationWarning, match="negative serial number"):
        filters_before_call = list(warnings.filters)
        info = certificate_info(certificate.der)
        filters_after_call = list(warnings.filters)

    assert info == CertificateInfo(
        certificate.fingerprint, certificate.not_valid_before, certificate.not_valid_after, issuer=certificate.issuer
    )
    assert filters_while_parsing == [filters_before_call]
    assert filters_after_call == filters_before_call


def test_certificate_info_is_immutable_and_slotted(certificate: SelfSignedCertificate) -> None:
    info = certificate_info(certificate.der)
    with pytest.raises(AttributeError):
        info.fingerprint = FOREIGN_FINGERPRINT  # type: ignore[misc]
    assert not hasattr(info, "__dict__")


@pytest.mark.parametrize("known", _DATE_FIELDS)
def test_certificate_info_refuses_only_one_known_date(known: str) -> None:
    dates: dict[str, Any] = {
        _BEFORE: None,
        _AFTER: None,
        known: datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC),
    }
    with pytest.raises(ValueError, match=r"^not_valid_before and not_valid_after are either both known or both None$"):
        CertificateInfo(FOREIGN_FINGERPRINT, **dates)


@pytest.mark.parametrize(
    "date",
    [
        datetime.datetime(2026, 1, 1),  # noqa: DTZ001 - the naive date is the point
        datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone(datetime.timedelta(hours=1))),
    ],
    ids=["naive", "not UTC"],
)
@pytest.mark.parametrize("field", _DATE_FIELDS)
def test_certificate_info_refuses_a_date_that_is_not_utc(field: str, date: datetime.datetime) -> None:
    dates: dict[str, Any] = {
        _BEFORE: datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC),
        _AFTER: datetime.datetime(2027, 1, 1, tzinfo=datetime.UTC),
        field: date,
    }
    with pytest.raises(ValueError, match=r"is not a timezone-aware UTC date$"):
        CertificateInfo(FOREIGN_FINGERPRINT, **dates)


@pytest.mark.parametrize(
    "fingerprint",
    [FOREIGN_FINGERPRINT.upper(), format_fingerprint(FOREIGN_FINGERPRINT), "ab" * 20, "zz" * 32],
    ids=["uppercase", "colons", "SHA-1 length", "not hex"],
)
def test_certificate_info_refuses_a_fingerprint_in_another_spelling(fingerprint: str) -> None:
    with pytest.raises(ValueError, match=r"(64 lowercase hex digits|is not a SHA-256 fingerprint)"):
        CertificateInfo(fingerprint, None, None)


# --- the pinned client ---------------------------------------------------------------------


async def test_pinned_client_logs_in_over_https(
    tls_comexio: FakeComexio, tls_session: aiohttp.ClientSession, certificate: SelfSignedCertificate
) -> None:
    client = _pinned_client(tls_comexio, tls_session, format_fingerprint(certificate.fingerprint))

    await client.login()

    assert client.fingerprint == certificate.fingerprint
    assert (await client.get_raw_config()).comexio_version == "11.0.2"
    (login,) = [request for request in tls_comexio.received_at("POST", LOGIN_PATH) if "loginsubmit" in request.form]
    assert login.referer == f"https://{tls_comexio.host}{LOGIN_PATH}"


async def test_pinned_client_writes_values_with_basic_auth_over_https(
    tls_comexio: FakeComexio, tls_session: aiohttp.ClientSession, certificate: SelfSignedCertificate
) -> None:
    tls_comexio.serve_text("GET", _API_PATH, "1")
    client = _pinned_client(tls_comexio, tls_session, certificate.fingerprint)

    await client.set_marker_value(5, 21.5)

    (request,) = tls_comexio.received_at("GET", _API_PATH)
    assert request.query == {"action": "set", "value": "21.5", "marker": "M5"}
    assert request.authorization == f"Basic {base64.b64encode(b'api:api-secret').decode()}"


@pytest.mark.parametrize("require_https", [False, True])
async def test_pinned_client_reads_the_api_settings_over_https(
    tls_comexio: FakeComexio,
    tls_session: aiohttp.ClientSession,
    certificate: SelfSignedCertificate,
    require_https: bool,
) -> None:
    tls_comexio.serve_text("GET", "/admin/api/home", load_fixture("api_settings_page.html"))
    client = _pinned_client(tls_comexio, tls_session, certificate.fingerprint)
    await client.login()

    settings = await client.get_api_settings(require_https=require_https)

    assert settings.active is True
    assert len(tls_comexio.received_at("GET", "/admin/api/home")) == 1


_MISMATCH_CALLS: dict[str, tuple[str, Callable[[ComexioClient], Awaitable[Any]]]] = {
    "login": ("Login key request", lambda client: client.login()),
    "config": ("Admin page", lambda client: client.get_raw_config()),
    "value write": ("Writing marker M5", lambda client: client.set_marker_value(5, 1)),
    "redirect": (_SAVING_PLAN, lambda client: client._request_redirect("POST", _PLAN_SAVE_PATH, what=_SAVING_PLAN)),
}


@pytest.mark.parametrize("call", list(_MISMATCH_CALLS))
async def test_pinned_client_refuses_a_foreign_certificate_before_sending_anything(
    tls_comexio: FakeComexio, tls_session: aiohttp.ClientSession, certificate: SelfSignedCertificate, call: str
) -> None:
    what, send = _MISMATCH_CALLS[call]
    client = _pinned_client(tls_comexio, tls_session, FOREIGN_FINGERPRINT)
    request = send(client)

    with pytest.raises(ComexioCertificateMismatchError) as excinfo:
        await request

    assert str(excinfo.value) == (
        f"{what} refused: Comexio at {tls_comexio.host!r} presented the TLS certificate "
        f"{format_fingerprint(certificate.fingerprint)} instead of the pinned {format_fingerprint(FOREIGN_FINGERPRINT)}"
    )
    assert excinfo.value.expected == FOREIGN_FINGERPRINT
    assert excinfo.value.actual == certificate.fingerprint
    assert isinstance(excinfo.value, ComexioError)
    assert not isinstance(excinfo.value, ComexioConnectionError)
    assert tls_comexio.requests == []


async def test_pinned_client_does_not_fall_back_to_plain_http(
    comexio: FakeComexio, session: aiohttp.ClientSession, certificate: SelfSignedCertificate
) -> None:
    client = _pinned_client(comexio, session, certificate.fingerprint)
    request = client.login()

    with pytest.raises(ComexioConnectionError, match=r"^Login key request failed: "):
        await request

    assert comexio.requests == []


async def test_pinned_client_refuses_a_redirect_to_plain_http(
    tls_comexio: FakeComexio,
    tls_session: aiohttp.ClientSession,
    certificate: SelfSignedCertificate,
    comexio: FakeComexio,
) -> None:
    location = f"http://{comexio.host}{ADMIN_PATH}"

    async def downgrade(_request: web.Request) -> web.Response:
        raise web.HTTPFound(location)

    tls_comexio.serve("GET", ADMIN_PATH, downgrade)
    client = _pinned_client(tls_comexio, tls_session, certificate.fingerprint)
    request = client.get_raw_config()

    with pytest.raises(ComexioResponseError) as excinfo:
        await request

    assert str(excinfo.value) == _refused_redirect_message("Admin page", location)
    assert excinfo.value.status == 302
    assert comexio.requests == []


@pytest.mark.parametrize(
    "location",
    [
        "https://comexio.example.com/admin/",  # another host
        "//comexio.example.com/admin/",  # scheme-relative, another host
        "https://127.0.0.1:1/admin/",  # same host, another port
        "HTTP://{host}/admin/",  # plain http, in capitals
    ],
)
async def test_pinned_client_refuses_a_redirect_off_its_origin(
    tls_comexio: FakeComexio, tls_session: aiohttp.ClientSession, certificate: SelfSignedCertificate, location: str
) -> None:
    location = location.format(host=tls_comexio.host)

    async def redirect(_request: web.Request) -> web.Response:
        return web.Response(status=302, headers={"Location": location})

    tls_comexio.serve("GET", ADMIN_PATH, redirect)
    client = _pinned_client(tls_comexio, tls_session, certificate.fingerprint)
    request = client.get_raw_config()

    with pytest.raises(ComexioResponseError) as excinfo:
        await request

    assert str(excinfo.value) == _refused_redirect_message("Admin page", location)
    assert tls_comexio.requests == [("GET", ADMIN_PATH)]


async def test_pinned_client_reports_a_redirect_without_a_location(
    tls_comexio: FakeComexio, tls_session: aiohttp.ClientSession, certificate: SelfSignedCertificate
) -> None:
    async def redirect(_request: web.Request) -> web.Response:
        return web.Response(status=302)

    tls_comexio.serve("GET", ADMIN_PATH, redirect)
    client = _pinned_client(tls_comexio, tls_session, certificate.fingerprint)
    request = client.get_raw_config()

    with pytest.raises(ComexioResponseError) as excinfo:
        await request

    assert str(excinfo.value) == "Admin page failed: Comexio answered 302 without a Location header"
    assert excinfo.value.status == 302


@pytest.mark.parametrize("location", ["https://[bad/admin/", "https://127.0.0.1:99999/admin/"])
async def test_pinned_client_refuses_an_unparsable_redirect(
    tls_comexio: FakeComexio, tls_session: aiohttp.ClientSession, certificate: SelfSignedCertificate, location: str
) -> None:
    async def redirect(_request: web.Request) -> web.Response:
        return web.Response(status=302, headers={"Location": location})

    tls_comexio.serve("GET", ADMIN_PATH, redirect)
    client = _pinned_client(tls_comexio, tls_session, certificate.fingerprint)
    request = client.get_raw_config()

    with pytest.raises(ComexioResponseError) as excinfo:
        await request

    assert str(excinfo.value) == f"Admin page failed: Comexio redirected to an unparsable location {location!r}"
    assert tls_comexio.requests == [("GET", ADMIN_PATH)]


@pytest.mark.parametrize(
    ("status", "method", "followed_with"),
    [
        (301, "POST", "GET"),
        (302, "POST", "GET"),
        (303, "POST", "GET"),
        (303, "PUT", "GET"),
        (301, "PUT", "PUT"),
        (307, "POST", "POST"),
        (308, "POST", "POST"),
    ],
)
async def test_pinned_client_follows_a_redirect_within_its_origin(
    tls_comexio: FakeComexio,
    tls_session: aiohttp.ClientSession,
    certificate: SelfSignedCertificate,
    status: int,
    method: str,
    followed_with: str,
) -> None:
    async def moved(_request: web.Request) -> web.Response:
        return web.Response(status=status, headers={"Location": _MOVED_PATH})

    followed_bodies: list[bytes] = []

    async def target(request: web.Request) -> web.Response:
        followed_bodies.append(await request.read())
        return web.Response(text="1")

    tls_comexio.serve(method, _API_PATH, moved)
    tls_comexio.serve(followed_with, _MOVED_PATH, target)
    client = _pinned_client(tls_comexio, tls_session, certificate.fingerprint)

    body = await client._request_text(method, _API_PATH, what="Moved", data={"a": "1"}, params={"q": "1"})

    assert body == "1"
    # Like aiohttp: 303, and 301/302 on a POST, continue as a GET without the form; the old query never follows.
    sent_form = {"a": "1"} if method == "POST" else {}
    followed_form = sent_form if followed_with == "POST" else {}
    assert [(r.method, r.path, r.query, r.form) for r in tls_comexio.received] == [
        (method, _API_PATH, {"q": "1"}, sent_form),
        (followed_with, _MOVED_PATH, {}, followed_form),
    ]
    # The fake records forms only for POST: check the body itself, so a PUT that loses it shows too.
    assert followed_bodies == [b"" if followed_with == "GET" else b"a=1"]


async def test_pinned_client_keeps_a_head_request_on_a_303(
    tls_comexio: FakeComexio, tls_session: aiohttp.ClientSession, certificate: SelfSignedCertificate
) -> None:
    async def moved(_request: web.Request) -> web.Response:
        return web.Response(status=303, headers={"Location": _MOVED_PATH})

    async def target(_request: web.Request) -> web.Response:
        return web.Response()

    tls_comexio.serve("HEAD", _API_PATH, moved)
    tls_comexio.serve("HEAD", _MOVED_PATH, target)
    client = _pinned_client(tls_comexio, tls_session, certificate.fingerprint)

    await client._request_text("HEAD", _API_PATH, what="Moved")

    # Like aiohttp (RFC 9110, 15.4.4): a 303 turns a request into a GET, but a HEAD stays a HEAD.
    assert tls_comexio.requests == [("HEAD", _API_PATH), ("HEAD", _MOVED_PATH)]


async def test_pinned_client_resolves_each_hop_against_the_previous_one(
    tls_comexio: FakeComexio, tls_session: aiohttp.ClientSession, certificate: SelfSignedCertificate
) -> None:
    async def first(_request: web.Request) -> web.Response:
        raise web.HTTPFound("second")  # relative: /admin/second

    async def second(_request: web.Request) -> web.Response:
        raise web.HTTPFound(f"HTTPS://{tls_comexio.host}{_MOVED_PATH}")  # same origin, scheme in capitals

    tls_comexio.serve("GET", "/admin/first", first)
    tls_comexio.serve("GET", "/admin/second", second)
    tls_comexio.serve_text("GET", _MOVED_PATH, "1")
    client = _pinned_client(tls_comexio, tls_session, certificate.fingerprint)

    assert await client._request_text("GET", "/admin/first", what="Moved") == "1"
    assert tls_comexio.requests == [("GET", "/admin/first"), ("GET", "/admin/second"), ("GET", _MOVED_PATH)]


async def test_pinned_client_stops_an_endless_redirect_loop(
    tls_comexio: FakeComexio, tls_session: aiohttp.ClientSession, certificate: SelfSignedCertificate
) -> None:
    async def loop(_request: web.Request) -> web.Response:
        raise web.HTTPFound(ADMIN_PATH)

    tls_comexio.serve("GET", ADMIN_PATH, loop)
    client = _pinned_client(tls_comexio, tls_session, certificate.fingerprint)
    request = client.get_raw_config()

    with pytest.raises(ComexioResponseError) as excinfo:
        await request

    assert str(excinfo.value) == "Admin page failed: more than 10 redirects"
    assert excinfo.value.status == 302
    assert tls_comexio.requests == [("GET", ADMIN_PATH)] * 11


async def test_redirect_through_pinned_client_works(
    tls_comexio: FakeComexio, tls_session: aiohttp.ClientSession, certificate: SelfSignedCertificate
) -> None:
    async def redirect(_request: web.Request) -> web.Response:
        raise web.HTTPFound("/admin/function_function_module/home?saved=1")

    tls_comexio.serve("POST", _PLAN_SAVE_PATH, redirect)
    client = _pinned_client(tls_comexio, tls_session, certificate.fingerprint)

    location = await client._request_redirect("POST", _PLAN_SAVE_PATH, what=_SAVING_PLAN)

    assert location == "/admin/function_function_module/home?saved=1"


async def test_unpinned_client_still_uses_plain_http(client: ComexioClient, comexio: FakeComexio) -> None:
    await client.login()

    assert client.fingerprint is None
    assert comexio.received_at("GET", ADMIN_PATH)


def test_pinned_client_rejects_a_host_with_url_parts() -> None:
    session: Any = object()  # never used: construction fails first
    with pytest.raises(ValueError) as excinfo:
        ComexioClient("comexio.local/admin", USERNAME, PASSWORD, session=session, fingerprint=FOREIGN_FINGERPRINT)
    assert str(excinfo.value) == "'comexio.local/admin' is not a host name or address"


def test_client_rejects_an_invalid_fingerprint() -> None:
    session: Any = object()  # never used: construction fails first
    with pytest.raises(ValueError) as excinfo:
        ComexioClient(_SYNTHETIC_HOST, USERNAME, PASSWORD, session=session, fingerprint="34:54:BE:0E")
    assert str(excinfo.value) == "'34:54:BE:0E' is not a SHA-256 fingerprint (64 hex digits)"


def test_pinned_client_never_warns_about_plain_http(caplog: pytest.LogCaptureFixture) -> None:
    session: Any = object()  # never used: only the warning is exercised
    remote = ComexioClient("comexio.example.com", USERNAME, PASSWORD, session=session, fingerprint=FOREIGN_FINGERPRINT)

    with caplog.at_level(logging.WARNING, logger="aiocomexio.client"):
        remote._warn_plain_http()

    assert caplog.records == []


@pytest.mark.parametrize(
    ("first", "second", "same"),
    [
        ("https://comexio.local/a", "https://comexio.local:443/b", True),
        ("https://comexio.local/a", "HTTPS://Comexio.Local/b", True),
        ("https://comexio.local/a", "https://comexio.local:8443/a", False),
        ("https://comexio.local/a", "http://comexio.local:443/a", False),
        ("https://comexio.local/a", "https://comexio.local:0/a", False),
        ("http://comexio.local/a", "http://comexio.local:80/b", True),
        ("http://comexio.local/a", "http://comexio.local:8080/a", False),
        ("http://comexio.local:443/a", "https://comexio.local/a", False),
    ],
)
def test_origin_fills_in_the_default_port(first: str, second: str, same: bool) -> None:
    assert (_origin(first) == _origin(second)) is same


@pytest.mark.parametrize(
    ("current", "target", "follows"),
    [
        ("http://comexio.local/a", "http://comexio.local:80/b", True),
        ("http://comexio.local/a", "https://comexio.local/a", True),
        ("http://comexio.local/a", "https://comexio.local:443/a", True),
        ("http://comexio.local:8080/a", "https://comexio.local/a", True),  # plain HTTP on any port, on to HTTPS
        ("https://comexio.local/a", "http://comexio.local/a", False),  # no downgrade back to plain HTTP
        ("http://comexio.local/a", "https://other.local/a", False),
        ("http://comexio.local/a", "https://comexio.local:8443/a", False),
        ("http://comexio.local:8080/a", "http://comexio.local/a", False),
    ],
)
def test_an_unpinned_redirect_stays_on_its_origin_or_moves_on_to_https(
    current: str, target: str, follows: bool
) -> None:
    assert _same_origin_or_https(current, target) is follows
