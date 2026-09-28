"""ComexioClient against the in-process fake Comexio server."""

import asyncio
import json
import logging
from typing import Any

import aiohttp
import pytest
from aiohttp import web

from aiocomexio import (
    ComexioAuthenticationError,
    ComexioClient,
    ComexioConnectionError,
    ComexioDataError,
    ComexioResponseError,
    LiveStates,
    session_kwargs,
)
from tests.fake_comexio import (
    ADMIN_PATH,
    BUS_WORKLOAD_PATH,
    DASHBOARD_REFRESH_PATH,
    EXTENSION_FIRMWARE_PATH,
    FUNCTION_MODULE_PATH,
    KNX_CATALOG_PATH,
    LOAD_ALL_ELEMENTS_PATH,
    LOAD_ELEMENTS_PATH,
    LOGIN_PATH,
    PASSWORD,
    SALT,
    USERNAME,
    FakeComexio,
    json_response,
)

_ELEMENT = {"id": 7, "name": "", "position_x": 0, "position_y": 0}
_CONNECTION = {"input": {"FubElementId": 7, "IOPos": 0}, "output": []}


# --- login ---------------------------------------------------------------------------------


async def test_login_sends_rsa_encrypted_password(client: ComexioClient, comexio: FakeComexio) -> None:
    await client.login()

    (form,) = comexio.logins
    assert (form["username"], form["encryption"], form["target"]) == (USERNAME, "rsa", "/board/home/login")
    with_password, without_password = comexio.decrypted_blocks
    nonce = without_password.removeprefix(SALT)
    assert len(nonce) == 20
    assert with_password == f"{SALT}{nonce}{PASSWORD}"
    # The admin page is only served with the session cookie — the login really stuck.
    assert comexio.requests[-1] == ("GET", ADMIN_PATH)


async def test_login_rejected_raises_authentication_error(comexio: FakeComexio, session: aiohttp.ClientSession) -> None:
    client = ComexioClient(comexio.host, USERNAME, "wrong", session=session)

    with pytest.raises(ComexioAuthenticationError):
        await client.login()


async def test_login_with_unencodable_password_fails_before_sending_it(
    comexio: FakeComexio, session: aiohttp.ClientSession
) -> None:
    client = ComexioClient(comexio.host, USERNAME, "pass€", session=session)

    with pytest.raises(ComexioAuthenticationError, match="ISO-8859-1"):
        await client.login()
    assert comexio.logins == []


async def test_login_does_not_reuse_an_earlier_session_cookie(
    logged_in: ComexioClient, comexio: FakeComexio, session: aiohttp.ClientSession
) -> None:
    # Same session, still holding the cookie of the successful login above.
    client = ComexioClient(comexio.host, USERNAME, "wrong", session=session)

    with pytest.raises(ComexioAuthenticationError):
        await client.login()


async def test_login_with_too_long_password_fails_before_sending_it(
    comexio: FakeComexio, session: aiohttp.ClientSession
) -> None:
    # 2048-bit key: 256 - 11 padding bytes, minus salt and nonce, leaves 221 bytes for the password.
    fitting = ComexioClient(comexio.host, USERNAME, "x" * 221, session=session)
    too_long = ComexioClient(comexio.host, USERNAME, "x" * 222, session=session)

    with pytest.raises(ComexioAuthenticationError, match="rejected"):
        await fitting.login()
    with pytest.raises(ComexioAuthenticationError, match="too long.*221 bytes"):
        await too_long.login()
    assert len(comexio.logins) == 1


@pytest.mark.parametrize(
    "keys",
    [
        {"salt": "U2Fseg==", "modulus": "zz", "exponent": "10001"},
        {"modulus": "abc", "exponent": "10001"},
        ["not", "an", "object"],
        {"salt": "U2Fseg==", "modulus": "0", "exponent": "0"},
        # A valid 256-bit key: 32 - 11 bytes cannot even hold salt + nonce.
        {"salt": "U2Fseg==", "modulus": f"{(1 << 255) + 1:x}", "exponent": "10001"},
    ],
)
async def test_login_with_unusable_key_response_raises_data_error(
    client: ComexioClient, comexio: FakeComexio, keys: Any
) -> None:
    comexio.serve_json("POST", LOGIN_PATH, keys)

    with pytest.raises(ComexioDataError):
        await client.login()


async def test_login_key_request_answered_with_login_form_is_a_data_error(
    client: ComexioClient, comexio: FakeComexio
) -> None:
    # Credentials were not even sent yet, so this is no authentication verdict.
    comexio.serve_text("POST", LOGIN_PATH, "<html><form>Anmeldung</form></html>")

    with pytest.raises(ComexioDataError, match="not JSON"):
        await client.login()


async def test_login_check_http_error_raises_response_error(client: ComexioClient, comexio: FakeComexio) -> None:
    original = comexio.routes[("POST", LOGIN_PATH)]

    async def login_then_break_admin(request: web.Request) -> web.StreamResponse:
        comexio.serve_text("GET", ADMIN_PATH, "Internal Server Error", status=500)
        return await original(request)

    comexio.serve("POST", LOGIN_PATH, login_then_break_admin)

    with pytest.raises(ComexioResponseError) as excinfo:
        await client.login()
    assert excinfo.value.status == 500


async def test_login_on_unreachable_server_raises_connection_error(comexio: FakeComexio) -> None:
    host = comexio.host
    await comexio.server.close()

    async with aiohttp.ClientSession(**session_kwargs()) as session:
        client = ComexioClient(host, USERNAME, PASSWORD, session=session)
        with pytest.raises(ComexioConnectionError):
            await client.login()


async def test_request_timeout_raises_connection_error(comexio: FakeComexio) -> None:
    async def slow(_request: web.Request) -> web.Response:
        await asyncio.sleep(1)
        return web.Response(text="late")

    comexio.serve("GET", ADMIN_PATH, slow)

    async with aiohttp.ClientSession(**session_kwargs(timeout=0.1)) as s:
        client = ComexioClient(comexio.host, USERNAME, PASSWORD, session=s)
        with pytest.raises(ComexioConnectionError):
            await client.get_raw_config()


def test_plain_http_warning_only_for_non_local_hosts_and_only_once(caplog: pytest.LogCaptureFixture) -> None:
    session: Any = object()  # never used: only the warning is exercised
    remote = ComexioClient("comexio.example.com", USERNAME, PASSWORD, session=session)
    local = ComexioClient("192.168.0.5", USERNAME, PASSWORD, session=session)

    with caplog.at_level(logging.WARNING, logger="aiocomexio.client"):
        remote._warn_plain_http()
        remote._warn_plain_http()
        local._warn_plain_http()

    (record,) = caplog.records
    assert "plain HTTP" in record.message
    assert record.args == ("comexio.example.com",)


# --- config scraping -----------------------------------------------------------------------


async def test_get_raw_config_scrapes_both_pages(logged_in: ComexioClient) -> None:
    config = await logged_in.get_raw_config()

    assert config.variables["FubModules"]["2"]["1"]["Name"] == "Licht, Wohnen"
    assert "Broken" not in config.variables
    assert config.io_types == {"1": {"binary": True, "min": 0, "max": 1, "unit": ""}}
    assert config.io_input_types == {"1": {"input": True}}
    assert config.comexio_version == "11.0.2"


async def test_get_raw_config_http_error_raises_response_error(logged_in: ComexioClient, comexio: FakeComexio) -> None:
    comexio.serve_text("GET", FUNCTION_MODULE_PATH, "Bad Gateway", status=502)

    with pytest.raises(ComexioResponseError) as excinfo:
        await logged_in.get_raw_config()
    assert excinfo.value.status == 502


async def test_get_raw_config_on_expired_session_raises_authentication_error(
    logged_in: ComexioClient, comexio: FakeComexio
) -> None:
    comexio.serve_text("GET", FUNCTION_MODULE_PATH, "<html><form>Anmeldung</form></html>")

    with pytest.raises(ComexioAuthenticationError, match="login form"):
        await logged_in.get_raw_config()


async def test_get_raw_config_marker_named_like_the_login_form_is_no_login_page(
    logged_in: ComexioClient, comexio: FakeComexio
) -> None:
    modules = {"2": {"1": {"Name": "Anmeldung"}}}
    comexio.serve_text("GET", FUNCTION_MODULE_PATH, f"<script>var $FubModules = {json.dumps(modules)};</script>")

    config = await logged_in.get_raw_config()

    assert config.variables["FubModules"] == modules


async def test_get_raw_config_undecodable_page_raises_data_error(
    logged_in: ComexioClient, comexio: FakeComexio
) -> None:
    async def broken(_request: web.Request) -> web.Response:
        return web.Response(body=b"\xff\xfe<html>", content_type="text/html", charset="utf-8")

    comexio.serve("GET", FUNCTION_MODULE_PATH, broken)

    with pytest.raises(ComexioDataError, match="not valid utf-8"):
        await logged_in.get_raw_config()


async def test_fetch_marker_titles(logged_in: ComexioClient) -> None:
    assert await logged_in.fetch_marker_titles() == {1: "Licht, Wohnen"}


async def test_fetch_marker_titles_without_fub_modules_raises(logged_in: ComexioClient, comexio: FakeComexio) -> None:
    comexio.serve_text("GET", FUNCTION_MODULE_PATH, "<html><script>var $Fubs = {};</script></html>")

    with pytest.raises(ComexioDataError, match="FubModules"):
        await logged_in.fetch_marker_titles()


async def test_fetch_marker_titles_skips_malformed_entries(logged_in: ComexioClient, comexio: FakeComexio) -> None:
    modules = {"2": {"1": {"Name": "Licht"}, "x": {"Name": "bad id"}, "3": None, "4": "not an object"}}
    comexio.serve_text("GET", FUNCTION_MODULE_PATH, f"<script>var $FubModules = {json.dumps(modules)};</script>")

    assert await logged_in.fetch_marker_titles() == {1: "Licht", 3: ""}


async def test_get_knx_dpt_catalog(logged_in: ComexioClient, comexio: FakeComexio) -> None:
    comexio.serve_text(
        "GET",
        KNX_CATALOG_PATH,
        '<script>var $KnxPoints = {"1": {"KnxDeviceId": 2}}; var $KnxDpt = {"3": {"KnxBaseTypeId": 9}};</script>',
    )

    catalog = await logged_in.get_knx_dpt_catalog()

    assert catalog == {"KnxPoints": {"1": {"KnxDeviceId": 2}}, "KnxDpt": {"3": {"KnxBaseTypeId": 9}}}


async def test_get_knx_dpt_catalog_without_data_raises(logged_in: ComexioClient, comexio: FakeComexio) -> None:
    comexio.serve_text("GET", KNX_CATALOG_PATH, "<html>changed page</html>")

    with pytest.raises(ComexioDataError):
        await logged_in.get_knx_dpt_catalog()


async def test_get_knx_dpt_catalog_on_expired_session_raises_authentication_error(
    logged_in: ComexioClient, comexio: FakeComexio
) -> None:
    comexio.serve_text("GET", KNX_CATALOG_PATH, "<html><form>Anmeldung</form></html>")

    with pytest.raises(ComexioAuthenticationError):
        await logged_in.get_knx_dpt_catalog()


# --- live values ---------------------------------------------------------------------------


async def test_get_live_states_splits_markers_and_knx(logged_in: ComexioClient, comexio: FakeComexio) -> None:
    received: dict[str, Any] = {}

    async def refresh(request: web.Request) -> web.Response:
        received.update(json.loads(str((await request.post())["json"])))
        return json_response(
            {"result": {"1": {"value": 1}, "2": {"value": 0}, "knxIo_11_1": {"value": 21.5}, "messages": []}}
        )

    comexio.serve("POST", DASHBOARD_REFRESH_PATH, refresh)

    states = await logged_in.get_live_states(2, knx_max_id=1)

    assert states == LiveStates(markers={"1": {"value": 1}, "2": {"value": 0}}, knx={"1": {"value": 21.5}})
    assert received == {
        "1": {"action": "get", "MarkerName": "M1"},
        "2": {"action": "get", "MarkerName": "M2"},
        "knxIo_11_1": {"action": "get", "KnxIo": "K1", "Unit": "any"},
        "messages": {"action": "messages"},
    }


@pytest.mark.parametrize("payload", [{"error": "x"}, {"result": []}, ["result"]])
async def test_get_live_states_without_result_object_raises(
    logged_in: ComexioClient, comexio: FakeComexio, payload: Any
) -> None:
    comexio.serve_json("POST", DASHBOARD_REFRESH_PATH, payload)

    with pytest.raises(ComexioDataError):
        await logged_in.get_live_states(1)


async def test_get_live_states_non_json_raises(logged_in: ComexioClient, comexio: FakeComexio) -> None:
    comexio.serve_text("POST", DASHBOARD_REFRESH_PATH, "<html>error</html>")

    with pytest.raises(ComexioDataError, match="not JSON"):
        await logged_in.get_live_states(1)


async def test_get_live_states_on_expired_session_raises_authentication_error(
    logged_in: ComexioClient, comexio: FakeComexio
) -> None:
    comexio.serve_text("POST", DASHBOARD_REFRESH_PATH, "<html><form>Anmeldung</form></html>")

    with pytest.raises(ComexioAuthenticationError):
        await logged_in.get_live_states(1)


@pytest.mark.parametrize(
    ("connection", "expected"),
    [
        (json.dumps({"7": [1, 0], "9": 5}), {"7": [1, 0], "9": [5]}),
        (json.dumps([[0], [1]]), {"0": [0], "1": [1]}),
        ({"7": [1]}, {"7": [1]}),
        ("0:not_found", {}),
        (json.dumps("text"), {}),  # any non-JSON-looking string is the "not running" sentinel
        ("", {}),
        (None, {}),
        ([], {}),
        ({}, {}),
    ],
)
async def test_get_function_plan_connection_values(
    logged_in: ComexioClient, comexio: FakeComexio, connection: Any, expected: dict[str, list[Any]]
) -> None:
    comexio.serve_json("POST", DASHBOARD_REFRESH_PATH, {"result": {"connection": connection}})

    assert await logged_in.get_function_plan_connection_values(3) == expected


@pytest.mark.parametrize("connection", ["{broken", "[1,", 5, True, 0, False])
async def test_get_function_plan_connection_values_malformed_raises(
    logged_in: ComexioClient, comexio: FakeComexio, connection: Any
) -> None:
    comexio.serve_json("POST", DASHBOARD_REFRESH_PATH, {"result": {"connection": connection}})

    with pytest.raises(ComexioDataError):
        await logged_in.get_function_plan_connection_values(3)


@pytest.mark.parametrize("payload", [{"result": {}}, {"result": []}, {"error": "x"}, ["result"]])
async def test_get_function_plan_connection_values_without_connection_entry_raises(
    logged_in: ComexioClient, comexio: FakeComexio, payload: Any
) -> None:
    comexio.serve_json("POST", DASHBOARD_REFRESH_PATH, payload)

    with pytest.raises(ComexioDataError, match=r"result\.connection"):
        await logged_in.get_function_plan_connection_values(3)


async def test_get_function_plan_connection_values_http_error_raises(
    logged_in: ComexioClient, comexio: FakeComexio
) -> None:
    comexio.serve_json("POST", DASHBOARD_REFRESH_PATH, {}, status=502)

    with pytest.raises(ComexioResponseError):
        await logged_in.get_function_plan_connection_values(3)


# --- function plans ------------------------------------------------------------------------


async def test_load_function_plan_rekeys_list_collections(logged_in: ComexioClient, comexio: FakeComexio) -> None:
    fub_ids: list[str] = []

    async def load(request: web.Request) -> web.Response:
        fub_ids.append(request.query["fubid"])
        return json_response({"elements": [_ELEMENT], "connections": [_CONNECTION], "extra": 1})

    comexio.serve("GET", LOAD_ELEMENTS_PATH, load)

    plan = await logged_in.load_function_plan(4)

    assert fub_ids == ["4"]
    assert plan == {"elements": {"7": _ELEMENT}, "connections": {"0": _CONNECTION}, "extra": 1}


async def test_load_function_plan_null_collections_are_empty(logged_in: ComexioClient, comexio: FakeComexio) -> None:
    comexio.serve_json("GET", LOAD_ELEMENTS_PATH, {"elements": None})

    assert await logged_in.load_function_plan(4) == {"elements": {}, "connections": {}}


@pytest.mark.parametrize(
    ("payload", "strict"),
    [
        ({"elements": None}, True),
        ({"error": "no plan"}, True),
        (["x"], False),
        ({"elements": [1]}, False),
        ({"elements": 5}, False),
    ],
)
async def test_load_function_plan_rejects_unusable_payload(
    logged_in: ComexioClient, comexio: FakeComexio, payload: Any, strict: bool
) -> None:
    comexio.serve_json("GET", LOAD_ELEMENTS_PATH, payload)

    with pytest.raises(ComexioDataError):
        await logged_in.load_function_plan(4, strict=strict)


async def test_load_all_function_plans_filters_and_skips_malformed(
    logged_in: ComexioClient, comexio: FakeComexio, caplog: pytest.LogCaptureFixture
) -> None:
    comexio.serve_json(
        "GET",
        LOAD_ALL_ELEMENTS_PATH,
        {
            "1": {"elements": {"7": _ELEMENT}, "connections": []},
            "2": {"elements": {}, "connections": {}},
            "3": {"error": "x"},
            "4": "not an object",
            "5": {"elements": ["not an object"]},
            "x": {"elements": {}},
        },
    )

    with caplog.at_level(logging.DEBUG, logger="aiocomexio.client"):
        plans = await logged_in.load_all_function_plans([1, 3, 4, 5, 9])
    strict = await logged_in.load_all_function_plans(strict=True)

    assert plans == {
        1: {"elements": {"7": _ELEMENT}, "connections": {}},
        3: {"error": "x", "elements": {}, "connections": {}},
    }
    assert "plan 4" in caplog.text
    assert "plan 5" in caplog.text
    assert "missing from the bulk result: [4, 5, 9]" in caplog.text
    assert sorted(strict) == [1, 2]


async def test_load_all_function_plans_with_empty_filter_skips_the_request(
    logged_in: ComexioClient, comexio: FakeComexio
) -> None:
    requests_before = len(comexio.requests)

    assert await logged_in.load_all_function_plans([]) == {}
    assert len(comexio.requests) == requests_before


async def test_load_all_function_plans_empty_array_is_no_plans(logged_in: ComexioClient, comexio: FakeComexio) -> None:
    comexio.serve_json("GET", LOAD_ALL_ELEMENTS_PATH, [])

    assert await logged_in.load_all_function_plans() == {}


@pytest.mark.parametrize("second", [{"elements": {"7": _ELEMENT}}, "not an object"])
async def test_load_all_function_plans_same_id_twice_raises(
    logged_in: ComexioClient, comexio: FakeComexio, second: Any
) -> None:
    comexio.serve_json("GET", LOAD_ALL_ELEMENTS_PATH, {"1": {"elements": {}}, "01": second})

    with pytest.raises(ComexioDataError, match="plan 1 twice"):
        await logged_in.load_all_function_plans()


async def test_load_all_function_plans_non_object_raises(logged_in: ComexioClient, comexio: FakeComexio) -> None:
    comexio.serve_json("GET", LOAD_ALL_ELEMENTS_PATH, ["x"])

    with pytest.raises(ComexioDataError):
        await logged_in.load_all_function_plans()


# --- system info ---------------------------------------------------------------------------


async def test_get_bus_workload(logged_in: ComexioClient, comexio: FakeComexio) -> None:
    comexio.serve_json("POST", BUS_WORKLOAD_PATH, {"workload": 12, "sd": True})

    assert await logged_in.get_bus_workload() == {"workload": 12, "sd": True}


async def test_get_bus_workload_non_object_raises(logged_in: ComexioClient, comexio: FakeComexio) -> None:
    comexio.serve_json("POST", BUS_WORKLOAD_PATH, [12])

    with pytest.raises(ComexioDataError):
        await logged_in.get_bus_workload()


async def test_check_extension_firmware(logged_in: ComexioClient, comexio: FakeComexio) -> None:
    updates = [{"name": "BASE", "update": False}]
    comexio.serve_json("POST", EXTENSION_FIRMWARE_PATH, {"ok": "ok", "data": updates})

    assert await logged_in.check_extension_firmware() == updates


@pytest.mark.parametrize(
    "payload",
    [{"ok": "error", "msg": "busy"}, {"ok": "ok", "data": {}}, {"ok": "ok"}, {"ok": "ok", "data": [1]}, []],
)
async def test_check_extension_firmware_error_payload_raises(
    logged_in: ComexioClient, comexio: FakeComexio, payload: Any
) -> None:
    comexio.serve_json("POST", EXTENSION_FIRMWARE_PATH, payload)

    with pytest.raises(ComexioDataError):
        await logged_in.check_extension_firmware()


def test_host_property(client: ComexioClient, comexio: FakeComexio) -> None:
    assert client.host == comexio.host
