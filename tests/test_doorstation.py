"""Doorstation: detection on the IO home page and the first frame of its camera stream (MJPEG)."""

import asyncio
import gzip
import itertools
import logging
import re
from collections.abc import Iterable, Iterator
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

import aiohttp
import pytest
from aiohttp import web

from aiocomexio import (
    ComexioAuthenticationError,
    ComexioClient,
    ComexioConnectionError,
    ComexioDataError,
    ComexioResponseError,
    Doorstation,
)
from aiocomexio import client as client_module
from aiocomexio import doorstation as doorstation_module
from aiocomexio.client import parse_doorstations
from aiocomexio.doorstation import (
    JpegFrameReader,
    doorstation_stream_path,
    doorstations_from_attributes,
    first_jpeg_frame,
)
from tests.fake_comexio import LOGIN_PAGE, SESSION_COOKIE, SESSION_VALUE, FakeComexio, Handler

IO_HOME_PATH = "/admin/in_output/home"
STREAM_PATH = "/proxy/cam/7/"
BOUNDARY = b"limesnontitiae"

# Synthetic JPEG building blocks: markers and segments as ITU-T T.81 lays them out, payload bytes made up.
SOI = b"\xff\xd8"
EOI = b"\xff\xd9"


def _segment(code: int, payload: bytes) -> bytes:
    return bytes((0xFF, code)) + (len(payload) + 2).to_bytes(2) + payload


# APP0 (JFIF header), then an APP1 that embeds a complete thumbnail JPEG (its own SOI and EOI), a quantization
# table, then one scan whose entropy-coded data holds a stuffed 0xFF (FF 00) and a restart marker (FF D0).
APP0 = _segment(0xE0, b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00")
THUMBNAIL_APP1 = _segment(0xE1, b"Exif\x00\x00" + SOI + b"\x01\x02" + EOI)
DQT = _segment(0xDB, bytes(65))
SOS = _segment(0xDA, b"\x01\x01\x00\x00\x3f\x00")
SCAN = b"\x12\x34\xff\x00\x56\xff\xd0\x78\x9a"
FRAME = SOI + APP0 + THUMBNAIL_APP1 + DQT + SOS + SCAN + EOI


# The BASE block of an IO home page: no data-extid, only its device counter (the markup the 1-Wire count reads).
BASE_BLOCK = '<div id="tabs-1"><h3>BASE</h3><div id="number_of_1" class="number_of_type_30">0</div></div>'


def _part(frame: bytes) -> bytes:
    """One part of an MJPEG stream, framed as Comexio's stream proxy frames it."""
    return b"--" + BOUNDARY + b"\r\nContent-Type: image/jpeg\r\n\r\n" + frame + b"\r\n"


def _element(tag: str = "div", **attributes: str) -> str:
    rendered = " ".join(f'{name.replace("_", "-", 1)}="{value}"' for name, value in attributes.items())
    return f"<{tag} {rendered}></{tag}>"


def _doorstation_tab(ext_id: str, name: str, camera_id: str | None = None) -> str:
    """A Doorstation tab: the tile and a few elements that repeat the extension and camera ids."""
    camera = ext_id if camera_id is None else camera_id
    return (
        f'<div id="tabs-{ext_id}"><h3>Doorstation XXXX-XXXX-XXXX {name}</h3>'
        f'<div class="device_tile device_type_16" data-extid="{ext_id}" data-extname="{name}">'
        f'<img class="cam" data-extid="{ext_id}" data-extname="{name}" data-camera_id="{camera}"'
        f' data-door_ext="{ext_id}">'
        f'<button data-extid="{ext_id}" data-extname="{name}" data-camera_id="{camera}">Live</button>'
        "</div></div>"
    )


# --- detection ---------------------------------------------------------------------------------------------------


def test_parse_doorstations_reads_every_doorstation_tab() -> None:
    html = (
        "<!DOCTYPE html><html><body>"
        + BASE_BLOCK
        + _doorstation_tab("7", "Haust&uuml;r &amp; Hof")
        + _doorstation_tab("9", "Garage")
        + "</body></html>"
    )

    assert parse_doorstations(html) == {
        7: Doorstation(ext_id=7, camera_id=7, name="Haustür & Hof"),
        9: Doorstation(ext_id=9, camera_id=9, name="Garage"),
    }


def test_parse_doorstations_without_a_camera_element_finds_none() -> None:
    html = f'<html><body>{BASE_BLOCK}<div id="number_of_2" class="number_of_type_30_2">3</div></body></html>'

    assert parse_doorstations(html) == {}


@pytest.mark.parametrize(
    "wrapper",
    [
        "<script>{}</script>",
        "<template>{}</template>",
        "<textarea>{}</textarea>",
        "<!-- {} -->",
    ],
    ids=["script", "template", "textarea", "comment"],
)
def test_parse_doorstations_ignores_markup_a_browser_shows_as_text_or_not_at_all(wrapper: str) -> None:
    html = wrapper.format(_doorstation_tab("5", "Hidden")) + _doorstation_tab("7", "Haustür")

    assert parse_doorstations(html) == {7: Doorstation(ext_id=7, camera_id=7, name="Haustür")}


def test_parse_doorstations_reads_attributes_as_a_browser_does() -> None:
    # Unquoted values, a ">" inside a quoted value, and the first of two equal attribute names.
    html = "<img data-camera_id=7 data-extid=7 data-extid=8 data-extname='Tür > Hof'>"

    assert parse_doorstations(html) == {7: Doorstation(ext_id=7, camera_id=7, name="Tür > Hof")}


def test_a_doorstation_without_a_name_has_name_none() -> None:
    html = _element(data_extid="7", data_camera_id="7") + _element(data_extid="8", data_extname="", data_camera_id="8")

    assert parse_doorstations(html) == {
        7: Doorstation(ext_id=7, camera_id=7, name=None),
        8: Doorstation(ext_id=8, camera_id=8, name=None),
    }


def test_an_empty_camera_id_names_no_camera_and_is_skipped_at_debug(caplog: pytest.LogCaptureFixture) -> None:
    html = _element(data_extid="3", data_camera_id="") + _doorstation_tab("7", "Haustür")

    with caplog.at_level(logging.DEBUG, logger="aiocomexio"):
        result = parse_doorstations(html)

    assert result == {7: Doorstation(ext_id=7, camera_id=7, name="Haustür")}
    assert [record.getMessage() for record in caplog.records] == [
        "Skipped 1 Doorstation elements with an empty camera id"
    ]


@pytest.mark.parametrize(
    ("ext_id", "camera_id"),
    [
        ("7", "true"),
        ("7", "-3"),
        ("7", "1.5"),
        ("7", "0"),
        ("7", "٣"),
        ("7", "9" * 19),
        ("", "7"),
        ("x", "7"),
        (None, "7"),
    ],
)
def test_an_element_with_unusable_ids_is_skipped_and_logged(
    ext_id: str | None, camera_id: str, caplog: pytest.LogCaptureFixture
) -> None:
    attributes = {"data_camera_id": camera_id} | ({} if ext_id is None else {"data_extid": ext_id})
    html = _element(**attributes) + _doorstation_tab("9", "Garage")

    with caplog.at_level(logging.WARNING, logger="aiocomexio"):
        result = parse_doorstations(html)

    assert result == {9: Doorstation(ext_id=9, camera_id=9, name="Garage")}
    assert [record.getMessage() for record in caplog.records] == [
        "Skipped 1 Doorstation elements without a usable extension and camera id"
    ]


def test_camera_elements_none_of_them_usable_mean_a_changed_page() -> None:
    html = _element(data_extid="a", data_camera_id="7") + _element(data_extid="7", data_camera_id="b")

    with pytest.raises(ComexioDataError) as info:
        parse_doorstations(html)

    assert str(info.value) == "None of the 2 Doorstation elements has a usable extension and camera id"


def test_a_doorstation_named_twice_differently_keeps_the_first_and_logs(caplog: pytest.LogCaptureFixture) -> None:
    html = _doorstation_tab("7", "Haustür") + _doorstation_tab("7", "Hof\nTor", camera_id="8")

    with caplog.at_level(logging.WARNING, logger="aiocomexio"):
        result = parse_doorstations(html)

    assert result == {7: Doorstation(ext_id=7, camera_id=7, name="Haustür")}
    assert [record.getMessage() for record in caplog.records] == [
        "Doorstation of extension 7 is named twice (camera 7 'Haustür', then camera 8 'Hof\\nTor'), "
        "the first one counts"
    ]


def test_doorstations_that_share_a_camera_are_kept_and_logged(caplog: pytest.LogCaptureFixture) -> None:
    html = _doorstation_tab("9", "Garage", camera_id="4") + _doorstation_tab("7", "Haustür", camera_id="4")

    with caplog.at_level(logging.WARNING, logger="aiocomexio"):
        result = parse_doorstations(html)

    assert result == {
        9: Doorstation(ext_id=9, camera_id=4, name="Garage"),
        7: Doorstation(ext_id=7, camera_id=4, name="Haustür"),
    }
    assert [record.getMessage() for record in caplog.records] == [
        "Doorstations of extensions [7, 9] share the camera 4"
    ]


def test_the_parser_keeps_each_distinct_attribute_set_once() -> None:
    parser = client_module._DoorstationParser()
    parser.feed(_doorstation_tab("7", "Haustür") * 500)
    parser.close()

    assert list(parser.elements) == [
        (("data-extid", "7"), ("data-extname", "Haustür"), ("data-camera_id", "7")),
    ]


def test_doorstations_from_attributes_skips_elements_without_a_camera() -> None:
    elements = [{"data-extid": "7"}, {"data-extid": "8", "data-camera_id": "8", "data-extname": "Hof"}]

    assert doorstations_from_attributes(elements) == {8: Doorstation(ext_id=8, camera_id=8, name="Hof")}


# --- stream path --------------------------------------------------------------------------------------------------


def test_stream_path_of_a_camera() -> None:
    assert doorstation_stream_path(7) == STREAM_PATH
    assert Doorstation(ext_id=7, camera_id=12, name=None).stream_path == "/proxy/cam/12/"


@pytest.mark.parametrize("camera_id", [True, "7", 7.0, None])
def test_stream_path_refuses_a_camera_id_that_is_no_int(camera_id: Any) -> None:
    with pytest.raises(TypeError) as info:
        doorstation_stream_path(camera_id)

    assert str(info.value) == f"camera_id must be an int, not {type(camera_id).__name__}"


@pytest.mark.parametrize("camera_id", [0, -1, 10**18])
def test_stream_path_refuses_a_camera_id_out_of_range(camera_id: int) -> None:
    with pytest.raises(ValueError, match=r"^camera_id must be a positive Comexio id$"):
        doorstation_stream_path(camera_id)


def test_stream_url_is_on_the_io_server(logged_in: ComexioClient, comexio: FakeComexio) -> None:
    assert logged_in.doorstation_stream_url(7) == f"http://{comexio.host}{STREAM_PATH}"


# --- first JPEG frame ---------------------------------------------------------------------------------------------


def test_first_frame_of_a_stream_is_cut_out_whole() -> None:
    stream = _part(FRAME) + _part(SOI + SOS + b"\x01" + EOI)

    assert first_jpeg_frame(stream) == FRAME


def test_a_frame_is_complete_only_with_its_own_eoi() -> None:
    # Every cut before the final EOI — inside the thumbnail, the scan, a stuffed byte or a restart marker — is
    # incomplete; the thumbnail's EOI must not end the frame early.
    stream = _part(FRAME)
    frame_end = stream.index(FRAME) + len(FRAME)

    incomplete = [cut for cut in range(frame_end) if first_jpeg_frame(stream[:cut]) is not None]

    assert incomplete == []
    assert first_jpeg_frame(stream[:frame_end]) == FRAME


def test_fill_bytes_before_a_marker_are_skipped() -> None:
    frame = SOI + b"\xff\xff" + APP0 + SOS + SCAN + b"\xff\xff" + EOI

    assert first_jpeg_frame(bytearray(frame)) == frame


def test_a_progressive_frame_runs_over_every_scan() -> None:
    dht = _segment(0xC4, bytes(20))
    frame = SOI + DQT + SOS + SCAN + dht + SOS + b"\x01\x02" + EOI

    assert first_jpeg_frame(frame) == frame


def test_data_without_a_start_of_image_has_no_frame() -> None:
    assert first_jpeg_frame(b"--" + BOUNDARY + b"\r\nContent-Type: image/jpeg\r\n\r\n") is None


def test_a_frame_without_a_marker_after_its_start_is_malformed() -> None:
    with pytest.raises(ComexioDataError) as info:
        first_jpeg_frame(b"xx" + SOI + b"\x00\x01")

    assert str(info.value) == "Camera frame has no JPEG marker at offset 4"


def test_a_segment_shorter_than_its_length_field_is_malformed() -> None:
    with pytest.raises(ComexioDataError) as info:
        first_jpeg_frame(SOI + b"\xff\xe0\x00\x01")

    assert str(info.value) == "Camera frame has a JPEG segment of length 1"


# --- client -------------------------------------------------------------------------------------------------------


def _logged_in(request: web.Request) -> bool:
    return request.cookies.get(SESSION_COOKIE) == SESSION_VALUE


def _admin_page(text: str) -> Handler:
    """A handler that, like Comexio, serves text only with the admin session and the login form otherwise."""

    async def handler(request: web.Request) -> web.Response:
        return web.Response(text=text if _logged_in(request) else LOGIN_PAGE, content_type="text/html")

    return handler


async def test_get_doorstations_reads_the_io_home_page(logged_in: ComexioClient, comexio: FakeComexio) -> None:
    comexio.serve("GET", IO_HOME_PATH, _admin_page(_doorstation_tab("7", "Haustür")))

    assert await logged_in.get_doorstations() == {7: Doorstation(ext_id=7, camera_id=7, name="Haustür")}
    assert len(comexio.received_at("GET", IO_HOME_PATH)) == 1


async def test_both_camera_calls_need_the_admin_session(client: ComexioClient, comexio: FakeComexio) -> None:
    # The handlers of the reads above answer only a logged-in client; without login() both calls fail.
    comexio.serve("GET", IO_HOME_PATH, _admin_page(_doorstation_tab("7", "Haustür")))
    comexio.serve("GET", STREAM_PATH, _stream(_part(FRAME)))

    with pytest.raises(ComexioAuthenticationError) as page_info:
        await client.get_doorstations()
    with pytest.raises(ComexioAuthenticationError) as snapshot_info:
        await client.get_doorstation_snapshot(7)

    # Both requests reached the fake, which answered the login form: the error comes from that answer.
    assert len(comexio.received_at("GET", IO_HOME_PATH)) == 1
    assert len(comexio.received_at("GET", STREAM_PATH)) == 1
    assert str(page_info.value) == "Comexio served the login form, the session is not logged in (IO home page)"
    assert str(snapshot_info.value) == (
        "Comexio served the login form, the session is not logged in (Doorstation camera 7 snapshot)"
    )


async def test_get_doorstations_on_expired_session_raises_authentication_error(
    logged_in: ComexioClient, comexio: FakeComexio
) -> None:
    comexio.serve_text("GET", IO_HOME_PATH, '<html><form><input id="loginsubmit"></form></html>')

    with pytest.raises(ComexioAuthenticationError) as info:
        await logged_in.get_doorstations()

    assert str(info.value) == "Comexio served the login form, the session is not logged in (IO home page)"


def _stream(*chunks: bytes, keep_open: bool = True) -> Handler:
    """A handler that sends chunks of an MJPEG stream one by one and, like a camera, does not end the stream.

    Only with the admin session: without it the handler serves the login form, as Comexio does for its admin pages.
    """

    async def handler(request: web.Request) -> web.StreamResponse:
        if not _logged_in(request):
            return web.Response(text=LOGIN_PAGE, content_type="text/html")
        response = web.StreamResponse(
            headers={"Content-Type": f"multipart/x-mixed-replace;boundary={BOUNDARY.decode()}"}
        )
        await response.prepare(request)
        for chunk in chunks:
            await response.write(chunk)
            await asyncio.sleep(0)
        if keep_open:
            await asyncio.sleep(30)
        return response

    return handler


async def test_snapshot_is_the_first_frame_of_the_stream(logged_in: ComexioClient, comexio: FakeComexio) -> None:
    stream = _part(FRAME) + _part(SOI + SOS + b"\x01" + EOI)
    # Split inside the final EOI: a marker that spans two chunks must still complete the frame.
    split = stream.index(FRAME) + len(FRAME) - 1
    comexio.serve("GET", STREAM_PATH, _stream(stream[:100], stream[100:split], stream[split:]))

    assert await logged_in.get_doorstation_snapshot(7) == FRAME
    assert len(comexio.received_at("GET", STREAM_PATH)) == 1


def _encoded_stream(encoding: str, body: bytes, asked_for: list[str | None]) -> Handler:
    """A handler that sends body under the Content-Encoding given and notes the Accept-Encoding it was asked with."""

    async def handler(request: web.Request) -> web.StreamResponse:
        asked_for.append(request.headers.get("Accept-Encoding"))
        return web.Response(body=body, headers={"Content-Encoding": encoding}, content_type="multipart/x-mixed-replace")

    return handler


@pytest.mark.parametrize("encoding", ["gzip", "deflate", "br", "identity, gzip"])
async def test_snapshot_asks_for_the_stream_unencoded_and_never_decompresses_it(
    logged_in: ComexioClient, comexio: FakeComexio, monkeypatch: pytest.MonkeyPatch, encoding: str
) -> None:
    # Decompressed chunks would be allocated before the byte cap applies: a small gzip body can expand enormously.
    # The body stays gzip under every label: the client must refuse it before reading it.
    asked_for: list[str | None] = []
    comexio.serve("GET", STREAM_PATH, _encoded_stream(encoding, gzip.compress(_part(FRAME)), asked_for))
    options: list[dict[str, Any]] = []
    request = logged_in._session.request

    def spy(*args: Any, **kwargs: Any) -> Any:
        options.append(kwargs)
        return request(*args, **kwargs)

    monkeypatch.setattr(logged_in._session, "request", spy)

    with pytest.raises(ComexioDataError) as info:
        await logged_in.get_doorstation_snapshot(7)

    assert str(info.value) == f"Doorstation camera 7 snapshot: the stream came {encoding!r}-encoded, not unencoded"
    assert asked_for == ["identity"]
    assert [kwargs.get("auto_decompress") for kwargs in options] == [False]


@pytest.mark.parametrize(
    ("call", "headers", "expected"),
    [
        pytest.param("text", None, "identity", id="text-request"),
        pytest.param("redirect", None, "identity", id="redirect-request"),
        pytest.param("text", {}, "identity", id="empty-headers"),
        pytest.param("text", {"Accept-Encoding": "gzip"}, "gzip", id="a-header-of-the-caller-wins"),
        pytest.param("text", {"accept-encoding": "gzip"}, "gzip", id="a-lowercase-header-of-the-caller-wins"),
        pytest.param("text", {"Referer": "http://x/"}, "identity", id="other-headers-stay"),
    ],
)
async def test_every_request_asks_for_an_unencoded_answer(
    logged_in: ComexioClient,
    comexio: FakeComexio,
    call: str,
    headers: dict[str, str] | None,
    expected: str,
) -> None:
    # An error page is read up to a byte cap, and aiohttp decompresses before the reader sees the bytes: the cap
    # holds for the page itself only if the device is asked not to compress it.
    asked_for: list[tuple[str | None, str | None]] = []

    async def handler(request: web.Request) -> web.StreamResponse:
        asked_for.append((request.headers.get("Accept-Encoding"), request.headers.get("Referer")))
        return web.Response(status=302, headers={"Location": "/elsewhere"}) if call == "redirect" else web.Response()

    comexio.serve("POST", "/probe", handler)
    options: dict[str, Any] = {"headers": headers} if headers is not None else {}

    if call == "redirect":
        await logged_in._request_redirect("POST", "/probe", what="Probe", **options)
    else:
        await logged_in._request_text("POST", "/probe", what="Probe", **options)

    assert asked_for == [(expected, (headers or {}).get("Referer"))]


@pytest.mark.parametrize(
    ("method_name", "status"),
    [pytest.param("_request_text", 200, id="text-request"), pytest.param("_request_redirect", 302, id="redirect")],
)
async def test_every_request_leaves_the_answer_undecompressed(
    logged_in: ComexioClient, comexio: FakeComexio, monkeypatch: pytest.MonkeyPatch, method_name: str, status: int
) -> None:
    # The compressed-answer tests pass either way (the Content-Encoding header stays): only the option pins it.
    async def handler(_request: web.Request) -> web.StreamResponse:
        return web.Response(status=status, headers={"Location": "/elsewhere"} if status == 302 else {})

    comexio.serve("POST", "/probe", handler)
    options: list[dict[str, Any]] = []
    request = logged_in._session.request

    def spy(*args: Any, **kwargs: Any) -> Any:
        options.append(kwargs)
        return request(*args, **kwargs)

    monkeypatch.setattr(logged_in._session, "request", spy)
    send = getattr(logged_in, method_name)

    await send("POST", "/probe", what="Probe")

    assert [kwargs.get("auto_decompress") for kwargs in options] == [False]


async def test_a_followed_redirect_asks_for_an_unencoded_answer_too(
    logged_in: ComexioClient, comexio: FakeComexio
) -> None:
    asked_for: list[str | None] = []

    async def hop(request: web.Request) -> web.StreamResponse:
        asked_for.append(request.headers.get("Accept-Encoding"))
        return web.Response(status=302, headers={"Location": "/elsewhere"})

    async def target(request: web.Request) -> web.StreamResponse:
        asked_for.append(request.headers.get("Accept-Encoding"))
        return web.Response(text="done")

    comexio.serve("GET", "/probe", hop)
    comexio.serve("GET", "/elsewhere", target)

    assert await logged_in._request_text("GET", "/probe", what="Probe") == "done"
    assert asked_for == ["identity", "identity"]


@pytest.mark.parametrize("encoding", ["identity", " Identity "])
async def test_snapshot_reads_a_stream_marked_unencoded(
    logged_in: ComexioClient, comexio: FakeComexio, encoding: str
) -> None:
    comexio.serve("GET", STREAM_PATH, _encoded_stream(encoding, _part(FRAME), []))

    assert await logged_in.get_doorstation_snapshot(7) == FRAME


async def test_snapshot_of_a_stream_that_ends_without_a_frame_raises(
    logged_in: ComexioClient, comexio: FakeComexio
) -> None:
    comexio.serve("GET", STREAM_PATH, _stream(_part(FRAME[:50]), keep_open=False))

    with pytest.raises(ComexioDataError) as info:
        await logged_in.get_doorstation_snapshot(7)

    assert str(info.value) == (
        "Doorstation camera 7 snapshot: the stream ('multipart/x-mixed-replace') ended after"
        f" {len(_part(FRAME[:50]))} bytes without a complete JPEG"
    )


async def test_snapshot_reads_no_more_than_the_byte_cap(
    logged_in: ComexioClient, comexio: FakeComexio, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(client_module, "_MAX_SNAPSHOT_BYTES", 64)
    comexio.serve("GET", STREAM_PATH, _stream(*[b"\x00" * 32] * 4))

    with pytest.raises(ComexioDataError) as info:
        await logged_in.get_doorstation_snapshot(7)

    assert str(info.value) == "Doorstation camera 7 snapshot: no complete JPEG in the first 64 bytes of the stream"


async def test_snapshot_of_a_silent_stream_times_out(
    logged_in: ComexioClient, comexio: FakeComexio, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(client_module, "_SNAPSHOT_TIMEOUT_SEC", 0.05)
    comexio.serve("GET", STREAM_PATH, _stream(_part(FRAME[:50])))

    with pytest.raises(ComexioConnectionError) as info:
        await logged_in.get_doorstation_snapshot(7)

    assert str(info.value) == "Doorstation camera 7 snapshot: no complete frame within 0.05 s"


async def test_snapshot_on_expired_session_raises_authentication_error(
    logged_in: ComexioClient, comexio: FakeComexio
) -> None:
    comexio.serve_text("GET", STREAM_PATH, '<html><form><input id="loginsubmit"></form></html>')

    with pytest.raises(ComexioAuthenticationError) as info:
        await logged_in.get_doorstation_snapshot(7)

    assert str(info.value) == (
        "Comexio served the login form, the session is not logged in (Doorstation camera 7 snapshot)"
    )


async def test_snapshot_of_an_unknown_camera_raises_response_error(
    logged_in: ComexioClient, comexio: FakeComexio
) -> None:
    comexio.serve_text("GET", STREAM_PATH, "no such camera", status=404)

    with pytest.raises(ComexioResponseError) as info:
        await logged_in.get_doorstation_snapshot(7)

    assert info.value.status == 404
    assert str(info.value) == "Doorstation camera 7 snapshot failed: HTTP 404: no such camera"


class _ErrorBody:
    """resp.content of an error answer that hands out the given chunks, then b""; counts the bytes handed out."""

    def __init__(self, chunks: Iterable[bytes]) -> None:
        self._chunks = iter(chunks)
        self.handed_out = 0

    async def read(self, n: int = -1) -> bytes:
        chunk = next(self._chunks, b"")
        part = chunk if n < 0 else chunk[:n]
        self.handed_out += len(part)
        return part


class _ErrorResponse:
    status = 500

    def __init__(self, chunks: Iterable[bytes], charset: str | None = None) -> None:
        self.content = _ErrorBody(chunks)
        self.charset = charset
        self.headers: dict[str, str] = {}


async def test_an_error_page_is_read_up_to_its_byte_cap_only() -> None:
    resp: Any = _ErrorResponse(itertools.repeat(b"x" * 1000))  # a body that never ends

    text = await client_module._error_page_text(resp, "snapshot")

    assert text == "x" * client_module._ERROR_BODY_MAX_BYTES
    assert resp.content.handed_out == client_module._ERROR_BODY_MAX_BYTES


def _breaks_off_after(first: bytes) -> Iterator[bytes]:
    yield first
    raise aiohttp.ClientPayloadError("connection closed mid-body")


async def test_an_error_page_that_breaks_off_mid_read_gives_no_excerpt() -> None:
    resp: Any = _ErrorResponse(_breaks_off_after(b"half an error page"))

    assert await client_module._error_page_text(resp, "snapshot") == ""


@pytest.mark.parametrize(
    ("chunks", "charset", "text"),
    [
        ([b"Fehler: T\xfcr"], "latin-1", "Fehler: Tür"),
        ([b"Fehler: T\xc3", b"\xbcr"], None, "Fehler: Tür"),  # a character split between two chunks
        ([b"T\xc3\xbcr"], "no-such-charset", "Tür"),
        ([b"T\xfcr"], "utf-8", "T�r"),
    ],
    ids=["declared-charset", "utf-8-by-default", "unknown-charset", "undecodable-byte"],
)
async def test_an_error_page_is_decoded_in_its_charset(chunks: list[bytes], charset: str | None, text: str) -> None:
    resp: Any = _ErrorResponse(chunks, charset)

    assert await client_module._error_page_text(resp, "snapshot") == text


async def test_snapshot_refuses_an_invalid_camera_id_before_any_request(
    logged_in: ComexioClient, comexio: FakeComexio
) -> None:
    requests_before = len(comexio.requests)

    with pytest.raises(ValueError, match=r"^camera_id must be a positive Comexio id$"):
        await logged_in.get_doorstation_snapshot(0)

    assert len(comexio.requests) == requests_before


# --- review additions: page shape, names, marker edge cases, exact chunk boundaries -------------------------------


@pytest.mark.parametrize("html", ["<html><body><p>Wartung</p></body></html>", ""], ids=["other-page", "empty"])
def test_a_page_without_any_extension_counter_is_a_changed_page(html: str) -> None:
    with pytest.raises(ComexioDataError) as info:
        parse_doorstations(html)

    assert str(info.value) == "IO home page has no extension counters, the Doorstations are unknown"


async def test_get_doorstations_of_a_changed_page_raises_instead_of_finding_none(
    logged_in: ComexioClient, comexio: FakeComexio
) -> None:
    comexio.serve_text("GET", IO_HOME_PATH, "<html><body><p>Wartung</p></body></html>")

    with pytest.raises(ComexioDataError, match=r"^IO home page has no extension counters"):
        await logged_in.get_doorstations()


def test_a_page_whose_camera_elements_name_no_camera_and_no_counter_is_a_changed_page() -> None:
    html = '<div data-extid="3" data-extname="Tuer" data-camera_id=""></div><div data-camera_id=""></div>'

    with pytest.raises(ComexioDataError) as info:
        parse_doorstations(html)

    assert str(info.value) == "IO home page has no extension counters, the Doorstations are unknown"


def test_one_element_that_names_a_camera_marks_the_page_beside_empty_camera_ids() -> None:
    html = f'<div data-extid="3" data-camera_id=""></div>{_doorstation_tab("7", "Haustür")}'

    assert parse_doorstations(html) == {7: Doorstation(ext_id=7, camera_id=7, name="Haustür")}


def test_camera_elements_without_a_camera_beside_a_counter_are_no_doorstation() -> None:
    assert parse_doorstations(f'{BASE_BLOCK}<div data-extid="3" data-camera_id=""></div>') == {}


async def test_get_doorstations_of_a_page_without_doorstations_finds_none(
    logged_in: ComexioClient, comexio: FakeComexio
) -> None:
    comexio.serve_text("GET", IO_HOME_PATH, f"<html><body>{BASE_BLOCK}</body></html>")

    assert await logged_in.get_doorstations() == {}


@pytest.mark.parametrize("named_first", [True, False], ids=["named-first", "unnamed-first"])
def test_an_element_without_a_name_but_another_camera_clashes(
    named_first: bool, caplog: pytest.LogCaptureFixture
) -> None:
    named = _element(data_extid="7", data_extname="Haustür", data_camera_id="7")
    unnamed = _element(data_extid="7", data_camera_id="8")
    html = named + unnamed if named_first else unnamed + named

    with caplog.at_level(logging.WARNING, logger="aiocomexio"):
        result = parse_doorstations(html)

    first = (7, "Haustür") if named_first else (8, None)
    second = "camera 8 ''" if named_first else "camera 7 'Haustür'"
    assert result == {7: Doorstation(ext_id=7, camera_id=first[0], name=first[1])}
    assert [record.getMessage() for record in caplog.records] == [
        f"Doorstation of extension 7 is named twice (camera {first[0]} '{first[1] or ''}', then {second}), "
        "the first one counts"
    ]


@pytest.mark.parametrize("named_first", [True, False], ids=["named-first", "unnamed-first"])
def test_an_element_without_a_name_does_not_clash_with_a_named_one(
    named_first: bool, caplog: pytest.LogCaptureFixture
) -> None:
    named = _element(data_extid="7", data_extname="Haustür", data_camera_id="7")
    unnamed = _element(data_extid="7", data_camera_id="7")
    html = named + unnamed if named_first else unnamed + named

    with caplog.at_level(logging.WARNING, logger="aiocomexio"):
        result = parse_doorstations(html)

    assert result == {7: Doorstation(ext_id=7, camera_id=7, name="Haustür")}
    assert caplog.records == []


def test_the_same_element_twice_names_one_doorstation_silently(caplog: pytest.LogCaptureFixture) -> None:
    element = {"data-extid": "7", "data-extname": "Haustür", "data-camera_id": "7"}

    with caplog.at_level(logging.DEBUG, logger="aiocomexio"):
        result = doorstations_from_attributes([element, dict(element)])

    assert result == {7: Doorstation(ext_id=7, camera_id=7, name="Haustür")}
    assert caplog.records == []


def test_two_names_for_the_same_camera_keep_the_first_and_log(caplog: pytest.LogCaptureFixture) -> None:
    html = _doorstation_tab("7", "Haustür") + _doorstation_tab("7", "Hof")

    with caplog.at_level(logging.WARNING, logger="aiocomexio"):
        result = parse_doorstations(html)

    assert result == {7: Doorstation(ext_id=7, camera_id=7, name="Haustür")}
    assert [record.getMessage() for record in caplog.records] == [
        "Doorstation of extension 7 is named twice (camera 7 'Haustür', then camera 7 'Hof'), the first one counts"
    ]


@pytest.mark.parametrize("code", [0x01, *range(0xD0, 0xD8)], ids=lambda code: f"0x{code:02X}")
def test_a_standalone_marker_outside_a_scan_has_no_length_field(code: int) -> None:
    frame = SOI + bytes((0xFF, code)) + APP0 + SOS + SCAN + EOI

    assert first_jpeg_frame(frame) == frame


@pytest.mark.parametrize("second_scan", [False, True], ids=["then-eoi", "then-a-second-scan"])
def test_a_dnl_segment_after_the_first_scan_is_walked_like_any_other_segment(second_scan: bool) -> None:
    # T.81 (B.2.5) puts DNL right after the first scan, between two scans, never inside entropy-coded data, where a
    # marker code other than a stuffed byte or a restart marker ends the scan.
    dnl = _segment(0xDC, b"\x00\x10")
    frame = SOI + SOS + SCAN + dnl + (SOS + SCAN if second_scan else b"") + EOI

    assert first_jpeg_frame(frame) == frame


def test_every_restart_marker_inside_a_scan_belongs_to_it() -> None:
    scan = b"".join(bytes((0xFF, code)) + b"\x11" for code in range(0xD0, 0xD8))
    frame = SOI + SOS + scan + EOI

    assert first_jpeg_frame(frame) == frame


def test_fill_bytes_before_a_restart_marker_inside_a_scan_are_skipped() -> None:
    frame = SOI + SOS + b"\x11\xff\xff\xff\xd0\x22\xff\xff\x00\x33" + EOI

    assert first_jpeg_frame(frame) == frame


def test_a_scan_that_ends_inside_fill_bytes_is_incomplete() -> None:
    assert first_jpeg_frame(SOI + SOS + b"\x11\xff\xff") is None


@pytest.mark.parametrize(
    "after_sos",
    [EOI, b"\xff\xff" + EOI, DQT + b"\x01" + EOI, b"\xff\xd0" + EOI, b"\xff\xff\xd3\xff\xd4" + EOI],
    ids=["eoi", "fill-bytes-then-eoi", "next-segment", "restart-marker-only", "restart-markers-and-fill-bytes"],
)
def test_a_scan_without_entropy_coded_data_is_malformed(after_sos: bytes) -> None:
    with pytest.raises(ComexioDataError) as info:
        first_jpeg_frame(SOI + APP0 + SOS + after_sos)

    assert str(info.value) == f"Camera frame has a scan without entropy-coded data at offset {len(SOI + APP0 + SOS)}"


@pytest.mark.parametrize(
    ("frame", "eoi_code_at"),
    [(SOI + EOI, 3), (SOI + APP0 + THUMBNAIL_APP1 + DQT + EOI, 3 + len(APP0 + THUMBNAIL_APP1 + DQT))],
    ids=["right-after-start", "after-tables"],
)
def test_an_image_that_ends_before_any_scan_is_malformed(frame: bytes, eoi_code_at: int) -> None:
    data = b"--x\r\n\r\n" + frame + b"\r\n"

    with pytest.raises(ComexioDataError) as info:
        first_jpeg_frame(data)

    assert str(info.value) == f"Camera frame ends at offset {7 + eoi_code_at} before its first scan"


def test_an_empty_segment_is_skipped() -> None:
    frame = SOI + _segment(0xFE, b"") + SOS + SCAN + EOI

    assert first_jpeg_frame(frame) == frame


def test_a_segment_of_length_zero_is_malformed() -> None:
    with pytest.raises(ComexioDataError) as info:
        first_jpeg_frame(SOI + b"\xff\xe0\x00\x00")

    assert str(info.value) == "Camera frame has a JPEG segment of length 0"


# SOS headers whose length does not fit their component count (T.81, B.2.3: 6 + 2 * Ns, Ns from 1 to 4).
_MALFORMED_SOS_HEADERS = [
    _segment(0xDA, b""),
    _segment(0xDA, b"\x00\x00\x3f\x00"),
    _segment(0xDA, b"\x05" + bytes(10) + b"\x00\x3f\x00"),
    _segment(0xDA, b"\x01\x01\x00\x02\x00\x00\x3f\x00"),
    _segment(0xDA, b"\x02\x01\x00\x00\x3f\x00"),
    _segment(0xDA, b"\x04" + bytes(6) + b"\x00\x3f\x00"),
]
_MALFORMED_SOS_IDS = [
    "length-2",
    "no-component",
    "five-components",
    "longer-than-its-components",
    "shorter-than-two-components",
    "shorter-than-four-components",
]


@pytest.mark.parametrize("sos", _MALFORMED_SOS_HEADERS, ids=_MALFORMED_SOS_IDS)
def test_an_sos_header_that_does_not_fit_its_components_is_malformed(sos: bytes) -> None:
    with pytest.raises(ComexioDataError) as info:
        first_jpeg_frame(SOI + sos + SCAN + EOI)

    assert str(info.value) == f"Camera frame has a malformed SOS header (length {len(sos) - 2}) at offset 3"


@pytest.mark.parametrize("components", [1, 4])
def test_an_sos_header_with_one_to_four_components_is_read(components: int) -> None:
    frame = SOI + _segment(0xDA, bytes((components,)) + bytes(2 * components) + b"\x00\x3f\x00") + SCAN + EOI

    assert first_jpeg_frame(frame) == frame
    assert _fed_byte_by_byte(frame) == frame


@pytest.mark.parametrize(("code", "name"), [(0x00, "0x00"), (0xD8, "0xD8")], ids=["stuffed-byte", "second-soi"])
def test_a_marker_that_cannot_start_a_segment_is_malformed(code: int, name: str) -> None:
    with pytest.raises(ComexioDataError) as info:
        first_jpeg_frame(SOI + APP0 + bytes((0xFF, code)) + b"\x00\x10" + EOI)

    assert str(info.value) == f"Camera frame has the unexpected JPEG marker {name} at offset {len(SOI + APP0) + 1}"


def test_data_that_ends_inside_a_length_field_or_fill_bytes_is_incomplete() -> None:
    assert first_jpeg_frame(SOI + b"\xff\xe0\x00") is None
    assert first_jpeg_frame(SOI + b"\xff\xff\xff") is None


def test_a_reader_fed_a_growing_buffer_returns_the_frame_once_its_eoi_arrived() -> None:
    # Byte by byte: every marker, length field and the SOI itself is split across two calls once.
    stream = _part(FRAME)
    reader = JpegFrameReader()
    buffer = bytearray()
    frames: list[bytes | None] = [None]
    while len(buffer) < len(stream) and frames[-1] is None:
        buffer.append(stream[len(buffer)])
        frames.append(reader.frame(buffer))

    assert frames[-1] == FRAME
    assert len(buffer) == _FRAME_END


# SOS headers whose component selector is 0xFF: read while the header is still incomplete, the scan's resume
# position must not fall back into it, or these bytes would end the scan (an EOI, or a marker outside a scan).
_SOS_HEADERS_WITH_FF = [
    b"\xff\xda\x00\x08\x01\xff\xd9\x00\x3f\x00",  # component selector 0xFF, then the bytes of an EOI
    b"\xff\xda\x00\x08\x01\xff\x11\x00\x3f\x00",  # component selector 0xFF, then a marker outside a scan
    b"\xff\xda\x00\x0c\x03\x01\x00\x02\x11\xff\xd9\x00\x3f\xff",  # 3 components, 0xFF in the last selector and Ah/Al
]
_SOS_HEADER_IDS = ["eoi-bytes", "marker-bytes", "three-components"]
_FRAMES_WITH_FF_IN_THE_SOS_HEADER = [SOI + header + b"\x11\x22\x33" + EOI for header in _SOS_HEADERS_WITH_FF]


def _fed_byte_by_byte(data: bytes) -> bytes | None:
    """What one JpegFrameReader finds when data arrives one byte per call."""
    reader = JpegFrameReader()
    buffer = bytearray()
    for byte in data:
        buffer.append(byte)
        if (frame := reader.frame(buffer)) is not None:
            return frame
    return None


@pytest.mark.parametrize("frame", _FRAMES_WITH_FF_IN_THE_SOS_HEADER, ids=_SOS_HEADER_IDS)
def test_a_reader_fed_byte_by_byte_resumes_a_scan_after_its_sos_header(frame: bytes) -> None:
    assert first_jpeg_frame(frame) == frame
    assert _fed_byte_by_byte(frame) == frame


@pytest.mark.parametrize(
    "frame",
    [SOI + SOS + SCAN + b"\xff\xff\xff" + EOI, SOI + SOS + SCAN + b"\xff\xff" + b"\xff\xd1\x42" + EOI],
    ids=["fill-bytes-before-eoi", "fill-bytes-before-a-restart-marker"],
)
def test_a_reader_fed_byte_by_byte_ends_a_scan_after_its_fill_bytes(frame: bytes) -> None:
    assert first_jpeg_frame(frame) == frame
    assert _fed_byte_by_byte(frame) == frame


@pytest.mark.parametrize(
    "data",
    [
        *(SOI + APP0 + SOS + after_sos for after_sos in (EOI, b"\xff\xd0" + EOI, b"\xff\xff\xd3\xff\xd4" + EOI)),
        SOI + APP0 + SOS + b"\xff\xff" + EOI,
        SOI + APP0 + _SOS_HEADERS_WITH_FF[2] + b"\xff" + EOI,
        b"--x\r\n\r\n" + SOI + APP0 + THUMBNAIL_APP1 + DQT + EOI + b"\r\n",
        SOI + b"\xff\xe0\x00\x00",
        SOI + APP0 + b"\xff\x00\x00\x10" + EOI,
        SOI + APP0 + _SOS_HEADERS_WITH_FF[0] + EOI,
        *(SOI + APP0 + sos + SCAN + EOI for sos in _MALFORMED_SOS_HEADERS),
    ],
    ids=[
        "empty-scan",
        "restart-marker-scan",
        "restart-markers-scan",
        "fill-bytes-scan",
        "fill-bytes-after-ff-in-the-sos-header",
        "eoi-before-scan",
        "length-0",
        "stuffed-byte",
        "empty-scan-after-ff-in-its-header",
        *(f"sos-{name}" for name in _MALFORMED_SOS_IDS),
    ],
)
def test_a_reader_fed_byte_by_byte_raises_like_one_call(data: bytes) -> None:
    with pytest.raises(ComexioDataError) as one_call:
        first_jpeg_frame(data)
    with pytest.raises(ComexioDataError) as byte_by_byte:
        _fed_byte_by_byte(data)

    assert str(byte_by_byte.value) == str(one_call.value)


def test_a_reader_searches_a_growing_scan_once() -> None:
    reader = JpegFrameReader()
    buffer = _SearchCountingBuffer(SOI + SOS)
    assert reader.frame(buffer) is None
    for _ in range(500):
        buffer += bytes(range(1, 101))  # entropy-coded data without any 0xFF
        assert reader.frame(buffer) is None
    buffer += EOI

    assert reader.frame(buffer) == bytes(buffer)
    assert buffer.searched <= 2 * len(buffer)


class _LookupCountingBuffer(bytearray):
    """A read buffer that counts how many single bytes are looked up in it."""

    looked_up = 0

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, int):
            self.looked_up += 1
        return super().__getitem__(key)


@pytest.mark.parametrize(
    ("before", "after"),
    [(SOI, APP0 + SOS + SCAN + EOI), (SOI + SOS + b"\x11", b"\xd0\x22" + EOI)],
    ids=["between-segments", "inside-a-scan"],
)
def test_a_reader_reads_a_growing_run_of_fill_bytes_once(before: bytes, after: bytes) -> None:
    # Each call brings one more 0xFF fill byte: going back to the start of the run would read it n²/2 times.
    reader = JpegFrameReader()
    buffer = _LookupCountingBuffer(before)
    for _ in range(500):
        buffer.append(0xFF)
        assert reader.frame(buffer) is None
    buffer += after

    assert reader.frame(buffer) == bytes(buffer)
    assert buffer.looked_up <= 4 * len(buffer)


@pytest.mark.parametrize("frame", _FRAMES_WITH_FF_IN_THE_SOS_HEADER, ids=_SOS_HEADER_IDS)
def test_a_reader_fed_two_parts_finds_the_frame_at_every_cut(frame: bytes) -> None:
    for cut in range(1, len(frame)):
        reader = JpegFrameReader()
        buffer = bytearray(frame[:cut])
        reader.frame(buffer)
        buffer += frame[cut:]

        assert reader.frame(buffer) == frame, f"cut at {cut}"


class _SearchCountingBuffer(bytearray):
    """A read buffer that adds up how many bytes its find calls search."""

    searched = 0

    def find(self, sub: Any, start: Any = 0, *rest: Any) -> int:
        self.searched += len(self) - start
        return super().find(sub, start, *rest)


def test_a_reader_searches_the_bytes_before_the_soi_once() -> None:
    # Every chunk before the frame holds an EOI, so the reader is asked for a frame after each of them.
    junk = b"--x\r\n" + EOI
    reader = JpegFrameReader()
    buffer = _SearchCountingBuffer()
    for _ in range(500):
        buffer += junk
        assert reader.frame(buffer) is None
    buffer += _part(FRAME)

    assert reader.frame(buffer) == FRAME
    assert buffer.searched <= 2 * len(buffer)


async def test_a_stream_of_segments_is_walked_once(monkeypatch: pytest.MonkeyPatch) -> None:
    # Each segment carries an EOI and arrives in a chunk of its own: rereading from the start would walk n²/2 segments.
    walked: list[int] = []
    segment_end = doorstation_module._segment_end

    def counting_segment_end(data: bytes | bytearray, pos: int) -> int | None:
        walked.append(pos)
        return segment_end(data, pos)

    monkeypatch.setattr(doorstation_module, "_segment_end", counting_segment_end)
    segments = [_segment(0xFE, EOI)] * 500
    resp: Any = _ChunkedResponse([SOI, *segments, SOS + SCAN + EOI])

    assert await client_module._read_first_frame(resp, "snapshot") == SOI + b"".join(segments) + SOS + SCAN + EOI
    assert len(walked) == len(segments) + 1  # every segment once, then the SOS


class _ChunkedContent:
    """resp.content of a response that hands out the given chunks (aiohttp may merge real ones).

    Like aiohttp's StreamReader.read(n), a read returns at most n bytes of what is buffered; the rest of a chunk
    stays for the next read, and an empty answer is the end of the stream.
    """

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)
        self.reads = 0
        self.asked: list[int] = []

    async def read(self, n: int = -1) -> bytes:
        self.reads += 1
        self.asked.append(n)
        if not self._chunks:
            return b""
        chunk = self._chunks.pop(0)
        if 0 <= n < len(chunk):
            self._chunks.insert(0, chunk[n:])
            return chunk[:n]
        return chunk


class _ChunkedResponse:
    content_type = "multipart/x-mixed-replace"

    def __init__(self, chunks: list[bytes]) -> None:
        self.content = _ChunkedContent(chunks)


def _split_stream(*cuts: int) -> list[bytes]:
    # One part only: a second frame with an EOI of its own would complete the read even if the first EOI was missed.
    stream = _part(FRAME)
    bounds = [0, *cuts, len(stream)]
    return [stream[start:end] for start, end in zip(bounds, bounds[1:], strict=False)]


_FRAME_START = len(_part(FRAME)) - len(FRAME) - 2
_FRAME_END = _FRAME_START + len(FRAME)
_THUMBNAIL_END = _FRAME_START + len(SOI + APP0 + THUMBNAIL_APP1)  # just after the thumbnail's EOI


@pytest.mark.parametrize(
    "cuts",
    [
        (_FRAME_END - 1,),
        (_FRAME_END - 2,),
        (_THUMBNAIL_END, _FRAME_END - 2),
        (_THUMBNAIL_END - 1, _THUMBNAIL_END + 3),
        tuple(range(1, _FRAME_END + 5)),
    ],
    ids=["split-inside-eoi", "eoi-starts-a-chunk", "thumbnail-eoi-ends-a-chunk", "thumbnail-eoi-split", "byte-by-byte"],
)
async def test_first_frame_across_exact_chunk_boundaries(cuts: tuple[int, ...]) -> None:
    resp = _ChunkedResponse(_split_stream(*cuts))

    assert await client_module._read_first_frame(cast(Any, resp), "snapshot") == FRAME
    # The read stops with the chunk that brings the frame's EOI, not later.
    assert resp.content.reads == 1 + sum(cut < _FRAME_END for cut in cuts)


async def test_a_stream_page_that_is_no_stream_names_its_content_type() -> None:
    resp: Any = _ChunkedResponse([b"<html><body>Wartung</body></html>"])
    resp.content_type = "text/html"

    with pytest.raises(ComexioDataError) as info:
        await client_module._read_first_frame(resp, "snapshot")

    assert str(info.value) == "snapshot: the stream ('text/html') ended after 33 bytes without a complete JPEG"


async def test_a_stream_of_exactly_the_byte_cap_is_read_whole(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client_module, "_MAX_SNAPSHOT_BYTES", 64)
    resp: Any = _ChunkedResponse([b"\x00" * 32, b"\x00" * 32])

    with pytest.raises(ComexioDataError) as info:
        await client_module._read_first_frame(resp, "snapshot")

    assert str(info.value) == "snapshot: no complete JPEG in the first 64 bytes of the stream"
    assert resp.content.reads == 2


async def test_a_stream_that_stalls_at_the_byte_cap_does_not_wait_for_more(monkeypatch: pytest.MonkeyPatch) -> None:
    # No byte after the cap can complete a frame, so the read must end with the chunk that reaches the cap
    # and not ask the stream for another one (which a stalled stream would answer with a timeout).
    monkeypatch.setattr(client_module, "_MAX_SNAPSHOT_BYTES", 64)
    resp: Any = _ChunkedResponse([SOI + b"\xff\xe1\xff\xff" + bytes(58), b"\x00"])

    with pytest.raises(ComexioDataError) as info:
        await client_module._read_first_frame(resp, "snapshot")

    assert str(info.value) == "snapshot: no complete JPEG in the first 64 bytes of the stream"
    assert resp.content.asked == [64]  # one read, no second one for a stream that stalls at the cap


async def test_one_byte_over_the_cap_stops_the_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client_module, "_MAX_SNAPSHOT_BYTES", 64)
    # An image that never ends: its start, then a segment that claims to run on far beyond the cap.
    resp: Any = _ChunkedResponse([SOI + b"\xff\xe1\xff\xff" + bytes(57), b"\x00\x00", b"\x00", b"\x00"])

    with pytest.raises(ComexioDataError) as info:
        await client_module._read_first_frame(resp, "snapshot")

    assert str(info.value) == "snapshot: no complete JPEG in the first 64 bytes of the stream"
    # The second read asks for the one byte left under the cap, not for the whole two-byte chunk, and the read
    # that reaches the cap is the last one: the other byte of that chunk stays in the stream.
    assert resp.content.asked == [64, 1]


async def test_a_login_form_that_runs_past_the_cap_is_an_expired_session(monkeypatch: pytest.MonkeyPatch) -> None:
    page = LOGIN_PAGE.encode()
    monkeypatch.setattr(client_module, "_MAX_SNAPSHOT_BYTES", len(page) + 8)
    resp: Any = _ChunkedResponse([page, bytes(16), bytes(16)])

    with pytest.raises(ComexioAuthenticationError) as info:
        await client_module._read_first_frame(resp, "snapshot")

    assert str(info.value) == "Comexio served the login form, the session is not logged in (snapshot)"
    assert resp.content.asked == [len(page) + 8, 8]  # the second read takes 8 of the 16 bytes, the rest stays


async def test_a_login_form_past_the_cap_is_not_seen(monkeypatch: pytest.MonkeyPatch) -> None:
    # Only the kept bytes count: a login form that starts beyond the cap is a stream without a frame.
    monkeypatch.setattr(client_module, "_MAX_SNAPSHOT_BYTES", 64)
    resp: Any = _ChunkedResponse([bytes(64), LOGIN_PAGE.encode()])

    with pytest.raises(ComexioDataError) as info:
        await client_module._read_first_frame(resp, "snapshot")

    assert str(info.value) == "snapshot: no complete JPEG in the first 64 bytes of the stream"


def _frame_ending_at_the_cap(padding: int) -> Any:
    """A stream whose second chunk brings a whole frame to a cap of 2 * len(FRAME), padding bytes late."""
    return _ChunkedResponse([bytes(len(FRAME) + padding), FRAME + b"\x00" * 8])


async def test_a_frame_that_ends_at_the_cap_is_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client_module, "_MAX_SNAPSHOT_BYTES", 2 * len(FRAME))
    resp = _frame_ending_at_the_cap(padding=0)

    assert await client_module._read_first_frame(resp, "snapshot") == FRAME


async def test_a_frame_that_ends_past_the_cap_is_too_large(monkeypatch: pytest.MonkeyPatch) -> None:
    # The frame's EOI lies one byte past the cap: that byte is never read, so the frame does not complete.
    monkeypatch.setattr(client_module, "_MAX_SNAPSHOT_BYTES", 2 * len(FRAME))
    resp = _frame_ending_at_the_cap(padding=1)

    with pytest.raises(ComexioDataError) as info:
        await client_module._read_first_frame(resp, "snapshot")

    assert str(info.value) == f"snapshot: no complete JPEG in the first {2 * len(FRAME)} bytes of the stream"


async def test_an_eoi_split_by_the_read_that_reaches_the_cap_completes_the_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Only the last byte of the frame fits under the cap; the EOI search must still see the 0xFF before it.
    monkeypatch.setattr(client_module, "_MAX_SNAPSHOT_BYTES", len(FRAME))
    resp: Any = _ChunkedResponse([FRAME[:-1], FRAME[-1:] + b"\x00" * 8])

    assert await client_module._read_first_frame(resp, "snapshot") == FRAME


async def test_no_read_asks_for_more_than_the_bytes_left_under_the_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client_module, "_MAX_SNAPSHOT_BYTES", 64)
    # One chunk far larger than the cap: the stream is asked for the bytes left under the cap and never for more, so
    # nothing past the cap is taken from it (aiohttp's reader would otherwise hand over a whole buffered chunk).
    resp: Any = _ChunkedResponse([SOI + b"\xff\xe1\xff\xff" + bytes(8 * 1024 * 1024)])

    with pytest.raises(ComexioDataError):
        await client_module._read_first_frame(resp, "snapshot")

    assert resp.content.asked == [64]


async def test_each_read_asks_for_what_is_left_under_the_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client_module, "_MAX_SNAPSHOT_BYTES", 64)
    resp: Any = _ChunkedResponse([SOI + b"\xff\xe1\xff\xff" + bytes(10), bytes(20), bytes(100)])

    with pytest.raises(ComexioDataError):
        await client_module._read_first_frame(resp, "snapshot")

    assert resp.content.asked == [64, 64 - 16, 64 - 36]


async def test_the_real_stream_reader_keeps_what_lies_past_the_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    # aiohttp's reader hands over what is buffered, so a read for the whole buffer would take every byte past the cap
    # out of the stream: with the real reader, the bytes the cap does not need must stay in it.
    monkeypatch.setattr(client_module, "_MAX_SNAPSHOT_BYTES", 64)
    content = aiohttp.StreamReader(MagicMock(), 2**16, loop=asyncio.get_running_loop())
    content.feed_data(SOI + b"\xff\xe1\xff\xff" + bytes(1024 * 1024))
    content.feed_eof()
    resp: Any = SimpleNamespace(content=content, content_type="multipart/x-mixed-replace")

    with pytest.raises(ComexioDataError) as info:
        await client_module._read_first_frame(resp, "snapshot")

    assert str(info.value) == "snapshot: no complete JPEG in the first 64 bytes of the stream"
    assert len(await content.read()) == 6 + 1024 * 1024 - 64


async def test_a_stream_that_is_empty_from_the_start_names_the_content_type() -> None:
    resp: Any = _ChunkedResponse([])

    with pytest.raises(ComexioDataError) as info:
        await client_module._read_first_frame(resp, "snapshot")

    assert str(info.value) == (
        "snapshot: the stream ('multipart/x-mixed-replace') ended after 0 bytes without a complete JPEG"
    )


async def test_snapshot_of_a_malformed_frame_names_the_camera(logged_in: ComexioClient, comexio: FakeComexio) -> None:
    comexio.serve("GET", STREAM_PATH, _stream(SOI + b"xx" + EOI))

    with pytest.raises(ComexioDataError) as info:
        await logged_in.get_doorstation_snapshot(7)

    assert str(info.value) == "Doorstation camera 7 snapshot: Camera frame has no JPEG marker at offset 2"


async def test_snapshot_of_a_stream_that_breaks_off_raises_connection_error(
    logged_in: ComexioClient, comexio: FakeComexio
) -> None:
    async def handler(request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse(headers={"Content-Type": "multipart/x-mixed-replace;boundary=x"})
        await response.prepare(request)
        await response.write(_part(FRAME[:50]))
        assert request.transport is not None
        request.transport.close()
        return response

    comexio.serve("GET", STREAM_PATH, handler)

    with pytest.raises(ComexioConnectionError) as info:
        await logged_in.get_doorstation_snapshot(7)

    assert re.fullmatch(r"Doorstation camera 7 snapshot failed: \w+\(.*\)", str(info.value), re.DOTALL)
    assert "no complete frame" not in str(info.value)


async def test_snapshot_of_a_server_that_sends_no_headers_times_out(
    logged_in: ComexioClient, comexio: FakeComexio, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(client_module, "_SNAPSHOT_TIMEOUT_SEC", 0.05)

    async def handler(request: web.Request) -> web.StreamResponse:
        await asyncio.sleep(1)
        return web.StreamResponse()

    comexio.serve("GET", STREAM_PATH, handler)

    with pytest.raises(ComexioConnectionError) as info:
        await logged_in.get_doorstation_snapshot(7)

    assert str(info.value) == "Doorstation camera 7 snapshot: no complete frame within 0.05 s"


async def test_a_cancelled_snapshot_stays_cancelled(logged_in: ComexioClient, comexio: FakeComexio) -> None:
    comexio.serve("GET", STREAM_PATH, _stream(_part(FRAME[:50])))
    task = asyncio.create_task(logged_in.get_doorstation_snapshot(7))
    await asyncio.sleep(0.05)

    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.parametrize(
    "error",
    [
        aiohttp.SocketTimeoutError("Timeout on reading data from socket"),
        aiohttp.ConnectionTimeoutError("Connection timeout"),
        aiohttp.ServerTimeoutError("Timeout"),
        TimeoutError(),  # the session's total timeout raises a plain TimeoutError
    ],
    ids=["socket-read", "connect", "server", "session-total"],
)
async def test_a_timeout_other_than_the_snapshots_own_is_a_transport_error(
    error: Exception, logged_in: ComexioClient, comexio: FakeComexio, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def timing_out(resp: object, what: str) -> bytes:
        raise error

    monkeypatch.setattr(client_module, "_read_first_frame", timing_out)
    comexio.serve("GET", STREAM_PATH, _stream(_part(FRAME)))

    with pytest.raises(ComexioConnectionError) as info:
        await logged_in.get_doorstation_snapshot(7)

    assert info.value.__cause__ is error
    assert str(info.value).startswith("Doorstation camera 7 snapshot failed: ")


@pytest.mark.parametrize(
    "html",
    [
        f"<script>{BASE_BLOCK}</script>",
        f"<!-- {BASE_BLOCK} -->",
        '<div class="number_of_type_30">0</div>',
        '<div id="my_number_of_1" class="number_of_type_30">0</div>',
        '<span id="number_of_1" class="number_of_type_30">0</span>',
        '<div id="number_of_1">0</div>',
        '<input id="number_of_retries" value="3">',
        '<div id="number_of_1" class="x_number_of_type_30">0</div>',
        '<div id="number_of_1" class="number_of_type_status">0</div>',
        '<div id="number_of_1" class="number_of_type_30_">0</div>',
        '<div id="number_of_1" class="number_of_type_">0</div>',
        # A page that kept the other Doorstation attributes but lost data-camera_id has changed.
        _element(data_extid="7", data_extname="EXT1"),
    ],
    ids=[
        "hidden-in-script",
        "hidden-in-comment",
        "no-id",
        "id-not-a-prefix",
        "no-div",
        "no-type-class",
        "input",
        "class-not-a-prefix",
        "type-not-a-number",
        "type-with-empty-suffix",
        "type-missing",
        "extension-element-without-counter",
    ],
)
def test_only_an_extension_counter_marks_the_io_home_page(html: str) -> None:
    with pytest.raises(ComexioDataError, match=r"^IO home page has no extension counters"):
        parse_doorstations(html)


@pytest.mark.parametrize(
    "html",
    [
        '<div id="number_of_1" class="tab number_of_type_30_2 big">4</div>',
        '<div id="number_of_1" class="number_of_type_30_3">0</div>',
    ],
    ids=["type-class-among-others", "suffixed-type"],
)
def test_a_counter_among_other_or_with_suffixed_classes_marks_the_io_home_page(html: str) -> None:
    assert parse_doorstations(html) == {}


def test_a_camera_element_without_an_extension_id_is_named_as_unusable() -> None:
    html = _element(data_camera_id="7")

    with pytest.raises(ComexioDataError) as info:
        parse_doorstations(html)

    assert str(info.value) == "None of the 1 Doorstation elements has a usable extension and camera id"
