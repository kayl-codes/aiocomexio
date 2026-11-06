"""parse_config: scraped Comexio config -> markers / IOs / KNX / Web-IO commands."""

import json
import logging
from typing import Any

import pytest
from syrupy.assertion import SnapshotAssertion

from aiocomexio import ComexioClient, IoAddress
from aiocomexio.config import (
    TYPE_DIGITAL,
    ParseOptions,
    clean_value,
    io_schema_title,
    is_extension_offline,
    iter_group,
    marker_kind,
    normalize_io_unit,
    parse_config,
)
from aiocomexio.const import MarkerKind, WebioClass
from aiocomexio.webio import build_webio_commands
from tests.common import load_json_fixture
from tests.fake_comexio import DASHBOARD_REFRESH_PATH, FakeComexio


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


def test_io_entry_carries_its_extension_id(basic_result: dict[str, Any]) -> None:
    """get_io_live_states needs the extension group key as ExtId; the IO id alone is not enough."""
    ios = {io["identifier"]: io for io in basic_result["io_all"]}

    assert {ident: (io["id"], io["ext_id"]) for ident, io in ios.items()} == {
        "I1": ("11", "1"),
        "Q1": ("12", "1"),
        "AI1": ("13", "1"),
        "Q2": ("14", "1"),
        "QI1": ("21", "2"),
    }


def test_io_live_states_override_the_config_value() -> None:
    """An IO in io_live_states gets that value; an IO missing from it or mapped to None keeps the config's "Value"."""
    result = _parse(load_json_fixture("config_basic.json"), io_live_states={"11": "0", "13": "9,96078", "21": None})
    values = {io["identifier"]: io["value"] for io in result["io_all"]}

    assert values == {"I1": 0.0, "Q1": 0.0, "AI1": 9.96078, "Q2": 0.0, "QI1": 55.0}


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


_DIRECTION_IO_TYPES = {
    "1": {"id": 1, "binary": False, "min": 0, "max": 10, "unit": "V"},
    "2": {"id": 2, "binary": True, "min": 0, "max": 1, "unit": "0/1"},
    "80": {"id": 80, "binary": False, "min": -2000, "max": 2000, "unit": "var"},
    "81": {"id": 81, "binary": False, "min": "-2147483648", "max": "2147483647", "unit": "Wh"},
    "87": {"id": 87, "binary": True, "min": 0, "max": 1, "unit": "0/1"},
    "120": {"id": 120, "binary": True, "min": 0, "max": 1, "unit": "0/1"},
}
_DIRECTION_IO_INPUT_TYPES = {
    "1": {"id": 1, "input": True},
    "2": {"id": 2, "input": False},
    "80": {"id": 80, "input": False},
    "81": {"id": 81, "input": False},
    "87": {"id": 87, "input": False},
    "120": {"id": 120, "input": True},
}


@pytest.mark.parametrize(
    ("identifier", "type_id", "flag", "is_input"),
    [
        ("Q1", 80, 0, True),  # Smart Meter reactive power: IntIsOutput 0 beats the relay prefix
        ("E1", 81, 0, True),  # energy counter, $IOInputTypes says output
        ("DI1", 87, 0, True),
        ("DI1", 87, "0", True),
        ("Q1", 2, 1, False),  # a real relay stays writable
        ("X1", 1, 1, False),  # IntIsOutput 1: Comexio writes it, so does HA (Alarm1, Bright1, CurVol1)
        ("Alarm1", 120, 1, False),  # IntIsOutput 1 beats $IOInputTypes saying input
        ("Alarm1", 120, "1", False),
        ("Alarm1", 120, None, True),  # no flag: $IOInputTypes as before
        ("I1", 2, 1, False),  # IntIsOutput 1 beats the input prefix: Comexio writes it
        ("QI1", 2, 1, False),
        ("I1", 2, 0, True),
        ("I1", 2, None, True),  # no flag: the input prefix beats $IOInputTypes saying output
        ("I1", 2, "yes", True),  # an unusable flag is no flag
        ("I1", 2, True, True),
        ("Q1", 1, 2, False),
        ("AO1", 1, None, False),  # no flag: the analog output prefix beats $IOInputTypes saying input
        ("AO1", 1, 1, False),  # 0-10 V analog output, although $IOInputTypes says input
        ("AO4", 120, 1, False),  # "Digital 10V": a binary analog output
        ("AO1", 1, 0, True),  # IntIsOutput 0 beats the analog output prefix as well
        ("AO1\n", 1, None, True),  # matched exactly: no AO, so $IOInputTypes decides
        ("AO١", 1, None, True),  # non-ASCII digit: no AO either
        ("Q1\n", 2, None, False),  # no relay output either, $IOInputTypes says output
        ("DI1", 87, None, False),  # no flag: $IOInputTypes as before
        ("DI1", 87, "yes", False),
        ("DI1", 87, True, False),
        ("DI1", 87, False, False),  # bool is no flag, although False == 0
        ("DI1", 87, 0.0, False),
        ("DI1", 87, " 0", False),
        ("DI1", 87, 2, False),
    ],
)
def test_int_is_output_flag_sets_the_direction(identifier: str, type_id: int, flag: Any, is_input: bool) -> None:
    """IntIsOutput 0 makes an IO read-only (#249), 1 writable, whatever else says; without it the prefix decides."""
    row: dict[str, Any] = {"Id": 1, "Identifier": identifier, "InOutputTypeId": type_id, "Active": True}
    if flag is not None:
        row["IntIsOutput"] = flag
    conf = {"FubModules": {"1": {"1": {"extension": {"Name": "SM1", "Identifier": "1-2-3"}, "inoutput": {"1": row}}}}}

    (io,) = parse_config(conf, io_types=_DIRECTION_IO_TYPES, io_input_types=_DIRECTION_IO_INPUT_TYPES)["io_all"]

    assert io["is_input"] is is_input


@pytest.mark.parametrize(
    ("unit", "expected"),
    [(" ", ""), (" W ", "W"), ("var", "var"), ("0/1", ""), (" ? ", ""), ("\\u00b0C", "°C"), (None, "")],
)
def test_normalize_io_unit(unit: Any, expected: str) -> None:
    assert normalize_io_unit(unit) == expected


def test_io_type_missing_from_the_table_falls_back_to_identifier() -> None:
    # Regression: with io_types present, an IO whose type row is missing became "analog, max 1"
    # — a QI1 dimmer output then got the range 0..1 instead of the identifier's analog default.
    conf = {
        "FubModules": {
            "1": {
                "1": {
                    "extension": {"Name": "BASE", "Identifier": "1000-2000-3000"},
                    "inoutput": {
                        "1": {"Id": 1, "Identifier": "Q1", "InOutputTypeId": 99, "Active": True},
                        "2": {"Id": 2, "Identifier": "QI1", "InOutputTypeId": 98, "Active": True},
                        "3": {"Id": 3, "Identifier": "AI1", "InOutputTypeId": 3, "Active": True},
                        "4": {"Id": 4, "Identifier": "AO1", "InOutputTypeId": 97, "Active": True, "IntIsOutput": 1},
                    },
                }
            }
        }
    }
    io_types = {"3": {"binary": False, "min": 0, "max": 100, "unit": "%"}}

    ios = {io["identifier"]: io for io in parse_config(conf, io_types=io_types)["io"]}

    assert (ios["Q1"]["is_binary"], ios["Q1"]["max"]) == (True, 1)
    assert (ios["QI1"]["is_binary"], ios["QI1"]["max"]) == (False, 0)
    assert (ios["AI1"]["is_binary"], ios["AI1"]["max"]) == (False, 100)
    # A writable analog output keeps the Web-IO default range instead of "analog, max 1" (#259).
    assert (ios["AO1"]["is_binary"], ios["AO1"]["min"], ios["AO1"]["max"], ios["AO1"]["is_input"]) == (
        False,
        None,
        None,
        False,
    )


@pytest.mark.parametrize("description", ["ai1", " AI1 ", "AI1", "", None])
def test_io_description_repeating_the_identifier_is_no_description(description: str | None) -> None:
    # Regression: the legacy name compared case-sensitively and unstripped, so "ai1" gave
    # "BASE AI1 ai1" while {IoTitle} (casefolded) already said "#nn".
    conf = {
        "FubModules": {
            "1": {
                "1": {
                    "extension": {"Name": "BASE", "Identifier": "1000-2000-3000"},
                    "inoutput": {
                        "1": {"Id": 1, "Identifier": "AI1", "Description": description, "Active": True},
                    },
                }
            }
        }
    }

    [io] = parse_config(conf, io_types={})["io"]

    assert (io["name"], io["ha_name"]) == ("BASE AI1", "AI1 #nn")


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


@pytest.mark.parametrize("raw", ["nan", "inf", "-Infinity", float("nan"), float("-inf")])
def test_clean_value_reads_non_finite_as_zero_with_a_warning(raw: Any, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="aiocomexio.config"):
        assert clean_value(raw) == 0.0

    assert "Failed to clean value" in caplog.text


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


@pytest.mark.parametrize("type_id", [float("inf"), float("nan")])
def test_non_finite_webio_type_id_falls_back(type_id: float) -> None:
    """json.loads accepts Infinity/NaN; int() of them must not abort parse_config."""
    conf = {
        "WebDevices": {"30": {"Name": "HomeAssistant [M]", "Ip": "192.168.1.20", "WebDeviceBaseId": 7}},
        "FubModules": {"10": {"30": {"101": {"Name": "HA M1 Licht", "WebCommandId": 5, "TypeId": type_id}}}},
    }

    assert parse_config(conf)["webio_commands"] == {
        "HA M1 Licht": {"webIoId": "101", "cmdId": 5, "typeId": 1, "webioClass": WebioClass.MARKER}
    }


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
    # A garbage type row is no row: classified by identifier / unresolved, not silently "analog".
    assert (result["io"][0]["is_binary"], result["io"][0]["max"]) == (True, 1)
    assert result["extensions"] == {"2": {"name": "Ext2", "serial": ""}}
    assert [m["id"] for m in result["markers"]] == ["2"]
    assert (result["knx"][0]["type"], result["knx"][0]["dpt_ambiguous"]) == (TYPE_DIGITAL, True)
    assert result["webio_devices"][WebioClass.MARKER]["device_id"] == "30"


@pytest.mark.parametrize(
    ("row", "logged"),
    [
        ({"binary": None, "min": 0, "max": 100}, "'2': None"),
        ({"binary": "false", "min": 0, "max": 100}, "'2': 'false'"),
        ({"binary": 0, "min": 0, "max": 100}, "'2': 0"),
        # Without "binary" the kind is just as unknown — no silent "analog".
        ({"min": 0, "max": 100}, "'2': missing"),
        # Not a mapping at all: just as unusable, and reported as well.
        ("binary", "'2': not a row ('binary')"),
    ],
)
def test_a_type_row_without_a_bool_binary_is_no_row(row: Any, logged: str, caplog: pytest.LogCaptureFixture) -> None:
    """Kept, "binary": null would put is_binary None into an active IO and abort the Web-IO build."""
    conf = {
        "FubModules": {
            "1": {"2": {"inoutput": {"5": {"Id": 5, "Identifier": "Q1", "InOutputTypeId": 2, "Active": True}}}},
            "11": {"1": {"Id": 1, "Name": "Wert", "Type": 2}},
        },
    }

    with caplog.at_level(logging.WARNING):
        result = parse_config(conf, io_types={"2": row})

    # Once per parse, not once per IO / KNX object reading the row.
    rows_warnings = [r.getMessage() for r in caplog.records if "IO type table rows" in r.getMessage()]
    assert len(rows_warnings) == 1
    assert '1 IO type table rows have no boolean "binary" and are ignored' in rows_warnings[0]
    assert logged in rows_warnings[0]
    # The only row is unusable, so the table counts as missing.
    assert (
        "The IO type table has no usable row at all — handled like a missing table: IOs are classified by their "
        "identifier only (a Quickbus IO, whose identifier says nothing, as analog)"
    ) in [r.getMessage() for r in caplog.records]

    # Classified by identifier (Q1 = binary output) / unresolved KNX, like a type without a row.
    assert [(io["identifier"], io["is_binary"], io["max"]) for io in result["io"]] == [("Q1", True, 1)]
    assert (result["knx"][0]["type"], result["knx"][0]["dpt_ambiguous"]) == (TYPE_DIGITAL, True)
    commands = build_webio_commands("/api/webhook/x", result)
    assert [c["TypeId"] for c in commands if "Q1" in c["Name"]] == [1]


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


async def test_io_ext_ids_from_parse_config_feed_get_io_live_states(
    basic_result: dict[str, Any], logged_in: ComexioClient, comexio: FakeComexio
) -> None:
    """parse_config keeps ext_id a str; (ext_id, id) pairs must go into get_io_live_states unchanged."""
    comexio.serve_json("POST", DASHBOARD_REFRESH_PATH, {"result": {"io_11": "1", "io_21": "2,5"}})

    states = await logged_in.get_io_live_states(IoAddress(io["ext_id"], io["id"]) for io in basic_result["io_all"])

    assert states == {"11": "1", "21": "2.5"}
    (request,) = comexio.received_at("POST", DASHBOARD_REFRESH_PATH)
    sent = json.loads(request.form["json"])
    assert sent["io_11"] == {"action": "get", "ExtId": 1, "IOId": "11"}
    assert sent["io_21"] == {"action": "get", "ExtId": 2, "IOId": "21"}
    # The answer's keys are what parse_config's io_live_states expects.
    reparsed = _parse(load_json_fixture("config_basic.json"), io_live_states=states)
    assert {io["identifier"]: io["value"] for io in reparsed["io_all"]}["QI1"] == 2.5


def _multiroom_conf(ext_type: Any, rows: dict[str, dict[str, Any]]) -> dict[str, Any]:
    ext = {"Name": "vMRSRV1", "Identifier": "", "TypeId": ext_type}
    return {"FubModules": {"1": {"18": {"extension": ext, "inoutput": rows}}}}


# The Multiroom-Server's IOs as firmware 11.1.5 lists them: type 151 is an output in $IOInputTypes, type 3 an input.
_MULTIROOM_IO_TYPES = {
    "3": {"id": 3, "binary": True, "min": 0, "max": 1, "unit": "0/1"},
    "56": {"id": 56, "binary": False, "min": 0, "max": 100, "unit": "%"},
    "151": {"id": 151, "binary": True, "min": 0, "max": 1, "unit": "0/1"},
}
_MULTIROOM_IO_INPUT_TYPES = {
    "3": {"id": 3, "input": True},
    "56": {"id": 56, "input": True},
    "151": {"id": 151, "input": False},
}
_MULTIROOM_ROWS = {
    str(io_id): {
        "Id": io_id,
        "Identifier": ident,
        "InOutputTypeId": type_id,
        "Active": 1,
        "IntIsOutput": 1,
        "Timeout": 1,
    }
    for io_id, ident, type_id in [
        (519, "Next1", 151),
        (521, "Prev1", 151),
        (522, "Play1", 151),
        (523, "Pause1", 151),
        (525, "Toggle1", 3),
        (526, "Alarm1", 3),
        (527, "Bell1", 3),
        (529, "Custom1", 3),
        (520, "CurVol1", 56),
    ]
}


def _multiroom_ios(ext_type: Any = 32, rows: dict[str, dict[str, Any]] | None = None) -> dict[str, dict[str, Any]]:
    conf = _multiroom_conf(ext_type, rows if rows is not None else _MULTIROOM_ROWS)
    parsed = parse_config(conf, io_types=_MULTIROOM_IO_TYPES, io_input_types=_MULTIROOM_IO_INPUT_TYPES)
    ios = {io["identifier"]: io for io in parsed["io_all"]}
    assert len(ios) == len(parsed["io_all"]), "an identifier repeats: the dict would hide an IO"
    return ios


def test_multiroom_server_ios_comexio_writes_are_writable_and_its_key_presses_momentary() -> None:
    # Measured live 2026-11-06: Next1-Pause1 and Toggle1 fall back to 0 by themselves, Alarm1/Bell1/Custom1 latch.
    ios = _multiroom_ios()

    assert {ident: io["is_input"] for ident, io in ios.items()} == dict.fromkeys(ios, False)
    assert {ident for ident, io in ios.items() if io["momentary"]} == {"Next1", "Prev1", "Play1", "Pause1", "Toggle1"}


@pytest.mark.parametrize("ext_type", ["32", 32, "032"])
def test_multiroom_server_type_id_is_read_as_a_canonical_id(ext_type: Any) -> None:
    assert _multiroom_ios(ext_type)["Next1"]["momentary"] is True


@pytest.mark.parametrize("ext_type", [27, "27", 37, None, "32 ", True, 32.0])
def test_key_press_names_on_another_extension_are_no_momentary_ios(ext_type: Any) -> None:
    # A Touchpanel's (27, 2G: 37) Toggle1 is not known to reset itself; an unusable TypeId is no Multiroom-Server.
    assert not any(io["momentary"] for io in _multiroom_ios(ext_type).values())


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"IntIsOutput": 0}, "read-only"),
        ({"Identifier": "Next1a"}, "matched exactly"),
        ({"Identifier": "Next"}, "needs the number"),
    ],
)
def test_momentary_needs_a_writable_key_press_io(change: dict[str, Any], reason: str) -> None:
    rows = {"519": {**_MULTIROOM_ROWS["519"], **change}}

    (io,) = _multiroom_ios(rows=rows).values()

    assert io["momentary"] is False, reason


@pytest.mark.parametrize("type_id", [999, None])
def test_a_key_press_without_a_usable_type_row_stays_momentary(type_id: Any) -> None:
    # Device type and identifier make it a key press; the IO type table only says binary or analog.
    rows = {"519": {**_MULTIROOM_ROWS["519"], "InOutputTypeId": type_id}}

    (io,) = _multiroom_ios(rows=rows).values()

    assert io["is_input"] is False
    assert io["momentary"] is True


# A Touchpanel's IOs Comexio writes, as firmware 11.1.5 lists them (Bright1 is type 28 there, analog like 56).
_TOUCHPANEL_ROWS = {
    str(io_id): {**_MULTIROOM_ROWS["519"], "Id": io_id, "Identifier": ident, "InOutputTypeId": type_id}
    for io_id, ident, type_id in [(530, "DisplayOn1", 151), (533, "Bright1", 56), (534, "Mute1", 3)]
}


@pytest.mark.parametrize("ext_type", [27, "37"])
def test_a_touchpanels_display_on_is_momentary(ext_type: Any) -> None:
    # Measured live 2026-11-06 on a Touchpanel (27) and a Touchpanel-2G (37): DisplayOn1 falls back to 0,
    # Bright1 and Mute1 keep their value.
    ios = _multiroom_ios(ext_type, _TOUCHPANEL_ROWS)

    assert {ident for ident, io in ios.items() if io["momentary"]} == {"DisplayOn1"}
    assert not any(io["is_input"] for io in ios.values())


@pytest.mark.parametrize(
    ("identifier", "flag", "momentary"),
    [
        ("displayon1", 1, True),
        ("DISPLAYON2", 1, True),
        ("DisplayOn1", 0, False),  # read-only
        ("DisplayOn", 1, False),  # needs the number
        ("DisplayOn1a", 1, False),  # matched exactly
        ("XDisplayOn1", 1, False),
    ],
)
def test_a_touchpanel_key_press_is_matched_exactly(identifier: str, flag: int, momentary: bool) -> None:
    rows = {"530": {**_TOUCHPANEL_ROWS["530"], "Identifier": identifier, "IntIsOutput": flag}}

    (io,) = _multiroom_ios(37, rows).values()

    assert io["momentary"] is momentary


def test_display_on_of_a_multiroom_server_is_no_momentary_io() -> None:
    assert not any(io["momentary"] for io in _multiroom_ios(32, _TOUCHPANEL_ROWS).values())


@pytest.mark.parametrize("identifier", ["next1", "TOGGLE2", "pAuSe1"])
def test_momentary_ignores_the_case_of_the_identifier(identifier: str) -> None:
    rows = {"519": {**_MULTIROOM_ROWS["519"], "Identifier": identifier}}

    (io,) = _multiroom_ios(rows=rows).values()

    assert io["momentary"] is True


def test_a_quickbus_io_on_a_multiroom_server_is_no_momentary_io() -> None:
    # A Quickbus IO cannot be written by name (its identifier repeats across devices), so it is no key press.
    conf = load_json_fixture("config_quickbus.json")
    base = conf["FubModules"]["1"]["1"]
    base["extension"]["TypeId"] = 32
    base["inoutput"]["204"].update({"Identifier": "Next1", "InOutputTypeId": 151})

    parsed = parse_config(conf, io_types=_MULTIROOM_IO_TYPES, io_input_types=_MULTIROOM_IO_INPUT_TYPES)
    (io,) = [io for io in parsed["io_all"] if io["id"] == "204"]

    assert io["quickbus"] is not None
    assert io["is_input"] is False
    assert io["momentary"] is False


def test_no_io_of_the_quickbus_fixture_is_momentary() -> None:
    conf = load_json_fixture("config_quickbus.json")

    ios = parse_config(conf)["io_all"]

    assert ios
    assert all(io["momentary"] is False for io in ios)
