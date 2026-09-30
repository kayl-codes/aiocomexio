"""ComexioClient writes (API values, Web-IO lifecycle, markers) against the fake Comexio server."""

import base64
import html
import json
import re
from collections.abc import Awaitable, Callable
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
)
from aiocomexio.webio import CONTENT_TYPE_JSON
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
    comexio.serve_text("GET", API_PATH, "")

    await api_client.set_knx_value(3, 1)
    await api_client.set_io_value("UD 1", "QD2", 0)

    knx, io = comexio.received_at("GET", API_PATH)
    assert knx.query == {"action": "set", "value": "1", "knx": "K3"}
    assert io.query == {"action": "set", "value": "0", "ext": "UD 1", "io": "QD2"}


async def test_set_value_without_api_user_sends_no_credentials(logged_in: ComexioClient, comexio: FakeComexio) -> None:
    comexio.serve_text("GET", API_PATH, "")

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
        '<input type="text" id="min_cmd_io_0" value="-500000"><input id="MAX_cmd_io_0" value="abc">',
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
    ("page", "error"), [("<form></form>", ComexioDataError), (LOGIN_PAGE, ComexioAuthenticationError)]
)
async def test_get_webio_command_range_without_fields_raises(
    logged_in: ComexioClient, comexio: FakeComexio, page: str, error: type[Exception]
) -> None:
    comexio.serve_text("GET", WEBIO_EDIT_COMMAND_PATH, page)

    with pytest.raises(error):
        await logged_in.get_webio_command_range(1324, 34)


# --- markers and KNX objects ------------------------------------------------------------------------


@pytest.mark.parametrize(("binary", "marker_type"), [(True, "1"), (False, "2")])
async def test_create_marker(logged_in: ComexioClient, comexio: FakeComexio, binary: bool, marker_type: str) -> None:
    comexio.serve_json("POST", MARKER_ADD_PATH, {"ok": True, "saved": "271"})

    assert await logged_in.create_marker(binary=binary) == 271
    (request,) = comexio.received_at("POST", MARKER_ADD_PATH)
    assert request.form == {"type": marker_type}


@pytest.mark.parametrize(
    ("answer", "error"),
    [({"ok": False}, ComexioRequestRejectedError), ({"ok": True}, ComexioDataError), ("x", ComexioDataError)],
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
