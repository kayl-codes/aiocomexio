"""Certificate pinning: fingerprint helpers, the bare-handshake fetch and the pinned HTTPS client."""

import datetime
import hashlib
import logging
import ssl
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from aiohttp import web
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from aiocomexio import (
    ComexioCertificateMismatchError,
    ComexioClient,
    ComexioConnectionError,
    fetch_certificate_fingerprint,
    format_fingerprint,
    normalize_fingerprint,
    session_kwargs,
)
from aiocomexio.tls import https_address
from tests.fake_comexio import ADMIN_PATH, LOGIN_PATH, PASSWORD, USERNAME, FakeComexio

# Synthetic: a fingerprint no test certificate has.
FOREIGN_FINGERPRINT = "ab" * 32
_PLAN_SAVE_PATH = "/admin/function_function_module/save_fub"


class SelfSignedCertificate:
    """A self-signed certificate made for the test run, the way Comexio signs its own."""

    def __init__(self, directory: Path) -> None:
        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "comexio.test")])
        now = datetime.datetime.now(datetime.UTC)
        certificate = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=1))
            .sign(key, hashes.SHA256())
        )
        self.fingerprint = hashlib.sha256(certificate.public_bytes(serialization.Encoding.DER)).hexdigest()
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
    with pytest.raises(ValueError, match="is not a SHA-256 fingerprint"):
        normalize_fingerprint(value)


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
    ],
)
def test_https_address_uses_the_port_in_host_or_443(host: str, expected: tuple[str, int]) -> None:
    assert https_address(host) == expected


@pytest.mark.parametrize("host", ["", ":443", "comexio:notaport"])
def test_https_address_rejects_malformed_hosts(host: str) -> None:
    with pytest.raises(ValueError):
        https_address(host)


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


# --- the pinned client ---------------------------------------------------------------------


async def test_pinned_client_logs_in_over_https(
    tls_comexio: FakeComexio, tls_session: aiohttp.ClientSession, certificate: SelfSignedCertificate
) -> None:
    client = ComexioClient(
        tls_comexio.host,
        USERNAME,
        PASSWORD,
        session=tls_session,
        fingerprint=format_fingerprint(certificate.fingerprint),
    )

    await client.login()

    assert client.fingerprint == certificate.fingerprint
    assert (await client.get_raw_config()).comexio_version == "11.0.2"
    (login,) = [request for request in tls_comexio.received_at("POST", LOGIN_PATH) if "loginsubmit" in request.form]
    assert login.referer == f"https://{tls_comexio.host}{LOGIN_PATH}"


@pytest.mark.parametrize("call", ["login", "redirect"])
async def test_pinned_client_refuses_a_foreign_certificate_before_sending_anything(
    tls_comexio: FakeComexio, tls_session: aiohttp.ClientSession, certificate: SelfSignedCertificate, call: str
) -> None:
    client = ComexioClient(tls_comexio.host, USERNAME, PASSWORD, session=tls_session, fingerprint=FOREIGN_FINGERPRINT)

    request = (
        client.login()
        if call == "login"
        else client._request_redirect("POST", _PLAN_SAVE_PATH, what="Saving function plan 1")
    )

    with pytest.raises(ComexioCertificateMismatchError) as excinfo:
        await request

    what = "Login key request" if call == "login" else "Saving function plan 1"
    assert str(excinfo.value) == (
        f"{what} refused: Comexio at {tls_comexio.host!r} presented the TLS certificate "
        f"{format_fingerprint(certificate.fingerprint)} instead of the pinned {format_fingerprint(FOREIGN_FINGERPRINT)}"
    )
    assert excinfo.value.expected == FOREIGN_FINGERPRINT
    assert excinfo.value.actual == certificate.fingerprint
    assert not isinstance(excinfo.value, ComexioConnectionError)
    assert tls_comexio.requests == []


async def test_pinned_client_does_not_fall_back_to_plain_http(
    comexio: FakeComexio, session: aiohttp.ClientSession, certificate: SelfSignedCertificate
) -> None:
    client = ComexioClient(comexio.host, USERNAME, PASSWORD, session=session, fingerprint=certificate.fingerprint)

    with pytest.raises(ComexioConnectionError):
        await client.login()
    assert comexio.requests == []


async def test_unpinned_client_still_uses_plain_http(client: ComexioClient, comexio: FakeComexio) -> None:
    await client.login()

    assert client.fingerprint is None
    assert comexio.received_at("GET", ADMIN_PATH)


def test_client_rejects_an_invalid_fingerprint() -> None:
    session: Any = object()  # never used: construction fails first
    with pytest.raises(ValueError, match="is not a SHA-256 fingerprint"):
        ComexioClient("192.168.0.5", USERNAME, PASSWORD, session=session, fingerprint="34:54:BE:0E")


def test_pinned_client_never_warns_about_plain_http(caplog: pytest.LogCaptureFixture) -> None:
    session: Any = object()  # never used: only the warning is exercised
    remote = ComexioClient("comexio.example.com", USERNAME, PASSWORD, session=session, fingerprint=FOREIGN_FINGERPRINT)

    with caplog.at_level(logging.WARNING, logger="aiocomexio.client"):
        remote._warn_plain_http()

    assert caplog.records == []


async def test_redirect_through_pinned_client_works(
    tls_comexio: FakeComexio, tls_session: aiohttp.ClientSession, certificate: SelfSignedCertificate
) -> None:
    async def redirect(_request: web.Request) -> web.Response:
        raise web.HTTPFound("/admin/function_function_module/home?saved=1")

    tls_comexio.serve("POST", _PLAN_SAVE_PATH, redirect)
    client = ComexioClient(
        tls_comexio.host, USERNAME, PASSWORD, session=tls_session, fingerprint=certificate.fingerprint
    )

    location = await client._request_redirect("POST", _PLAN_SAVE_PATH, what="Saving function plan 1")

    assert location == "/admin/function_function_module/home?saved=1"
