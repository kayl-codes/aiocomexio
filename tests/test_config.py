"""parse_config: scraped Comexio config -> markers / IOs / KNX / Web-IO commands."""

from typing import Any

import pytest
from syrupy.assertion import SnapshotAssertion

from aiocomexio.config import (
    TYPE_ANALOG,
    ParseOptions,
    clean_value,
    io_schema_title,
    is_extension_offline,
    iter_group,
    marker_kind,
    parse_config,
)
from aiocomexio.const import MarkerKind, WebioClass
from tests.common import load_json_fixture


def _parse(conf: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    """parse_config with the fixture IO type tables, as the client passes them after scraping."""
    return parse_config(
        conf,
        io_types=load_json_fixture("io_types.json"),
        io_input_types=load_json_fixture("io_input_types.json"),
        **kwargs,
    )


@pytest.fixture
def basic_result() -> dict[str, Any]:
    return _parse(
        load_json_fixture("config_basic.json"),
        live_states={"1": "1", "2": "21,5"},
        referenced_markers={"5"},
        knx_live_states={"1": "19,5"},
        knx_dpt_catalog=load_json_fixture("knx_dpt_catalog.json"),
    )


def _by_id(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {item["id"]: item for item in items}


def test_parse_config_snapshot(basic_result: dict[str, Any], snapshot: SnapshotAssertion) -> None:
    """Characterization snapshot of the complete parse result — the safety net for refactors."""
    assert basic_result == snapshot


def test_markers_named_or_referenced_are_imported(basic_result: dict[str, Any]) -> None:
    markers = _by_id(basic_result["markers"])

    assert sorted(markers, key=int) == ["1", "2", "3", "4", "5", "7"]
    assert markers["1"]["ha_name"] == "M1 Licht Wohnen"
    assert (markers["1"]["type"], markers["1"]["value"]) == ("digital", 1.0)
    assert (markers["2"]["type"], markers["2"]["value"]) == ("analog", 21.5)


def test_unnamed_referenced_marker_gets_placeholder_title(basic_result: dict[str, Any]) -> None:
    marker = _by_id(basic_result["markers"])["5"]

    assert (marker["title"], marker["name"], marker["no_name"]) == ("#nn", "M5 #nn", True)


@pytest.mark.parametrize(
    ("marker_id", "kind"),
    [("1", MarkerKind.NORMAL), ("3", MarkerKind.READ_ONLY), ("4", MarkerKind.TRIGGER), ("7", MarkerKind.KNX_BRIDGE)],
)
def test_marker_kind_from_title_suffix(basic_result: dict[str, Any], marker_id: str, kind: MarkerKind) -> None:
    assert _by_id(basic_result["markers"])[marker_id]["kind"] == kind


@pytest.mark.parametrize(
    ("title", "is_marker", "kind"),
    [
        ("Rollo [K3]", True, MarkerKind.KNX_BRIDGE),
        ("Rollo [K3]", False, MarkerKind.NORMAL),
        ("Boiler [RO]  ", True, MarkerKind.READ_ONLY),
        ("Taster [TRIG] [RO]", True, MarkerKind.READ_ONLY),
        ("Taster [TP]", False, MarkerKind.TRIGGER),
        ("Licht", True, MarkerKind.NORMAL),
    ],
)
def test_marker_kind(title: str, is_marker: bool, kind: MarkerKind) -> None:
    assert marker_kind(title, is_marker=is_marker) == kind


def test_io_classification(basic_result: dict[str, Any]) -> None:
    ios = {io["identifier"]: io for io in basic_result["io_all"]}

    assert (ios["I1"]["is_binary"], ios["I1"]["is_input"]) == (True, True)
    assert (ios["Q1"]["is_binary"], ios["Q1"]["is_input"], ios["Q1"]["unit"]) == (True, False, "")
    assert (ios["AI1"]["is_binary"], ios["AI1"]["unit"], ios["AI1"]["value"]) == (False, "°C", 21.5)
    assert ios["AI1"]["name"] == "BASE AI1"
    assert (ios["QI1"]["is_input"], ios["QI1"]["offline"]) == (True, True)


def test_io_classification_without_type_tables_falls_back_to_identifier() -> None:
    conf = {
        "FubModules": {
            "1": {
                "1": {
                    "extension": {"Name": "BASE", "Identifier": "1000-2000-3000"},
                    "inoutput": {
                        "1": {"Id": 1, "Identifier": "Q1", "InOutputTypeId": 2, "Active": True},
                        "2": {"Id": 2, "Identifier": "QI1", "InOutputTypeId": 6, "Active": True},
                        "3": {"Id": 3, "Identifier": "Temp", "InOutputTypeId": "x", "Active": True},
                    },
                }
            }
        }
    }

    ios = {io["identifier"]: io for io in parse_config(conf)["io"]}

    assert (ios["Q1"]["is_binary"], ios["Q1"]["max"], ios["Q1"]["is_input"]) == (True, 1, False)
    assert (ios["QI1"]["is_binary"], ios["QI1"]["max"], ios["QI1"]["is_input"]) == (False, 0, True)
    assert (ios["Temp"]["type_id_raw"], ios["Temp"]["is_input"]) == (1, True)


def test_inactive_io_is_labelled_but_gets_no_entity(basic_result: dict[str, Any]) -> None:
    all_ids = {io["identifier"] for io in basic_result["io_all"]}
    active_ids = {io["identifier"] for io in basic_result["io"]}

    assert all_ids - active_ids == {"Q2"}


def test_extensions_carry_name_and_serial(basic_result: dict[str, Any]) -> None:
    assert basic_result["extensions"] == {
        "1": {"name": "BASE", "serial": "1000-2000-3000"},
        "2": {"name": "UD 1", "serial": "5010"},
    }


def test_webio_commands_only_contain_own_classes(basic_result: dict[str, Any]) -> None:
    assert basic_result["webio_commands"] == {
        "HA M1 Licht Wohnen": {"webIoId": "101", "cmdId": 5, "typeId": 1, "webioClass": WebioClass.MARKER},
        "HA M2 Solltemperatur": {"webIoId": "102", "cmdId": 6, "typeId": 2, "webioClass": WebioClass.MARKER},
        "HA IO BASE Q1": {"webIoId": "103", "cmdId": 7, "typeId": 1, "webioClass": WebioClass.IO},
    }


def test_webio_name_lexicon_covers_foreign_devices(basic_result: dict[str, Any]) -> None:
    assert basic_result["webio_names"]["104"] == {"name": "40. Fremdbefehl", "analog": True}
    assert "105" not in basic_result["webio_names"]


def test_webio_device_ip_is_stripped(basic_result: dict[str, Any]) -> None:
    """Regression: Comexio scrapes a leading space into Ip, which faked an IP-mismatch audit."""
    devices = basic_result["webio_devices"]

    assert devices[WebioClass.MARKER] == {"device_id": "30", "device_ip": "192.168.1.20:8123", "base_id": "7"}
    assert devices[WebioClass.IO]["device_id"] == "31"
    assert devices[WebioClass.KNX] == {"device_id": None, "device_ip": None, "base_id": None}


def test_array_shaped_groups_are_parsed_like_objects() -> None:
    """Regression: gap-free id groups arrive as JSON arrays and crashed setup with `.items()`."""
    result = _parse(load_json_fixture("config_array_groups.json"))

    assert [(m["id"], m["type"]) for m in result["markers"]] == [("0", "digital"), ("1", "analog")]
    assert {name: cmd["webIoId"] for name, cmd in result["webio_commands"].items()} == {
        "HA M0 Merker Null": "0",
        "HA M1 Merker Eins": "1",
        "HA IO BASE Q1": "7",
    }
    assert result["webio_names"]["1"]["name"] == "0. HA M1 Merker Eins"


def test_empty_config_yields_empty_result() -> None:
    result = parse_config({})

    assert (result["markers"], result["io"], result["knx"], result["webio_commands"]) == ([], [], [], {})


def test_legacy_single_webio_device_logs_migration_hint(caplog: pytest.LogCaptureFixture) -> None:
    parse_config({"WebDevices": {"1": {"Name": "HomeAssistant", "Ip": "192.168.1.20"}}})

    assert "Found a legacy Web-IO device named 'HomeAssistant'" in caplog.text


def test_options_override_names_and_schema() -> None:
    conf = {
        "FubModules": {"2": {"1": {"Id": 1, "Name": "Licht", "Type": 1}}},
        "WebDevices": {"9": {"Name": "Haus [M]", "Ip": "192.168.1.20", "WebDeviceBaseId": 1}},
    }
    options = ParseOptions(webio_name="Haus", server_alias="srv", schema_marker="{ServerAlias} {MarkerTitle} {Unknown}")

    result = parse_config(conf, options=options)

    assert result["markers"][0]["ha_name"] == "srv Licht {Unknown}"
    assert result["webio_devices"][WebioClass.MARKER]["device_id"] == "9"


def test_io_without_description_gets_placeholder_title(basic_result: dict[str, Any]) -> None:
    """Regression: an IO with an empty description rendered as "BASE AI1 AI1" under the default schema."""
    io = _by_id(basic_result["io"])["13"]

    assert (io["ha_name"], io["name"]) == ("AI1 #nn", "BASE AI1")


@pytest.mark.parametrize(
    ("desc", "ident", "expected"),
    [
        ("Licht Flur", "Q1", "Licht Flur"),
        ("  Licht Flur ", "Q1", "Licht Flur"),
        ("", "I6", "#nn"),
        ("   ", "I6", "#nn"),
        ("I6", "I6", "#nn"),
        ("i6", "I6", "#nn"),
        ("I6 Diele", "I6", "I6 Diele"),
    ],
)
def test_io_schema_title(desc: str, ident: str, expected: str) -> None:
    assert io_schema_title(desc, ident) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("21,5", 21.5), ("3", 3.0), (7, 7.0), (None, 0.0), ("n/a", 0.0), ([1], 0.0)],
)
def test_clean_value(raw: Any, expected: float) -> None:
    assert clean_value(raw) == expected


def test_iter_group_accepts_object_array_and_none() -> None:
    assert list(iter_group({"3": "a"})) == [("3", "a")]
    assert list(iter_group(["a", "b"])) == [("0", "a"), ("1", "b")]
    assert list(iter_group(None)) == []
    assert list(iter_group("garbage")) == []


def test_non_dict_webio_command_entries_are_skipped() -> None:
    conf = {
        "WebDevices": {"30": {"Name": "HomeAssistant [M]", "Ip": "192.168.1.20", "WebDeviceBaseId": 7}},
        "FubModules": {"10": {"30": {"101": {"Name": "HA M1 Licht", "WebCommandId": 5, "TypeId": "x"}, "102": None}}},
    }

    result = parse_config(conf)

    assert result["webio_commands"] == {
        "HA M1 Licht": {"webIoId": "101", "cmdId": 5, "typeId": 1, "webioClass": WebioClass.MARKER}
    }
    assert list(result["webio_names"]) == ["101"]


@pytest.mark.parametrize("field", ["schema_marker", "schema_io", "schema_knx"])
@pytest.mark.parametrize("schema", ["M{MarkerId", "{0} x", "{MarkerTitle.nope}"])
def test_parse_options_rejects_malformed_schema(field: str, schema: str) -> None:
    with pytest.raises(ValueError, match=f"Invalid {field}"):
        ParseOptions(**{field: schema})


def test_missing_io_types_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    parse_config(load_json_fixture("config_basic.json"))

    assert "parse_config called without io_types" in caplog.text


def test_empty_io_types_is_not_logged_again(caplog: pytest.LogCaptureFixture) -> None:
    """{} = scraped but absent — parse_io_types already warned about that."""
    parse_config(load_json_fixture("config_basic.json"), io_types={})

    assert "parse_config called without io_types" not in caplog.text


def test_malformed_config_entries_are_skipped() -> None:
    """Non-dict groups, extensions, IOs, devices and type-table rows must not crash parsing."""
    conf = {
        "WebDevices": {"1": "garbage", "30": {"Name": "HomeAssistant [M]", "Ip": "1.2.3.4", "WebDeviceBaseId": 7}},
        "FubModules": {
            "1": {
                "1": "garbage",
                "2": {
                    "extension": "garbage",
                    "inoutput": [
                        {"Id": 5, "Identifier": "Q1", "InOutputTypeId": 2, "Active": True},
                        "garbage",
                        None,
                    ],
                },
            },
            "2": {"1": "garbage", "2": {"Id": 2, "Name": "Licht", "Type": 1}},
            "11": {"1": {"Id": 1, "Name": "Wert", "Type": 2}},
        },
    }

    result = parse_config(conf, io_types={"2": "garbage"}, io_input_types={"2": "garbage"})

    assert [io["identifier"] for io in result["io"]] == ["Q1"]
    assert result["io"][0]["ext_name"] == "Ext2"
    assert result["extensions"] == {"2": {"name": "Ext2", "serial": ""}}
    assert [m["id"] for m in result["markers"]] == ["2"]
    assert result["knx"][0]["type"] == TYPE_ANALOG
    assert result["webio_devices"][WebioClass.MARKER]["device_id"] == "30"


def test_malformed_identifiers_and_webio_command_names_do_not_crash(caplog: pytest.LogCaptureFixture) -> None:
    conf = {
        "WebDevices": {"30": {"Name": "HomeAssistant [M]", "Ip": "1.2.3.4", "WebDeviceBaseId": 7}},
        "FubModules": {
            "1": {
                "2": {
                    "extension": {"Name": 7, "Identifier": None},
                    "inoutput": {
                        "5": {"Id": 5, "Identifier": 17, "Description": 3, "InOutputTypeId": 2, "Active": True},
                        "6": {"Id": 6, "Identifier": ["x"], "InOutputTypeId": 2, "Active": True},
                    },
                }
            },
            "10": {"30": {"101": {"WebCommandId": 5, "TypeId": 1}, "102": {"Name": "", "TypeId": 1}}},
        },
    }

    result = parse_config(conf, io_types={"2": {"binary": False, "unit": 5}})

    assert result["extensions"] == {"2": {"name": "Ext2", "serial": ""}}
    assert result["io"][0]["unit"] == ""
    assert [io["identifier"] for io in result["io"]] == ["17", "6"]
    assert result["io"][0]["offline"] is True
    # An empty name stays visible to the consumer's audit; a missing one is skipped loudly.
    assert list(result["webio_commands"]) == [""]
    assert "non-string name None" in caplog.text


@pytest.mark.parametrize("identifier", [None, "", "5010", 17])
def test_is_extension_offline_without_serial(identifier: Any) -> None:
    assert is_extension_offline(identifier) is True


def test_non_string_marker_and_knx_names_do_not_crash(caplog: pytest.LogCaptureFixture) -> None:
    conf = {
        "FubModules": {
            "2": {
                "1": {"Id": 1, "Name": 42, "Type": 1},
                "2": {"Id": 2, "Name": ["x"], "Type": 1},
                "3": {"Id": 3, "Name": 1.5, "Type": 1},
            },
            "11": {"1": {"Id": 1, "Name": {"x": 1}, "Type": 1}},
        }
    }

    result = parse_config(conf, io_types={}, referenced_markers={"2"})

    assert [(m["id"], m["title"], m["no_name"]) for m in result["markers"]] == [
        ("1", "42", False),
        ("2", "#nn", True),
        ("3", "1.5", False),
    ]
    assert result["knx"] == []
    assert "K1 has a non-string name {'x': 1}" in caplog.text


def test_non_mapping_fub_modules_yields_empty_result() -> None:
    result = parse_config({"FubModules": ["garbage"]})

    assert (result["markers"], result["io"], result["knx"]) == ([], [], [])
