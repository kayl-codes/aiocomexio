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


def test_knx_loopback_command_digital() -> None:
    command = build_knx_loopback_webio_command(
        k_id=1, marker_id=300, is_analog=False, knx_dpt_catalog=None, catalog_stale=False
    )

    assert command["Name"] == "KNX K1 to M300"
    assert (command["TypeId"], command["Min"], command["Max"]) == (1, 0, 1)
    assert (command["PostGet"], command["Authentication"], command["Data"]) == (0, 1, "")
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
