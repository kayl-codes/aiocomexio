"""Web-IO command and class-template builders."""

import json
import logging
from typing import Any

import pytest
from syrupy.assertion import SnapshotAssertion

from aiocomexio.config import parse_config
from aiocomexio.const import (
    WEBIO_CLASS_NAME_KNX_LOOPBACK,
    WEBIO_MARKER_ANALOG_MAX,
    WEBIO_MARKER_ANALOG_MIN,
    WebioClass,
)
from aiocomexio.knx import KNX_DPT_ANALOG_RANGES
from aiocomexio.webio import (
    WEBIO_PROTOCOL_HTTP,
    WEBIO_PROTOCOL_HTTPS,
    build_io_webio_command,
    build_knx_loopback_webio_command,
    build_marker_webio_command,
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


def test_build_webio_commands_snapshot(parsed: dict[str, Any], snapshot: SnapshotAssertion) -> None:
    """Characterization snapshot of every command built from the basic fixture."""
    assert build_webio_commands(_HOOK, parsed) == snapshot


def test_build_webio_commands_default_protocol_is_http(parsed: dict[str, Any]) -> None:
    assert {cmd["Protocol"] for cmd in build_webio_commands(_HOOK, parsed)} == {WEBIO_PROTOCOL_HTTP}


@pytest.mark.parametrize("webio_class", list(WebioClass))
def test_build_webio_commands_writes_the_protocol(parsed: dict[str, Any], webio_class: WebioClass) -> None:
    # The basic fixture has no Quickbus devices; a missing IO type table keeps their IOs active.
    source = parse_config(load_json_fixture("config_quickbus.json")) if webio_class is WebioClass.QUICKBUS else parsed
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
    source = parse_config(load_json_fixture("config_quickbus.json")) if webio_class is WebioClass.QUICKBUS else parsed
    commands = build_webio_commands(_HOOK, source, webio_class, server='io"srv1')

    assert commands
    assert all(', server="io\\"srv1" }' in cmd["Data"] for cmd in commands)


@pytest.mark.parametrize("webio_class", list(WebioClass))
def test_build_webio_commands_gives_every_command_the_path_of_its_own_item(
    parsed: dict[str, Any], webio_class: WebioClass
) -> None:
    """A callable path gets the class and the parsed item of each command, so no two share a path."""
    source = parse_config(load_json_fixture("config_quickbus.json")) if webio_class is WebioClass.QUICKBUS else parsed

    asked: list[tuple[WebioClass, str]] = []
    items: list[Any] = []

    def path_for(cls: WebioClass, item: Any) -> str:
        path = f"/h/{cls}/{item['id']}/{item.get('ext_name', '')}"
        asked.append((cls, path))
        items.append(item)
        return path

    commands = build_webio_commands(path_for, source, webio_class)

    assert commands
    # Exactly the paths the callback returned, one per command, and only for the wanted class.
    assert sorted(cmd["Parameter"] for cmd in commands) == sorted(path for _, path in asked)
    assert {cls for cls, _ in asked} == {webio_class}
    assert len({cmd["Parameter"] for cmd in commands}) == len(commands)
    # The path of a command was asked for with the very item that command is built from: its payload names it.
    by_path = {cmd["Parameter"]: cmd for cmd in commands}
    for (_, path), item in zip(asked, items, strict=True):
        token = f'io="{item["identifier"]}"' if webio_class is WebioClass.IO else f'id="{item["id"]}"'
        assert token in by_path[path]["Data"]


def test_generate_webio_json_forwards_path_callable_and_server(parsed: dict[str, Any]) -> None:
    template = json.loads(
        generate_webio_json(lambda cls, item: f"/h/{cls}/{item['id']}", "n [M]", parsed, WebioClass.MARKER, server="s")
    )

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


def test_server_name_with_line_break_stays_a_valid_lua_string(parsed: dict[str, Any]) -> None:
    commands = build_webio_commands(_HOOK, parsed, WebioClass.MARKER, server="a\r\nb")

    assert all(', server="a\\r\\nb" }' in cmd["Data"] for cmd in commands)
