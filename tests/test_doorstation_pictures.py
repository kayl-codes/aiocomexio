"""Doorstation pictures: the list of getPictureList, the names that become paths, and the two client reads."""

import asyncio
import gzip
import itertools
import json
import logging
from datetime import datetime
from types import SimpleNamespace
from typing import Any, cast
from urllib.parse import urlsplit

import aiohttp
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
from aiocomexio import doorstation_pictures as pictures_module
from aiocomexio.doorstation import JpegFrameReader, first_jpeg_frame
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

# A complete synthetic JPEG: SOI, the frame header of a 1 x 1 picture with one component (SOF0), one scan (SOS header
# for one component, made-up entropy-coded data), EOI.
SOI = b"\xff\xd8"
EOI = b"\xff\xd9"
SOF = b"\xff\xc0" + (11).to_bytes(2) + b"\x08\x00\x01\x00\x01\x01\x01\x11\x00"
SOS = b"\xff\xda" + (8).to_bytes(2) + b"\x01\x01\x00\x00\x3f\x00"
HTML_BODY = b"<html>no picture</html>"
JPEG = SOI + SOF + SOS + b"\x12\x34\x56" + EOI
NOT_PLAIN_MESSAGE = "name must be a plain .jpg file name (letters, digits, '.', '_', '-')"
LOGIN_FORM_MESSAGE = "Comexio served the login form, the session is not logged in ({what})"


def _not_a_jpeg_message(body: bytes, what: str = "Doorstation 7 picture") -> str:
    """The message of an answer that is no JPEG from its first byte (served as image/jpeg)."""
    return (
        f"{what}: the answer ({'image/jpeg'!r}, {len(body)} bytes, starting {body[:2].hex() or 'empty'}) "
        "is no complete JPEG from its first byte"
    )


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
    "a" * 125 + ".jpg",  # 129 characters in all, one over
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
        ("snapshot.jpg", DoorstationPictureKind.SCREENSHOT, None),
        ("SHOT1.jpg", DoorstationPictureKind.SCREENSHOT, None),
        # `shot` wins over a bell or motion token in the same name.
        ("26-11-21-12-00-00_bell_shot.jpg", DoorstationPictureKind.SCREENSHOT, datetime(2026, 11, 21, 12, 0, 0)),
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
    with pytest.raises(ValueError) as of:
        picture_of(name)
    with pytest.raises(ValueError) as file_path:
        doorstation_picture_file_path(name)
    with pytest.raises(ValueError) as proxy_path:
        doorstation_picture_path(7, name)

    assert str(of.value) == NOT_PLAIN_MESSAGE
    assert str(file_path.value) == NOT_PLAIN_MESSAGE
    assert str(proxy_path.value) == NOT_PLAIN_MESSAGE


def test_a_name_of_the_longest_plain_length_is_plain() -> None:
    longest = "a" * 124 + ".jpg"  # 128 characters in all, as the README says; one more is in NOT_PLAIN
    assert len(longest) == 128

    assert is_plain_picture_name(longest)
    assert doorstation_picture_file_path(longest) == f"/images/doorstation/{longest}"


@pytest.mark.parametrize("name", [None, 5, b"a.jpg"])
def test_a_name_that_is_no_string_is_a_type_error(name: object) -> None:
    assert not is_plain_picture_name(name)
    with pytest.raises(TypeError) as info:
        doorstation_picture_file_path(name)  # type: ignore[arg-type]

    assert str(info.value) == f"name must be a str, not {type(name).__name__}"


def test_the_paths_of_a_picture_file() -> None:
    assert doorstation_picture_file_path(BELL) == f"/images/doorstation/{BELL}"
    assert doorstation_picture_path(7, BELL) == FILE_PATH


@pytest.mark.parametrize(("ext_id", "error"), [(0, ValueError), (-1, ValueError), (True, TypeError), ("7", TypeError)])
def test_the_proxy_path_needs_a_real_extension_id(ext_id: object, error: type[Exception]) -> None:
    with pytest.raises(error):
        doorstation_picture_path(ext_id, BELL)  # type: ignore[arg-type]


@pytest.mark.parametrize("text", ["", "  ", "null", " null\n", "\t", "\r\n", "\t \r\nnull\t", "\rnull", "null\t"])
def test_an_empty_answer_is_no_picture(text: str) -> None:
    assert parse_picture_list(text) == ()


@pytest.mark.parametrize(
    ("text", "offset"),
    [
        ("\xa0null\xa0", 0),
        ("null\xa0", 4),
        ("\u2003null", 0),
        ("\x0bnull", 0),
        ("\x0cnull", 0),
        ("\xa0", 0),
        ("null\u2028", 4),
    ],
    ids=[
        "nbsp-both-sides",
        "nbsp-after",
        "em-space-before",
        "vertical-tab",
        "form-feed",
        "nbsp-only",
        "line-separator",
    ],
)
def test_unicode_whitespace_around_an_empty_answer_is_no_json_and_raises(text: str, offset: int) -> None:
    # str.strip would drop these, but JSON's whitespace is the four ASCII characters only: a malformed answer is
    # no empty list.
    with pytest.raises(ComexioDataError) as info:
        parse_picture_list(text)

    assert str(info.value) == f"Doorstation picture list is not JSON (offset {offset})"


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
    answer = json.dumps(["../a.jpg", "b/c.jpg"])

    with pytest.raises(ComexioDataError) as info:
        parse_picture_list(answer)

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


def test_an_answer_longer_than_the_cap_is_refused_before_it_is_parsed() -> None:
    cap = pictures_module.MAX_LIST_CHARS
    names = ",".join(f'"{index}.jpg"' for index in range(cap // 9))  # more than the cap, a valid list
    text = f"[{names}]"
    assert len(text) > cap

    with pytest.raises(ComexioDataError) as info:
        parse_picture_list(text)

    assert str(info.value) == f"Doorstation picture list is {len(text)} characters long, more than {cap}"


@pytest.mark.parametrize(
    "text",
    [
        # not JSON at all: the length is refused first
        pytest.param("x" * (pictures_module.MAX_LIST_CHARS + 1), id="not-json"),
        # one character over, mostly blanks a strip would drop (ids stay short: pytest puts them in an env variable)
        pytest.param("[]" + " " * (pictures_module.MAX_LIST_CHARS - 1), id="one-over-blanks"),
    ],
)
def test_the_cap_is_checked_on_the_raw_text_before_anything_else(text: str) -> None:
    with pytest.raises(ComexioDataError) as info:
        parse_picture_list(text)

    cap = pictures_module.MAX_LIST_CHARS
    assert str(info.value) == f"Doorstation picture list is {cap + 1} characters long, more than {cap}"


def test_an_answer_of_exactly_the_cap_is_still_read() -> None:
    cap = pictures_module.MAX_LIST_CHARS
    text = f'["{BELL}"]'
    padded = text + " " * (cap - len(text))
    assert len(padded) == cap

    assert [picture.name for picture in parse_picture_list(padded)] == [BELL]


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


async def test_get_doorstation_pictures_is_sent_like_the_boards_other_ajax_posts(
    logged_in: ComexioClient, comexio: FakeComexio
) -> None:
    comexio.serve("POST", LIST_PATH, _list_handler(json.dumps([BELL])))

    await logged_in.get_doorstation_pictures(7)

    (received,) = comexio.received_at("POST", LIST_PATH)
    assert received.xhr
    assert urlsplit(received.referer or "").path == client_module._ADMIN_PATH


@pytest.mark.parametrize("answer", ["null", "", "[]"])
async def test_get_doorstation_pictures_of_a_doorstation_without_pictures(
    logged_in: ComexioClient, comexio: FakeComexio, answer: str
) -> None:
    comexio.serve("POST", LIST_PATH, _list_handler(answer))

    assert await logged_in.get_doorstation_pictures(7) == ()


async def test_get_doorstation_pictures_needs_the_admin_session(client: ComexioClient, comexio: FakeComexio) -> None:
    comexio.serve("POST", LIST_PATH, _list_handler(json.dumps([BELL])))

    with pytest.raises(ComexioAuthenticationError) as info:
        await client.get_doorstation_pictures(7)

    assert str(info.value) == LOGIN_FORM_MESSAGE.format(what="Doorstation 7 picture list")

    assert len(comexio.received_at("POST", LIST_PATH)) == 1


async def test_get_doorstation_pictures_of_a_changed_format_raises(
    logged_in: ComexioClient, comexio: FakeComexio
) -> None:
    comexio.serve("POST", LIST_PATH, _list_handler('{"pictures": []}'))

    with pytest.raises(ComexioDataError) as info:
        await logged_in.get_doorstation_pictures(7)

    assert str(info.value) == "Doorstation picture list is a JSON dict, not an array"


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

    with pytest.raises(ComexioDataError) as info:
        await logged_in.get_doorstation_picture(7, BELL)

    assert str(info.value) == _not_a_jpeg_message(b"junk" + JPEG)


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

    with pytest.raises(ComexioAuthenticationError) as info:
        await logged_in.get_doorstation_picture(7, BELL)

    assert str(info.value) == LOGIN_FORM_MESSAGE.format(what="Doorstation 7 picture")


@pytest.mark.parametrize(
    "body",
    [b"", b"<html>no picture</html>", SOI + SOF + SOS + b"\x12\x34", b"\x00" * 64],
    ids=["empty", "html", "cut-off", "no-jpeg-at-all"],
)
async def test_get_doorstation_picture_that_is_no_complete_jpeg_raises(
    logged_in: ComexioClient, comexio: FakeComexio, body: bytes
) -> None:
    comexio.serve("GET", FILE_PATH, _file_handler(body))

    with pytest.raises(ComexioDataError) as info:
        await logged_in.get_doorstation_picture(7, BELL)

    assert str(info.value) == _not_a_jpeg_message(body)


@pytest.mark.parametrize(
    ("body", "starting"),
    [(b"", "empty"), (b"<html>", "3c68"), (b"\x00" * 64, "0000"), (b"junk" + JPEG, "6a75")],
    ids=["empty", "html", "zeros", "junk-before-jpeg"],
)
async def test_the_message_of_a_non_jpeg_answer_names_its_first_two_bytes(
    logged_in: ComexioClient, comexio: FakeComexio, body: bytes, starting: str
) -> None:
    comexio.serve("GET", FILE_PATH, _file_handler(body))

    with pytest.raises(ComexioDataError) as info:
        await logged_in.get_doorstation_picture(7, BELL)

    assert str(info.value) == (
        f"Doorstation 7 picture: the answer ('image/jpeg', {len(body)} bytes, starting {starting}) "
        "is no complete JPEG from its first byte"
    )


async def test_a_login_form_that_holds_the_jpeg_start_bytes_is_still_a_login_form(
    logged_in: ComexioClient, comexio: FakeComexio
) -> None:
    # The bytes ff d8 inside a foreign body must not send it to the JPEG parser: it is no picture, it is the login form.
    body = LOGIN_PAGE.encode() + SOI + SOS
    comexio.serve("GET", FILE_PATH, _file_handler(body, content_type="text/html"))

    with pytest.raises(ComexioAuthenticationError) as info:
        await logged_in.get_doorstation_picture(7, BELL)

    assert str(info.value) == LOGIN_FORM_MESSAGE.format(what="Doorstation 7 picture")


async def test_get_doorstation_picture_with_a_malformed_structure_names_the_picture(
    logged_in: ComexioClient, comexio: FakeComexio
) -> None:
    # An EOI before any scan: an image without picture data (see first_jpeg_frame).
    comexio.serve("GET", FILE_PATH, _file_handler(SOI + EOI))

    with pytest.raises(ComexioDataError) as info:
        await logged_in.get_doorstation_picture(7, BELL)

    assert str(info.value) == "Doorstation 7 picture: Camera frame ends at offset 3 before its first scan"


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

    with pytest.raises(ComexioDataError) as info:
        await logged_in.get_doorstation_picture(7, BELL)

    assert str(info.value) == f"Doorstation 7 picture: the picture is larger than {len(JPEG)} bytes"


async def test_read_picture_never_asks_for_more_than_one_byte_over_the_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client_module, "_MAX_PICTURE_BYTES", 10)
    requested: list[int] = []

    class Content:
        async def read(self, size: int) -> bytes:
            requested.append(size)
            return b"x" * min(size, 4)  # a body that never ends, handed over in pieces of four bytes

    resp = cast("Any", SimpleNamespace(headers=CIMultiDict[str](), content=Content(), content_type="image/jpeg"))

    with pytest.raises(ComexioDataError) as info:
        await client_module._read_picture(resp, "picture")

    assert str(info.value) == "picture: the picture is larger than 10 bytes"

    # Every read asks only for what is left up to one byte over the cap: 11, then 11 - 4, then 11 - 8.
    assert requested == [11, 7, 3]


async def test_read_picture_puts_a_body_together_that_arrives_one_byte_at_a_time() -> None:
    position = 0

    class Content:
        async def read(self, size: int) -> bytes:
            nonlocal position
            piece = JPEG[position : position + min(size, 1)]
            position += len(piece)
            return piece

    resp = SimpleNamespace(headers=CIMultiDict[str](), content=Content(), content_type="image/jpeg")

    assert await client_module._read_picture(cast("Any", resp), "picture") == JPEG


async def test_read_picture_shortens_a_long_content_type_in_its_message() -> None:
    class Content:
        async def read(self, size: int) -> bytes:  # noqa: ARG002
            return b""

    resp = cast("Any", SimpleNamespace(headers=CIMultiDict[str](), content=Content(), content_type="text/" + "x" * 500))

    with pytest.raises(ComexioDataError) as info:
        await client_module._read_picture(resp, "picture")

    shown = "'text/" + "x" * 194 + "..."  # the repr cut to 200 characters, then the dots
    assert str(info.value) == (
        f"picture: the answer ({shown}, 0 bytes, starting empty) is no complete JPEG from its first byte"
    )


def _buffers_in_traceback(err: BaseException | None) -> list[bytes | bytearray]:
    """Every bytes or bytearray a local of a library frame holds, on the way of the exception, its cause, its context.

    Only frames of aiocomexio count (the test's own frame holds the body it passed in). Immutable bytes count as well
    as the buffer: a chunk of the response that a frame keeps is picture data kept alive just the same.
    """
    found: list[bytes | bytearray] = []
    seen: set[int] = set()
    while err is not None and id(err) not in seen:
        seen.add(id(err))
        frame = err.__traceback__
        while frame is not None:
            if str(frame.tb_frame.f_globals.get("__name__", "")).startswith("aiocomexio."):
                found += [value for value in frame.tb_frame.f_locals.values() if isinstance(value, bytes | bytearray)]
            frame = frame.tb_next
        found += _buffers_in_traceback(err.__cause__) + _buffers_in_traceback(err.__context__)
        err = None
    return found


class _PieceContent:
    """A response body of one piece (or, with `then`, one piece and then an exception), handed over by size."""

    def __init__(self, body: bytes, then: BaseException | None = None) -> None:
        self.rest = body
        self.then = then

    async def read(self, size: int) -> bytes:
        if not self.rest and self.then is not None:
            raise self.then
        piece, self.rest = self.rest[:size], self.rest[size:]
        return piece


def _picture_response(content: _PieceContent) -> Any:
    return cast("Any", SimpleNamespace(headers=CIMultiDict[str](), content=content, content_type="image/jpeg"))


@pytest.mark.parametrize(
    ("body", "cap", "error", "message"),
    [
        (JPEG, 10, ComexioDataError, "picture: the picture is larger than 10 bytes"),  # the cap passed
        (JPEG[:-1], 4_000_000, ComexioDataError, _not_a_jpeg_message(JPEG[:-1], "picture")),  # a cut picture
        (HTML_BODY, 4_000_000, ComexioDataError, _not_a_jpeg_message(HTML_BODY, "picture")),
        # not only the data error
        (LOGIN_PAGE.encode(), 4_000_000, ComexioAuthenticationError, LOGIN_FORM_MESSAGE.format(what="picture")),
    ],
    ids=["over-the-cap", "cut-off", "html", "login-form"],
)
async def test_a_failed_picture_read_leaves_no_picture_bytes_in_its_traceback(
    monkeypatch: pytest.MonkeyPatch, body: bytes, cap: int, error: type[Exception], message: str
) -> None:
    monkeypatch.setattr(client_module, "_MAX_PICTURE_BYTES", cap)
    resp = _picture_response(_PieceContent(body))

    with pytest.raises(error) as info:
        await client_module._read_picture(resp, "picture")

    assert str(info.value) == message
    held = _buffers_in_traceback(info.value)
    assert held  # the frames do hold the buffer (else this proves nothing) ...
    assert not any(held)  # ... and it is empty by now


async def test_a_picture_the_parser_refuses_leaves_no_bytes_in_the_chained_frames() -> None:
    body = SOI + b"\x00\x00\x00\x00"  # a JPEG start, then no marker where the structure needs one
    resp = _picture_response(_PieceContent(body))

    with pytest.raises(ComexioDataError) as info:
        await client_module._read_picture(resp, "picture")

    assert info.value.__cause__ is not None  # the parser's own error, chained
    held = _buffers_in_traceback(info.value.__cause__)
    assert held
    assert not any(held)


@pytest.mark.parametrize("failure", [asyncio.CancelledError(), aiohttp.ClientPayloadError("cut")])
async def test_a_read_that_breaks_off_half_way_leaves_no_picture_bytes_in_its_traceback(
    failure: BaseException,
) -> None:
    resp = _picture_response(_PieceContent(JPEG[:5], then=failure))

    with pytest.raises(type(failure)) as info:
        await client_module._read_picture(resp, "picture")

    held = _buffers_in_traceback(info.value)
    assert held
    assert not any(held)


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

    with pytest.raises(ComexioConnectionError) as info:
        await logged_in.get_doorstation_picture(7, BELL)

    cause = info.value.__cause__
    assert isinstance(cause, aiohttp.ClientError)
    assert str(info.value) == f"Doorstation 7 picture failed: {client_module._transport_error_text(cause)}"


@pytest.mark.parametrize("name", ["../secret.jpg", "a/b.jpg", "x.png", ""])
async def test_get_doorstation_picture_refuses_a_name_that_is_not_plain_before_any_request(
    logged_in: ComexioClient, comexio: FakeComexio, name: str
) -> None:
    requests_before = len(comexio.requests)

    with pytest.raises(ValueError) as info:
        await logged_in.get_doorstation_picture(7, name)

    assert str(info.value) == NOT_PLAIN_MESSAGE
    assert len(comexio.requests) == requests_before


@pytest.mark.parametrize(("ext_id", "error"), [(0, ValueError), (-1, ValueError), (True, TypeError), ("7", TypeError)])
async def test_get_doorstation_picture_refuses_a_bad_extension_id_before_any_request(
    logged_in: ComexioClient, comexio: FakeComexio, ext_id: object, error: type[Exception]
) -> None:
    requests_before = len(comexio.requests)

    with pytest.raises(error):
        await logged_in.get_doorstation_picture(ext_id, BELL)  # type: ignore[arg-type]

    assert len(comexio.requests) == requests_before


# --- the frame header: a scan with no start-of-frame marker before it is no picture a decoder can read ---------------

_SOF_CODES = [0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF]
_NOT_SOF_CODES = {0xC4: "dht", 0xC8: "jpg", 0xCC: "dac"}
_APP0 = b"\xff\xe0" + (4).to_bytes(2) + b"JF"
_SCAN = b"\x12\x34\x56"
_PROGRESSIVE_CODES = {0xC2, 0xC6, 0xCA, 0xCE}
_LOSSLESS_CODES = {0xC3, 0xC7, 0xCB, 0xCF}


def _scan_for(code: int) -> bytes:
    """An SOS header of one component with parameters that fit the process of the SOF marker code."""
    if code in _LOSSLESS_CODES:
        parameters = b"\x01\x00\x00"  # predictor 1, no point transform
    elif code in _PROGRESSIVE_CODES:
        parameters = b"\x00\x00\x00"  # the DC scan
    else:
        parameters = b"\x00\x3f\x00"  # the whole block
    return (
        b"\xff\xda" + (8).to_bytes(2) + b"\x01\x01\x00" + parameters
    )  # Ns, component 1, table selectors, then the rest


def _segment(code: int, payload: bytes = b"\x00") -> bytes:
    return bytes((0xFF, code)) + (len(payload) + 2).to_bytes(2) + payload


@pytest.mark.parametrize(
    "head",
    [SOI, SOI + _APP0],
    ids=["scan-right-after-soi", "scan-after-another-segment"],
)
async def test_get_doorstation_picture_with_a_scan_before_any_frame_header_raises(
    logged_in: ComexioClient, comexio: FakeComexio, head: bytes
) -> None:
    # Every marker is where the structure wants it, but there is no SOF, no size to decode to.
    comexio.serve("GET", FILE_PATH, _file_handler(head + SOS + _SCAN + EOI))

    with pytest.raises(ComexioDataError) as info:
        await logged_in.get_doorstation_picture(7, BELL)

    offset = len(head) + 1  # the offset names the code byte of the marker, one after its 0xFF
    assert (
        str(info.value) == f"Doorstation 7 picture: Camera frame has a scan at offset {offset} before its frame header"
    )


def test_a_stream_frame_is_still_read_without_a_frame_header() -> None:
    frame = SOI + _APP0 + SOS + _SCAN + EOI

    assert first_jpeg_frame(frame) == frame
    assert JpegFrameReader().frame(bytearray(frame)) == frame


def test_a_frame_header_is_required_only_when_asked_for() -> None:
    with pytest.raises(ComexioDataError) as info:
        first_jpeg_frame(SOI + _APP0 + SOS, require_frame_header=True)

    offset = len(SOI + _APP0) + 1  # the offset names the code byte of the marker, one after its 0xFF
    assert str(info.value) == f"Camera frame has a scan at offset {offset} before its frame header"


def test_a_half_received_scan_marker_with_no_frame_header_raises_at_once() -> None:
    reader = JpegFrameReader(require_frame_header=True)
    buffer = bytearray(SOI + SOS[:2])

    with pytest.raises(ComexioDataError) as info:
        reader.frame(buffer)

    assert str(info.value) == f"Camera frame has a scan at offset {len(SOI) + 1} before its frame header"


@pytest.mark.parametrize("code", _SOF_CODES, ids=[f"sof-{code:02x}" for code in _SOF_CODES])
def test_every_start_of_frame_marker_is_a_frame_header(code: int) -> None:
    frame = SOI + _APP0 + _segment(code, SOF[4:]) + _scan_for(code) + _SCAN + EOI

    assert first_jpeg_frame(frame, require_frame_header=True) == frame


@pytest.mark.parametrize("code", list(_NOT_SOF_CODES), ids=list(_NOT_SOF_CODES.values()))
def test_a_marker_between_the_sof_codes_that_is_no_frame_header_does_not_count(code: int) -> None:
    head = SOI + _segment(code)
    with pytest.raises(ComexioDataError) as info:
        first_jpeg_frame(head + SOS, require_frame_header=True)

    offset = len(head) + 1  # the offset names the code byte of the marker, one after its 0xFF
    assert str(info.value) == f"Camera frame has a scan at offset {offset} before its frame header"


@pytest.mark.parametrize(
    "neighbour",
    [_segment(0xBF), b"\xff\xd0"],
    ids=["just-below-the-sof-range", "restart-marker-just-above"],
)
def test_the_markers_next_to_the_sof_range_are_no_frame_header_either(neighbour: bytes) -> None:
    head = SOI + neighbour
    with pytest.raises(ComexioDataError) as info:
        first_jpeg_frame(head + SOS, require_frame_header=True)

    offset = len(head) + 1  # the offset names the code byte of the marker, one after its 0xFF
    assert str(info.value) == f"Camera frame has a scan at offset {offset} before its frame header"


def test_a_second_scan_needs_no_second_frame_header() -> None:
    frame = SOI + SOF + SOS + b"\x12\x34" + SOS + b"\x56\x78" + EOI

    assert first_jpeg_frame(frame, require_frame_header=True) == frame


@pytest.mark.parametrize("cut", range(1, len(SOF)), ids=[f"cut-after-{cut}-bytes" for cut in range(1, len(SOF))])
def test_a_frame_header_that_arrives_in_pieces_still_counts(cut: int) -> None:
    buffer = bytearray(SOI + SOF[:cut])
    reader = JpegFrameReader(require_frame_header=True)
    assert reader.frame(buffer) is None  # the SOF segment is not complete, the SOS has not come

    buffer += SOF[cut:] + SOS + _SCAN + EOI

    assert reader.frame(buffer) == JPEG


def _frame_header(
    *,
    code: int = 0xC0,
    precision: int = 8,
    height: int = 1,
    width: int = 1,
    components: tuple[bytes, ...] = (b"\x01\x11\x00",),
) -> bytes:
    """An SOF segment (SOF0 unless code says another); the component count and length follow the components."""
    payload = (
        bytes((precision,)) + height.to_bytes(2) + width.to_bytes(2) + bytes((len(components),)) + b"".join(components)
    )
    return _segment(code, payload)


def _picture_with(frame_header: bytes) -> bytes:
    return SOI + frame_header + _scan_for(frame_header[1]) + _SCAN + EOI


def test_the_synthetic_frame_header_is_the_one_the_tests_build() -> None:
    assert _frame_header() == SOF


@pytest.mark.parametrize(
    "frame_header",
    [
        _frame_header(height=65535, width=65535),
        _frame_header(components=(b"\x01\x22\x00", b"\x02\x11\x01", b"\x03\x11\x01")),
        _frame_header(components=(b"\x01\x44\x03",)),
        _frame_header(height=256, width=256),
        _frame_header(components=(b"\x01\x11\x00", b"\x02\x11\x00", b"\x03\x11\x00", b"\x04\x11\x00")),
        _frame_header(components=tuple(bytes((at, 0x11, 0)) for at in range(1, 256))),
    ],
    ids=[
        "largest-size",
        "three-components-subsampled",
        "largest-factors-and-table",
        "size-with-a-zero-low-byte",
        "four-components",
        "most-components-the-count-byte-holds",
    ],
)
def test_a_sound_frame_header_is_accepted(frame_header: bytes) -> None:
    picture = _picture_with(frame_header)

    assert first_jpeg_frame(picture, require_frame_header=True) == picture


_MALFORMED_FRAME_HEADERS = {
    "no-components": _segment(0xC0, b"\x08\x00\x01\x00\x01\x00"),
    "component-missing": _segment(0xC0, b"\x08\x00\x01\x00\x01\x02\x01\x11\x00"),
    "too-short-for-the-fixed-part": _segment(0xC0, b"\x08\x00\x01\x00\x01"),
    "longer-than-its-components": _segment(0xC0, b"\x08\x00\x01\x00\x01\x01\x01\x11\x00\x00"),
    "precision-zero": _frame_header(precision=0),
    "precision-too-large": _frame_header(precision=17),
    "height-zero": _frame_header(height=0),
    "width-zero": _frame_header(width=0),
    "horizontal-factor-zero": _frame_header(components=(b"\x01\x01\x00",)),
    "vertical-factor-zero": _frame_header(components=(b"\x01\x10\x00",)),
    "horizontal-factor-too-large": _frame_header(components=(b"\x01\x51\x00",)),
    "vertical-factor-too-large": _frame_header(components=(b"\x01\x15\x00",)),
    "quantization-table-too-large": _frame_header(components=(b"\x01\x11\x04",)),
    "second-component-bad": _frame_header(components=(b"\x01\x11\x00", b"\x02\x00\x00")),
    "last-component-bad": _frame_header(components=(b"\x01\x11\x00", b"\x02\x11\x00", b"\x03\x11\x04")),
    "component-id-repeated": _frame_header(components=(b"\x01\x11\x00", b"\x02\x11\x00", b"\x01\x11\x00")),
    "component-id-repeated-next-to-it": _frame_header(components=(b"\x01\x11\x00", b"\x01\x11\x00", b"\x02\x11\x00")),
    "component-id-repeated-at-the-end": _frame_header(components=(b"\x01\x11\x00", b"\x02\x11\x00", b"\x02\x11\x00")),
    "baseline-12-bit": _frame_header(precision=12),
    "baseline-16-bit": _frame_header(precision=16),
    "baseline-1-bit": _frame_header(precision=1),
}


@pytest.mark.parametrize("frame_header", list(_MALFORMED_FRAME_HEADERS.values()), ids=list(_MALFORMED_FRAME_HEADERS))
def test_a_malformed_frame_header_is_refused(frame_header: bytes) -> None:
    picture = _picture_with(frame_header)

    with pytest.raises(ComexioDataError) as info:
        first_jpeg_frame(picture, require_frame_header=True)

    length = len(frame_header) - 2  # the length field, which counts itself but not the marker
    assert str(info.value) == f"Camera frame has a malformed frame header (length {length}) at offset {len(SOI) + 1}"


@pytest.mark.parametrize("code", _SOF_CODES, ids=[f"sof-{code:02x}" for code in _SOF_CODES])
def test_a_malformed_frame_header_is_refused_for_every_start_of_frame_marker(code: int) -> None:
    frame_header = _segment(code, _frame_header(height=0)[4:])
    picture = _picture_with(frame_header)

    with pytest.raises(ComexioDataError) as info:
        first_jpeg_frame(picture, require_frame_header=True)

    assert str(info.value) == f"Camera frame has a malformed frame header (length 11) at offset {len(SOI) + 1}"


@pytest.mark.parametrize(
    "frame_header",
    [
        _frame_header(height=0),
        _frame_header(components=(b"\x01\x11\x00", b"\x01\x11\x00")),
        _frame_header(precision=12),
    ],
    ids=["height-zero", "component-id-repeated", "baseline-12-bit"],
)
def test_a_stream_frame_with_a_malformed_frame_header_is_still_read(frame_header: bytes) -> None:
    picture = _picture_with(frame_header)

    assert first_jpeg_frame(picture) == picture


@pytest.mark.parametrize("cut", range(1, len(SOF)), ids=[f"cut-after-{cut}-bytes" for cut in range(1, len(SOF))])
def test_a_malformed_frame_header_that_arrives_in_pieces_is_refused_once_whole(cut: int) -> None:
    header = _frame_header(width=0)
    reader = JpegFrameReader(require_frame_header=True)
    buffer = bytearray(SOI + header[:cut])
    assert reader.frame(buffer) is None  # not whole yet, so not judged yet

    buffer += header[cut:] + SOS + _SCAN + EOI

    with pytest.raises(ComexioDataError) as info:
        reader.frame(buffer)

    assert str(info.value) == f"Camera frame has a malformed frame header (length 11) at offset {len(SOI) + 1}"


async def test_get_doorstation_picture_with_a_malformed_frame_header_raises(
    logged_in: ComexioClient, comexio: FakeComexio
) -> None:
    comexio.serve("GET", FILE_PATH, _file_handler(_picture_with(_frame_header(height=0))))

    with pytest.raises(ComexioDataError) as info:
        await logged_in.get_doorstation_picture(7, BELL)

    assert str(info.value) == (
        f"Doorstation 7 picture: Camera frame has a malformed frame header (length 11) at offset {len(SOI) + 1}"
    )


# T.81, Table B.2, written out marker by marker: SOF0 baseline DCT 8 bits; SOF1 extended, SOF2 progressive, SOF5 and
# SOF6 differential, SOF9, SOF10, SOF13 and SOF14 arithmetic-coded DCT 8 or 12; SOF3, SOF7, SOF11 and SOF15 lossless
# 2 to 16.
_ALLOWED_PRECISIONS: dict[int, set[int]] = {
    0xC0: {8},
    0xC1: {8, 12},
    0xC2: {8, 12},
    0xC3: {2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16},
    0xC5: {8, 12},
    0xC6: {8, 12},
    0xC7: {2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16},
    0xC9: {8, 12},
    0xCA: {8, 12},
    0xCB: {2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16},
    0xCD: {8, 12},
    0xCE: {8, 12},
    0xCF: {2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16},
}
_PRECISION_CASES = [(code, precision) for code in _SOF_CODES for precision in range(18)]


@pytest.mark.parametrize(
    ("code", "precision"), _PRECISION_CASES, ids=[f"sof-{code:02x}-{bits}-bit" for code, bits in _PRECISION_CASES]
)
def test_the_sample_precision_follows_the_process_of_the_marker(code: int, precision: int) -> None:
    picture = _picture_with(_frame_header(code=code, precision=precision))

    if precision in _ALLOWED_PRECISIONS[code]:
        assert first_jpeg_frame(picture, require_frame_header=True) == picture
    else:
        with pytest.raises(ComexioDataError) as info:
            first_jpeg_frame(picture, require_frame_header=True)
        assert str(info.value) == f"Camera frame has a malformed frame header (length 11) at offset {len(SOI) + 1}"


def test_the_precision_table_names_every_start_of_frame_marker() -> None:
    assert sorted(_ALLOWED_PRECISIONS) == sorted(_SOF_CODES)


# --- the scan names components of its frame: an id the frame header lacks is no picture a decoder can read ----------

_THREE_COMPONENTS = _frame_header(components=(b"\x01\x11\x00", b"\x02\x11\x00", b"\x03\x11\x00"))


def _scan_header(*selectors: int, parameters: bytes = b"\x00\x3f\x00", tables: int = 0) -> bytes:
    """An SOS segment whose scan names the given components; parameters are Ss, Se and Ah/Al, tables the Td/Ta byte."""
    names = b"".join(bytes((selector, tables)) for selector in selectors)
    return _segment(0xDA, bytes((len(selectors),)) + names + parameters)


def _scan_mismatch_message(frame_header: bytes) -> str:
    offset = len(SOI) + len(frame_header) + 1  # the code byte of the SOS marker, one after its 0xFF
    return f"Camera frame has a scan whose components do not fit its frame at offset {offset}"


@pytest.mark.parametrize(
    "selectors",
    [(1,), (2,), (3,), (1, 2, 3), (2, 3), (3, 1)],
    ids=["first", "second", "third", "all", "two-of-three", "frame-order-not-required"],
)
def test_a_scan_of_components_the_frame_has_is_accepted(selectors: tuple[int, ...]) -> None:
    picture = SOI + _THREE_COMPONENTS + _scan_header(*selectors) + _SCAN + EOI

    assert first_jpeg_frame(picture, require_frame_header=True) == picture


@pytest.mark.parametrize(
    "selectors",
    [(4,), (0,), (1, 4), (2, 2), (1, 2, 1)],
    ids=["id-the-frame-lacks", "id-zero", "one-of-two-lacks", "same-twice", "first-again"],
)
def test_a_scan_that_names_a_component_the_frame_lacks_or_names_one_twice_is_refused(
    selectors: tuple[int, ...],
) -> None:
    picture = SOI + _THREE_COMPONENTS + _scan_header(*selectors) + _SCAN + EOI

    with pytest.raises(ComexioDataError) as info:
        first_jpeg_frame(picture, require_frame_header=True)

    assert str(info.value) == _scan_mismatch_message(_THREE_COMPONENTS)


def test_the_second_scan_of_a_frame_is_checked_against_its_frame_too() -> None:
    first_scan = _scan_header(1) + _SCAN
    picture = SOI + _THREE_COMPONENTS + first_scan + _scan_header(2, 7) + _SCAN + EOI

    with pytest.raises(ComexioDataError) as info:
        first_jpeg_frame(picture, require_frame_header=True)

    assert str(info.value) == _scan_mismatch_message(_THREE_COMPONENTS + first_scan)


def test_a_stream_frame_is_read_whatever_components_its_scan_names() -> None:
    picture = SOI + _THREE_COMPONENTS + _scan_header(9, 9) + _SCAN + EOI

    assert first_jpeg_frame(picture) == picture


_SPLIT_SCAN_HEADER = _scan_header(2, 3)


@pytest.mark.parametrize(
    "cut",
    range(1, len(_SPLIT_SCAN_HEADER)),
    ids=[f"cut-after-{cut}-bytes" for cut in range(1, len(_SPLIT_SCAN_HEADER))],
)
def test_a_scan_header_that_arrives_in_pieces_is_taken_once_whole(cut: int) -> None:
    header = _SPLIT_SCAN_HEADER
    reader = JpegFrameReader(require_frame_header=True)
    buffer = bytearray(SOI + _THREE_COMPONENTS + header[:cut])
    assert reader.frame(buffer) is None  # the segment is not whole yet: not judged yet

    buffer += header[cut:] + _SCAN + EOI

    assert reader.frame(buffer) == bytes(buffer)


# --- the scan parameters Ss, Se and Ah/Al fit the process of the frame (T.81, Table B.3, G.1.1.1.1, H.1.2.1) --------

_SEQUENTIAL_CODES = [0xC0, 0xC1, 0xC5, 0xC9, 0xCD]
# (Ss, Se, Ah/Al byte) -> accepted. A sequential scan holds the whole block, a progressive one a DC scan or an AC
# band of one component with successive approximation, a lossless one a predictor 0 to 7 and a point transform.
_SEQUENTIAL_SCANS = {
    (0, 63, 0x00): True,
    (1, 63, 0x00): False,
    (0, 62, 0x00): False,
    (0, 64, 0x00): False,
    (0, 0, 0x00): False,
    (0, 63, 0x01): False,
    (0, 63, 0x10): False,
}
_PROGRESSIVE_SCANS = {
    (0, 0, 0x00): True,  # DC, first scan
    (0, 0, 0x01): True,  # DC with a point transform
    (0, 0, 0x10): True,  # DC refinement of one bit
    (0, 0, 0x21): True,  # DC refinement from bit 2 to bit 1
    (0, 0, 0xDC): True,  # from bit 13 to bit 12: the largest shifts
    (0, 0, 0x0D): True,  # a first scan with the largest point transform
    (1, 5, 0xDC): True,
    (0, 0, 0x11): False,  # a refinement of the bit it already holds
    (0, 0, 0x12): False,  # a refinement from below the last bit
    (1, 5, 0x11): False,
    (1, 63, 0x00): True,  # the AC band of one component
    (1, 5, 0x21): True,
    (63, 63, 0x00): True,  # the last coefficient alone
    (0, 63, 0x00): False,  # a scan from the DC on is the DC scan only
    (0, 1, 0x00): False,
    (1, 0, 0x00): False,  # Ss after Se
    (5, 4, 0x00): False,
    (1, 64, 0x00): False,
    (0, 0, 0x20): False,  # a refinement continues from the bit above the last one
    (0, 0, 0x0E): False,  # a shift of 14
    (0, 0, 0xED): False,  # from bit 14 to bit 13: right order, shift too large
    (0, 0, 0x1F): False,  # the low shift 15
}
_LOSSLESS_SCANS = {
    (1, 0, 0x00): True,
    (0, 0, 0x00): True,  # no prediction (differential frames)
    (7, 0, 0x00): True,
    (7, 0, 0x0F): True,  # the largest point transform
    (8, 0, 0x00): False,
    (1, 1, 0x00): False,
    (1, 63, 0x00): False,
    (1, 0, 0x10): False,
}
_PARAMETER_CASES = [
    (code, params, accepted)
    for codes, scans in (
        (_SEQUENTIAL_CODES, _SEQUENTIAL_SCANS),
        (sorted(_PROGRESSIVE_CODES), _PROGRESSIVE_SCANS),
        (sorted(_LOSSLESS_CODES), _LOSSLESS_SCANS),
    )
    for code in codes
    for params, accepted in scans.items()
]


_PROGRESSIVE_THREE = _frame_header(code=0xC2, components=(b"\x01\x11\x00", b"\x02\x11\x00", b"\x03\x11\x00"))


def _scan_parameters_message(frame_header: bytes) -> str:
    offset = len(SOI) + len(frame_header) + 1  # the code byte of the SOS marker, one after its 0xFF
    return f"Camera frame has a scan with parameters its frame cannot have at offset {offset}"


@pytest.mark.parametrize(
    ("code", "params", "accepted"),
    _PARAMETER_CASES,
    ids=[f"sof-{code:02x}-ss{ss}-se{se}-a{approx:02x}" for code, (ss, se, approx), _ in _PARAMETER_CASES],
)
def test_the_scan_parameters_follow_the_process_of_the_frame(
    code: int, params: tuple[int, int, int], accepted: bool
) -> None:
    frame_header = _frame_header(code=code)
    picture = SOI + frame_header + _scan_header(1, parameters=bytes(params)) + _SCAN + EOI

    if accepted:
        assert first_jpeg_frame(picture, require_frame_header=True) == picture
    else:
        with pytest.raises(ComexioDataError) as info:
            first_jpeg_frame(picture, require_frame_header=True)
        assert str(info.value) == _scan_parameters_message(frame_header)


def test_the_precision_and_parameter_tables_name_every_start_of_frame_marker() -> None:
    assert sorted({*_SEQUENTIAL_CODES, *_PROGRESSIVE_CODES, *_LOSSLESS_CODES}) == sorted(_SOF_CODES)


@pytest.mark.parametrize(
    ("parameters", "accepted"),
    [(b"\x00\x00\x00", True), (b"\x01\x3f\x00", False), (b"\x01\x05\x00", False)],
    ids=["dc-scan-of-three-components", "ac-band-of-three-components", "short-ac-band-of-three-components"],
)
def test_an_ac_scan_of_a_progressive_frame_holds_one_component(parameters: bytes, accepted: bool) -> None:
    picture = SOI + _PROGRESSIVE_THREE + _scan_header(1, 2, 3, parameters=parameters) + _SCAN + EOI

    if accepted:
        assert first_jpeg_frame(picture, require_frame_header=True) == picture
    else:
        with pytest.raises(ComexioDataError) as info:
            first_jpeg_frame(picture, require_frame_header=True)
        assert str(info.value) == _scan_parameters_message(_PROGRESSIVE_THREE)


_VALID_PARAMETERS = {0xC0: b"\x00\x3f\x00", 0xC2: b"\x00\x00\x00", 0xC3: b"\x01\x00\x00"}


@pytest.mark.parametrize("tables", [0x00, 0x11, 0x33, 0xFF], ids=["zero", "one-one", "three-three", "all-ones"])
@pytest.mark.parametrize("code", list(_VALID_PARAMETERS), ids=[f"sof-{code:02x}" for code in _VALID_PARAMETERS])
def test_the_table_selectors_of_a_scan_are_not_judged(code: int, tables: int) -> None:
    scan = _scan_header(1, parameters=_VALID_PARAMETERS[code], tables=tables)
    picture = SOI + _frame_header(code=code) + scan + _SCAN + EOI

    assert first_jpeg_frame(picture, require_frame_header=True) == picture


def test_the_table_selectors_of_a_scan_of_three_components_are_not_judged() -> None:
    picture = SOI + _THREE_COMPONENTS + _scan_header(1, 2, 3, tables=0xFF) + _SCAN + EOI

    assert first_jpeg_frame(picture, require_frame_header=True) == picture


def test_the_second_scan_of_a_frame_has_its_parameters_checked_too() -> None:
    frame_header = _frame_header(code=0xC2)
    first_scan = _scan_header(1, parameters=b"\x00\x00\x00") + _SCAN
    accepted = SOI + frame_header + first_scan + _scan_header(1, parameters=b"\x01\x3f\x00") + _SCAN + EOI
    refused = SOI + frame_header + first_scan + _scan_header(1, parameters=b"\x00\x3f\x00") + _SCAN + EOI

    assert first_jpeg_frame(accepted, require_frame_header=True) == accepted
    with pytest.raises(ComexioDataError) as info:
        first_jpeg_frame(refused, require_frame_header=True)

    assert str(info.value) == _scan_parameters_message(frame_header + first_scan)


def test_a_later_frame_header_sets_the_process_the_next_scan_is_checked_against() -> None:
    sequential = _frame_header()
    progressive = _frame_header(code=0xC2)
    first_scan = _scan_header(1) + _SCAN
    ac_scan = _scan_header(1, parameters=b"\x01\x3f\x00") + _SCAN
    accepted = SOI + sequential + first_scan + progressive + ac_scan + EOI
    refused = SOI + progressive + _scan_header(1, parameters=b"\x00\x00\x00") + _SCAN + sequential + ac_scan + EOI

    assert first_jpeg_frame(accepted, require_frame_header=True) == accepted
    with pytest.raises(ComexioDataError) as info:
        first_jpeg_frame(refused, require_frame_header=True)

    prefix = progressive + _scan_header(1, parameters=b"\x00\x00\x00") + _SCAN + sequential
    assert str(info.value) == _scan_parameters_message(prefix)


@pytest.mark.parametrize(
    "cut",
    range(1, len(_scan_header(1, parameters=b"\x01\x3f\x00"))),
    ids=[f"cut-after-{cut}-bytes" for cut in range(1, len(_scan_header(1, parameters=b"\x01\x3f\x00")))],
)
def test_a_scan_header_with_bad_parameters_that_arrives_in_pieces_is_refused_once_whole(cut: int) -> None:
    header = _scan_header(1, parameters=b"\x01\x3f\x00")
    reader = JpegFrameReader(require_frame_header=True)
    buffer = bytearray(SOI + _frame_header() + header[:cut])
    assert reader.frame(buffer) is None  # the segment is not whole yet: not judged yet

    buffer += header[cut:] + _SCAN + EOI

    with pytest.raises(ComexioDataError) as info:
        reader.frame(buffer)

    assert str(info.value) == _scan_parameters_message(_frame_header())


def test_a_scan_that_fails_both_checks_is_reported_as_the_component_mismatch() -> None:
    picture = SOI + _frame_header() + _scan_header(9, parameters=b"\x01\x00\x00") + _SCAN + EOI

    with pytest.raises(ComexioDataError) as info:
        first_jpeg_frame(picture, require_frame_header=True)

    assert str(info.value) == _scan_mismatch_message(_frame_header())


def test_a_progressive_picture_of_several_scans_is_accepted() -> None:
    scans = [
        _scan_header(1, 2, 3, parameters=b"\x00\x00\x01"),  # DC of all components, one bit short
        _scan_header(1, parameters=b"\x01\x05\x01"),  # AC bands
        _scan_header(1, parameters=b"\x06\x3f\x01"),
        _scan_header(1, parameters=b"\x01\x3f\x10"),  # AC refinement
        _scan_header(1, 2, 3, parameters=b"\x00\x00\x10"),  # DC refinement
    ]
    picture = SOI + _PROGRESSIVE_THREE + b"".join(scan + _SCAN for scan in scans) + EOI

    assert first_jpeg_frame(picture, require_frame_header=True) == picture


def test_a_stream_frame_is_read_whatever_scan_parameters_it_holds() -> None:
    picture = SOI + _frame_header(code=0xC2) + _scan_header(1, parameters=b"\x40\x3f\xff") + _SCAN + EOI

    assert first_jpeg_frame(picture) == picture


@pytest.mark.parametrize(
    "cut",
    range(1, len(_SPLIT_SCAN_HEADER)),
    ids=[f"cut-after-{cut}-bytes" for cut in range(1, len(_SPLIT_SCAN_HEADER))],
)
def test_a_scan_header_that_arrives_in_pieces_is_refused_once_whole(cut: int) -> None:
    header = _scan_header(2, 9)
    reader = JpegFrameReader(require_frame_header=True)
    buffer = bytearray(SOI + _THREE_COMPONENTS + header[:cut])
    assert reader.frame(buffer) is None  # the segment is not whole yet: not judged yet

    buffer += header[cut:] + _SCAN + EOI

    with pytest.raises(ComexioDataError) as info:
        reader.frame(buffer)

    assert str(info.value) == _scan_mismatch_message(_THREE_COMPONENTS)


def test_a_second_frame_header_gives_the_scans_after_it_its_own_components() -> None:
    second_frame = _frame_header(components=(b"\x05\x11\x00", b"\x06\x11\x00"))
    picture = SOI + _THREE_COMPONENTS + _scan_header(1) + _SCAN + second_frame + _scan_header(5) + _SCAN + EOI

    assert first_jpeg_frame(picture, require_frame_header=True) == picture


def test_a_component_id_of_the_first_frame_is_no_id_of_the_second() -> None:
    second_frame = _frame_header(components=(b"\x05\x11\x00", b"\x06\x11\x00"))
    head = SOI + _THREE_COMPONENTS + _scan_header(1) + _SCAN + second_frame
    picture = head + _scan_header(1) + _SCAN + EOI

    with pytest.raises(ComexioDataError) as info:
        first_jpeg_frame(picture, require_frame_header=True)

    assert str(info.value) == f"Camera frame has a scan whose components do not fit its frame at offset {len(head) + 1}"


def test_a_frame_whose_components_have_the_ids_zero_and_one_takes_a_scan_of_zero() -> None:
    frame_header = _frame_header(components=(b"\x00\x11\x00", b"\x01\x11\x00"))
    picture = SOI + frame_header + _scan_header(0) + _SCAN + EOI

    assert first_jpeg_frame(picture, require_frame_header=True) == picture


# --- the list is capped while it is read, not after it has been read and decoded ---------------------------------


class _EndlessContent:
    """A body that never ends, handed over in pieces of four bytes."""

    def __init__(self) -> None:
        self.requested: list[int] = []

    async def read(self, size: int) -> bytes:
        self.requested.append(size)
        return b"x" * min(size, 4)


async def test_a_text_read_stops_at_the_cap_a_caller_names() -> None:
    content = _EndlessContent()
    resp = cast(
        "Any", SimpleNamespace(headers=CIMultiDict[str](), content=content, content_type="text/html", charset="utf-8")
    )

    with pytest.raises(ComexioDataError) as info:
        await client_module._response_text(resp, "list", max_bytes=10)

    assert str(info.value) == "list: response is longer than 10 bytes"
    assert content.requested == [11, 7, 3]  # one byte over the cap and not a byte more, whatever the generic cap is


def _text_response(content: Any) -> Any:
    return cast(
        "Any", SimpleNamespace(headers=CIMultiDict[str](), content=content, content_type="text/html", charset="utf-8")
    )


@pytest.mark.parametrize(
    ("content", "error", "message"),
    [
        (_PieceContent(b'["26-11', then=asyncio.CancelledError()), asyncio.CancelledError, ""),
        (_PieceContent(b'["26-11', then=aiohttp.ClientPayloadError("cut")), aiohttp.ClientPayloadError, "cut"),
        (_EndlessContent(), ComexioDataError, "list: response is longer than 10 bytes"),
        (
            _PieceContent(b"\xff\xfe"),
            ComexioDataError,
            "list: response is not valid utf-8 (byte 0: invalid start byte)",
        ),
    ],
    ids=["cancelled-half-way", "payload-error-half-way", "over-the-cap", "not-decodable"],
)
async def test_a_failed_text_read_leaves_no_answer_bytes_in_its_traceback(
    content: Any, error: type[BaseException], message: str
) -> None:
    resp = _text_response(content)

    with pytest.raises(error) as info:
        await client_module._response_text(resp, "list", max_bytes=10)

    assert str(info.value) == message
    held = _buffers_in_traceback(info.value)
    assert held  # the frame does hold its buffer (else this proves nothing) ...
    assert not any(held)  # ... and it is empty by now


async def test_get_doorstation_pictures_refuses_a_list_over_the_cap_while_reading_it(
    logged_in: ComexioClient, comexio: FakeComexio
) -> None:
    cap = pictures_module.MAX_LIST_CHARS
    assert cap < client_module._RESPONSE_MAX_BYTES  # the generic cap would have let this answer through
    comexio.serve("POST", LIST_PATH, _list_handler(" " * (cap + 1)))

    with pytest.raises(ComexioDataError) as info:
        await logged_in.get_doorstation_pictures(7)

    assert str(info.value) == f"Doorstation 7 picture list: response is longer than {cap} bytes"


async def test_get_doorstation_pictures_reads_a_list_of_exactly_the_cap(
    logged_in: ComexioClient, comexio: FakeComexio
) -> None:
    text = json.dumps([BELL])
    comexio.serve("POST", LIST_PATH, _list_handler(text + " " * (pictures_module.MAX_LIST_CHARS - len(text))))

    assert [picture.name for picture in await logged_in.get_doorstation_pictures(7)] == [BELL]
