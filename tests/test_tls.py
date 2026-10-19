"""Certificate pinning: fingerprint helpers, the bare-handshake fetch and the pinned HTTPS client."""

import asyncio
import base64
import datetime
import hashlib
import logging
import ssl
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from aiohttp import web
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

import aiocomexio.tls
from aiocomexio import (
    ComexioCertificateMismatchError,
    ComexioClient,
    ComexioConnectionError,
    ComexioDataError,
    ComexioError,
    ComexioResponseError,
    fetch_certificate_fingerprint,
    format_fingerprint,
    normalize_fingerprint,
    session_kwargs,
)
from aiocomexio.client import _https_origin
from aiocomexio.tls import https_address
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
    # The handshake uses most of the 1.2 s; a fresh timeout for closing would take the total to 2.2 s.
    # 2 s leaves 0.8 s of slack for a loaded test runner.
    monkeypatch.setattr(asyncio, "open_connection", _open_connection_returning(writer, delay=1.0))
    monkeypatch.setattr(aiocomexio.tls, "FINGERPRINT_FETCH_TIMEOUT_SEC", 1.2)
    started = asyncio.get_running_loop().time()

    fingerprint = await fetch_certificate_fingerprint(_SYNTHETIC_HOST)

    assert fingerprint == hashlib.sha256(b"synthetic certificate").hexdigest()
    assert asyncio.get_running_loop().time() - started < 2.0
    assert writer.closed


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

    tls_comexio.serve(method, _API_PATH, moved)
    tls_comexio.serve_text(followed_with, _MOVED_PATH, "1")
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
    ],
)
def test_https_origin_fills_in_the_default_port(first: str, second: str, same: bool) -> None:
    assert (_https_origin(first) == _https_origin(second)) is same
