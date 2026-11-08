"""Web-IO command and class-template builders."""

import json
import logging
import re
from collections.abc import Mapping
from typing import Any

import pytest
from syrupy.assertion import SnapshotAssertion

from aiocomexio.config import TYPE_ANALOG, TYPE_DIGITAL, parse_config
from aiocomexio.const import (
    WEBIO_CLASS_NAME_KNX_LOOPBACK,
    WEBIO_MARKER_ANALOG_MAX,
    WEBIO_MARKER_ANALOG_MIN,
    WebioClass,
    webio_class_name,
)
from aiocomexio.knx import KNX_DPT_ANALOG_RANGES
from aiocomexio.onewire import OneWireSensor
from aiocomexio.webio import (
    WEBIO_PROTOCOL_HTTP,
    WEBIO_PROTOCOL_HTTPS,
    build_io_webio_command,
    build_knx_loopback_webio_command,
    build_marker_webio_command,
    build_onewire_webio_command,
    build_webio_commands,
    generate_webio_json,
    knx_loopback_class_json,
    knx_loopback_range,
    knx_webio_range,
    lua_escape,
    safe_webio_range,
)
from tests.common import load_json_fixture

_HOOK = "/api/webhook/comexio_srv"
_GENERIC_RANGE: tuple[float, float] = (WEBIO_MARKER_ANALOG_MIN, WEBIO_MARKER_ANALOG_MAX)
_BAD_ONEWIRE_DATA = 'parsed_data["onewire"] must map addresses to OneWireSensor'


def _exactly(message: str) -> str:
    """A pytest.raises match pattern that accepts message only, without extra or missing text."""
    return f"^{re.escape(message)}$"


def _webio_log(caplog: pytest.LogCaptureFixture) -> list[tuple[int, str]]:
    """Level and full message of every warning or error aiocomexio.webio logged."""
    return [
        (r.levelno, r.getMessage())
        for r in caplog.records
        if r.name == "aiocomexio.webio" and r.levelno >= logging.WARNING
    ]


@pytest.fixture
def catalog() -> dict[str, Any]:
    catalog: dict[str, Any] = load_json_fixture("knx_dpt_catalog.json")
    return catalog


@pytest.fixture
def parsed(catalog: dict[str, Any]) -> dict[str, Any]:
    return parse_config(
        load_json_fixture("config_basic.json"),
        io_types=load_json_fixture("io_types.json"),
        io_input_types=load_json_fixture("io_input_types.json"),
        knx_dpt_catalog=catalog,
    )


def _sensor(config_id: int, prefix: str, kind: str | None) -> OneWireSensor:
    return OneWireSensor(
        address=f"{prefix}{config_id}",
        config_id=config_id,
        prefix=prefix,
        name=f"Sensor {config_id}",
        hw_id=f"10-test-{config_id}",
        kind=kind,
    )


def _onewire_sensors() -> dict[str, OneWireSensor]:
    """Keyed by address like get_onewire_sensors, deliberately not in config id order."""
    sensors = [_sensor(10, "OIB", TYPE_DIGITAL), _sensor(2, "OT", TYPE_ANALOG), _sensor(3, "OH", TYPE_ANALOG)]
    return {sensor.address: sensor for sensor in sensors}


def _source(parsed: dict[str, Any], webio_class: WebioClass) -> dict[str, Any]:
    """A parsed config that has items of webio_class."""
    if webio_class is WebioClass.QUICKBUS:
        # The basic fixture has no Quickbus devices; a missing IO type table keeps their IOs active.
        return parse_config(load_json_fixture("config_quickbus.json"))
    if webio_class is WebioClass.ONEWIRE:
        return {**parsed, "onewire": _onewire_sensors()}
    return parsed


def test_build_webio_commands_snapshot(parsed: dict[str, Any], snapshot: SnapshotAssertion) -> None:
    """Characterization snapshot of every command built from the basic fixture."""
    assert build_webio_commands(_HOOK, parsed) == snapshot


def test_build_webio_commands_default_protocol_is_http(parsed: dict[str, Any]) -> None:
    assert {cmd["Protocol"] for cmd in build_webio_commands(_HOOK, parsed)} == {WEBIO_PROTOCOL_HTTP}


@pytest.mark.parametrize("webio_class", list(WebioClass))
def test_build_webio_commands_writes_the_protocol(parsed: dict[str, Any], webio_class: WebioClass) -> None:
    source = _source(parsed, webio_class)
    commands = build_webio_commands(_HOOK, source, webio_class, protocol=WEBIO_PROTOCOL_HTTPS)

    assert commands
    assert {cmd["Protocol"] for cmd in commands} == {WEBIO_PROTOCOL_HTTPS}


def test_build_single_commands_write_the_protocol() -> None:
    marker = build_marker_webio_command(
        {"id": "5", "name": "M5 Licht", "type": "digital"}, _HOOK, protocol=WEBIO_PROTOCOL_HTTPS
    )
    io_command = build_io_webio_command(
        {"ext_name": "BASE", "identifier": "Q1", "is_binary": True}, _HOOK, protocol=WEBIO_PROTOCOL_HTTPS
    )

    assert (marker["Protocol"], io_command["Protocol"]) == (WEBIO_PROTOCOL_HTTPS, WEBIO_PROTOCOL_HTTPS)


@pytest.mark.parametrize(
    ("webio_class", "prefix"),
    [(WebioClass.MARKER, "HA M"), (WebioClass.IO, "HA IO "), (WebioClass.KNX, "HA K")],
)
def test_build_webio_commands_filters_by_class(parsed: dict[str, Any], webio_class: WebioClass, prefix: str) -> None:
    names = [cmd["Name"] for cmd in build_webio_commands(_HOOK, parsed, webio_class)]

    assert names
    assert all(name.startswith(prefix) for name in names)


def test_build_webio_commands_skips_ignored_markers_and_knx(parsed: dict[str, Any]) -> None:
    all_names = {cmd["Name"] for cmd in build_webio_commands(_HOOK, parsed)}
    marker_id = int(parsed["markers"][0]["id"])
    knx_id = int(parsed["knx"][0]["id"])

    names = {
        cmd["Name"]
        for cmd in build_webio_commands(_HOOK, parsed, ignored_marker_ids={marker_id}, ignored_knx_ids={knx_id})
    }

    assert all_names - names == {f"HA {parsed['markers'][0]['name']}", f"HA {parsed['knx'][0]['name']}"}


def test_generate_webio_json_wraps_class_commands(parsed: dict[str, Any]) -> None:
    payload = json.loads(generate_webio_json(_HOOK, "HomeAssistant [IO]", parsed, WebioClass.IO))

    assert payload["data"] == "web_io"
    assert payload["base"] == {"Identifier": "HomeAssistant [IO]", "UseCookies": 0, "Login": 2, "BaseId": 0}
    assert payload["commands"] == build_webio_commands(_HOOK, parsed, WebioClass.IO)


def test_generate_webio_json_passes_the_protocol(parsed: dict[str, Any]) -> None:
    payload = json.loads(
        generate_webio_json(_HOOK, "HomeAssistant [M]", parsed, WebioClass.MARKER, protocol=WEBIO_PROTOCOL_HTTPS)
    )

    assert payload["commands"]
    assert {cmd["Protocol"] for cmd in payload["commands"]} == {WEBIO_PROTOCOL_HTTPS}


@pytest.mark.parametrize(
    ("v_min", "v_max", "expected"),
    [
        (0, 100, (0, 100)),
        (-32768, 32767, _GENERIC_RANGE),
        (0, 30000, _GENERIC_RANGE),
        (0, 40001, (0, 40001)),
    ],
)
def test_safe_webio_range_widens_int16_danger_zone(v_min: float, v_max: float, expected: tuple[float, float]) -> None:
    assert safe_webio_range(v_min, v_max) == expected


@pytest.mark.parametrize(
    ("dpt_min", "dpt_max", "expected"),
    [
        (None, 100, _GENERIC_RANGE),
        (0, None, _GENERIC_RANGE),
        (-273, 670760, (-273, 670760)),
        (0, 4294967295, (0, 4294967295)),
        (-32768, 32767, _GENERIC_RANGE),
    ],
)
def test_knx_webio_range(dpt_min: float | None, dpt_max: float | None, expected: tuple[float, float]) -> None:
    assert knx_webio_range(dpt_min, dpt_max) == expected


def test_lua_escape_escapes_backslash_before_quote() -> None:
    assert lua_escape('a"b\\c') == 'a\\"b\\\\c'


def test_build_marker_webio_command_digital() -> None:
    command = build_marker_webio_command({"id": "5", "name": "M5 Licht", "type": "digital"}, _HOOK)

    assert command["Name"] == "HA M5 Licht"
    assert (command["TypeId"], command["Min"], command["Max"]) == (1, 0, 1)
    assert command["Parameter"] == _HOOK
    assert command["Data"] == (
        'function data(a)\r\n  local d = { id="5", value=a, type="marker" }\r\n  return json_stringify(d)\r\nend'
    )


def test_build_marker_webio_command_analog_marker_uses_generic_range() -> None:
    command = build_marker_webio_command(
        {"id": "2", "name": "M2 Soll", "type": "analog", "dpt_min": 0, "dpt_max": 10}, _HOOK
    )

    assert (command["TypeId"], command["Min"], command["Max"]) == (2, *_GENERIC_RANGE)


def test_build_marker_webio_command_analog_knx_uses_dpt_range() -> None:
    command = build_marker_webio_command(
        {"id": "8", "name": "K8 Temp", "type": "analog", "dpt_min": -273, "dpt_max": 670760},
        _HOOK,
        source_type=WebioClass.KNX,
    )

    assert (command["Min"], command["Max"]) == (-273, 670760)
    assert 'type="knx"' in command["Data"]


def test_build_io_webio_command_coerces_missing_bounds() -> None:
    command = build_io_webio_command(
        {"ext_name": 'UD "1"', "identifier": "QI1", "is_binary": False, "min": None, "max": None}, _HOOK
    )

    assert command["Name"] == 'HA IO UD "1" QI1'
    assert (command["TypeId"], command["Min"], command["Max"]) == (2, 0, 100)
    assert 'ext="UD \\"1\\"", io="QI1", value=a, type="io"' in command["Data"]


def test_build_io_webio_command_binary_defaults_to_zero_one() -> None:
    command = build_io_webio_command({"ext_name": "BASE", "identifier": "Q1", "is_binary": True}, _HOOK)

    assert (command["TypeId"], command["Min"], command["Max"]) == (1, 0, 1)


@pytest.mark.parametrize(
    "io_item",
    [
        {"id": "7", "ext_name": "BASE", "identifier": "X1", "is_binary": None},
        # An untyped Quickbus IO, as io_all holds it: dispatched to build_quickbus_webio_command.
        {
            "id": "211",
            "ext_name": "BASE",
            "identifier": "G1",
            "is_binary": None,
            "quickbus": {"device_id": "2", "name": "MT"},
        },
    ],
)
def test_build_io_webio_command_rejects_an_unknown_kind(io_item: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="unknown kind"):
        build_io_webio_command(io_item, _HOOK)


def test_knx_loopback_command_digital() -> None:
    command = build_knx_loopback_webio_command(
        k_id=1, marker_id=300, is_analog=False, knx_dpt_catalog=None, catalog_stale=False
    )

    assert command["Name"] == "KNX K1 to M300"
    assert (command["TypeId"], command["Min"], command["Max"]) == (1, 0, 1)
    assert (command["PostGet"], command["Authentication"], command["Data"], command["Protocol"]) == (
        0,
        1,
        "",
        WEBIO_PROTOCOL_HTTP,
    )
    assert command["Parameter"] == ('function parameter(a)\r\n  return "/api/?action=set&marker=M300&value="..a\r\nend')


def test_knx_loopback_command_analog_uses_resolved_dpt_range(catalog: dict[str, Any]) -> None:
    command = build_knx_loopback_webio_command(
        k_id=1, marker_id=309, is_analog=True, knx_dpt_catalog=catalog, catalog_stale=False
    )

    dpt_min, dpt_max, _unit, _step = KNX_DPT_ANALOG_RANGES[(9, 1)]
    assert (command["TypeId"], command["Min"], command["Max"]) == (2, dpt_min, dpt_max)


@pytest.mark.parametrize(
    ("use_catalog", "stale", "k_id", "reason"),
    [
        (False, False, 1, "catalog not yet fetched"),
        (True, True, 1, "catalog stale"),
        (True, False, 99, "DPT chain resolution failed"),
    ],
)
def test_knx_loopback_range_falls_back_with_reason(
    catalog: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
    use_catalog: bool,
    stale: bool,
    k_id: int,
    reason: str,
) -> None:
    caplog.set_level(logging.DEBUG, logger="aiocomexio.webio")

    result = knx_loopback_range(k_id, 300, knx_dpt_catalog=catalog if use_catalog else None, catalog_stale=stale)

    assert result == _GENERIC_RANGE
    assert reason in caplog.text


def test_knx_loopback_range_falls_back_for_dpt_without_range(
    catalog: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="aiocomexio.webio")
    catalog["KnxDpt"]["20"] = {"KnxBaseTypeId": 14, "KnxSubId": 56}

    assert knx_loopback_range(1, 300, knx_dpt_catalog=catalog, catalog_stale=False) == _GENERIC_RANGE
    assert "DPT14.56 has no entry" in caplog.text


def test_knx_loopback_class_json() -> None:
    command = build_knx_loopback_webio_command(
        k_id=1, marker_id=300, is_analog=False, knx_dpt_catalog=None, catalog_stale=False
    )

    payload = json.loads(knx_loopback_class_json([command]))

    assert payload["base"] == {"Identifier": WEBIO_CLASS_NAME_KNX_LOOPBACK, "UseCookies": 0, "Login": 3, "BaseId": 0}
    assert payload["commands"] == [command]
    assert json.loads(knx_loopback_class_json())["commands"] == []


def test_build_webio_commands_default_payload_has_no_server(parsed: dict[str, Any]) -> None:
    assert all("server=" not in cmd["Data"] for cmd in build_webio_commands(_HOOK, parsed))


@pytest.mark.parametrize("webio_class", list(WebioClass))
def test_build_webio_commands_writes_the_server_into_every_payload(
    parsed: dict[str, Any], webio_class: WebioClass
) -> None:
    source = _source(parsed, webio_class)
    commands = build_webio_commands(_HOOK, source, webio_class, server='io"srv1')

    assert commands
    assert all(', server="io\\"srv1" }' in cmd["Data"] for cmd in commands)


@pytest.mark.parametrize("webio_class", list(WebioClass))
def test_build_webio_commands_gives_every_command_the_path_of_its_own_item(
    parsed: dict[str, Any], webio_class: WebioClass
) -> None:
    """A callable path gets the class and the parsed item of each command, so no two share a path."""
    source = _source(parsed, webio_class)

    asked: list[tuple[WebioClass, str]] = []
    items: list[Any] = []

    def path_for(cls: WebioClass, item: Any) -> str:
        key = item.address if isinstance(item, OneWireSensor) else f"{item['id']}/{item.get('ext_name', '')}"
        path = f"/h/{cls}/{key}"
        asked.append((cls, path))
        items.append(item)
        return path

    commands = build_webio_commands(path_for, source, webio_class)

    assert commands
    # A callable path builds every command a fixed path builds, not just the first ones it was asked for.
    assert len(commands) == len(asked) == len(build_webio_commands(_HOOK, source, webio_class))
    # Exactly the paths the callback returned, one per command, and only for the wanted class.
    assert sorted(cmd["Parameter"] for cmd in commands) == sorted(path for _, path in asked)
    assert {cls for cls, _ in asked} == {webio_class}
    assert len({cmd["Parameter"] for cmd in commands}) == len(commands)
    # The path of a command was asked for with the very item that command is built from: its payload names it.
    by_path = {cmd["Parameter"]: cmd for cmd in commands}
    for (_, path), item in zip(asked, items, strict=True):
        if webio_class is WebioClass.IO:
            token = f'io="{item["identifier"]}"'
        elif webio_class is WebioClass.ONEWIRE:
            token = f'id="{item.address}"'
        else:
            token = f'id="{item["id"]}"'
        assert token in by_path[path]["Data"]


def _path_by_id(cls: WebioClass, item: Mapping[str, Any] | OneWireSensor) -> str:
    key = item.address if isinstance(item, OneWireSensor) else item["id"]
    return f"/h/{cls}/{key}"


def test_generate_webio_json_forwards_path_callable_and_server(parsed: dict[str, Any]) -> None:
    template = json.loads(generate_webio_json(_path_by_id, "n [M]", parsed, WebioClass.MARKER, server="s"))

    expected = [f"/h/marker/{m['id']}" for m in parsed["markers"]]
    assert sorted(c["Parameter"] for c in template["commands"]) == sorted(expected)
    assert all('server="s"' in c["Data"] for c in template["commands"])


def test_path_callable_that_handles_one_class_only_serves_that_class_upload(parsed: dict[str, Any]) -> None:
    def markers_only(cls: WebioClass, item: Any) -> str:
        assert cls is WebioClass.MARKER
        return f"/h/{item['id']}"

    commands = build_webio_commands(markers_only, parsed, WebioClass.MARKER)

    assert commands


def test_non_string_server_is_rejected(parsed: dict[str, Any]) -> None:
    with pytest.raises(TypeError, match="server must be a str"):
        build_webio_commands(_HOOK, parsed, WebioClass.MARKER, server=5)  # type: ignore[arg-type]


def test_non_string_server_is_rejected_without_any_command() -> None:
    with pytest.raises(TypeError, match="server must be a str"):
        build_webio_commands(_HOOK, {}, server=5)  # type: ignore[arg-type]


def test_path_callable_returning_a_non_string_is_rejected(parsed: dict[str, Any]) -> None:
    with pytest.raises(TypeError, match="must return a str"):
        build_webio_commands(lambda cls, item: 7, parsed, WebioClass.MARKER)  # type: ignore[arg-type,return-value]


def test_lua_escape_leaves_line_breaks_of_existing_fields_alone() -> None:
    assert lua_escape("a\r\nb") == "a\r\nb"


def test_server_name_with_line_break_stays_a_valid_lua_string(parsed: dict[str, Any]) -> None:
    commands = build_webio_commands(_HOOK, parsed, WebioClass.MARKER, server="a\r\nb\0c")

    assert all(', server="a\\r\\nb\\000c" }' in cmd["Data"] for cmd in commands)


# --- 1-Wire ---


def test_onewire_class_name() -> None:
    assert webio_class_name("HomeAssistant", WebioClass.ONEWIRE) == "HomeAssistant [1Wire]"


def test_build_onewire_webio_command_analog() -> None:
    command = build_onewire_webio_command(_sensor(2, "OT", TYPE_ANALOG), _HOOK, server="srv")

    assert (command["Name"], command["TypeId"], command["Min"], command["Max"], command["Parameter"]) == (
        "HA 1Wire OT2",
        2,
        *_GENERIC_RANGE,
        _HOOK,
    )
    assert 'local d = { id="OT2", value=a, type="onewire", server="srv" }' in command["Data"]
    assert command["Protocol"] == WEBIO_PROTOCOL_HTTP


def test_onewire_address_with_quotes_and_line_breaks_stays_a_valid_lua_string() -> None:
    sensor = OneWireSensor(
        address='O"T\\1\r\n\x001', config_id=1, prefix="OT", name="Sensor 1", hw_id="28-1", kind=TYPE_ANALOG
    )

    command = build_onewire_webio_command(sensor, _HOOK)

    # NUL before a digit: \0001 (NUL, then "1"), not \01 (byte 1).
    assert 'local d = { id="O\\"T\\\\1\\r\\n\\0001", value=a, type="onewire" }' in command["Data"]


def test_build_onewire_webio_command_digital() -> None:
    command = build_onewire_webio_command(_sensor(10, "OIB", TYPE_DIGITAL), _HOOK, protocol=WEBIO_PROTOCOL_HTTPS)

    assert (command["Name"], command["TypeId"], command["Min"], command["Max"]) == ("HA 1Wire OIB10", 1, 0, 1)
    assert 'id="OIB10", value=a, type="onewire" }' in command["Data"]
    assert command["Protocol"] == WEBIO_PROTOCOL_HTTPS


@pytest.mark.parametrize("kind", [None, "analogue"])
def test_build_onewire_webio_command_refuses_an_unknown_kind(kind: str | None) -> None:
    sensor = _sensor(4, "OT", kind)

    with pytest.raises(ValueError, match=_exactly("1-Wire sensor 'OT4' has an unknown kind, no Web-IO command")):
        build_onewire_webio_command(sensor, _HOOK)


def test_build_onewire_webio_command_escapes_the_address() -> None:
    sensor = OneWireSensor(address='O"T\\1', config_id=1, prefix="O", name="", hw_id="", kind=TYPE_ANALOG)

    assert 'id="O\\"T\\\\1"' in build_onewire_webio_command(sensor, _HOOK)["Data"]


def test_build_webio_commands_builds_one_command_per_sensor_by_config_id(parsed: dict[str, Any]) -> None:
    commands = build_webio_commands(_HOOK, _source(parsed, WebioClass.ONEWIRE), WebioClass.ONEWIRE)

    assert [cmd["Name"] for cmd in commands] == ["HA 1Wire OT2", "HA 1Wire OH3", "HA 1Wire OIB10"]


def test_build_webio_commands_without_a_class_puts_the_onewire_commands_last(parsed: dict[str, Any]) -> None:
    commands = build_webio_commands(
        _HOOK, _source(parsed, WebioClass.ONEWIRE), None, protocol=WEBIO_PROTOCOL_HTTPS, server="s"
    )

    assert [cmd["Name"] for cmd in commands[-3:]] == ["HA 1Wire OT2", "HA 1Wire OH3", "HA 1Wire OIB10"]
    assert len(commands) == len(build_webio_commands(_HOOK, parsed)) + 3
    assert all(cmd["Protocol"] == WEBIO_PROTOCOL_HTTPS and 'server="s"' in cmd["Data"] for cmd in commands[-3:])


@pytest.mark.parametrize("webio_class", [None, WebioClass.ONEWIRE, WebioClass.MARKER])
def test_build_webio_commands_skips_a_sensor_of_unknown_kind(
    parsed: dict[str, Any], caplog: pytest.LogCaptureFixture, webio_class: WebioClass | None
) -> None:
    """Logged at warning for every class: the sensor stays without pushed values, whichever class is uploaded."""
    sensors = {**_onewire_sensors(), "OT4": _sensor(4, "OT", None), "OT5": _sensor(5, "OT", "analogue")}
    asked: list[Any] = []

    def path_for(cls: WebioClass, item: Any) -> str:
        asked.append(item)
        return _HOOK

    commands = build_webio_commands(path_for, {**parsed, "onewire": sensors}, webio_class)

    names = [cmd["Name"] for cmd in commands]
    assert "HA 1Wire OT4" not in names
    assert "HA 1Wire OT5" not in names
    assert all(getattr(item, "address", None) not in ("OT4", "OT5") for item in asked)
    assert _webio_log(caplog) == [
        (
            logging.WARNING,
            "2 1-Wire sensors are of unknown kind (neither analog nor digital) and get no Web-IO command: OT4, OT5",
        )
    ]


@pytest.mark.parametrize("onewire", [None, {}])
def test_build_webio_commands_without_known_sensors_builds_no_onewire_command(
    parsed: dict[str, Any], onewire: dict[str, OneWireSensor] | None
) -> None:
    assert build_webio_commands(_HOOK, {**parsed, "onewire": onewire}, WebioClass.ONEWIRE) == []


def test_build_webio_commands_without_the_onewire_key_builds_no_onewire_command(parsed: dict[str, Any]) -> None:
    assert "onewire" not in parsed
    assert build_webio_commands(_HOOK, parsed, WebioClass.ONEWIRE) == []


_BAD_ONEWIRE_VALUES = [
    pytest.param([], id="empty list"),
    pytest.param("", id="empty str"),
    pytest.param(["OT1"], id="list"),
    pytest.param({"OT1": {"address": "OT1", "kind": "analog"}}, id="dict value"),
    pytest.param({"OT2": _sensor(2, "OT", TYPE_ANALOG), "OT1": "x"}, id="one value not a sensor"),
    pytest.param({"OT9": _sensor(2, "OT", TYPE_ANALOG)}, id="sensor under another address"),
    pytest.param(
        {"OT2": _sensor(2, "OT", TYPE_ANALOG), "OH9": _sensor(3, "OH", TYPE_ANALOG)},
        id="one of two sensors under another address",
    ),
]


@pytest.mark.parametrize("onewire", _BAD_ONEWIRE_VALUES)
@pytest.mark.parametrize("webio_class", [None, WebioClass.ONEWIRE])
def test_build_webio_commands_refuses_onewire_data_of_another_type(
    parsed: dict[str, Any], onewire: Any, webio_class: WebioClass | None
) -> None:
    """A wrong value never passes as an installation without sensors."""
    source = {**parsed, "onewire": onewire}

    with pytest.raises(TypeError, match=_exactly(_BAD_ONEWIRE_DATA)):
        build_webio_commands(_HOOK, source, webio_class)


@pytest.mark.parametrize("onewire", _BAD_ONEWIRE_VALUES)
@pytest.mark.parametrize(
    ("webio_class", "label"),
    [(WebioClass.MARKER, "Marker"), (WebioClass.IO, "IO"), (WebioClass.KNX, "KNX"), (WebioClass.QUICKBUS, "Quickbus")],
)
def test_onewire_data_of_another_type_does_not_abort_another_class(
    parsed: dict[str, Any], caplog: pytest.LogCaptureFixture, onewire: Any, webio_class: WebioClass, label: str
) -> None:
    """Like a malformed marker id, bad 1-Wire data must not abort the upload of an unrelated class."""
    commands = build_webio_commands(_HOOK, {**parsed, "onewire": onewire}, webio_class)

    assert commands == build_webio_commands(_HOOK, parsed, webio_class)
    assert _webio_log(caplog) == [
        (
            logging.ERROR,
            f"{_BAD_ONEWIRE_DATA}; the {label} commands are built without 1-Wire sensors "
            "and not checked for name collisions with them",
        )
    ]


@pytest.mark.parametrize("webio_class", [WebioClass.MARKER, WebioClass.IO, WebioClass.KNX])
def test_onewire_sensors_leave_the_other_classes_untouched(parsed: dict[str, Any], webio_class: WebioClass) -> None:
    asked: list[tuple[WebioClass, Any]] = []

    def path_for(cls: WebioClass, item: Any) -> str:
        asked.append((cls, item))
        return _HOOK

    commands = build_webio_commands(path_for, _source(parsed, WebioClass.ONEWIRE), webio_class)

    assert commands == build_webio_commands(_HOOK, parsed, webio_class)
    # A 1-Wire command is only built for the collision check: its path is never asked for.
    assert {cls for cls, _ in asked} == {webio_class}
    assert not any(isinstance(item, OneWireSensor) for _, item in asked)


@pytest.mark.parametrize(
    ("marker_name", "logged_names"),
    [("1Wire OT2", "1 Web-IO command names"), ("1wire ot2", "2 Web-IO command names")],
    ids=["same case", "other case"],
)
def test_a_onewire_command_name_colliding_with_a_marker_drops_both(
    parsed: dict[str, Any], caplog: pytest.LogCaptureFixture, marker_name: str, logged_names: str
) -> None:
    """Names collide ignoring case, like every other Web-IO command name."""
    clash = {"id": "99", "name": marker_name, "type": "analog"}
    source = {**_source(parsed, WebioClass.ONEWIRE), "markers": [*parsed["markers"], clash]}

    onewire_commands = build_webio_commands(_HOOK, source, WebioClass.ONEWIRE)
    marker_commands = build_webio_commands(_HOOK, source, WebioClass.MARKER)

    # Only the two colliding commands go: the other sensors and markers stay.
    assert [cmd["Name"] for cmd in onewire_commands] == ["HA 1Wire OH3", "HA 1Wire OIB10"]
    assert marker_commands == build_webio_commands(_HOOK, parsed, WebioClass.MARKER)
    assert marker_commands
    names = sorted({"HA 1Wire OT2", f"HA {marker_name}"})
    collision = (
        logging.WARNING,
        f"{logged_names} are generated more than once, those commands are left out "
        f"(rename the item in Comexio or change the naming schema): {', '.join(names)}",
    )
    # Logged once per build: the ONEWIRE and the MARKER build above.
    assert _webio_log(caplog) == [collision, collision]


def test_generate_webio_json_for_the_onewire_class(parsed: dict[str, Any]) -> None:
    source = _source(parsed, WebioClass.ONEWIRE)

    template = json.loads(generate_webio_json(_HOOK, "HomeAssistant [1Wire]", source, WebioClass.ONEWIRE))

    assert template["base"]["Identifier"] == "HomeAssistant [1Wire]"
    assert template["commands"] == build_webio_commands(_HOOK, source, WebioClass.ONEWIRE)
