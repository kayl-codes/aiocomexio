"""ComexioClient writes (API values, Web-IO lifecycle, markers) against the fake Comexio server."""

import base64
import html
import json
import logging
import math
import re
import time
from collections.abc import Awaitable, Callable
from html.parser import HTMLParser
from typing import Any

import aiohttp
import pytest
from aiohttp import web

from aiocomexio import (
    ComexioAuthenticationError,
    ComexioClient,
    ComexioDataError,
    ComexioRequestRejectedError,
    ComexioResponseError,
    WebioBaseInfo,
    WebioCommandForm,
)
from aiocomexio import client as client_module
from aiocomexio.webio import CONTENT_TYPE_JSON, WEBIO_PROTOCOL_HTTPS
from tests.fake_comexio import LOGIN_PAGE, PASSWORD, USERNAME, FakeComexio

API_PATH = "/api/"
WEBIO_HOME_PATH = "/admin/web_io/home"
WEBIO_ADD_PATH = "/admin/web_io/add"
WEBIO_BASE_WINDOW_PATH = "/admin/web_io/baseDeviceWindow/"
WEBIO_DELETE_DEVICE_PATH = "/admin/web_io/delete_device/"
WEBIO_DELETE_BASE_PATH = "/admin/web_io/delete_web_device_base/"
WEBIO_SAVE_DEVICE_PATH = "/admin/web_io/save"
WEBIO_CREATE_DEVICE_PATH = "/admin/web_io/saveDeviceWindow"
WEBIO_UPLOAD_PATH = "/admin/web_io/upload_device_settings"
WEBIO_SAVE_COMMAND_PATH = "/admin/web_io/save_command"
WEBIO_DELETE_COMMAND_PATH = "/admin/web_io/delete_web_command/"
WEBIO_EDIT_COMMAND_PATH = "/admin/web_io/edit_command/"
UNIQUE_CHECK_PATH = "/admin/_helper/isunique"
MARKER_ADD_PATH = "/admin/flag/add/"
MARKER_SAVE_PATH = "/admin/flag/saveOne"
KNX_SAVE_PATH = "/admin/knx_one_wire/saveKnx/"
DELETE_ELEMENT_PATH = "/admin/function_function_module/delete_element/"
SYSTEM_DASHBOARD_PATH = "/admin/admin_dashboard/home/"

API_USER = "api"
API_PASSWORD = "api-secret"  # nosec B105

_COMMAND = {
    "Name": "M5 Licht",
    "Parameter": "/api/webhook/comexio_abc",
    "Data": "return '{}'",
    "TypeId": 1,
    "Min": 0,
    "Max": 1,
}


def _decoded_ref(value: str) -> Any:
    return json.loads(base64.b64decode(value))


@pytest.fixture
def api_client(comexio: FakeComexio, session: aiohttp.ClientSession) -> ComexioClient:
    return ComexioClient(
        comexio.host, USERNAME, PASSWORD, session=session, api_username=API_USER, api_password=API_PASSWORD
    )


# --- value writes ----------------------------------------------------------------------------


async def test_set_marker_value_sends_basic_auth(api_client: ComexioClient, comexio: FakeComexio) -> None:
    comexio.serve_text("GET", API_PATH, "")

    await api_client.set_marker_value(5, 21.5)

    (request,) = comexio.received_at("GET", API_PATH)
    assert request.query == {"action": "set", "value": "21.5", "marker": "M5"}
    assert request.authorization == f"Basic {base64.b64encode(b'api:api-secret').decode()}"


async def test_set_value_encodes_api_credentials_as_latin1(
    comexio: FakeComexio, session: aiohttp.ClientSession
) -> None:
    comexio.serve_text("GET", API_PATH, "")
    client = ComexioClient(comexio.host, USERNAME, PASSWORD, session=session, api_username="api", api_password="sé")

    await client.set_marker_value(5, 1)

    (request,) = comexio.received_at("GET", API_PATH)
    assert request.authorization == f"Basic {base64.b64encode('api:sé'.encode('iso-8859-1')).decode()}"


async def test_set_value_with_unencodable_api_password_fails_before_sending(
    comexio: FakeComexio, session: aiohttp.ClientSession
) -> None:
    client = ComexioClient(comexio.host, USERNAME, PASSWORD, session=session, api_username="api", api_password="€")

    with pytest.raises(ComexioAuthenticationError, match="ISO-8859-1"):
        await client.set_marker_value(5, 1)
    assert comexio.received_at("GET", API_PATH) == []


async def test_set_knx_and_io_values_address_their_target(api_client: ComexioClient, comexio: FakeComexio) -> None:
    comexio.serve_text("GET", API_PATH, "1")

    await api_client.set_knx_value(3, 1)
    await api_client.set_io_value("UD 1", "QD2", 0)

    knx, io = comexio.received_at("GET", API_PATH)
    assert knx.query == {"action": "set", "value": "1", "knx": "K3"}
    assert io.query == {"action": "set", "value": "0", "ext": "UD 1", "io": "QD2"}


async def test_set_io_value_percent_encodes_spaces_in_the_extension_name(
    api_client: ComexioClient, comexio: FakeComexio
) -> None:
    comexio.serve_text("GET", API_PATH, "1")

    await api_client.set_io_value("LED-Dimmer A3", "Q1", 50)

    (request,) = comexio.received_at("GET", API_PATH)
    assert "ext=LED-Dimmer%20A3" in request.raw_path
    assert "+" not in request.raw_path
    assert request.query["ext"] == "LED-Dimmer A3"


@pytest.mark.parametrize(
    ("call", "target", "query"),
    [
        (
            lambda c: c.set_io_value("LED-Dimmer A3", "Q1", 50),
            "IO 'Q1' of 'LED-Dimmer A3'",
            {"action": "set", "value": "50", "ext": "LED-Dimmer A3", "io": "Q1"},
        ),
        (lambda c: c.set_marker_value(5, 1), "marker M5", {"action": "set", "value": "1", "marker": "M5"}),
        (lambda c: c.set_knx_value(3, 1), "KNX object K3", {"action": "set", "value": "1", "knx": "K3"}),
    ],
)
async def test_set_value_not_found_answer_raises_naming_the_target(
    api_client: ComexioClient,
    comexio: FakeComexio,
    call: Callable[[ComexioClient], Awaitable[None]],
    target: str,
    query: dict[str, str],
) -> None:
    comexio.serve_text("GET", API_PATH, "0:not_found")

    with pytest.raises(ComexioRequestRejectedError, match=re.escape(target)):
        await call(api_client)

    # The rejection must come from the server's answer, not from a check before the request went out.
    (request,) = comexio.received_at("GET", API_PATH)
    assert request.query == query


_AWKWARD_EXT_NAMES = ["A&B", "A#B", "A+B", "A%B", "A%41", "A/B", "A=B", "A?B", "Küche ä", "100% & #1 + a=b/c?d"]


@pytest.mark.parametrize("ext", _AWKWARD_EXT_NAMES)
async def test_set_io_value_round_trips_awkward_extension_names(
    api_client: ComexioClient, comexio: FakeComexio, ext: str
) -> None:
    comexio.serve_text("GET", API_PATH, "1")

    await api_client.set_io_value(ext, "Q1", 1)

    (request,) = comexio.received_at("GET", API_PATH)
    # The characters that would split or end the query never go out raw, and "+" is never a space.
    # yarl may leave "?" and "/" literal (legal in a query), so only the round trip pins those.
    query_part = request.raw_path.split("?", 1)[1]
    ext_part = query_part.split("ext=", 1)[1].split("&io=", 1)[0]
    assert query_part.count("&") == 3
    assert not set(ext_part) & set("&#+= ")
    # A raw "%" followed by two hex digits ("A%41") would decode to another name, so every "%" goes out as "%25".
    assert ext_part.count("%25") == ext.count("%")
    assert ext_part.isascii()
    assert request.query == {"action": "set", "value": "1", "ext": ext, "io": "Q1"}


async def test_set_io_value_encodes_an_umlaut_as_utf8(api_client: ComexioClient, comexio: FakeComexio) -> None:
    comexio.serve_text("GET", API_PATH, "1")

    await api_client.set_io_value("Küche", "Q1", 1)

    (request,) = comexio.received_at("GET", API_PATH)
    assert "ext=K%C3%BCche" in request.raw_path


@pytest.mark.parametrize("answer", ["0:not_found\n", "  5:not_found ", "5:not_found", "12:not_found\r\n"])
async def test_set_value_padded_or_numbered_not_found_answer_raises(
    api_client: ComexioClient, comexio: FakeComexio, answer: str
) -> None:
    comexio.serve_text("GET", API_PATH, answer)

    with pytest.raises(ComexioRequestRejectedError, match="marker M5"):
        await api_client.set_marker_value(5, 1)

    (request,) = comexio.received_at("GET", API_PATH)
    assert request.query == {"action": "set", "value": "1", "marker": "M5"}


@pytest.mark.parametrize("answer", ["error: not_found in table", "", "0", "ok"])
async def test_set_value_unknown_answer_warns_without_raising(
    api_client: ComexioClient, comexio: FakeComexio, caplog: pytest.LogCaptureFixture, answer: str
) -> None:
    comexio.serve_text("GET", API_PATH, answer)

    with caplog.at_level(logging.WARNING, logger="aiocomexio.client"):
        await api_client.set_marker_value(5, 1)

    (record,) = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert "Writing marker M5" in record.getMessage()
    assert "instead of '1'" in record.getMessage()
    assert (answer or "''") in record.getMessage()


async def test_set_io_value_warning_quotes_names_so_a_newline_cannot_forge_a_log_line(
    api_client: ComexioClient, comexio: FakeComexio, caplog: pytest.LogCaptureFixture
) -> None:
    comexio.serve_text("GET", API_PATH, "ok")

    with caplog.at_level(logging.WARNING, logger="aiocomexio.client"):
        await api_client.set_io_value("UD 1\nWARNING forged", "Q1\r", 1)

    (record,) = [r for r in caplog.records if r.levelno == logging.WARNING]
    message = record.getMessage()
    assert "\n" not in message
    assert "\r" not in message
    assert "Writing IO 'Q1\\r' of 'UD 1\\nWARNING forged'" in message


async def test_set_value_accepted_answer_does_not_warn(
    api_client: ComexioClient, comexio: FakeComexio, caplog: pytest.LogCaptureFixture
) -> None:
    comexio.serve_text("GET", API_PATH, " 1\n")

    with caplog.at_level(logging.WARNING, logger="aiocomexio.client"):
        await api_client.set_marker_value(5, 1)

    assert not caplog.records
    (request,) = comexio.received_at("GET", API_PATH)
    assert request.query == {"action": "set", "value": "1", "marker": "M5"}


@pytest.mark.parametrize(("value", "expected"), [(0.5, "0.5"), (-1, "-1"), (50.0, "50.0"), (1, "1")])
async def test_set_value_formats_the_value_as_str(
    api_client: ComexioClient, comexio: FakeComexio, value: float, expected: str
) -> None:
    comexio.serve_text("GET", API_PATH, "1")

    await api_client.set_marker_value(5, value)

    (request,) = comexio.received_at("GET", API_PATH)
    assert request.query["value"] == expected == str(value)


async def test_set_value_accepted_answer_succeeds(
    api_client: ComexioClient, comexio: FakeComexio, caplog: pytest.LogCaptureFixture
) -> None:
    comexio.serve_text("GET", API_PATH, "1")

    with caplog.at_level(logging.WARNING, logger="aiocomexio.client"):
        await api_client.set_io_value("LED-Dimmer A3", "Q1", 50)

    assert not caplog.records
    (request,) = comexio.received_at("GET", API_PATH)
    assert request.query == {"action": "set", "value": "50", "ext": "LED-Dimmer A3", "io": "Q1"}


@pytest.mark.parametrize(("value", "expected"), [(1e16, "1e+16"), (1e-07, "1e-07")])
async def test_set_value_exponent_values_round_trip_without_a_raw_plus(
    api_client: ComexioClient, comexio: FakeComexio, value: float, expected: str
) -> None:
    comexio.serve_text("GET", API_PATH, "1")

    await api_client.set_marker_value(5, value)

    (request,) = comexio.received_at("GET", API_PATH)
    assert "+" not in request.raw_path
    assert request.query["value"] == expected
    if "+" in expected:
        assert "value=1e%2B16" in request.raw_path


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
async def test_set_value_rejects_non_finite_values_before_sending(
    api_client: ComexioClient, comexio: FakeComexio, value: float
) -> None:
    with pytest.raises(ValueError, match="not a finite number"):
        await api_client.set_marker_value(5, value)
    assert comexio.received_at("GET", API_PATH) == []


async def test_set_value_accepts_a_huge_int_without_overflow(api_client: ComexioClient, comexio: FakeComexio) -> None:
    comexio.serve_text("GET", API_PATH, "1")

    await api_client.set_marker_value(5, 10**400)

    (request,) = comexio.received_at("GET", API_PATH)
    assert request.query["value"] == "1" + "0" * 400


async def test_set_value_not_found_with_trailing_text_only_warns(
    api_client: ComexioClient, comexio: FakeComexio, caplog: pytest.LogCaptureFixture
) -> None:
    comexio.serve_text("GET", API_PATH, "0:not_found extra")

    with caplog.at_level(logging.WARNING, logger="aiocomexio.client"):
        await api_client.set_marker_value(5, 1)

    (record,) = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert "0:not_found extra" in record.getMessage()


async def test_set_value_on_the_manual_query_url_sends_credentials_and_maps_401(
    api_client: ComexioClient, comexio: FakeComexio
) -> None:
    comexio.serve_text("GET", API_PATH, "1")
    await api_client.set_io_value("LED-Dimmer A3", "Q1", 50)
    (request,) = comexio.received_at("GET", API_PATH)
    assert request.authorization == f"Basic {base64.b64encode(b'api:api-secret').decode()}"
    assert "ext=LED-Dimmer%20A3" in request.raw_path

    comexio.serve_text("GET", API_PATH, "", status=401)
    with pytest.raises(ComexioAuthenticationError, match="API credentials"):
        await api_client.set_io_value("LED-Dimmer A3", "Q1", 50)


async def test_set_value_without_api_user_sends_no_credentials(logged_in: ComexioClient, comexio: FakeComexio) -> None:
    comexio.serve_text("GET", API_PATH, "1")

    await logged_in.set_marker_value(5, 1)

    (request,) = comexio.received_at("GET", API_PATH)
    assert request.authorization is None


async def test_set_value_rejected_credentials_raise_authentication_error(
    api_client: ComexioClient, comexio: FakeComexio
) -> None:
    comexio.serve_text("GET", API_PATH, "", status=401)

    with pytest.raises(ComexioAuthenticationError, match="API credentials"):
        await api_client.set_marker_value(5, 1)


async def test_set_value_unauthorized_without_api_user_names_the_missing_setting(
    logged_in: ComexioClient, comexio: FakeComexio
) -> None:
    comexio.serve_text("GET", API_PATH, "", status=401)

    with pytest.raises(ComexioAuthenticationError, match="none are configured"):
        await logged_in.set_marker_value(5, 1)


async def test_set_value_rejects_colon_in_api_username_before_sending(
    comexio: FakeComexio, session: aiohttp.ClientSession
) -> None:
    client = ComexioClient(comexio.host, USERNAME, PASSWORD, session=session, api_username="a:b", api_password="x")

    with pytest.raises(ComexioAuthenticationError, match="':'"):
        await client.set_marker_value(5, 1)
    assert comexio.received_at("GET", API_PATH) == []


async def test_set_value_http_error_raises_response_error(api_client: ComexioClient, comexio: FakeComexio) -> None:
    comexio.serve_text("GET", API_PATH, "", status=500)

    with pytest.raises(ComexioResponseError) as excinfo:
        await api_client.set_marker_value(5, 1)
    assert excinfo.value.status == 500


# --- Web-IO classes and devices ---------------------------------------------------------------


def _webio_add_page(bases: dict[str, str], devices: dict[str, str] | None = None) -> str:
    """A Web-IO add page as Comexio renders it: both lists as JSON, the classes also as <option>s."""
    base_list = {key: {"Id": int(key), "Identifier": name, "BaseId": 0} for key, name in bases.items()}
    device_list = {key: {"Id": int(key), "Name": name, "WebDeviceBaseId": 1} for key, name in (devices or {}).items()}
    options = "".join(f'<option value="{key}">{html.escape(name)}</option>' for key, name in bases.items())
    return (
        '<div id="add_edit_device_info_message_hover"></div><script type="text/javascript"> '
        f"DeviceList={json.dumps(device_list) if device_list else '[]'}; DeviceBaseList={json.dumps(base_list)}; "
        f'</script> <form id="new_device_form"><select id="web_device_base">{options}</select></form>'
    )


async def test_get_webio_base_info_finds_class_and_deletability(logged_in: ComexioClient, comexio: FakeComexio) -> None:
    comexio.serve_text("GET", WEBIO_ADD_PATH, _webio_add_page({"12": "HA [M]"}))
    comexio.serve_text("GET", WEBIO_BASE_WINDOW_PATH, '<a href="/admin/web_io/delete_web_device_base/?id=12">x</a>')

    assert await logged_in.get_webio_base_info("ha [m]") == WebioBaseInfo(base_id="12", deletable=True)


async def test_get_webio_base_info_class_in_use_is_not_deletable(
    logged_in: ComexioClient, comexio: FakeComexio
) -> None:
    comexio.serve_text("GET", WEBIO_ADD_PATH, _webio_add_page({"12": "HA [M]"}))
    comexio.serve_text("GET", WEBIO_BASE_WINDOW_PATH, "<div>no delete link</div>")

    assert await logged_in.get_webio_base_info("HA [M]") == WebioBaseInfo(base_id="12", deletable=False)


async def test_get_webio_base_info_delete_link_of_a_longer_id_is_not_deletable(
    logged_in: ComexioClient, comexio: FakeComexio
) -> None:
    """Class 1 is in use; only class 12 has a delete link, which must not count for class 1."""
    comexio.serve_text("GET", WEBIO_ADD_PATH, _webio_add_page({"1": "HA [M]", "12": "Other"}))
    comexio.serve_text("GET", WEBIO_BASE_WINDOW_PATH, '<a href="/admin/web_io/delete_web_device_base/?id=12">x</a>')

    assert await logged_in.get_webio_base_info("HA [M]") == WebioBaseInfo(base_id="1", deletable=False)


async def test_get_webio_base_info_matches_names_with_html_special_characters(
    logged_in: ComexioClient, comexio: FakeComexio
) -> None:
    """The <option> label reads "A &amp; B"; the JSON list carries the real name."""
    comexio.serve_text("GET", WEBIO_ADD_PATH, _webio_add_page({"12": "A & B", "13": "a & b"}))
    comexio.serve_text("GET", WEBIO_BASE_WINDOW_PATH, "<div></div>")

    assert await logged_in.get_webio_base_info("a & b") == WebioBaseInfo(base_id="13", deletable=False)


async def test_get_webio_base_info_absent_class_is_none(logged_in: ComexioClient, comexio: FakeComexio) -> None:
    comexio.serve_text("GET", WEBIO_ADD_PATH, _webio_add_page({"12": "Other"}))

    assert await logged_in.get_webio_base_info("HA [M]") is None
    assert comexio.received_at("GET", WEBIO_BASE_WINDOW_PATH) == []


@pytest.mark.parametrize(
    "page",
    [
        '<select><option value="12">Other</option></select>',
        "<script>DeviceBaseList={};</script>",
        "<html><body>Wartungsmodus</body></html>",
        "<script>DeviceList=null; DeviceBaseList={};</script>",
        "<script>DeviceList=[]; DeviceBaseList={}; DeviceList={};</script>",
        '<script>DeviceList=[]; DeviceBaseList={"12": {"Id": 12, "Identifier": "HA [M]"</script>',
    ],
)
async def test_webio_lookups_on_a_page_without_both_lists_raise(
    logged_in: ComexioClient, comexio: FakeComexio, page: str
) -> None:
    """Not the add page: "no such class/device" would make the caller upload or recreate one."""
    comexio.serve_text("GET", WEBIO_ADD_PATH, page)

    with pytest.raises(ComexioDataError, match="DeviceBaseList/DeviceList"):
        await logged_in.get_webio_base_info("HA [M]")
    with pytest.raises(ComexioDataError, match="DeviceBaseList/DeviceList"):
        await logged_in.get_webio_device_id("HA [IO]")


@pytest.mark.parametrize(
    "base_list",
    [
        {"12": {"Id": 12, "Ident": "HA [M]"}},  # field renamed
        {"12": {"Id": 12, "Identifier": None}},
        {"0": {"Id": 12, "Identifier": "HA [M]"}},  # keyed by position, not by id
        {"12": "HA [M]"},
    ],
)
async def test_webio_list_with_unexpected_entries_raises_instead_of_reporting_absent(
    logged_in: ComexioClient, comexio: FakeComexio, base_list: dict[str, Any]
) -> None:
    comexio.serve_text(
        "GET", WEBIO_ADD_PATH, f"<script>DeviceList=[]; DeviceBaseList={json.dumps(base_list)};</script>"
    )

    with pytest.raises(ComexioDataError, match="Web-IO list entry"):
        await logged_in.get_webio_base_info("Other")


async def test_webio_list_error_caps_a_long_key(logged_in: ComexioClient, comexio: FakeComexio) -> None:
    key = "k" * 1000
    base_list = {key: {"Id": 12, "Identifier": "HA [M]"}}
    comexio.serve_text(
        "GET", WEBIO_ADD_PATH, f"<script>DeviceList=[]; DeviceBaseList={json.dumps(base_list)};</script>"
    )

    with pytest.raises(ComexioDataError) as excinfo:
        await logged_in.get_webio_base_info("Other")

    assert str(excinfo.value) == (
        f"Web-IO list entry {repr(key)[:200]}... has no 'Identifier' or a different Id: {repr(base_list[key])}"
    )


async def test_get_webio_base_info_login_form_is_not_an_absent_class(
    logged_in: ComexioClient, comexio: FakeComexio
) -> None:
    comexio.serve_text("GET", WEBIO_ADD_PATH, LOGIN_PAGE)

    with pytest.raises(ComexioAuthenticationError):
        await logged_in.get_webio_base_info("HA [M]")


async def test_get_webio_base_info_failed_fetch_raises(logged_in: ComexioClient, comexio: FakeComexio) -> None:
    comexio.serve_text("GET", WEBIO_ADD_PATH, "", status=500)

    with pytest.raises(ComexioResponseError):
        await logged_in.get_webio_base_info("HA [M]")


async def test_get_webio_device_id(logged_in: ComexioClient, comexio: FakeComexio) -> None:
    comexio.serve_text("GET", WEBIO_ADD_PATH, _webio_add_page({"1": "HA [IO]"}, {"34": "HA [IO]", "35": "Küche"}))

    assert await logged_in.get_webio_device_id("HA [IO]") == "34"
    assert await logged_in.get_webio_device_id("küche") == "35"
    assert await logged_in.get_webio_device_id("HA [M]") is None


async def test_get_webio_device_id_without_devices_is_none(logged_in: ComexioClient, comexio: FakeComexio) -> None:
    """PHP renders an empty device list as []; that is a valid page listing no device."""
    comexio.serve_text("GET", WEBIO_ADD_PATH, _webio_add_page({"1": "HA [IO]"}))

    assert await logged_in.get_webio_device_id("HA [IO]") is None


async def test_get_webio_device_id_login_form_raises(logged_in: ComexioClient, comexio: FakeComexio) -> None:
    comexio.serve_text("GET", WEBIO_ADD_PATH, LOGIN_PAGE)

    with pytest.raises(ComexioAuthenticationError):
        await logged_in.get_webio_device_id("HA [IO]")


async def test_delete_webio_device(logged_in: ComexioClient, comexio: FakeComexio) -> None:
    comexio.serve_text("GET", WEBIO_DELETE_DEVICE_PATH, "<div>ok</div>")

    assert await logged_in.delete_webio_device(34) is True
    (request,) = comexio.received_at("GET", WEBIO_DELETE_DEVICE_PATH)
    assert request.query == {"id": "34"}


async def test_delete_webio_device_in_use_returns_false(logged_in: ComexioClient, comexio: FakeComexio) -> None:
    comexio.serve_text("GET", WEBIO_DELETE_DEVICE_PATH, '<div class="ui-state-error">in use</div>')

    assert await logged_in.delete_webio_device(34) is False


async def test_delete_webio_base(logged_in: ComexioClient, comexio: FakeComexio) -> None:
    comexio.serve_text("GET", WEBIO_DELETE_BASE_PATH, "")

    await logged_in.delete_webio_base("12")

    (request,) = comexio.received_at("GET", WEBIO_DELETE_BASE_PATH)
    assert request.query == {"id": "12"}


async def test_delete_webio_base_http_error_raises(logged_in: ComexioClient, comexio: FakeComexio) -> None:
    comexio.serve_text("GET", WEBIO_DELETE_BASE_PATH, "", status=500)

    with pytest.raises(ComexioResponseError):
        await logged_in.delete_webio_base("12")


async def test_upload_webio_class_returns_base_id(logged_in: ComexioClient, comexio: FakeComexio) -> None:
    uploaded: dict[str, Any] = {}

    async def handler(request: web.Request) -> web.Response:
        form = await request.post()
        file_field = form["file"]
        assert isinstance(file_field, web.FileField)
        uploaded.update(filename=file_field.filename, body=file_field.file.read().decode(), name=form["set_name"])
        return web.Response(text=json.dumps({"ok": True, "base_id": 17}), content_type="text/html")

    comexio.serve("POST", WEBIO_UPLOAD_PATH, handler)

    base_id = await logged_in.upload_webio_class('{"data": "web_io"}', class_name="HA [M]", filename="ha_abc.json")

    assert base_id == "17"
    assert uploaded == {"filename": "ha_abc.json", "body": '{"data": "web_io"}', "name": "HA [M]"}


@pytest.mark.parametrize(
    ("answer", "error"),
    [
        ({"ok": False, "error": "name exists"}, ComexioRequestRejectedError),
        ({"ok": True}, ComexioDataError),
        ([], ComexioDataError),
    ],
)
async def test_upload_webio_class_without_usable_answer_raises(
    logged_in: ComexioClient, comexio: FakeComexio, answer: Any, error: type[Exception]
) -> None:
    comexio.serve_json("POST", WEBIO_UPLOAD_PATH, answer)

    with pytest.raises(error):
        await logged_in.upload_webio_class("{}", class_name="HA [M]", filename="ha_abc.json")


async def test_create_webio_device_posts_the_device_form(logged_in: ComexioClient, comexio: FakeComexio) -> None:
    comexio.serve_text("POST", WEBIO_CREATE_DEVICE_PATH, "")

    await logged_in.create_webio_device("HA [M]", 17, "192.168.0.10:8123", username="u", password="p")

    (request,) = comexio.received_at("POST", WEBIO_CREATE_DEVICE_PATH)
    assert request.form == {
        "name": "HA [M]",
        "ip": "192.168.0.10:8123",
        "web_device_base": "17",
        "username": "u",
        "password": "p",
        "web_device_base_sample": "none",
        "identifier": "",
        "form_login": "2",
    }


async def test_update_webio_device_address(logged_in: ComexioClient, comexio: FakeComexio) -> None:
    comexio.serve_json("POST", WEBIO_SAVE_DEVICE_PATH, {"save": 1})

    await logged_in.update_webio_device_address(34, "192.168.0.11:8123", "HA [M]")

    (request,) = comexio.received_at("POST", WEBIO_SAVE_DEVICE_PATH)
    assert request.form["no_reload"] == "true"
    assert json.loads(request.form["JSON"]) == {
        "web_device_id": "34",
        "name_34": "HA [M]",
        "ip_34": "192.168.0.11:8123",
        "username_34": "",
        "password_34": "",
        "checkca_34": "0",
        "pinnedpubkey_34": "",
        "form_login_34": "2",
    }


async def test_update_webio_device_address_unconfirmed_raises(logged_in: ComexioClient, comexio: FakeComexio) -> None:
    comexio.serve_json("POST", WEBIO_SAVE_DEVICE_PATH, {"save": 0})

    with pytest.raises(ComexioRequestRejectedError):
        await logged_in.update_webio_device_address(34, "192.168.0.11:8123", "HA [M]")


# --- Web-IO commands ------------------------------------------------------------------------------


async def test_save_webio_command_new(logged_in: ComexioClient, comexio: FakeComexio) -> None:
    comexio.serve_text("POST", WEBIO_SAVE_COMMAND_PATH, "")

    await logged_in.save_webio_command(34, _COMMAND, base_id=17)

    (request,) = comexio.received_at("POST", WEBIO_SAVE_COMMAND_PATH)
    form = dict(request.form)
    assert _decoded_ref(form.pop("id_cmd_io_0")) == {"src": "command", "id": None}
    assert form == {
        "dlg_web_device_id": "34",
        "deviceBaseId": "17",
        "protocol": "0",
        "parameter": _COMMAND["Parameter"],
        "header_modifier": CONTENT_TYPE_JSON,
        "data": _COMMAND["Data"],
        "port": "",
        "post_get": "1",
        "authentication": "0",
        "req_freq": "",
        "reply_interpreter": "",
        "name_cmd_io_0": "M5 Licht",
        "function_cmd_io_0": "1_1_0",
        "input_cmd_io_0": "1",
        "type_cmd_io_0": "1",
        "send_on_one_cmd_io_0": "0",
        "min_cmd_io_0": "0",
        "max_cmd_io_0": "1",
        "default_value_cmd_io_0": "",
        "id_cmd_io_sample": "",
        "name_cmd_io_sample": "",
        "function_cmd_io_sample": "0_1_0",
        "input_cmd_io_sample": "1",
        "type_cmd_io_sample": "2",
        "send_on_one_cmd_io_sample": "0",
        "min_cmd_io_sample": "0",
        "max_cmd_io_sample": "1",
        "default_value_cmd_io_sample": "",
        "DefaultActive": "1",
    }


async def test_save_webio_command_update_keeps_command_specific_request_shape(
    logged_in: ComexioClient, comexio: FakeComexio
) -> None:
    comexio.serve_text("POST", WEBIO_SAVE_COMMAND_PATH, "")
    loopback = {**_COMMAND, "HeaderModifier": "", "PostGet": 0, "Authentication": 1}

    await logged_in.save_webio_command(34, loopback, command_id="1324")

    (request,) = comexio.received_at("POST", WEBIO_SAVE_COMMAND_PATH)
    form = request.form
    assert _decoded_ref(form["id_cmd_io_0"]) == {"src": "command", "id": 1324}
    assert form["id"] == "1324"
    assert "deviceBaseId" not in form
    assert (form["header_modifier"], form["post_get"], form["authentication"]) == ("", "0", "1")


async def test_save_webio_command_posts_the_command_protocol(logged_in: ComexioClient, comexio: FakeComexio) -> None:
    comexio.serve_text("POST", WEBIO_SAVE_COMMAND_PATH, "")

    await logged_in.save_webio_command(34, {**_COMMAND, "Protocol": WEBIO_PROTOCOL_HTTPS}, base_id=17)

    (request,) = comexio.received_at("POST", WEBIO_SAVE_COMMAND_PATH)
    assert request.form["protocol"] == "1"


@pytest.mark.parametrize("protocol", [None, True, "1", 1.0])
async def test_save_webio_command_rejects_a_non_int_protocol(
    logged_in: ComexioClient, comexio: FakeComexio, protocol: object
) -> None:
    with pytest.raises(ValueError, match="protocol must be an int"):
        await logged_in.save_webio_command(34, {**_COMMAND, "Protocol": protocol}, base_id=17)

    assert not comexio.received_at("POST", WEBIO_SAVE_COMMAND_PATH)


async def test_save_webio_command_http_error_raises(logged_in: ComexioClient, comexio: FakeComexio) -> None:
    comexio.serve_text("POST", WEBIO_SAVE_COMMAND_PATH, "", status=500)

    with pytest.raises(ComexioResponseError):
        await logged_in.save_webio_command(34, _COMMAND, base_id=17)


async def test_delete_webio_command(logged_in: ComexioClient, comexio: FakeComexio) -> None:
    comexio.serve_text("GET", WEBIO_DELETE_COMMAND_PATH, "")

    await logged_in.delete_webio_command(1324, 34)

    (request,) = comexio.received_at("GET", WEBIO_DELETE_COMMAND_PATH)
    assert request.query == {"id": "1324", "dev": "34"}


async def test_get_webio_command_range(logged_in: ComexioClient, comexio: FakeComexio) -> None:
    comexio.serve_text(
        "GET",
        WEBIO_EDIT_COMMAND_PATH,
        '<input type="text" id="min_cmd_io_0" value="-500000"><input id="max_cmd_io_0" value="abc">',
    )

    assert await logged_in.get_webio_command_range(1324, 34) == (-500000.0, None)
    (request,) = comexio.received_at("GET", WEBIO_EDIT_COMMAND_PATH)
    assert request.query == {"Id": "1324", "TestDevice": "34"}


async def test_get_webio_command_range_field_without_value_is_none(
    logged_in: ComexioClient, comexio: FakeComexio
) -> None:
    comexio.serve_text("GET", WEBIO_EDIT_COMMAND_PATH, '<input id="min_cmd_io_0"><input id="max_cmd_io_0" value="1">')

    assert await logged_in.get_webio_command_range(1324, 34) == (None, 1.0)


@pytest.mark.parametrize(
    ("html", "expected"),
    [
        ("<input id='min_cmd_io_0' value='-5'><input id=max_cmd_io_0 value=5>", (-5.0, 5.0)),
        ('<input id="min_cmd_io_0" value="&#45;5"><input id="max_cmd_io_0" value="1&#46;5" />', (-5.0, 1.5)),
        ('<input title="a > b" id="min_cmd_io_0" value="-5"><input id="max_cmd_io_0" value="5">', (-5.0, 5.0)),
        ('<input id="min_cmd_io_0" value="-5" value="0"><input id="max_cmd_io_0" value="5">', (-5.0, 5.0)),
        ('<input id="min_cmd_io_0" value="-5"><input id="min_cmd_io_0" value="0">', (-5.0, None)),
        ('<!-- <input id="min_cmd_io_0" value="0"> --><input id="min_cmd_io_0" value="-5">', (-5.0, None)),
        ('<input id="min_cmd_io_0_extra" value="0"><input id="max_cmd_io_0" value="5">', (None, 5.0)),
        ('<input id="max_cmd_io_0" value="5">', (None, 5.0)),
        ('<input id="min_cmd_io_0"><input id="min_cmd_io_0" value="3">', (None, None)),
        ('<input id="min_cmd_io_0" value="-5"><input id="max_cmd_io_0" value="5', (-5.0, None)),
        ('<input id="MIN_cmd_io_0" value="0"><input id="min_cmd_io_0" value="-5">', (-5.0, None)),
        ('<textarea><input id="min_cmd_io_0" value="0"></textarea><input id="min_cmd_io_0" value="-5">', (-5.0, None)),
        ('<title><input id="max_cmd_io_0" value="0"></title><input id="max_cmd_io_0" value="5">', (None, 5.0)),
        ('<textarea/><input id="min_cmd_io_0" value="0"></textarea><input id="min_cmd_io_0" value="-5">', (-5.0, None)),
        *(
            (f'<{tag}><input id="min_cmd_io_0" value="0"></{tag}><input id="min_cmd_io_0" value="-5">', (-5.0, None))
            for tag in ("xmp", "iframe", "noembed", "noframes", "noscript")
        ),
        ('<input id="max_cmd_io_0" value="5"><plaintext></plaintext><input id="min_cmd_io_0" value="-5">', (None, 5.0)),
        ('<template><input id="min_cmd_io_0" value="0"></template><input id="min_cmd_io_0" value="-5">', (-5.0, None)),
        # An SVG or MathML element is no form field; an integration point reads HTML again, a breakout tag ends it.
        ('<svg><input id="min_cmd_io_0" value="0"></svg><input id="min_cmd_io_0" value="-5">', (-5.0, None)),
        ('<math><input id="min_cmd_io_0" value="0"></math><input id="min_cmd_io_0" value="-5">', (-5.0, None)),
        ('<svg><foreignObject><input id="min_cmd_io_0" value="-5"></foreignObject></svg>', (-5.0, None)),
        ('<math><mi><input id="min_cmd_io_0" value="-5"></mi></math>', (-5.0, None)),
        ('<math><annotation-xml encoding="Text/HTML"><input id="min_cmd_io_0" value="-5"></math>', (-5.0, None)),
        (
            '<math><annotation-xml><input id="min_cmd_io_0" value="0"></math><input id="min_cmd_io_0" value="-5">',
            (-5.0, None),
        ),
        ('<math><annotation-xml><svg><foreignObject><input id="min_cmd_io_0" value="-5">', (-5.0, None)),
        ('<svg><mi><input id="min_cmd_io_0" value="0"></svg><input id="min_cmd_io_0" value="-5">', (-5.0, None)),
        (
            '<math><mi><mglyph><input id="min_cmd_io_0" value="0"></math><input id="min_cmd_io_0" value="-5">',
            (-5.0, None),
        ),
        ('<svg><g><div><input id="min_cmd_io_0" value="-5">', (-5.0, None)),
        ('<svg><font color="red"><input id="min_cmd_io_0" value="-5">', (-5.0, None)),
        ('<svg><font><input id="min_cmd_io_0" value="0"></svg><input id="min_cmd_io_0" value="-5">', (-5.0, None)),
        ('<svg/><input id="min_cmd_io_0" value="-5">', (-5.0, None)),
        ('<svg><title></svg><input id="min_cmd_io_0" value="-5">', (-5.0, None)),
        ('<svg><style></svg><input id="min_cmd_io_0" value="-5">', (-5.0, None)),
        *(
            (
                f'<svg></{tag}><input id="min_cmd_io_0" value="-5"></svg><input id="min_cmd_io_0" value="0">',
                (-5.0, None),
            )
            for tag in ("p", "br")
        ),
        (
            '<svg><foreignObject><div></foreignObject><input id="min_cmd_io_0" value="-5"></svg>'
            '<input id="min_cmd_io_0" value="0">',
            (-5.0, None),
        ),
        (
            '<svg><foreignObject><div></div></foreignObject><input id="min_cmd_io_0" value="0"></svg>'
            '<input id="min_cmd_io_0" value="-5">',
            (-5.0, None),
        ),
        ('<math><mi><span><mglyph><input id="min_cmd_io_0" value="-5">', (-5.0, None)),
        ('<svg><g><g></g><input id="min_cmd_io_0" value="0"></svg><input id="min_cmd_io_0" value="-5">', (-5.0, None)),
        (
            '<svg></svg><style><input id="min_cmd_io_0" value="0"></style><input id="min_cmd_io_0" value="-5">',
            (-5.0, None),
        ),
        (
            '<svg><foreignObject/><input id="min_cmd_io_0" value="0"></svg><input id="min_cmd_io_0" value="-5">',
            (-5.0, None),
        ),
        ('<math><svg><mi><input id="min_cmd_io_0" value="-5">', (-5.0, None)),
        (
            '<math><annotation-xml><mi><input id="min_cmd_io_0" value="-5">',
            (-5.0, None),
        ),
        (
            '<svg><foreignObject><svg><div></div></foreignObject><input id="min_cmd_io_0" value="0"></svg>'
            '<input id="min_cmd_io_0" value="-5">',
            (-5.0, None),
        ),
        *((f'<math><{tag}><input id="min_cmd_io_0" value="-5">', (-5.0, None)) for tag in ("mo", "mn", "ms", "mtext")),
        *((f'<svg><{tag}><input id="min_cmd_io_0" value="-5">', (-5.0, None)) for tag in ("desc", "title")),
        ('<math><annotation-xml encoding="application/xhtml+xml"><input id="min_cmd_io_0" value="-5">', (-5.0, None)),
        (
            '<math><mi><malignmark><input id="min_cmd_io_0" value="0"></math><input id="min_cmd_io_0" value="-5">',
            (-5.0, None),
        ),
        ('<math><annotation-xml encoding="text/html"><mglyph><input id="min_cmd_io_0" value="-5">', (-5.0, None)),
        *((f'<svg><font {attr}=x><input id="min_cmd_io_0" value="-5">', (-5.0, None)) for attr in ("face", "size")),
        (
            '<template><svg><style></template><input id="min_cmd_io_0" value="-5">',
            (-5.0, None),
        ),
        (
            '<svg><foreignObject><template><svg></template></foreignObject><input id="min_cmd_io_0" value="0"></svg>'
            '<input id="min_cmd_io_0" value="-5">',
            (-5.0, None),
        ),
        *(
            (
                f'<svg><foreignObject><{tag}><svg><circle></{tag}><input id="min_cmd_io_0" value="-5">'
                '</foreignObject></svg><input id="min_cmd_io_0" value="0">',
                (-5.0, None),
            )
            for tag in ("div", "span")
        ),
        *(
            (f'<{tag}><svg><circle></{tag}><input id="min_cmd_io_0" value="-5">', (-5.0, None))
            for tag in ("div", "span")
        ),
        ('<span><math><mi></span><input id="min_cmd_io_0" value="-5">', (-5.0, None)),
        (
            '<svg><foreignObject><span><svg><foreignObject></span></foreignObject><input id="min_cmd_io_0" value="0">'
            '</svg></span></foreignObject></svg><input id="min_cmd_io_0" value="-5">',
            (-5.0, None),
        ),
        (
            '<input id="max_cmd_io_0" value="5"><svg><foreignObject><span><svg><foreignObject></span></foreignObject>'
            '</svg></span></foreignObject><input id="min_cmd_io_0" value="0">',
            (None, 5.0),
        ),
        ('<svg><g><div></div><input id="min_cmd_io_0" value="-5">', (-5.0, None)),
        (
            '<math><mi><b><span></b><mglyph><input id="min_cmd_io_0" value="0"></math>'
            '<input id="min_cmd_io_0" value="-5">',
            (-5.0, None),
        ),
        (
            '<svg><foreignObject><select><input id="min_cmd_io_0" value="-5"></foreignObject>'
            '<input id="min_cmd_io_0" value="0">',
            (-5.0, None),
        ),
        (
            '<input id="min_cmd_io_0" value="-5"><svg><foreignObject><br></foreignObject>'
            '<input id="max_cmd_io_0" value="9">',
            (-5.0, None),
        ),
        (
            '<input id="max_cmd_io_0" value="5"><svg><foreignObject><select><select></foreignObject>'
            '<input id="min_cmd_io_0" value="0">',
            (None, 5.0),
        ),
        (
            '<input id="max_cmd_io_0" value="5"><svg><foreignObject><textarea></textarea></foreignObject>'
            '<input id="min_cmd_io_0" value="0">',
            (None, 5.0),
        ),
        ('<template><svg><foreignObject></template></foreignObject><input id="min_cmd_io_0" value="-5">', (-5.0, None)),
        (
            '<input id="max_cmd_io_0" value="5"><svg><a><foreignObject><a><svg></a>'
            '<input id="min_cmd_io_0" value="-5">',
            (-5.0, 5.0),
        ),
    ],
    ids=[
        "single-quoted-and-unquoted",
        "entities-and-self-closing",
        "quoted-gt-before-the-id",
        "first-duplicate-attribute-wins",
        "first-input-of-an-id-wins",
        "commented-out-input",
        "longer-id-is-another-input",
        "max-only",
        "first-input-without-value-wins",
        "cut-off-input-is-unknown",
        "upper-case-id-is-another-input",
        "input-written-into-a-textarea",
        "input-written-into-the-title",
        "input-after-a-self-closing-textarea",
        "input-written-into-an-xmp",
        "input-written-into-an-iframe",
        "input-written-into-a-noembed",
        "input-written-into-a-noframes",
        "input-written-into-a-noscript",
        "no-end-tag-ends-a-plaintext",
        "input-in-a-template",
        "input-in-an-svg",
        "input-in-a-math",
        "input-in-a-foreign-object",
        "input-in-a-mathml-text-integration-point",
        "input-in-an-html-annotation",
        "input-in-an-annotation-without-html-encoding",
        "svg-in-an-annotation",
        "mathml-tag-in-an-svg-is-no-integration-point",
        "mglyph-stays-mathml",
        "breakout-tag-ends-the-svg",
        "font-with-color-ends-the-svg",
        "font-without-attributes-stays-svg",
        "self-closing-svg",
        "svg-title-is-no-text-element",
        "svg-style-is-no-raw-text",
        "p-end-tag-ends-the-svg",
        "br-end-tag-ends-the-svg",
        "open-html-element-keeps-the-foreign-object",
        "closed-html-element-lets-the-foreign-object-end",
        "html-element-in-a-mathml-text-point-makes-mglyph-html",
        "inner-element-of-the-same-name-ends-alone",
        "style-after-a-closed-svg-is-raw-text",
        "self-closing-foreign-object",
        "svg-in-math-is-mathml",
        "annotation-child-stays-mathml",
        "breakout-stops-at-the-integration-point",
        "input-in-an-mo",
        "input-in-an-mn",
        "input-in-an-ms",
        "input-in-an-mtext",
        "input-in-an-svg-desc",
        "input-in-an-svg-title",
        "input-in-an-xhtml-annotation",
        "malignmark-stays-mathml",
        "mglyph-in-an-html-annotation-is-html",
        "font-with-face-ends-the-svg",
        "font-with-size-ends-the-svg",
        "svg-in-a-template",
        "template-end-tag-ends-the-svg-inside-it",
        "div-end-tag-in-a-foreign-object-ends-the-svg-inside-it",
        "span-end-tag-in-a-foreign-object-ends-the-svg-inside-it",
        "div-end-tag-ends-the-svg-inside-it",
        "span-end-tag-ends-the-svg-inside-it",
        "span-end-tag-ends-the-math-inside-it",
        "end-tag-reaches-no-element-outside-the-integration-point",
        "ignored-end-tag-leaves-the-inner-svg-open",
        "closed-breakout-element-leaves-no-svg-open",
        "end-tag-closes-the-elements-above-its-own",
        "select-ended-by-an-input-is-closed",
        "void-element-stays-closed",
        "select-in-a-select-only-ends-it",
        "textarea-is-closed-by-its-end-tag",
        "template-end-tag-ends-the-foreign-elements-inside-it",
        "html-end-tag-skips-a-foreign-element-of-the-same-name",
    ],
)
async def test_get_webio_command_range_reads_the_inputs_as_a_browser_does(
    logged_in: ComexioClient, comexio: FakeComexio, html: str, expected: tuple[float | None, float | None]
) -> None:
    comexio.serve_text("GET", WEBIO_EDIT_COMMAND_PATH, html)

    assert await logged_in.get_webio_command_range(1324, 34) == expected


# The special elements an end tag can meet open, and the elements whose end tag looks for them in scope.
_OPEN_SPECIAL_ELEMENTS = [
    *("address", "applet", "article", "aside", "blockquote", "button", "center", "dd", "details", "dir", "div", "dl"),
    *("dt", "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6", "header"),
    *("hgroup", "li", "listing", "main", "marquee", "menu", "nav", "object", "ol", "p", "pre", "search", "section"),
    *("summary", "table", "ul"),
]
_SCOPED_ELEMENTS = [
    *("address", "applet", "article", "aside", "blockquote", "button", "center", "details", "dialog", "dir", "div"),
    *("dl", "dd", "dt", "fieldset", "figcaption", "figure", "footer", "h1", "h2", "h3", "h4", "h5", "h6", "header"),
    *("hgroup", "listing", "main", "marquee", "menu", "nav", "object", "ol", "pre", "search", "section", "summary"),
    "ul",
]
# Start tags that close an open <p> (besides <p>, list items, headings, <hr> and <table>), and formatting elements.
_P_CLOSING_BLOCKS = [
    *("address", "article", "aside", "blockquote", "center", "details", "dialog", "dir", "dl", "fieldset"),
    *("figcaption", "figure", "footer", "header", "hgroup", "main", "menu", "nav", "ol", "search", "section"),
    *("summary", "ul", "pre", "listing"),
]
_FORMATTING_TAGS = ["big", "code", "em", "nobr", "s", "small", "strike", "strong", "tt", "u"]
_TABLE_PART_PREFIXES = {
    "caption": "<table><caption>",
    "tbody": "<table><tbody>",
    "thead": "<table><thead>",
    "tfoot": "<table><tfoot>",
    "tr": "<table><tr>",
    "td": "<table><tr><td>",
    "th": "<table><tr><th>",
}


@pytest.mark.parametrize(
    ("opening", "closed"),
    [
        # An end tag does not reach past a special element or a scope boundary.
        *(
            pytest.param(f"<span><{tag}><svg></span>", False, id=f"span-end-tag-stops-at-{tag}")
            for tag in _OPEN_SPECIAL_ELEMENTS
        ),
        pytest.param("<div><object><svg></div>", False, id="div-end-tag-stops-at-an-object"),
        pytest.param("<div><table><svg></div>", False, id="div-end-tag-stops-at-a-table"),
        pytest.param("<li><marquee><svg></li>", False, id="li-end-tag-stops-at-a-marquee"),
        pytest.param("<li><ul><svg></li>", False, id="li-end-tag-stops-at-a-ul"),
        pytest.param("<li><ol><svg></li>", False, id="li-end-tag-stops-at-an-ol"),
        pytest.param("<div><math><annotation-xml></div>", False, id="div-end-tag-stops-at-an-annotation"),
        pytest.param("<table><tr><td><table><svg></td>", False, id="td-end-tag-stops-at-a-nested-table"),
        pytest.param("<form><svg></form>", False, id="form-end-tag-removes-the-form-alone"),
        # Outside a table a browser ignores the start tag of a table part, and a <head> in the body.
        *(
            pytest.param(f"<{tag}><svg></{tag}>", False, id=f"{tag}-outside-a-table-is-ignored")
            for tag in ("td", "caption", "tr")
        ),
        pytest.param("<head><svg></head>", False, id="head-in-the-body-is-ignored"),
        *(
            pytest.param(f"<span><{tag}><svg></span>", True, id=f"ignored-{tag}-is-no-barrier")
            for tag in ("td", "th", "tr", "tbody", "thead", "tfoot", "caption", "colgroup", "head", "body", "html")
        ),
        # An end tag looked for in scope closes the elements above its own, special or not.
        *(
            pytest.param(
                f"<{tag}><{'section' if tag == 'center' else 'center'}><svg></{tag}>",
                True,
                id=f"{tag}-end-tag-in-scope",
            )
            for tag in _SCOPED_ELEMENTS
        ),
        pytest.param("<li><div><svg></li>", True, id="li-end-tag-in-scope"),
        pytest.param("<table><svg></table>", True, id="table-end-tag"),
        pytest.param("<table><center><svg></table>", True, id="table-end-tag-in-table-scope"),
        *(
            pytest.param(f"{prefix}<center><svg></{part}>", True, id=f"{part}-end-tag-in-table-scope")
            for part, prefix in _TABLE_PART_PREFIXES.items()
        ),
        pytest.param("<table><tr><td><svg></td>", True, id="td-end-tag-ends-the-svg-inside-it"),
        pytest.param("<table><tr><td><svg></tr>", True, id="tr-end-tag-ends-the-cell-and-the-svg-inside-it"),
        pytest.param("<ul><li><svg></li>", True, id="li-end-tag-ends-the-svg-inside-it"),
        pytest.param("<span><b><svg></span>", True, id="span-end-tag-ends-a-formatting-element-and-the-svg"),
        pytest.param("<div><span><svg></div>", True, id="div-end-tag-ends-a-span-and-the-svg"),
        pytest.param(
            "<span><template><div></template><svg></span>", True, id="template-end-tag-ends-everything-inside"
        ),
        pytest.param("<p><button><svg></p>", True, id="p-end-tag-breaks-out-of-the-svg"),
        # Any heading end tag closes the innermost open heading.
        pytest.param("<h1><svg></h2>", True, id="h2-end-tag-ends-an-h1"),
        pytest.param("<h2><svg></h1>", True, id="h1-end-tag-ends-an-h2"),
        pytest.param("<h1><center><svg></h2>", True, id="heading-end-tag-in-scope"),
        # A formatting end tag (adoption agency) keeps the special elements inside it open and ends the svg above.
        pytest.param("<b><div><svg></b>", True, id="b-end-tag-keeps-a-div"),
        pytest.param("<a><p><svg></a>", True, id="a-end-tag-keeps-a-p"),
        pytest.param("<font><li><svg></font>", True, id="font-end-tag-keeps-an-li"),
        pytest.param("<i><button><svg></i>", True, id="i-end-tag-keeps-a-button"),
        pytest.param("<span><b><div><svg></b><svg></span>", False, id="div-kept-by-a-formatting-end-tag-stays-special"),
        # A start tag closes what a browser closes before it: no closed element is left as a barrier.
        pytest.param("<span><p>a<div>b</div><svg></span>", True, id="div-start-tag-closes-a-p"),
        pytest.param("<b><p>x<h1>y</h1><svg></b>", True, id="heading-start-tag-closes-a-p"),
        pytest.param("<span><li>a<li>b</li><svg></span>", True, id="li-start-tag-closes-an-li"),
        pytest.param("<span><dd>a<dt>b</dt><svg></span>", True, id="dt-start-tag-closes-a-dd"),
        pytest.param("<span><h1>a<h2>b</h2><svg></span>", True, id="heading-start-tag-closes-a-heading"),
        pytest.param("<span><table><tr><td>a<td>b</td></table><svg></span>", True, id="td-start-tag-closes-a-td"),
        pytest.param("<span><button>a<button>b</button><svg></span>", True, id="button-start-tag-closes-a-button"),
        pytest.param("<span><form><form></form><svg></span>", True, id="form-in-a-form-is-ignored"),
        pytest.param("<span><form><div></form></div><svg></span>", True, id="form-end-tag-reaches-past-a-div"),
        pytest.param(
            "<span><form><span></form><svg></span><svg></span>", True, id="form-end-tag-removes-only-the-form"
        ),
        *(
            pytest.param(f"<p><{tag}><h1><svg></{tag}>", True, id=f"{tag}-start-tag-closes-a-p")
            for tag in _P_CLOSING_BLOCKS
        ),
        pytest.param("<p><li><form><svg></li>", True, id="li-start-tag-closes-a-p"),
        pytest.param("<p><dd><p><svg></dd>", True, id="dd-start-tag-closes-a-p"),
        pytest.param("<p><dt><ol><svg></dt>", True, id="dt-start-tag-closes-a-p"),
        pytest.param("<p><p><h1><div><svg></h1>", True, id="p-start-tag-closes-a-p"),
        pytest.param("<p><hr><a><h3><svg></a>", True, id="hr-start-tag-closes-a-p"),
        pytest.param("<p><h3><hr></ol><svg></h3>", True, id="h3-start-tag-closes-a-p"),
        pytest.param("<span><p><table></table><svg></span>", True, id="table-start-tag-closes-a-p-without-quirks"),
        pytest.param("<p><b></ul><span><svg></b>", True, id="span-start-tag-keeps-a-p"),
        pytest.param("<p><b><button><svg></b></form>", True, id="button-start-tag-keeps-a-p"),
        pytest.param("<table><p><button></p><svg></button>", True, id="p-end-tag-stops-at-a-button"),
        pytest.param("<p><button><dd></p><svg></button>", True, id="p-in-a-button-is-no-p-to-close"),
        pytest.param("<p><p><object><h2><svg></object>", True, id="p-start-tag-closes-a-p-in-button-scope"),
        pytest.param("<p><li><center><p><svg></li>", True, id="li-start-tag-closes-a-p-and-then-a-p"),
        *(pytest.param(f"<h1><svg></h{level}>", True, id=f"h{level}-end-tag-ends-an-h1") for level in range(3, 7)),
        pytest.param("<h1><table><svg></h1>", False, id="heading-end-tag-stops-at-a-table"),
        pytest.param("<h1><b><h2><svg></b>", True, id="heading-start-tag-closes-a-heading-below-a-b"),
        *(pytest.param(f"<{tag}><p><svg></{tag}>", True, id=f"{tag}-end-tag-keeps-a-p") for tag in _FORMATTING_TAGS),
        pytest.param("<b><i><div></b><svg></i>", True, id="b-end-tag-keeps-an-i-before-a-div"),
        pytest.param("<b><div><i><div></b><svg></i>", True, id="b-end-tag-keeps-an-i-before-the-last-div"),
        pytest.param("<b></ul><div></b><svg></b>", False, id="b-end-tag-closes-the-b"),
        pytest.param("<b><table><p><svg></b>", False, id="b-end-tag-stops-at-a-table"),
        pytest.param("<a><applet><svg></a>", False, id="a-end-tag-stops-at-an-applet"),
        pytest.param("<li><div><li><svg></div>", False, id="li-start-tag-closes-an-li-past-a-div"),
        pytest.param("<li><address><li></h2><svg></address>", False, id="li-start-tag-closes-an-li-past-an-address"),
        pytest.param("<dd><address><dt><svg></address>", False, id="dt-start-tag-closes-a-dd-past-an-address"),
        pytest.param("<dd><ul><dd><svg></ul>", True, id="dd-start-tag-stops-at-a-ul"),
        pytest.param("<dt><section><dd><svg></section>", True, id="dd-start-tag-stops-at-a-section"),
        pytest.param("<dt><h2><dd><a><svg></dt></h3>", True, id="dd-start-tag-stops-at-a-heading"),
        pytest.param("<dd><section><b><dt><a><address><svg></section>", True, id="dt-start-tag-closes-the-inner-dd"),
        pytest.param("<li><dd><svg></li>", True, id="dd-start-tag-closes-no-li"),
        pytest.param("<dd><dt><dd><svg></dt>", False, id="dd-start-tag-closes-a-dt"),
        pytest.param("<button><ul><button><svg></ul>", False, id="button-start-tag-closes-a-button-past-a-ul"),
        pytest.param("<b><table><td><caption><th><svg></td>", False, id="th-start-tag-closes-a-caption"),
        pytest.param("<table><tbody><tbody></tbody><svg></tbody>", False, id="tbody-start-tag-closes-a-tbody"),
        pytest.param("<table><tfoot><tbody><svg></tfoot>", False, id="tbody-start-tag-closes-a-tfoot"),
        pytest.param("<td><table><tr><dt><tr><svg></dt>", False, id="tr-start-tag-closes-a-tr"),
        pytest.param("<table><dl><tbody><tr><tbody><svg></tr>", False, id="tbody-start-tag-closes-a-tr"),
        pytest.param("<table><thead><tbody><svg></thead>", False, id="tbody-start-tag-closes-a-thead"),
        pytest.param("<table><dd><caption><svg><caption></dd>", False, id="dd-end-tag-stops-at-a-caption"),
        pytest.param("<table><h2><td></dl><svg><th></h2>", False, id="h2-end-tag-stops-at-a-td"),
        pytest.param("<select><h2></select><svg></h1>", False, id="select-end-tag-closes-a-heading"),
        # The current HTML standard: an end tag does not reach past an open <select>.
        pytest.param("<span><div><select></div></select><svg></span>", False, id="div-end-tag-stops-at-a-select"),
        pytest.param("<span><p><select><div></select></div><svg></span>", False, id="div-in-a-select-keeps-a-p"),
        # Only MathML's <annotation-xml> bounds the scope; SVG's is an ordinary SVG element.
        pytest.param("<span><svg><annotation-xml></span>", True, id="svg-annotation-xml-is-no-barrier"),
        # A browser inserts the <tbody> and <tr> a row or cell put straight into a table lacks; a table part ends a
        # caption.
        pytest.param("<table><tr><svg></tbody>", True, id="tr-in-a-table-gets-a-tbody"),
        pytest.param("<table><td><svg></tr>", True, id="td-in-a-table-gets-a-tr"),
        pytest.param("<table><td><svg></tbody>", True, id="td-in-a-table-gets-a-tbody"),
        pytest.param("<table><tbody><td><svg></tr>", True, id="td-in-a-tbody-gets-a-tr"),
        pytest.param("<table><div><td><svg></td>", True, id="td-start-tag-closes-a-div-in-a-table"),
        pytest.param("<table><caption><td><svg></tr>", True, id="td-start-tag-closes-a-caption"),
        pytest.param("<table><div><tbody><svg></div>", False, id="tbody-start-tag-closes-a-div-in-a-table"),
        pytest.param("<table><td><caption><svg></td>", False, id="caption-start-tag-closes-a-cell"),
        pytest.param("<table><td><caption><math></tbody>", False, id="caption-in-a-cell-gets-no-tbody"),
        pytest.param("<table><td><col><svg></td>", False, id="col-start-tag-closes-a-cell"),
        pytest.param("<table><td><colgroup><svg></td>", False, id="colgroup-start-tag-closes-a-cell"),
        pytest.param("<table><colgroup><svg></colgroup>", False, id="colgroup-is-closed-by-its-content"),
        pytest.param("<div><table><colgroup><th><svg></colgroup>", False, id="colgroup-end-tag-after-a-cell"),
        pytest.param("<table><h1><thead><caption><svg></thead>", False, id="caption-start-tag-closes-a-thead"),
        pytest.param("<table><td><select></td><svg>", False, id="cell-end-tag-ends-a-select"),
        pytest.param("<table><tr><td><select></tr><math>", False, id="row-end-tag-ends-a-select"),
        pytest.param("<table><caption><select></caption><svg>", False, id="caption-end-tag-ends-a-select"),
        pytest.param(
            "<table><td><svg><foreignObject><svg></td>", True, id="cell-end-tag-reaches-past-a-foreign-object"
        ),
        *(
            pytest.param(f"<table>{prefix}<svg></{end}>", True, id=f"th-after-{prefix}-ends-with-{end}")
            for prefix, end in (("<th>", "tbody"), ("<th>", "tr"), ("<tbody><th>", "tr"), ("<tr><th>", "tr"))
        ),
        pytest.param("<table><thead><tr><svg></thead>", True, id="thead-end-tag-ends-a-row"),
        pytest.param("<table><tfoot><tr><svg></tfoot>", True, id="tfoot-end-tag-ends-a-row"),
        pytest.param("<p><b>x<div><table><tr><td></td><td><svg></b>", False, id="b-is-not-reopened-in-a-second-cell"),
        pytest.param(
            "<p><b>x<div><table><caption>a</caption><caption><svg></b>",
            False,
            id="b-is-not-reopened-in-a-second-caption",
        ),
        # A browser reopens a formatting element an end tag closed implicitly before most start tags, an <svg> too.
        pytest.param("<b><i></b><svg></i>", True, id="i-closed-by-a-b-end-tag-is-reopened"),
        pytest.param("<h1><i><pre></h4><svg></i>", True, id="i-closed-by-a-heading-end-tag-is-reopened"),
        pytest.param("<a><button><ol><font></ol><math></annotation-xml></font>", True, id="font-is-reopened"),
        pytest.param("<b><i></b></i><svg></i>", False, id="i-end-tag-forgets-a-closed-i"),
        pytest.param("<table><td><i></td><svg></i>", False, id="i-is-not-reopened-outside-its-cell"),
        pytest.param("<template><b></template><svg></b>", False, id="b-is-not-reopened-outside-its-template"),
        pytest.param("<table><th><i></th><svg></i>", False, id="i-is-not-reopened-outside-its-th"),
        *(
            pytest.param(f"<b><{tag}><i></{tag}><svg></i>", False, id=f"i-is-not-reopened-outside-its-{tag}")
            for tag in ("applet", "marquee", "object")
        ),
        pytest.param("<b><applet></applet><i></b><svg></i>", True, id="closed-applet-leaves-no-marker"),
        pytest.param("<i><b><u></i><div><svg></b>", True, id="every-closed-formatting-element-is-reopened"),
        pytest.param("<b><svg><foreignObject><i></b><div><svg></i>", True, id="i-in-a-foreign-object-is-open"),
        pytest.param("<svg><foreignObject><b><i></b><svg></i>", True, id="i-is-reopened-in-a-foreign-object"),
        pytest.param("<a><span><a><svg></span>", False, id="a-start-tag-closes-an-a-and-a-span"),
        pytest.param("<b><i></b><table><svg></i>", True, id="table-does-not-reopen-an-i"),
        pytest.param("<b><i></b><table><tr><svg></i>", True, id="tr-is-no-marker"),
        pytest.param("<b><i></b><a><svg></i>", True, id="a-reopens-an-i"),
        pytest.param("<i><button><b><caption></button><svg></b>", True, id="button-is-no-marker"),
        pytest.param("<a>x<a><svg></a>", True, id="a-start-tag-closes-an-a"),
    ],
)
async def test_get_webio_command_range_ends_foreign_content_as_a_browser_does(
    logged_in: ComexioClient, comexio: FakeComexio, opening: str, closed: bool
) -> None:
    """Whether an end tag closes an open <svg>/<math> depends on the HTML elements open around it.

    With the SVG/MathML element closed, the min input after it is a form field; left open, the input is SVG/MathML.
    """
    comexio.serve_text(
        "GET",
        WEBIO_EDIT_COMMAND_PATH,
        f'<input id="max_cmd_io_0" value="5">{opening}<input id="min_cmd_io_0" value="0">',
    )

    assert await logged_in.get_webio_command_range(1324, 34) == ((0.0 if closed else None), 5.0)


@pytest.mark.parametrize(
    "html",
    [
        pytest.param("<svg><foreignObject><p>a<div>b</div></foreignObject>", id="p-closed-by-a-div"),
        pytest.param("<svg><foreignObject><li>a<li>b</li></foreignObject>", id="li-closed-by-an-li"),
    ],
)
async def test_get_webio_command_range_ends_a_foreign_object_after_implicitly_closed_html(
    logged_in: ComexioClient, comexio: FakeComexio, html: str
) -> None:
    """An HTML element a start tag closed implicitly does not keep the integration point open."""
    comexio.serve_text(
        "GET",
        WEBIO_EDIT_COMMAND_PATH,
        f'<input id="max_cmd_io_0" value="5">{html}<input id="min_cmd_io_0" value="0">',
    )

    assert await logged_in.get_webio_command_range(1324, 34) == (None, 5.0)


def _emulate_an_older_html_parser(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make html.parser behave as an older 3.13 release: only <script> and <style> content is raw text."""
    monkeypatch.setattr(client_module, "_HTML_PARSER_SCRIPTING", {})
    monkeypatch.setattr(HTMLParser, "RCDATA_CONTENT_ELEMENTS", (), raising=False)
    monkeypatch.setattr(HTMLParser, "CDATA_CONTENT_ELEMENTS", ("script", "style"))
    set_cdata_mode = HTMLParser.set_cdata_mode

    def set_cdata_mode_without_plaintext(self: HTMLParser, elem: str, *args: Any, **kwargs: Any) -> None:
        if elem != "plaintext":
            set_cdata_mode(self, elem, *args, **kwargs)

    monkeypatch.setattr(HTMLParser, "set_cdata_mode", set_cdata_mode_without_plaintext)
    # They read every <![ as a marked section and let no caller tell a CDATA section from a bogus comment.
    monkeypatch.delattr(HTMLParser, "_set_support_cdata", raising=False)
    parse_html_declaration = HTMLParser.parse_html_declaration

    def parse_html_declaration_as_marked_section(self: HTMLParser, i: int) -> int:
        if self.rawdata.startswith("<![", i):
            return self.parse_marked_section(i)
        return parse_html_declaration(self, i)

    monkeypatch.setattr(HTMLParser, "parse_html_declaration", parse_html_declaration_as_marked_section)


@pytest.mark.parametrize(
    ("html", "expected"),
    [
        ('<![if x]><input id="min_cmd_io_0" value="-5"><![endif]>', (-5.0, None)),
        ('<svg><![if x]></svg><input id="min_cmd_io_0" value="-5">', (-5.0, None)),
        ('<svg><![CDATA [x></svg><input id="min_cmd_io_0" value="-5">', (-5.0, None)),
        (
            '<svg><![cdata[x><input id="min_cmd_io_0" value="-5">]]></svg><input id="min_cmd_io_0" value="0">',
            (0.0, None),
        ),
        (
            '<math><![CDATA[x><input id="min_cmd_io_0" value="0">]]></math><input id="min_cmd_io_0" value="-5">',
            (-5.0, None),
        ),
        ('<svg><![CDATA[]]></svg><input id="min_cmd_io_0" value="-5">', (-5.0, None)),
        ('<input id="max_cmd_io_0" value="5"><svg><![CDATA[x><input id="min_cmd_io_0" value="-5">', (None, 5.0)),
        ('<![CDATA[x><input id="min_cmd_io_0" value="-5">', (-5.0, None)),
        (
            '<![CDATA[x><input id="min_cmd_io_0" value="-5">]]><input id="min_cmd_io_0" value="0">',
            (-5.0, None),
        ),
        (
            '<svg><![CDATA[x><div><input id="min_cmd_io_0" value="0">]]></svg><input id="min_cmd_io_0" value="-5">',
            (-5.0, None),
        ),
        ('<svg></svg><![CDATA[x><input id="min_cmd_io_0" value="-5">]]>', (-5.0, None)),
        ('<svg><foreignObject><div/><![CDATA[x><input id="min_cmd_io_0" value="-5">]]>', (-5.0, None)),
        ('<svg><foreignObject><div><![CDATA[x><input id="min_cmd_io_0" value="-5">]]></foreignObject>', (-5.0, None)),
    ],
    ids=[
        "marked-section-in-html",
        "marked-section-in-an-svg",
        "misspelt-cdata-section-in-an-svg",
        "lowercase-cdata-section-in-an-svg",
        "cdata-section-in-a-math",
        "empty-cdata-section-in-an-svg",
        "cdata-section-the-page-ends-in",
        "bogus-comment-the-page-ends-in",
        "bogus-comment-in-html",
        "cdata-section-in-an-svg",
        "bogus-comment-after-the-svg-end-tag",
        "bogus-comment-after-a-self-closing-html-tag-in-a-foreign-object",
        "bogus-comment-in-an-html-element-in-a-foreign-object",
    ],
)
@pytest.mark.parametrize("older_html_parser", [False, True], ids=["html-parser", "older-html-parser"])
async def test_get_webio_command_range_reads_cdata_only_in_foreign_content(
    logged_in: ComexioClient,
    comexio: FakeComexio,
    monkeypatch: pytest.MonkeyPatch,
    html: str,
    expected: tuple[float | None, float | None],
    older_html_parser: bool,
) -> None:
    """Outside SVG/MathML a browser reads <![CDATA[ as a bogus comment that ends at the next ">"."""
    if older_html_parser:
        _emulate_an_older_html_parser(monkeypatch)
    comexio.serve_text("GET", WEBIO_EDIT_COMMAND_PATH, html)

    assert await logged_in.get_webio_command_range(1324, 34) == expected


@pytest.mark.parametrize(
    ("html", "expected"),
    [
        # Every raw-text and escapable raw-text element, written out so that a slash after a new one is checked too.
        *(
            (f'<{tag}/><input id="min_cmd_io_0" value="0"></{tag}><input id="min_cmd_io_0" value="-5">', (-5.0, None))
            for tag in ("script", "style", "xmp", "iframe", "noembed", "noframes", "noscript", "textarea", "title")
        ),
        ('<script/><textarea></script><input id="min_cmd_io_0" value="-5">', (-5.0, None)),
        ('<svg><style/></svg><input id="min_cmd_io_0" value="-5">', (-5.0, None)),
        ('<select><style/><input id="min_cmd_io_0" value="-5"></select>', (-5.0, None)),
        ('<textarea><style></textarea><input id="min_cmd_io_0" value="-5">', (-5.0, None)),
        ('<textarea><style/></textarea><input id="min_cmd_io_0" value="-5">', (-5.0, None)),
    ],
    ids=[
        *(
            f"{tag}-with-a-slash"
            for tag in ("script", "style", "xmp", "iframe", "noembed", "noframes", "noscript", "textarea", "title")
        ),
        "script-with-a-slash-ends-at-its-end-tag",
        "svg-style-with-a-slash-is-closed",
        "style-with-a-slash-in-a-select-is-ignored",
        "style-in-a-textarea-is-text",
        "style-with-a-slash-in-a-textarea-is-text",
    ],
)
@pytest.mark.parametrize("older_html_parser", [False, True], ids=["html-parser", "older-html-parser"])
async def test_get_webio_command_range_reads_a_text_element_with_a_slash_as_text(
    logged_in: ComexioClient,
    comexio: FakeComexio,
    monkeypatch: pytest.MonkeyPatch,
    html: str,
    expected: tuple[float | None, float | None],
    older_html_parser: bool,
) -> None:
    """A browser ignores the slash of an HTML start tag: what follows <style/> is its text up to </style>."""
    if older_html_parser:
        _emulate_an_older_html_parser(monkeypatch)
    comexio.serve_text("GET", WEBIO_EDIT_COMMAND_PATH, html)

    assert await logged_in.get_webio_command_range(1324, 34) == expected


@pytest.mark.parametrize("tag", ["style", "xmp", "iframe", "noembed", "noframes"])
@pytest.mark.parametrize("slash", ["", "/"], ids=["start-tag", "start-tag-with-a-slash"])
async def test_get_webio_command_range_ends_a_raw_text_element_at_its_end_tag_inside_a_comment(
    logged_in: ComexioClient, comexio: FakeComexio, tag: str, slash: str
) -> None:
    """Raw text knows no comment: the first </xmp> ends an <xmp>, with or without a slash on its start tag."""
    comexio.serve_text(
        "GET",
        WEBIO_EDIT_COMMAND_PATH,
        f'<{tag}{slash}><!-- </{tag}> --><input id="min_cmd_io_0" value="-5">'
        f'</{tag}><input id="min_cmd_io_0" value="0">',
    )

    assert await logged_in.get_webio_command_range(1324, 34) == (-5.0, None)


# Pages that keep `depth` elements open (or listed as active formatting ones) when the field after them comes.
_NESTED_PAGES: dict[str, Callable[[int], str]] = {
    "html": lambda depth: "<div>" * depth,
    "formatting": lambda depth: "<b>" * depth,
    "foreign": lambda depth: "<svg>" + "<g>" * (depth - 1) + "</svg>",
    "integration-point": lambda depth: "<svg><foreignObject>" + "<div>" * (depth - 2),
    "template": lambda depth: "<template>" * depth + "</template>" * depth,
}
_FIELD = '<input id="min_cmd_io_0" value="-5">'


@pytest.mark.parametrize("nesting", list(_NESTED_PAGES))
async def test_get_webio_command_range_reads_a_page_nested_up_to_the_limit(
    logged_in: ComexioClient, comexio: FakeComexio, nesting: str
) -> None:
    page = _NESTED_PAGES[nesting](client_module._MAX_OPEN_ELEMENTS) + _FIELD
    comexio.serve_text("GET", WEBIO_EDIT_COMMAND_PATH, page)

    assert await logged_in.get_webio_command_range(1324, 34) == (-5.0, None)


@pytest.mark.parametrize("depth", [client_module._MAX_OPEN_ELEMENTS + 1, 16000], ids=["limit-plus-one", "16000"])
@pytest.mark.parametrize("nesting", list(_NESTED_PAGES))
async def test_get_webio_command_range_rejects_a_page_nested_past_the_limit_at_once(
    logged_in: ComexioClient, comexio: FakeComexio, nesting: str, depth: int
) -> None:
    """Each tag searches the open elements: past the limit the parser stops instead of taking quadratic time (16000
    nested <b> took 6.5 s, blocking the event loop)."""
    comexio.serve_text("GET", WEBIO_EDIT_COMMAND_PATH, _NESTED_PAGES[nesting](depth) + _FIELD)

    started = time.perf_counter()
    with pytest.raises(ComexioDataError, match=f"nests more than {client_module._MAX_OPEN_ELEMENTS} elements"):
        await logged_in.get_webio_command_range(1324, 34)
    assert time.perf_counter() - started < 1


@pytest.mark.parametrize(
    "repeated",
    ["<p><b></p>", '<p><b class="x" id="y"></p><p><b id="y" class="x"></p>', "<div><i></div>", "<p><b></p><p><i></p>"],
)
async def test_get_webio_command_range_reopens_at_most_three_equal_formatting_elements(
    logged_in: ComexioClient, comexio: FakeComexio, repeated: str
) -> None:
    """A browser lists no more than three equal formatting elements (same tag and attributes, in any order) past the
    last marker, so an unclosed <b> in every paragraph does not nest the page deeper with each one."""
    comexio.serve_text("GET", WEBIO_EDIT_COMMAND_PATH, repeated * 10_000 + _FIELD)

    started = time.perf_counter()
    assert await logged_in.get_webio_command_range(1324, 34) == (-5.0, None)
    assert time.perf_counter() - started < 1


async def test_get_webio_command_range_rejects_many_different_implicitly_closed_formatting_elements(
    logged_in: ComexioClient, comexio: FakeComexio
) -> None:
    """Formatting elements with different attributes are all reopened, as a browser does, up to the limit."""
    page = "".join(f'<p><b id="{index}"></p>' for index in range(client_module._MAX_OPEN_ELEMENTS)) + _FIELD
    comexio.serve_text("GET", WEBIO_EDIT_COMMAND_PATH, page)

    with pytest.raises(ComexioDataError, match=f"nests more than {client_module._MAX_OPEN_ELEMENTS} elements"):
        await logged_in.get_webio_command_range(1324, 34)


@pytest.mark.parametrize(
    ("nested", "repeated"),
    [
        pytest.param("<b>" * 254, "<i></i>", id="formatting-under-formatting"),
        pytest.param("<b>" + "<div>" * 253, "<x></x>", id="formatting-under-blocks"),
        pytest.param("<p><object>" + "<div>" * 252, "<div></div>", id="marker-under-blocks"),
        pytest.param("<span>" * 254, "</i>", id="end-tag-past-the-open-elements"),
        pytest.param("<svg>" + "<g>" * 253, "<g/>", id="foreign"),
    ],
)
async def test_get_webio_command_range_reads_a_page_nested_near_the_limit_in_linear_time(
    logged_in: ComexioClient, comexio: FakeComexio, nested: str, repeated: str
) -> None:
    """Tag after tag at nearly the most open elements: about 50 KB parse in about 0.1 s, not seconds."""
    closed = "</svg>" if nested.startswith("<svg>") else ""
    comexio.serve_text("GET", WEBIO_EDIT_COMMAND_PATH, nested + repeated * (50_000 // len(repeated)) + closed + _FIELD)

    started = time.perf_counter()
    assert await logged_in.get_webio_command_range(1324, 34) == (-5.0, None)
    assert time.perf_counter() - started < 1


@pytest.mark.skipif(not client_module._HTML_PARSER_SCRIPTING, reason="html.parser cannot read a <noscript> as raw text")
async def test_get_webio_command_range_reads_a_noscript_as_raw_text(
    logged_in: ComexioClient, comexio: FakeComexio
) -> None:
    """A browser runs scripts, so a <noscript> is raw text: its end tag ends it even inside a comment-like text."""
    comexio.serve_text(
        "GET", WEBIO_EDIT_COMMAND_PATH, '<noscript><!--</noscript><input id="min_cmd_io_0" value="-5">-->'
    )

    assert await logged_in.get_webio_command_range(1324, 34) == (-5.0, None)


@pytest.mark.parametrize(
    "tag",
    # The breakout start tags of the HTML standard, written out so that the list in client.py is checked.
    [
        *("b", "big", "blockquote", "body", "br", "center", "code", "dd", "div", "dl", "dt", "em", "embed", "h1"),
        *("h2", "h3", "h4", "h5", "h6", "head", "hr", "i", "img", "li", "listing", "menu", "meta", "nobr", "ol", "p"),
        *("pre", "ruby", "s", "small", "span", "strong", "strike", "sub", "sup", "table", "tt", "u", "ul", "var"),
    ],
)
async def test_get_webio_command_range_reads_html_after_a_breakout_tag(
    logged_in: ComexioClient, comexio: FakeComexio, tag: str
) -> None:
    comexio.serve_text("GET", WEBIO_EDIT_COMMAND_PATH, f'<svg><g><{tag}><input id="min_cmd_io_0" value="-5">')

    assert await logged_in.get_webio_command_range(1324, 34) == (-5.0, None)


@pytest.mark.parametrize(
    "opening",
    [
        "<textarea>",
        "<title>",
        "<textarea/>",
        "<TEXTAREA>",
        "<xmp>",
        "<iframe>",
        "<noembed>",
        "<noframes>",
        "<noscript>",
    ],
    ids=["textarea", "title", "slash", "upper", "xmp", "iframe", "noembed", "noframes", "noscript"],
)
async def test_get_webio_command_form_reads_text_only_content_as_text_on_an_older_html_parser(
    logged_in: ComexioClient, comexio: FakeComexio, monkeypatch: pytest.MonkeyPatch, opening: str
) -> None:
    """Older 3.13 releases of html.parser report markup inside a <textarea>, <title> or raw-text element as tags.

    Another end tag inside it (here </b> and </select>) does not end it, only its own does.
    """
    _emulate_an_older_html_parser(monkeypatch)
    closing = "</" + opening.strip("</>").lower() + ">"
    comexio.serve_text(
        "GET",
        WEBIO_EDIT_COMMAND_PATH,
        f'{opening}</b><select id="protocol"><option value="5" selected></select><input id="min_cmd_io_0" value="0">'
        f'{closing}<input id="min_cmd_io_0" value="-5"><select id="protocol"><option value="1" selected></select>',
    )

    assert await logged_in.get_webio_command_form(1324, 34) == WebioCommandForm(min=-5.0, max=None, protocol=1)


async def test_get_webio_command_form_reads_the_rest_after_a_plaintext_as_text_on_an_older_html_parser(
    logged_in: ComexioClient, comexio: FakeComexio, monkeypatch: pytest.MonkeyPatch
) -> None:
    _emulate_an_older_html_parser(monkeypatch)
    comexio.serve_text(
        "GET",
        WEBIO_EDIT_COMMAND_PATH,
        '<input id="max_cmd_io_0" value="5"><plaintext></plaintext><input id="min_cmd_io_0" value="-5">'
        '<select id="protocol"><option value="1" selected></select>',
    )

    assert await logged_in.get_webio_command_form(1324, 34) == WebioCommandForm(min=None, max=5.0, protocol=None)


@pytest.mark.parametrize("older_parser", [False, True], ids=["this-parser", "older-parser"])
@pytest.mark.parametrize("tag", ["title", "xmp", "plaintext", "style"])
async def test_get_webio_command_form_ignores_a_text_only_tag_in_the_select(
    logged_in: ComexioClient, comexio: FakeComexio, monkeypatch: pytest.MonkeyPatch, tag: str, older_parser: bool
) -> None:
    """Inside an open select a browser ignores the tag, so neither the options nor the inputs after it are text."""
    if older_parser:
        _emulate_an_older_html_parser(monkeypatch)
    comexio.serve_text(
        "GET",
        WEBIO_EDIT_COMMAND_PATH,
        f'<select id="protocol"><option value="1"><{tag}><option value="5" selected></{tag}></select>{_RANGE_FIELDS}',
    )

    assert await logged_in.get_webio_command_form(1324, 34) == WebioCommandForm(min=0.0, max=1.0, protocol=5)


@pytest.mark.parametrize(
    ("tag", "value"),
    [(f'<input id="min_cmd_io_0" value="{text}">', text) for text in ("inf", "-inf", "nan", "1e999")]
    + [('<input id="min_cmd_io_0" value>', "")],  # a bare value is "", as for value=""
)
async def test_get_webio_command_range_rejects_a_value_that_is_not_finite(
    logged_in: ComexioClient, comexio: FakeComexio, caplog: pytest.LogCaptureFixture, tag: str, value: str
) -> None:
    comexio.serve_text("GET", WEBIO_EDIT_COMMAND_PATH, tag)

    with caplog.at_level(logging.WARNING, logger="aiocomexio.client"):
        assert await logged_in.get_webio_command_range(1324, 34) == (None, None)

    (warning,) = caplog.records
    assert warning.getMessage() == f"Web-IO command 1324 has a non-numeric min value: {value!r}"


async def test_get_webio_command_range_caps_a_long_non_numeric_value_in_the_warning(
    logged_in: ComexioClient, comexio: FakeComexio, caplog: pytest.LogCaptureFixture
) -> None:
    value = "a" * 1000
    comexio.serve_text("GET", WEBIO_EDIT_COMMAND_PATH, f'<input id="min_cmd_io_0" value="{value}">')

    with caplog.at_level(logging.WARNING, logger="aiocomexio.client"):
        assert await logged_in.get_webio_command_range(1324, 34) == (None, None)

    (warning,) = caplog.records
    assert warning.getMessage() == f"Web-IO command 1324 has a non-numeric min value: {repr(value)[:200]}..."


@pytest.mark.parametrize(
    ("page", "error"), [("<form></form>", ComexioDataError), (LOGIN_PAGE, ComexioAuthenticationError)]
)
async def test_get_webio_command_range_without_fields_raises(
    logged_in: ComexioClient, comexio: FakeComexio, page: str, error: type[Exception]
) -> None:
    comexio.serve_text("GET", WEBIO_EDIT_COMMAND_PATH, page)

    with pytest.raises(error):
        await logged_in.get_webio_command_range(1324, 34)


_RANGE_FIELDS = '<input id="min_cmd_io_0" value="0"><input id="max_cmd_io_0" value="1">'


@pytest.mark.parametrize(
    ("select", "protocol"),
    [
        pytest.param(
            '<select id="protocol"><option value="0">HTTP/1.1</option>'
            '<option value="1" selected="selected">HTTPS/1.1</option></select>',
            1,
            id="selected-option",
        ),
        pytest.param(
            '<SELECT class="x" ID="protocol"><option value="5">HTTPS/2</option>'
            '<option value="0">HTTP/1.1</option></SELECT>',
            5,
            id="none-selected-takes-first",
        ),
        pytest.param(
            '<select id="protocol"><option value="0" class="not-selected" data-selected="1">HTTP/1.1</option>'
            '<option value="1" selected>HTTPS/1.1</option></select>',
            1,
            id="selected-only-as-attribute",
        ),
        pytest.param(
            "<select name='p' id='protocol'><option data-value=9 value=4 selected>HTTP/2</option></select>",
            4,
            id="single-and-unquoted-attributes",
        ),
        pytest.param(
            '<select id = "protocol"><option value = "0"/><option value = 5 selected/></select>',
            5,
            id="spaces-around-equals-and-self-closing",
        ),
        pytest.param(
            '<select id="protocol"><option value="&#53;" value="0" selected>HTTPS/2</option></select>',
            5,
            id="entity-resolved-and-first-duplicate-wins",
        ),
        pytest.param(
            '<select title="a>b" id="protocol"><option value="1">HTTPS/1.1</option>'
            '<OPTION VALUE="5" title="x>y" SELECTED>HTTPS/2</OPTION></select>',
            5,
            id="quoted-gt-and-upper-case-option",
        ),
        pytest.param(
            '<select id="protocol"><option value="1" selected></select>'
            '<select id="protocol"><option value="5" selected></select>',
            1,
            id="first-protocol-select-only",
        ),
        pytest.param(
            '<select id="protocol"><option value="1"><option value="4"></select>'
            '<select id="x"><option value="7" selected></select><option value="9" selected>',
            1,
            id="options-after-the-select-do-not-count",
        ),
        pytest.param(
            '<!-- <select id="protocol"><option value="5" selected></select> -->'
            '<select id="protocol"><option value="1" selected></select>',
            1,
            id="commented-out-select",
        ),
        pytest.param(
            '<select id="protocol"><option value="0">HTTP/1.1</option><option value="5" selected',
            None,
            id="cut-off-answer",
        ),
        pytest.param('<select id="protocol"></select><option value="3" selected>', None, id="option-after-empty"),
        pytest.param(
            '<select id="protocol-extra"><option value="5" selected>HTTPS/2</option></select>'
            '<select id=protocol><option value="1" selected>HTTPS/1.1</option></select>',
            1,
            id="longer-id-is-another-select",
        ),
        pytest.param(
            '<select id="protocol"><option value="0" selected>HTTP/1.1</option>'
            '<option value="4">HTTP/2</option><option value="1" selected>HTTPS/1.1</option></select>',
            1,
            id="several-selected-takes-last",
        ),
        pytest.param('<select data-id="protocol"><option value="1" selected></option></select>', None, id="data-id"),
        pytest.param('<select id="protocol-extra"><option value="1" selected></option></select>', None, id="prefix-id"),
        pytest.param("", None, id="no-select"),
        pytest.param('<select id="protocol"></select>', None, id="no-options"),
        pytest.param(
            '<textarea><select id="protocol"><option value="5" selected></select></textarea>'
            '<select id="protocol"><option value="1" selected></select>',
            1,
            id="select-written-into-a-textarea",
        ),
        pytest.param('<select id="protocol"><option selected>\n 5 </option></select>', 5, id="text-as-value"),
        pytest.param(
            '<select id="protocol"><option>1</option><option>5</option></select>', 1, id="text-of-first-option"
        ),
        pytest.param(
            '<select id="protocol" multiple><option value="1" selected><option value="5" selected></select>',
            None,
            id="multiple-select",
        ),
        pytest.param('<select id="protocol" multiple><option value="1"></select>', None, id="multiple-none-selected"),
        pytest.param(
            '<select id="protocol"><option value="1" selected><select id="x"><option value="5" selected>',
            1,
            id="nested-select-ends-it",
        ),
        pytest.param(
            '<select id="a"><option>1<select id="protocol"><option value="5" selected></select>',
            None,
            id="protocol-select-inside-another-select-is-ignored",
        ),
        pytest.param('<select id="protocol"><option>1 5</option></select>', None, id="inner-whitespace-is-kept"),
        pytest.param('<select id="protocol"><option>&nbsp;5</option></select>', None, id="nbsp-is-no-whitespace"),
        pytest.param('<select id="protocol"><option>1<!-- x -->5</option></select>', 15, id="text-in-parts"),
        pytest.param('<select id="protocol"><option>1</option>x</select>', 1, id="text-after-the-option"),
        pytest.param('<select id="protocol"><option>1<optgroup label=x>2</select>', 1, id="optgroup-ends-the-option"),
        pytest.param('<select id="protocol"><option>1<hr>2</select>', 1, id="hr-ends-the-option"),
        pytest.param('<select id="protocol"><option>1</select>x', 1, id="text-after-the-select"),
        # An <input> or <textarea> start tag ends an open select; a later option is outside it.
        pytest.param(
            '<select id="protocol"><option value="1"><textarea></textarea><option value="5" selected></select>',
            1,
            id="textarea-ends-the-select",
        ),
        pytest.param(
            '<select id="protocol"><option value="1"><input><option value="5" selected></select>',
            1,
            id="input-ends-the-select",
        ),
        pytest.param(
            '<select id="protocol"><option value="1"><keygen><option value="5" selected></select>',
            1,
            id="keygen-ends-the-select",
        ),
        pytest.param(
            '<select id="protocol"><option value="1"><template></script><option value="5" selected></template>'
            "</select>",
            1,
            id="only-its-own-end-tag-ends-a-template",
        ),
        # Inside an open select a browser ignores another text-only element or a <style>: what follows is markup.
        *(
            pytest.param(
                f'<select id="protocol"><option value="1"><{tag}><option value="5" selected></{tag}></select>',
                5,
                id=f"{tag}-in-the-select-is-ignored",
            )
            for tag in ("title", "xmp", "iframe", "noembed", "noframes", "style", "svg", "math")
        ),
        pytest.param(
            '<svg><select id="protocol"><option value="5" selected></select></svg>'
            '<select id="protocol"><option value="1" selected></select>',
            1,
            id="select-in-an-svg",
        ),
        pytest.param(
            '<svg><foreignObject><select id="protocol"><option value="5" selected></select></foreignObject></svg>',
            5,
            id="select-in-a-foreign-object",
        ),
        pytest.param(
            '<svg><foreignObject><div><svg></div><select id="protocol"><option value="5" selected></select>'
            '</foreignObject></svg><select id="protocol"><option value="1" selected></select>',
            5,
            id="select-after-an-end-tag-that-ends-the-inner-svg",
        ),
        pytest.param(
            '<table><tr><td><select id="protocol"><option value="1"></td><option value="2" selected></select></table>',
            1,
            id="cell-end-tag-ends-the-select",
        ),
        pytest.param(
            '<select id="protocol"><option value="1"><plaintext><option value="5" selected></select>',
            5,
            id="plaintext-in-the-select-is-ignored",
        ),
        pytest.param(
            '<select id="protocol"><option value="1"><script><option value="5" selected></script></select>',
            1,
            id="script-in-the-select-stays-source",
        ),
        pytest.param('<select id="protocol"><option value="0000000001" selected></select>', 1, id="leading-zeros"),
        pytest.param(
            '<select id="protocol"><option value="' + "0" * 5000 + '2" selected></select>', 2, id="many-leading-zeros"
        ),
        pytest.param('<select id="protocol"><option value="0000000000" selected></select>', 0, id="only-zeros"),
        # An option's text leaves out script source but keeps a title's text; </hr> and a stray </optgroup> are ignored.
        pytest.param(
            '<select id="protocol"><option>1<script>5</script></option></select>', 1, id="script-in-the-option"
        ),
        pytest.param(
            '<select id="protocol"><option>1<script>5</script>7</option></select>', 17, id="text-after-the-script"
        ),
        pytest.param(
            '<select id="protocol"><option>1<template>5</template></option></select>', 1, id="template-in-the-option"
        ),
        pytest.param(
            '<select id="protocol"><option value="1"><template><option value="5" selected></template></select>',
            1,
            id="option-in-a-template",
        ),
        pytest.param(
            '<template><select id="protocol"><option value="5" selected></select></template>'
            '<select id="protocol"><option value="1" selected></select>',
            1,
            id="select-in-a-template",
        ),
        # A browser ignores the slash of <script/>: the rest of the page is its source.
        pytest.param(
            '<script/><select id="protocol"><option value="1" selected></select>', None, id="self-closing-script"
        ),
        pytest.param('<select id="protocol"><option>1<title>5</title></option></select>', 15, id="title-in-the-option"),
        pytest.param('<select id="protocol"><option>1</hr>5</option></select>', 15, id="hr-end-tag-is-ignored"),
        pytest.param('<select id="protocol"><option>1</optgroup>5</option></select>', 15, id="stray-optgroup-end-tag"),
        pytest.param(
            '<select id="protocol"><optgroup><option>1</optgroup>5</select>', 1, id="optgroup-end-tag-ends-the-option"
        ),
        # Without a selected option a browser shows the first enabled one; a list box shows none.
        pytest.param(
            '<select id="protocol"><option value="0" disabled><option value="1"></select>', 1, id="skips-disabled"
        ),
        pytest.param(
            '<select id="protocol"><optgroup disabled><option value="0"></optgroup><option value="1"></select>',
            1,
            id="skips-a-disabled-group",
        ),
        pytest.param(
            '<select id="protocol"><optgroup disabled><option value="0"><optgroup><option value="1"></select>',
            1,
            id="next-group-ends-the-disabled-one",
        ),
        pytest.param(
            '<select id="protocol"><optgroup disabled><option value="0"><hr><option value="1"></select>',
            1,
            id="hr-ends-the-disabled-group",
        ),
        pytest.param(
            '<optgroup disabled><select id="protocol"><option value="0"><option value="1"></select>',
            0,
            id="group-outside-the-select",
        ),
        pytest.param(
            '<select><optgroup disabled></select><select id="protocol"><option value="0"></select>',
            0,
            id="group-of-another-select",
        ),
        pytest.param('<select id="protocol"><option value="0" disabled></select>', None, id="only-disabled"),
        pytest.param(
            '<select id="protocol"><option value="0" disabled selected><option value="1"></select>',
            0,
            id="selected-disabled-counts",
        ),
        pytest.param('<select id="protocol" size="2"><option value="1"></select>', None, id="list-box"),
        pytest.param('<select id="protocol" size=" +3rows"><option value="1"></select>', None, id="list-box-size-text"),
        pytest.param(
            '<select id="protocol" size="2"><option value="1" selected></select>', 1, id="list-box-with-selected"
        ),
        pytest.param('<select id="protocol" size="1"><option value="1"></select>', 1, id="size-one"),
        pytest.param('<select id="protocol" size="0"><option value="1"></select>', 1, id="size-zero"),
        pytest.param('<select id="protocol" size="001"><option value="1"></select>', 1, id="size-zero-padded-one"),
        pytest.param('<select id="protocol" size="x"><option value="1"></select>', 1, id="size-invalid"),
        pytest.param('<select id="protocol" size="-2"><option value="1"></select>', 1, id="size-negative"),
        # A browser ignores the slash of a start tag: <select/> and <option/> open their element.
        pytest.param('<select id="protocol"/><option value="1"></select>', 1, id="self-closing-select-opens-it"),
        pytest.param('<select id="protocol"><option/>1</select>', 1, id="self-closing-option-keeps-its-text"),
    ],
)
async def test_get_webio_command_form_reads_protocol(
    logged_in: ComexioClient, comexio: FakeComexio, select: str, protocol: int | None
) -> None:
    other = '<select id="post_get"><option value="7" selected>POST</option></select>'
    comexio.serve_text("GET", WEBIO_EDIT_COMMAND_PATH, other + _RANGE_FIELDS + select)

    form = await logged_in.get_webio_command_form(1324, 34)

    assert form == WebioCommandForm(min=0.0, max=1.0, protocol=protocol)
    (request,) = comexio.received_at("GET", WEBIO_EDIT_COMMAND_PATH)
    assert request.query == {"Id": "1324", "TestDevice": "34"}


@pytest.mark.parametrize(
    ("option", "shown"),
    [
        ('<option value="https" selected>', "'https'"),
        ('<option value="1_0" selected>', "'1_0'"),
        ('<option value="٣" selected>', "'٣'"),
        ('<option value="-1" selected>', "'-1'"),
        ('<option value="" selected>', "''"),
        ("<option selected>", "'HTTPS'"),  # without a value attribute the option's text is its value
        ("<option value=1=2 selected>", "'1=2'"),  # an unquoted value keeps its "="
        # The last selected option counts, even when an earlier one has a number.
        ('<option value="0" selected>HTTP</option><option value="x" selected>', "'x'"),
        ('<option value="1234567890" selected>', "'1234567890'"),  # no protocol is that large
        # int() refuses more than 4300 digits; the value is shown capped.
        ('<option value="' + "9" * 5000 + '" selected>', "'" + "9" * 199 + "..."),
    ],
)
async def test_get_webio_command_form_non_numeric_protocol_warns(
    logged_in: ComexioClient, comexio: FakeComexio, caplog: pytest.LogCaptureFixture, option: str, shown: str
) -> None:
    select = f'<select id="protocol">{option}HTTPS</option></select>'
    comexio.serve_text("GET", WEBIO_EDIT_COMMAND_PATH, _RANGE_FIELDS + select)

    with caplog.at_level(logging.WARNING, logger="aiocomexio.client"):
        form = await logged_in.get_webio_command_form(1324, 34)

    assert form == WebioCommandForm(min=0.0, max=1.0, protocol=None)
    assert [(r.levelno, r.getMessage()) for r in caplog.records] == [
        (logging.WARNING, f"Web-IO command 1324 has an unknown protocol value: {shown}")
    ]


@pytest.mark.parametrize(
    ("select", "logged"),
    [
        ("", ["Web-IO command 1324 edit form has no protocol select"]),
        ('<select id="protocol"></select>', ["Web-IO command 1324 protocol select has no option"]),
        (
            '<select id="protocol" size="2"><option value="1"></select>',
            ["Web-IO command 1324 protocol select is a list box without a selected option"],
        ),
        (
            '<select id="protocol"><option value="1" disabled></select>',
            ["Web-IO command 1324 protocol select has no enabled option"],
        ),
        ('<select id="protocol"><option value="1" selected>', ["Web-IO command 1324 protocol select is not closed"]),
        (
            '<select id="protocol"><option value="0" selected><option value="1" selected></select>',
            ["Web-IO command 1324 protocol select has 2 selected options, the last counts"],
        ),
        ('<select id="protocol"><option value="0"><option value="1" selected></select>', []),
    ],
)
async def test_get_webio_command_form_protocol_debug_log(
    logged_in: ComexioClient, comexio: FakeComexio, caplog: pytest.LogCaptureFixture, select: str, logged: list[str]
) -> None:
    comexio.serve_text("GET", WEBIO_EDIT_COMMAND_PATH, _RANGE_FIELDS + select)

    with caplog.at_level(logging.DEBUG, logger="aiocomexio.client"):
        await logged_in.get_webio_command_form(1324, 34)

    messages = [r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG and "protocol" in r.getMessage()]
    assert messages == logged


# --- markers and KNX objects ------------------------------------------------------------------------


@pytest.mark.parametrize(("binary", "marker_type"), [(True, "1"), (False, "2")])
async def test_create_marker(logged_in: ComexioClient, comexio: FakeComexio, binary: bool, marker_type: str) -> None:
    comexio.serve_json("POST", MARKER_ADD_PATH, {"ok": True, "saved": "271"})

    assert await logged_in.create_marker(binary=binary) == 271
    (request,) = comexio.received_at("POST", MARKER_ADD_PATH)
    assert request.form == {"type": marker_type}


@pytest.mark.parametrize(
    ("answer", "error"),
    [
        ({"ok": False}, ComexioRequestRejectedError),
        ({"ok": True}, ComexioDataError),
        ("x", ComexioDataError),
        ({"ok": True, "saved": math.inf}, ComexioDataError),  # int() raises OverflowError, not ValueError
    ],
)
async def test_create_marker_without_id_raises(
    logged_in: ComexioClient, comexio: FakeComexio, answer: Any, error: type[Exception]
) -> None:
    comexio.serve_json("POST", MARKER_ADD_PATH, answer)

    with pytest.raises(error):
        await logged_in.create_marker(binary=True)


async def test_rename_marker(logged_in: ComexioClient, comexio: FakeComexio) -> None:
    comexio.serve_json("POST", UNIQUE_CHECK_PATH, {"result": True})
    comexio.serve_json("POST", MARKER_SAVE_PATH, {"saved": 271})

    await logged_in.rename_marker(271, "Licht [K5]", binary=False)

    (check,) = comexio.received_at("POST", UNIQUE_CHECK_PATH)
    assert check.form == {"model": "memory", "field": "name", "value": "Licht [K5]", "id": "271"}
    (save,) = comexio.received_at("POST", MARKER_SAVE_PATH)
    assert (save.form["name"], save.form["type"], save.form["default_type"], save.form["value_271"]) == (
        "Licht [K5]",
        "2",
        "2",
        "0",
    )
    # Without explicit values the marker gets Comexio's defaults: default value 0, not stored.
    assert (save.form["default"], save.form["default_default"], save.form["store_memory"]) == ("0", "0", "0")


@pytest.mark.parametrize(
    ("binary", "default", "store_memory", "expected"),
    [
        (True, 1, True, ("1", "1", "1", "1")),
        (False, 20, True, ("2", "20", "20", "1")),
        (False, 20.0, False, ("2", "20", "20", "0")),
        (False, 2.5, True, ("2", "2.5", "2.5", "1")),
        (False, -3, False, ("2", "-3", "-3", "0")),
        (False, 1e-05, False, ("2", "0.00001", "0.00001", "0")),
        (False, -0.0, False, ("2", "0", "0", "0")),
    ],
)
async def test_rename_marker_keeps_the_given_default_and_store_flag(
    logged_in: ComexioClient,
    comexio: FakeComexio,
    binary: bool,
    default: float,
    store_memory: bool,
    expected: tuple[str, ...],
) -> None:
    """The save takes the whole form: a marker in use keeps its values only if they are sent along."""
    comexio.serve_json("POST", UNIQUE_CHECK_PATH, {"result": True})
    comexio.serve_json("POST", MARKER_SAVE_PATH, {"saved": 271})

    await logged_in.rename_marker(271, "Licht", binary=binary, default=default, store_memory=store_memory)

    (save,) = comexio.received_at("POST", MARKER_SAVE_PATH)
    form = save.form
    sent = (form["type"], form["default"], form["default_default"], form["store_memory"])
    assert sent == expected
    # The client always sends value_<id>=0; Comexio ignores it on a save (live-verified 2026-10-10).
    assert form["value_271"] == "0"


@pytest.mark.parametrize(
    ("default", "store_memory", "error"),
    [
        (True, False, TypeError),
        (None, False, TypeError),
        ("20", False, TypeError),
        (float("nan"), False, ValueError),
        (float("inf"), False, ValueError),
        (0, 1, TypeError),
        (0, "0", TypeError),
        (0, None, TypeError),
    ],
)
async def test_rename_marker_rejects_values_it_cannot_send_faithfully(
    logged_in: ComexioClient, comexio: FakeComexio, default: Any, store_memory: Any, error: type[Exception]
) -> None:
    """A value that would silently overwrite the marker's setting is refused before anything is sent."""
    with pytest.raises(error):
        await logged_in.rename_marker(271, "Licht", binary=True, default=default, store_memory=store_memory)
    assert comexio.received_at("POST", UNIQUE_CHECK_PATH) == []
    assert comexio.received_at("POST", MARKER_SAVE_PATH) == []


async def test_rename_marker_name_taken_is_rejected_before_saving(
    logged_in: ComexioClient, comexio: FakeComexio
) -> None:
    comexio.serve_json("POST", UNIQUE_CHECK_PATH, {"result": False})

    with pytest.raises(ComexioRequestRejectedError, match=r"already in use \(.*result.*\)"):
        await logged_in.rename_marker(271, "Licht", binary=True)
    assert comexio.received_at("POST", MARKER_SAVE_PATH) == []


async def test_rename_marker_name_check_without_verdict_is_a_data_error(
    logged_in: ComexioClient, comexio: FakeComexio
) -> None:
    comexio.serve_json("POST", UNIQUE_CHECK_PATH, {"error": "unknown model"})

    with pytest.raises(ComexioDataError, match="no result"):
        await logged_in.rename_marker(271, "Licht", binary=True)
    assert comexio.received_at("POST", MARKER_SAVE_PATH) == []


async def test_rename_marker_unconfirmed_save_raises(logged_in: ComexioClient, comexio: FakeComexio) -> None:
    comexio.serve_json("POST", UNIQUE_CHECK_PATH, {"result": True})
    comexio.serve_json("POST", MARKER_SAVE_PATH, {"saved": 0})

    with pytest.raises(ComexioRequestRejectedError, match="not confirmed"):
        await logged_in.rename_marker(271, "Licht", binary=True)


async def test_rename_knx_object(logged_in: ComexioClient, comexio: FakeComexio) -> None:
    comexio.serve_json("POST", UNIQUE_CHECK_PATH, {"result": 1})
    comexio.serve_json("POST", KNX_SAVE_PATH, {"Ok": 1})

    await logged_in.rename_knx_object(5, "Licht [RO]")

    (check,) = comexio.received_at("POST", UNIQUE_CHECK_PATH)
    assert check.form["model"] == "oneWire"
    (save,) = comexio.received_at("POST", KNX_SAVE_PATH)
    assert save.form == {"id": "5", "field": "name", "value": "Licht [RO]"}


@pytest.mark.parametrize(("check", "save"), [({"result": 0}, {"Ok": 1}), ({"result": 1}, {"Ok": 0})])
async def test_rename_knx_object_refusal_raises(
    logged_in: ComexioClient, comexio: FakeComexio, check: dict[str, int], save: dict[str, int]
) -> None:
    comexio.serve_json("POST", UNIQUE_CHECK_PATH, check)
    comexio.serve_json("POST", KNX_SAVE_PATH, save)

    with pytest.raises(ComexioRequestRejectedError):
        await logged_in.rename_knx_object(5, "Licht [RO]")


@pytest.mark.parametrize(
    ("answer", "deleted"), [({"result": "1"}, True), ({"result": 1}, True), ({"result": "0"}, False)]
)
async def test_delete_marker(
    logged_in: ComexioClient, comexio: FakeComexio, answer: dict[str, Any], deleted: bool
) -> None:
    comexio.serve_json("POST", DELETE_ELEMENT_PATH, answer)

    assert await logged_in.delete_marker(271) is deleted
    (request,) = comexio.received_at("POST", DELETE_ELEMENT_PATH)
    assert (request.form["elementId"], request.form["type"], request.form["full"]) == ("271", "2", "true")
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", request.form["timestamp"])


@pytest.mark.parametrize(("body", "status", "error"), [("[]", 200, ComexioDataError), ("", 500, ComexioResponseError)])
async def test_delete_marker_failed_request_raises(
    logged_in: ComexioClient, comexio: FakeComexio, body: str, status: int, error: type[Exception]
) -> None:
    comexio.serve_text("POST", DELETE_ELEMENT_PATH, body, status=status)

    with pytest.raises(error):
        await logged_in.delete_marker(271)


@pytest.mark.parametrize("marker_id", ["271", 271.0, True, None])
async def test_delete_marker_non_int_id_raises_before_any_request(
    logged_in: ComexioClient, comexio: FakeComexio, marker_id: Any
) -> None:
    # Regression: "271" or True went out as the id — a delete of whatever element matches that text.
    with pytest.raises(TypeError):
        await logged_in.delete_marker(marker_id)
    assert comexio.received_at("POST", DELETE_ELEMENT_PATH) == []


# --- system --------------------------------------------------------------------------------------------


async def test_system_emergency_reboot(logged_in: ComexioClient, comexio: FakeComexio) -> None:
    comexio.serve_text("GET", SYSTEM_DASHBOARD_PATH, "")

    await logged_in.system_emergency_reboot()

    (request,) = comexio.received_at("GET", SYSTEM_DASHBOARD_PATH)
    assert request.query == {"id": "system", "restart": "1"}


async def test_system_emergency_reboot_http_error_raises(logged_in: ComexioClient, comexio: FakeComexio) -> None:
    comexio.serve_text("GET", SYSTEM_DASHBOARD_PATH, "", status=500)

    with pytest.raises(ComexioResponseError):
        await logged_in.system_emergency_reboot()


# --- lapsed admin session ------------------------------------------------------------------------------

_ADMIN_TEXT_CALLS: list[tuple[str, str, Callable[[ComexioClient], Awaitable[Any]]]] = [
    ("GET", WEBIO_DELETE_DEVICE_PATH, lambda client: client.delete_webio_device(34)),
    ("GET", WEBIO_DELETE_BASE_PATH, lambda client: client.delete_webio_base(12)),
    ("POST", WEBIO_CREATE_DEVICE_PATH, lambda client: client.create_webio_device("HA [M]", 12, "h:8123")),
    ("POST", WEBIO_SAVE_COMMAND_PATH, lambda client: client.save_webio_command(34, _COMMAND, base_id=12)),
    ("GET", WEBIO_DELETE_COMMAND_PATH, lambda client: client.delete_webio_command(1324, 34)),
    ("GET", SYSTEM_DASHBOARD_PATH, lambda client: client.system_emergency_reboot()),
]


@pytest.mark.parametrize(("method", "path", "call"), _ADMIN_TEXT_CALLS)
async def test_login_form_answer_is_not_reported_as_success(
    logged_in: ComexioClient,
    comexio: FakeComexio,
    method: str,
    path: str,
    call: Callable[[ComexioClient], Awaitable[Any]],
) -> None:
    # Comexio answers an admin request without a logged-in session with HTTP 200 and the login form.
    comexio.serve_text(method, path, LOGIN_PAGE)

    with pytest.raises(ComexioAuthenticationError):
        await call(logged_in)


async def test_login_form_in_base_window_is_not_an_undeletable_class(
    logged_in: ComexioClient, comexio: FakeComexio
) -> None:
    comexio.serve_text("GET", WEBIO_ADD_PATH, _webio_add_page({"12": "HA [M]"}))
    comexio.serve_text("GET", WEBIO_BASE_WINDOW_PATH, LOGIN_PAGE)

    with pytest.raises(ComexioAuthenticationError):
        await logged_in.get_webio_base_info("HA [M]")


_ADMIN_JSON_CALLS: list[tuple[str, Callable[[ComexioClient], Awaitable[Any]]]] = [
    (WEBIO_UPLOAD_PATH, lambda client: client.upload_webio_class("{}", class_name="HA [M]", filename="ha.json")),
    (WEBIO_SAVE_DEVICE_PATH, lambda client: client.update_webio_device_address(34, "h:8123", "HA [M]")),
    (MARKER_ADD_PATH, lambda client: client.create_marker(binary=True)),
    (UNIQUE_CHECK_PATH, lambda client: client.rename_marker(5, "Licht", binary=True)),
    (DELETE_ELEMENT_PATH, lambda client: client.delete_marker(5)),
]


@pytest.mark.parametrize(("path", "call"), _ADMIN_JSON_CALLS)
async def test_login_form_instead_of_json_raises_authentication_error(
    logged_in: ComexioClient, comexio: FakeComexio, path: str, call: Callable[[ComexioClient], Awaitable[Any]]
) -> None:
    comexio.serve_text("POST", path, LOGIN_PAGE)

    with pytest.raises(ComexioAuthenticationError):
        await call(logged_in)


async def test_anmeldung_label_alone_is_not_the_login_form(logged_in: ComexioClient, comexio: FakeComexio) -> None:
    # A Web-IO dialog can carry "Anmeldung" as a label (device login); only the form's submit button counts.
    comexio.serve_text("GET", WEBIO_DELETE_DEVICE_PATH, "<label>Anmeldung</label><div>ok</div>")

    assert await logged_in.delete_webio_device(34) is True


# --- request headers ---------------------------------------------------------------------------------


async def test_admin_actions_send_the_xhr_headers_comexio_expects(
    logged_in: ComexioClient, comexio: FakeComexio
) -> None:
    comexio.serve_text("GET", WEBIO_DELETE_DEVICE_PATH, "")
    comexio.serve_text("POST", WEBIO_CREATE_DEVICE_PATH, "")
    comexio.serve_text("GET", WEBIO_DELETE_COMMAND_PATH, "")
    comexio.serve_json("POST", UNIQUE_CHECK_PATH, {"result": True})
    comexio.serve_json("POST", MARKER_SAVE_PATH, {"saved": 5})

    await logged_in.delete_webio_device(34)
    await logged_in.create_webio_device("HA [M]", 12, "h:8123")
    await logged_in.delete_webio_command(1324, 34)
    await logged_in.rename_marker(5, "Licht", binary=True)

    base = f"http://{comexio.host}"
    headers = {
        (r.method, r.path): (r.xhr, r.referer)
        for r in comexio.received
        if r.path not in ("/admin/", "/board/home/login/")
    }
    assert headers == {
        ("GET", WEBIO_DELETE_DEVICE_PATH): (True, f"{base}{WEBIO_HOME_PATH}"),
        # Same as the integration: the device form and the delete/uniqueness calls send less.
        ("POST", WEBIO_CREATE_DEVICE_PATH): (True, None),
        ("GET", WEBIO_DELETE_COMMAND_PATH): (False, None),
        ("POST", UNIQUE_CHECK_PATH): (False, None),
        ("POST", MARKER_SAVE_PATH): (True, f"{base}/admin/flag/home"),
    }
