"""Doorstation pictures: the list of getPictureList, the names that become paths, and the two client reads."""

import asyncio
import gzip
import itertools
import json
import logging
from datetime import datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest
from aiohttp import web
from multidict import CIMultiDict

from aiocomexio import (
    ComexioAuthenticationError,
    ComexioClient,
    ComexioConnectionError,
    ComexioDataError,
    ComexioResponseError,
    DoorstationPicture,
    DoorstationPictureKind,
    doorstation_picture_file_path,
)
from aiocomexio import client as client_module
from aiocomexio.doorstation_pictures import (
    doorstation_picture_path,
    is_plain_picture_name,
    parse_picture_list,
    picture_of,
)
from tests.fake_comexio import LOGIN_PAGE, SESSION_COOKIE, SESSION_VALUE, FakeComexio, Handler

LIST_PATH = "/board/doorstation/getPictureList"
BELL = "26-11-23-18-04-09_bell.jpg"
MOTION = "26-11-23-18-05-30_pir.jpg"
OLDER_BELL = "26-11-22-07-00-00_bell.jpg"
SHOT = "26-11-21-12-00-00_shot.jpg"
FILE_PATH = f"/proxy/file/7/images/doorstation/{BELL}"

# A complete synthetic JPEG: SOI, one scan (SOS header for one component, made-up entropy-coded data), EOI.
SOI = b"\xff\xd8"
EOI = b"\xff\xd9"
SOS = b"\xff\xda" + (8).to_bytes(2) + b"\x01\x01\x00\x00\x3f\x00"
JPEG = SOI + SOS + b"\x12\x34\x56" + EOI

NOT_PLAIN = [
    "",
    ".jpg",
    "../etc/passwd.jpg",
    "a/b.jpg",
    "a\\b.jpg",
    "a%2fb.jpg",
    "a b.jpg",
    "a\nb.jpg",
    "picture.png",
    "picture.jpg ",
    "picture.JPG",
    "-leading-dash.jpg",
    ".hidden.jpg",
    "é.jpg",
    "٣.jpg",  # an Arabic-Indic digit: \d would take it
    "a" * 200 + ".jpg",
    "a" * 129 + ".jpg",
    "x.jpg\n",
    "a?b.jpg",
    "a#b.jpg",
    "a;b.jpg",
    "a:b.jpg",
    "a\x00.jpg",
]


@pytest.mark.parametrize(
    ("name", "kind", "taken_at"),
    [
        (BELL, DoorstationPictureKind.BELL, datetime(2026, 11, 23, 18, 4, 9)),
        (MOTION, DoorstationPictureKind.MOTION, datetime(2026, 11, 23, 18, 5, 30)),
        (SHOT, DoorstationPictureKind.SCREENSHOT, datetime(2026, 11, 21, 12, 0, 0)),
        ("26-11-21-12-00-00_screenshot.jpg", DoorstationPictureKind.SCREENSHOT, datetime(2026, 11, 21, 12, 0, 0)),
        ("26-11-21-12-00-00_BELL.jpg", DoorstationPictureKind.BELL, datetime(2026, 11, 21, 12, 0, 0)),
        ("26-11-21-12-00-00_other.jpg", DoorstationPictureKind.OTHER, datetime(2026, 11, 21, 12, 0, 0)),
        # A moment that does not exist: the kind stays, the moment is not known.
        ("26-13-01-00-00-00_bell.jpg", DoorstationPictureKind.BELL, None),
        ("26-04-31-00-00-00_pir.jpg", DoorstationPictureKind.MOTION, None),
        ("26-11-23-25-00-00_bell.jpg", DoorstationPictureKind.BELL, None),
        # A name that does not follow the pattern at all.
        ("picture.jpg", DoorstationPictureKind.OTHER, None),
        # `shot` anywhere in the name makes a screenshot, with or without the pattern or a moment.
        ("26-11-21-12-00-00_manual_shot.jpg", DoorstationPictureKind.SCREENSHOT, datetime(2026, 11, 21, 12, 0, 0)),
        ("shot1.jpg", DoorstationPictureKind.SCREENSHOT, None),
        ("26-13-01-00-00-00_shot.jpg", DoorstationPictureKind.SCREENSHOT, None),
    ],
)
def test_picture_of_reads_kind_and_moment_from_the_name(
    name: str, kind: DoorstationPictureKind, taken_at: datetime | None
) -> None:
    assert picture_of(name) == DoorstationPicture(name=name, kind=kind, taken_at=taken_at)


def test_the_moment_has_no_time_zone() -> None:
    # It is the Doorstation's wall clock; attaching a zone here would be a guess.
    moment = picture_of(BELL).taken_at

    assert moment is not None
    assert moment.tzinfo is None


@pytest.mark.parametrize("name", NOT_PLAIN)
def test_a_name_that_is_not_plain_is_refused_everywhere(name: str) -> None:
    assert not is_plain_picture_name(name)
    with pytest.raises(ValueError, match="plain .jpg file name"):
        picture_of(name)
    with pytest.raises(ValueError, match="plain .jpg file name"):
        doorstation_picture_file_path(name)
    with pytest.raises(ValueError, match="plain .jpg file name"):
        doorstation_picture_path(7, name)


def test_a_name_of_the_longest_plain_length_is_plain() -> None:
    longest = "a" * 124 + ".jpg"  # 128 characters in all

    assert is_plain_picture_name(longest)
    assert doorstation_picture_file_path(longest) == f"/images/doorstation/{longest}"


@pytest.mark.parametrize("name", [None, 5, b"a.jpg"])
def test_a_name_that_is_no_string_is_a_type_error(name: object) -> None:
    assert not is_plain_picture_name(name)
    with pytest.raises(TypeError, match="name must be a str"):
        doorstation_picture_file_path(name)  # type: ignore[arg-type]


def test_the_paths_of_a_picture_file() -> None:
    assert doorstation_picture_file_path(BELL) == f"/images/doorstation/{BELL}"
    assert doorstation_picture_path(7, BELL) == FILE_PATH


@pytest.mark.parametrize(("ext_id", "error"), [(0, ValueError), (-1, ValueError), (True, TypeError), ("7", TypeError)])
def test_the_proxy_path_needs_a_real_extension_id(ext_id: object, error: type[Exception]) -> None:
    with pytest.raises(error):
        doorstation_picture_path(ext_id, BELL)  # type: ignore[arg-type]


@pytest.mark.parametrize("text", ["", "  ", "null", " null\n"])
def test_an_empty_answer_is_no_picture(text: str) -> None:
    assert parse_picture_list(text) == ()


def test_an_empty_array_is_no_picture() -> None:
    assert parse_picture_list("[]") == ()


def test_the_list_is_sorted_newest_first_whatever_the_order_of_the_answer() -> None:
    answer = json.dumps([OLDER_BELL, MOTION, SHOT, BELL])

    assert [picture.name for picture in parse_picture_list(answer)] == [MOTION, BELL, OLDER_BELL, SHOT]
    assert [picture.name for picture in parse_picture_list(json.dumps(sorted([BELL, MOTION, SHOT, OLDER_BELL])))] == [
        MOTION,
        BELL,
        OLDER_BELL,
        SHOT,
    ]


def test_names_without_a_moment_come_last_in_name_order() -> None:
    names = ["zeta.jpg", "26-13-01-00-00-00_bell.jpg", BELL, "alpha.jpg"]

    assert [picture.name for picture in parse_picture_list(json.dumps(names))] == [
        BELL,
        "26-13-01-00-00-00_bell.jpg",
        "alpha.jpg",
        "zeta.jpg",
    ]


def test_the_same_moment_of_several_kinds_comes_in_name_order_whatever_the_order_of_the_answer() -> None:
    names = [f"26-11-23-18-04-09_{kind}.jpg" for kind in ("bell", "pir", "shot", "other")]

    for permutation in itertools.permutations(names):
        assert [p.name for p in parse_picture_list(json.dumps(permutation))] == sorted(names)


def test_a_name_listed_twice_counts_once_and_is_no_unusable_entry(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG, logger="aiocomexio.doorstation_pictures"):
        pictures = parse_picture_list(json.dumps([BELL, BELL, MOTION]))

    assert [p.name for p in pictures] == [MOTION, BELL]
    assert caplog.records == []


def test_entries_that_are_no_plain_name_are_dropped_and_counted_in_one_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    answer = json.dumps([BELL, "../x.jpg", 5, None, {"a": 1}, "dir/y.jpg"])

    with caplog.at_level(logging.WARNING, logger="aiocomexio.doorstation_pictures"):
        pictures = parse_picture_list(answer)

    assert [p.name for p in pictures] == [BELL]
    assert [record.getMessage() for record in caplog.records] == [
        "Skipped 5 entries of the Doorstation picture list that are no plain file name"
    ]
    assert "x.jpg" not in caplog.text  # the names come from the device: not logged


def test_a_list_without_one_usable_name_is_a_changed_format_not_no_picture() -> None:
    with pytest.raises(ComexioDataError) as info:
        parse_picture_list(json.dumps(["../a.jpg", "b/c.jpg"]))

    assert str(info.value) == "None of the 2 entries of the Doorstation picture list is a file name"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('{"a": 1}', "Doorstation picture list is a JSON dict, not an array"),
        ('"x.jpg"', "Doorstation picture list is a JSON str, not an array"),
        ("12", "Doorstation picture list is a JSON int, not an array"),
        ("true", "Doorstation picture list is a JSON bool, not an array"),
        ("<html><body>Error SECRET</body></html>", "Doorstation picture list is not JSON (offset 0)"),
        ('["a.jpg"', "Doorstation picture list is not JSON (offset 8)"),
    ],
)
def test_an_answer_that_is_no_array_raises_without_quoting_it(text: str, expected: str) -> None:
    with pytest.raises(ComexioDataError) as info:
        parse_picture_list(text)

    assert str(info.value) == expected
    assert "SECRET" not in str(info.value)
    assert info.value.__cause__ is None  # the decoder's message would quote the answer


def test_an_array_nested_too_deeply_raises_a_data_error(garbage_collected: None) -> None:
    nested = "[" * 200_000 + "]" * 200_000

    with pytest.raises(ComexioDataError) as info:
        parse_picture_list(nested)

    assert str(info.value) == "Doorstation picture list is nested too deeply"


def test_an_integer_too_long_to_read_raises_a_data_error(int_max_str_digits: None) -> None:
    # json.loads raises a plain ValueError for it (not a JSONDecodeError): it must not escape as one.
    text = "[" + "1" * 5000 + "]"

    with pytest.raises(ComexioDataError) as info:
        parse_picture_list(text)

    assert str(info.value) == "Doorstation picture list holds a number too long to read"
    assert info.value.__cause__ is None


def _list_handler(answer: str, *, form: list[dict[str, object]] | None = None) -> Handler:
    """A handler that, like Comexio, serves the list only with the admin session and the login form otherwise."""

    async def handler(request: web.Request) -> web.Response:
        if request.cookies.get(SESSION_COOKIE) != SESSION_VALUE:
            return web.Response(text=LOGIN_PAGE, content_type="text/html")
        if form is not None:
            form.append(dict(await request.post()))
        return web.Response(text=answer, content_type="text/html")

    return handler


async def test_get_doorstation_pictures_posts_the_extension_id_and_parses_the_answer(
    logged_in: ComexioClient, comexio: FakeComexio
) -> None:
    comexio.serve("POST", LIST_PATH, _list_handler(json.dumps([BELL, MOTION])))

    pictures = await logged_in.get_doorstation_pictures(7)

    assert [p.name for p in pictures] == [MOTION, BELL]
    (received,) = comexio.received_at("POST", LIST_PATH)
    assert received.form == {"cameraId": "7"}


@pytest.mark.parametrize("answer", ["null", "", "[]"])
async def test_get_doorstation_pictures_of_a_doorstation_without_pictures(
    logged_in: ComexioClient, comexio: FakeComexio, answer: str
) -> None:
    comexio.serve("POST", LIST_PATH, _list_handler(answer))

    assert await logged_in.get_doorstation_pictures(7) == ()


async def test_get_doorstation_pictures_needs_the_admin_session(client: ComexioClient, comexio: FakeComexio) -> None:
    comexio.serve("POST", LIST_PATH, _list_handler(json.dumps([BELL])))

    with pytest.raises(ComexioAuthenticationError, match="login form"):
        await client.get_doorstation_pictures(7)

    assert len(comexio.received_at("POST", LIST_PATH)) == 1


async def test_get_doorstation_pictures_of_a_changed_format_raises(
    logged_in: ComexioClient, comexio: FakeComexio
) -> None:
    comexio.serve("POST", LIST_PATH, _list_handler('{"pictures": []}'))

    with pytest.raises(ComexioDataError, match="not an array"):
        await logged_in.get_doorstation_pictures(7)


@pytest.mark.parametrize(("ext_id", "error"), [(0, ValueError), (True, TypeError), ("7", TypeError)])
async def test_get_doorstation_pictures_refuses_a_bad_extension_id_before_any_request(
    logged_in: ComexioClient, comexio: FakeComexio, ext_id: object, error: type[Exception]
) -> None:
    comexio.serve("POST", LIST_PATH, _list_handler("null"))

    with pytest.raises(error):
        await logged_in.get_doorstation_pictures(ext_id)  # type: ignore[arg-type]

    assert comexio.received_at("POST", LIST_PATH) == []


def _file_handler(body: bytes, *, status: int = 200, content_type: str = "image/jpeg") -> Handler:
    async def handler(request: web.Request) -> web.Response:
        if request.cookies.get(SESSION_COOKIE) != SESSION_VALUE:
            return web.Response(text=LOGIN_PAGE, content_type="text/html")
        return web.Response(body=body, status=status, content_type=content_type)

    return handler


async def test_get_doorstation_picture_returns_the_jpeg(logged_in: ComexioClient, comexio: FakeComexio) -> None:
    comexio.serve("GET", FILE_PATH, _file_handler(JPEG))

    assert await logged_in.get_doorstation_picture(7, BELL) == JPEG
    assert len(comexio.received_at("GET", FILE_PATH)) == 1


async def test_get_doorstation_picture_returns_only_the_image_not_what_follows_it(
    logged_in: ComexioClient, comexio: FakeComexio
) -> None:
    comexio.serve("GET", FILE_PATH, _file_handler(JPEG + b"trailing bytes"))

    assert await logged_in.get_doorstation_picture(7, BELL) == JPEG


async def test_get_doorstation_picture_reads_a_body_that_arrives_in_pieces(
    logged_in: ComexioClient, comexio: FakeComexio
) -> None:
    async def handler(request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse(headers={"Content-Type": "image/jpeg"})
        await response.prepare(request)
        for index in range(len(JPEG)):
            await response.write(JPEG[index : index + 1])
            await asyncio.sleep(0)
        return response

    comexio.serve("GET", FILE_PATH, handler)

    assert await logged_in.get_doorstation_picture(7, BELL) == JPEG


async def test_get_doorstation_picture_asks_for_an_unencoded_answer(
    logged_in: ComexioClient, comexio: FakeComexio
) -> None:
    asked_for: list[str | None] = []

    async def handler(request: web.Request) -> web.Response:
        asked_for.append(request.headers.get("Accept-Encoding"))
        return web.Response(body=JPEG, content_type="image/jpeg")

    comexio.serve("GET", FILE_PATH, handler)

    assert await logged_in.get_doorstation_picture(7, BELL) == JPEG
    assert asked_for == ["identity"]


async def test_get_doorstation_picture_that_does_not_start_like_a_jpeg_raises(
    logged_in: ComexioClient, comexio: FakeComexio
) -> None:
    # A stream may have a boundary before its frame, a file is a JPEG from its first byte.
    comexio.serve("GET", FILE_PATH, _file_handler(b"junk" + JPEG))

    with pytest.raises(ComexioDataError, match="is no complete JPEG from its first byte"):
        await logged_in.get_doorstation_picture(7, BELL)


async def test_get_doorstation_picture_needs_the_admin_session(client: ComexioClient, comexio: FakeComexio) -> None:
    comexio.serve("GET", FILE_PATH, _file_handler(JPEG))

    with pytest.raises(ComexioAuthenticationError) as info:
        await client.get_doorstation_picture(7, BELL)

    assert str(info.value) == "Comexio served the login form, the session is not logged in (Doorstation 7 picture)"


async def test_get_doorstation_picture_of_an_expired_session_raises_authentication_error(
    logged_in: ComexioClient, comexio: FakeComexio
) -> None:
    # A login form with status 200 where the picture should be: told apart from a file that is no JPEG.
    comexio.serve("GET", FILE_PATH, _file_handler(LOGIN_PAGE.encode(), content_type="text/html"))

    with pytest.raises(ComexioAuthenticationError, match="login form"):
        await logged_in.get_doorstation_picture(7, BELL)


@pytest.mark.parametrize(
    "body",
    [b"", b"<html>no picture</html>", SOI + SOS + b"\x12\x34", b"\x00" * 64],
    ids=["empty", "html", "cut-off", "no-jpeg-at-all"],
)
async def test_get_doorstation_picture_that_is_no_complete_jpeg_raises(
    logged_in: ComexioClient, comexio: FakeComexio, body: bytes
) -> None:
    comexio.serve("GET", FILE_PATH, _file_handler(body))

    with pytest.raises(
        ComexioDataError, match="Doorstation 7 picture: the answer .* is no complete JPEG from its first byte"
    ):
        await logged_in.get_doorstation_picture(7, BELL)


async def test_get_doorstation_picture_with_a_malformed_structure_names_the_picture(
    logged_in: ComexioClient, comexio: FakeComexio
) -> None:
    # An EOI before any scan: an image without picture data (see first_jpeg_frame).
    comexio.serve("GET", FILE_PATH, _file_handler(SOI + EOI))

    with pytest.raises(ComexioDataError, match="^Doorstation 7 picture: "):
        await logged_in.get_doorstation_picture(7, BELL)


@pytest.mark.parametrize("status", [404, 403, 500])
async def test_get_doorstation_picture_of_an_error_status_raises_a_response_error(
    logged_in: ComexioClient, comexio: FakeComexio, status: int
) -> None:
    comexio.serve("GET", FILE_PATH, _file_handler(b"gone", status=status, content_type="text/plain"))

    with pytest.raises(ComexioResponseError) as info:
        await logged_in.get_doorstation_picture(7, BELL)

    assert str(info.value) == f"Doorstation 7 picture failed: HTTP {status}: gone"
    assert info.value.status == status
    assert info.value.body == "gone"


async def test_get_doorstation_picture_refuses_a_picture_larger_than_the_cap(
    logged_in: ComexioClient, comexio: FakeComexio, monkeypatch: pytest.MonkeyPatch
) -> None:
    cap = len(JPEG) - 1
    monkeypatch.setattr(client_module, "_MAX_PICTURE_BYTES", cap)
    comexio.serve("GET", FILE_PATH, _file_handler(JPEG))

    with pytest.raises(ComexioDataError) as info:
        await logged_in.get_doorstation_picture(7, BELL)

    assert str(info.value) == f"Doorstation 7 picture: the picture is larger than {cap} bytes"


async def test_a_picture_one_byte_over_the_cap_is_refused(
    logged_in: ComexioClient, comexio: FakeComexio, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(client_module, "_MAX_PICTURE_BYTES", len(JPEG))
    comexio.serve("GET", FILE_PATH, _file_handler(JPEG + b"x"))

    with pytest.raises(ComexioDataError, match="the picture is larger than"):
        await logged_in.get_doorstation_picture(7, BELL)


async def test_read_picture_never_asks_for_more_than_one_byte_over_the_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client_module, "_MAX_PICTURE_BYTES", 10)
    requested: list[int] = []

    class Content:
        async def read(self, size: int) -> bytes:
            requested.append(size)
            return b"x" * size  # a body that never ends

    resp = SimpleNamespace(headers=CIMultiDict[str](), content=Content(), content_type="image/jpeg")

    with pytest.raises(ComexioDataError, match="larger than 10 bytes"):
        await client_module._read_picture(cast("Any", resp), "picture")

    assert sum(requested) == 11
    assert max(requested) <= 11


async def test_get_doorstation_picture_of_exactly_the_cap_is_returned(
    logged_in: ComexioClient, comexio: FakeComexio, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(client_module, "_MAX_PICTURE_BYTES", len(JPEG))
    comexio.serve("GET", FILE_PATH, _file_handler(JPEG))

    assert await logged_in.get_doorstation_picture(7, BELL) == JPEG


@pytest.mark.parametrize("encoding", ["gzip", "deflate", "br"])
async def test_get_doorstation_picture_asks_for_it_unencoded_and_refuses_a_compressed_answer(
    logged_in: ComexioClient, comexio: FakeComexio, encoding: str
) -> None:
    asked_for: list[str | None] = []

    async def handler(request: web.Request) -> web.Response:
        asked_for.append(request.headers.get("Accept-Encoding"))
        return web.Response(body=gzip.compress(JPEG), headers={"Content-Encoding": encoding}, content_type="image/jpeg")

    comexio.serve("GET", FILE_PATH, handler)

    with pytest.raises(ComexioDataError) as info:
        await logged_in.get_doorstation_picture(7, BELL)

    assert str(info.value) == f"Doorstation 7 picture: the answer came {encoding!r}-encoded, not unencoded"
    assert asked_for == ["identity"]


async def test_get_doorstation_picture_that_stalls_times_out(
    logged_in: ComexioClient, comexio: FakeComexio, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(client_module, "_PICTURE_TIMEOUT_SEC", 0.05)

    async def handler(request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse(headers={"Content-Type": "image/jpeg"})
        await response.prepare(request)
        await response.write(JPEG[:3])
        await asyncio.sleep(30)
        return response

    comexio.serve("GET", FILE_PATH, handler)

    with pytest.raises(ComexioConnectionError) as info:
        await logged_in.get_doorstation_picture(7, BELL)

    assert str(info.value) == "Doorstation 7 picture: not read within 0.05 s"


async def test_get_doorstation_picture_cut_off_by_the_server_raises_a_connection_error(
    logged_in: ComexioClient, comexio: FakeComexio
) -> None:
    async def handler(request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse(headers={"Content-Type": "image/jpeg", "Content-Length": str(len(JPEG))})
        await response.prepare(request)
        await response.write(JPEG[:3])
        assert request.transport is not None
        request.transport.close()
        return response

    comexio.serve("GET", FILE_PATH, handler)

    with pytest.raises(ComexioConnectionError, match="^Doorstation 7 picture"):
        await logged_in.get_doorstation_picture(7, BELL)


@pytest.mark.parametrize("name", ["../secret.jpg", "a/b.jpg", "x.png", ""])
async def test_get_doorstation_picture_refuses_a_name_that_is_not_plain_before_any_request(
    logged_in: ComexioClient, comexio: FakeComexio, name: str
) -> None:
    requests_before = len(comexio.requests)

    with pytest.raises(ValueError, match="plain .jpg file name"):
        await logged_in.get_doorstation_picture(7, name)

    assert len(comexio.requests) == requests_before


@pytest.mark.parametrize(("ext_id", "error"), [(0, ValueError), (-1, ValueError), (True, TypeError), ("7", TypeError)])
async def test_get_doorstation_picture_refuses_a_bad_extension_id_before_any_request(
    logged_in: ComexioClient, comexio: FakeComexio, ext_id: object, error: type[Exception]
) -> None:
    requests_before = len(comexio.requests)

    with pytest.raises(error):
        await logged_in.get_doorstation_picture(ext_id, BELL)  # type: ignore[arg-type]

    assert len(comexio.requests) == requests_before
