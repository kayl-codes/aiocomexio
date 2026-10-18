"""Quickbus devices (Multitouch buttons, motion sensors): parsing, Web-IO commands and plan labels.

The IOs of Quickbus devices sit in their extension's "inoutput" group like any other IO, but
their identifiers (G1, TL1, ...) repeat once per device; "lowcan" lists the devices and
"mapping.lowcan" maps each IO "Id" to its device.
"""

import copy
import json
import logging
from typing import Any

import pytest

from aiocomexio import config as config_module
from aiocomexio.config import ParseOptions, parse_config
from aiocomexio.const import WebioClass
from aiocomexio.function_plan.render_geometry import _apply_geometry_target_fields
from aiocomexio.function_plan.render_values import _pill_parts_io, element_search_id, io_owner
from aiocomexio.webio import build_io_webio_command, build_webio_commands, generate_webio_json
from tests.common import load_json_fixture

_HOOK = "/api/webhook/comexio_test"
_UNRESOLVED = "entries point to an unknown device, are malformed or contradict each other"

# Type ids as Comexio sends them: 134 = Multitouch gesture, 126 = motion (PIR), 12 = temperature,
# 111 = LED colour channel. $IOInputTypes marks gestures and PIR as "not an input".
_IO_TYPES = {
    "1": {"binary": True, "min": 0, "max": 1, "unit": "0/1"},
    "12": {"binary": False, "min": 0, "max": 100, "unit": "°C"},
    "111": {"binary": False, "min": 0, "max": 100, "unit": "%"},
    "126": {"binary": True, "min": 0, "max": 1, "unit": "0/1"},
    "134": {"binary": True, "min": 0, "max": 1, "unit": "0/1"},
}
_IO_INPUT_TYPES = {
    "1": {"input": True},
    "12": {"input": True},
    "111": {"input": True},
    "126": {"input": False},
    "134": {"input": False},
}


def _parse(conf: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    return parse_config(
        conf if conf is not None else load_json_fixture("config_quickbus.json"),
        io_types=_IO_TYPES,
        io_input_types=_IO_INPUT_TYPES,
        **kwargs,
    )


def _io_all(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {io["id"]: io for io in result["io_all"]}


def _base_ext(conf: dict[str, Any]) -> dict[str, Any]:
    ext: dict[str, Any] = conf["FubModules"]["1"]["1"]
    return ext


def test_quickbus_devices_are_listed_per_device_id() -> None:
    result = _parse()

    assert result["quickbus_devices"] == {
        "1": {"id": "1", "name": "MT Flur", "serial": "2400-1200-9000", "type_id": 21, "ext_id": "1"},
        "2": {"id": "2", "name": "MT Bad", "serial": "", "type_id": 21, "ext_id": "1"},
        "3": {"id": "3", "name": "BM Diele", "serial": "", "type_id": 22, "ext_id": "2"},
    }


def test_repeated_identifiers_are_tagged_with_their_own_device() -> None:
    """G1/TL1 exist once per device — only the device tag tells them apart."""
    ios = _io_all(_parse())

    assert {io_id: ios[io_id]["identifier"] for io_id in ("201", "211")} == {"201": "G1", "211": "G1"}
    assert ios["201"]["quickbus"] == {"device_id": "1", "name": "MT Flur"}
    assert ios["211"]["quickbus"] == {"device_id": "2", "name": "MT Bad"}
    assert ios["213"]["quickbus"] == {"device_id": "2", "name": "MT Bad"}
    # A device on another extension; its mapping holds the device id once as a digit string.
    assert ios["301"]["quickbus"] == {"device_id": "3", "name": "BM Diele"}
    assert ios["302"]["quickbus"] == {"device_id": "3", "name": "BM Diele"}


def test_plain_ios_next_to_quickbus_devices_stay_plain() -> None:
    ios = _io_all(_parse())

    assert ios["11"]["quickbus"] is None
    assert ios["11"]["name"] == "BASE I1 Taster Flur"
    # The extension's own TL1 shares the identifier with the devices' TL1 but is no device IO.
    assert ios["46"]["quickbus"] is None
    assert ios["46"]["name"] == "BASE TL1 Onboard Temperatur"


def test_quickbus_io_direction_comes_from_int_is_output() -> None:
    """$IOInputTypes marks gestures/PIR as no input — they must still be read-only inputs."""
    ios = _io_all(_parse())

    assert ios["201"]["is_input"] is True  # gesture
    assert ios["301"]["is_input"] is True  # PIR
    assert ios["204"]["is_input"] is False  # LED colour channel


def test_quickbus_io_is_named_after_its_device() -> None:
    ios = _io_all(_parse())

    assert ios["202"]["name"] == "MT Flur G18 Licht Flur"
    assert ios["202"]["ha_name"] == "G18 Licht Flur"
    assert ios["211"]["name"] == "MT Bad G1"
    assert ios["211"]["ha_name"] == "G1 #nn"


def test_ext_name_placeholder_renders_the_device_name() -> None:
    ios = _io_all(_parse(options=ParseOptions(schema_io="{ExtName} {IoId} {IoTitle}")))

    assert ios["202"]["ha_name"] == "MT Flur G18 Licht Flur"
    assert ios["202"]["ext_name"] == "BASE"
    assert ios["11"]["ha_name"] == "BASE I1 Taster Flur"


def test_inactive_quickbus_io_is_labelled_but_not_active() -> None:
    result = _parse()

    assert "212" in _io_all(result)
    assert "212" not in {io["id"] for io in result["io"]}


def test_extension_without_quickbus_has_no_devices() -> None:
    result = parse_config(load_json_fixture("config_basic.json"), io_types=_IO_TYPES)

    assert result["quickbus_devices"] == {}
    assert all(io["quickbus"] is None for io in result["io_all"])


def test_mapping_to_an_unknown_device_leaves_a_plain_io(caplog: pytest.LogCaptureFixture) -> None:
    conf = load_json_fixture("config_quickbus.json")
    _base_ext(conf)["mapping"]["lowcan"]["211"] = 9
    _base_ext(conf)["mapping"]["lowcan"]["213"] = True

    with caplog.at_level(logging.WARNING):
        ios = _io_all(_parse(conf))

    assert ios["211"]["quickbus"] is None
    assert ios["213"]["quickbus"] is None
    assert ios["201"]["quickbus"] == {"device_id": "1", "name": "MT Flur"}
    assert f"2 {_UNRESOLVED}" in caplog.text


def test_device_id_seen_on_two_extensions_drops_both(caplog: pytest.LogCaptureFixture) -> None:
    """Neither extension keeps the id — which one wins must not depend on the parse order."""
    conf = load_json_fixture("config_quickbus.json")
    multibridge = conf["FubModules"]["1"]["2"]
    multibridge["lowcan"] = {"1": {"Id": 1, "Name": "Doppelt", "Identifier": "", "TypeId": 22}}
    multibridge["mapping"]["lowcan"] = {"301": 1, "302": 1}

    with caplog.at_level(logging.WARNING):
        result = _parse(conf)

    ios = _io_all(result)
    assert "1" not in result["quickbus_devices"]
    assert ios["301"]["quickbus"] is None
    assert ios["302"]["quickbus"] is None
    assert ios["201"]["quickbus"] is None
    assert ios["211"]["quickbus"] == {"device_id": "2", "name": "MT Bad"}
    assert (
        "Quickbus device ids are not unique, their devices are dropped and their IOs treated as plain IOs: 1"
        in caplog.text
    )
    # The mapping entries of the dropped id are covered by that warning, not reported a second time.
    assert "Quickbus mapping of extension" not in caplog.text
    assert "no usable IO mapping" not in caplog.text


def test_device_id_listed_twice_in_one_extension_drops_both(caplog: pytest.LogCaptureFixture) -> None:
    """Neither entry may keep the id — the IOs of the second would land on the first device."""
    conf = load_json_fixture("config_quickbus.json")
    _base_ext(conf)["lowcan"]["7"] = {"Id": "02", "Name": "MT Kopie", "Identifier": "", "TypeId": 21}

    with caplog.at_level(logging.WARNING):
        result = _parse(conf)

    ios = _io_all(result)
    assert "2" not in result["quickbus_devices"]
    assert ios["211"]["quickbus"] is None
    assert ios["201"]["quickbus"] == {"device_id": "1", "name": "MT Flur"}
    assert "Quickbus device ids are not unique, their devices are dropped and their IOs treated as plain IOs: 2" in (
        caplog.text
    )


def test_mapping_to_a_device_of_another_extension_leaves_a_plain_io(caplog: pytest.LogCaptureFixture) -> None:
    conf = load_json_fixture("config_quickbus.json")
    multibridge = conf["FubModules"]["1"]["2"]
    del multibridge["lowcan"]
    multibridge["mapping"]["lowcan"] = {"301": 1}

    with caplog.at_level(logging.WARNING):
        ios = _io_all(_parse(conf))

    assert ios["301"]["quickbus"] is None
    assert f"Quickbus mapping of extension 2: 1 {_UNRESOLVED}" in caplog.text


@pytest.mark.parametrize("bad_key", ["x", "-1", "2.0"])
def test_malformed_io_key_in_mapping_is_reported(bad_key: str, caplog: pytest.LogCaptureFixture) -> None:
    conf = load_json_fixture("config_quickbus.json")
    _base_ext(conf)["mapping"]["lowcan"][bad_key] = 1

    with caplog.at_level(logging.WARNING):
        ios = _io_all(_parse(conf))

    assert ios["201"]["quickbus"] == {"device_id": "1", "name": "MT Flur"}
    assert f"1 {_UNRESOLVED}, those IOs are treated as plain IOs: '{bad_key}'->1" in caplog.text


def test_io_mapped_to_two_devices_stays_plain(caplog: pytest.LogCaptureFixture) -> None:
    """ "0211" and "211" are the same IO — with two different devices neither one is trusted."""
    conf = load_json_fixture("config_quickbus.json")
    _base_ext(conf)["mapping"]["lowcan"]["0211"] = 1

    with caplog.at_level(logging.WARNING):
        ios = _io_all(_parse(conf))

    assert ios["211"]["quickbus"] is None
    assert ios["213"]["quickbus"] == {"device_id": "2", "name": "MT Bad"}
    assert (
        f"Quickbus mapping of extension 1: 2 {_UNRESOLVED}, those IOs are treated as plain IOs: '211'->2, '0211'->1"
        in caplog.text
    )


@pytest.mark.parametrize("bad_ref", [9, "x", None])
@pytest.mark.parametrize("bad_first", [False, True])
def test_io_with_a_doubtful_alias_stays_plain(bad_ref: Any, bad_first: bool, caplog: pytest.LogCaptureFixture) -> None:
    """A second spelling of the IO id with an unknown or malformed device makes the IO doubtful."""
    conf = load_json_fixture("config_quickbus.json")
    lowcan = _base_ext(conf)["mapping"]["lowcan"]
    valid = lowcan.pop("211")
    lowcan.update({"0211": bad_ref, "211": valid} if bad_first else {"211": valid, "0211": bad_ref})

    with caplog.at_level(logging.WARNING):
        ios = _io_all(_parse(conf))

    assert ios["211"]["quickbus"] is None
    assert ios["213"]["quickbus"] == {"device_id": "2", "name": "MT Bad"}
    # Both entries are named, whichever came first — the trusted one is dropped along with the doubtful one.
    assert f"Quickbus mapping of extension 1: 2 {_UNRESOLVED}" in caplog.text
    assert "'211'->2" in caplog.text


@pytest.mark.parametrize("dropped_first", [False, True])
def test_io_with_an_alias_to_a_dropped_device_stays_plain(
    dropped_first: bool, caplog: pytest.LogCaptureFixture
) -> None:
    """The dropped device is reported with the non-unique ids; the IO itself is still doubtful."""
    conf = load_json_fixture("config_quickbus.json")
    conf["FubModules"]["1"]["2"]["lowcan"]["3"]["Id"] = 1
    conf["FubModules"]["1"]["2"]["mapping"]["lowcan"] = {}
    lowcan = _base_ext(conf)["mapping"]["lowcan"]
    del lowcan["211"]
    lowcan.update({"0211": 1, "211": 2} if dropped_first else {"211": 2, "0211": 1})

    with caplog.at_level(logging.WARNING):
        ios = _io_all(_parse(conf))

    assert ios["211"]["quickbus"] is None
    assert ios["212"]["quickbus"] == {"device_id": "2", "name": "MT Bad"}
    assert f"Quickbus mapping of extension 1: 1 {_UNRESOLVED}, those IOs are treated as plain IOs: '211'->2" in (
        caplog.text
    )


def test_same_device_listed_twice_for_one_io_is_no_conflict(caplog: pytest.LogCaptureFixture) -> None:
    conf = load_json_fixture("config_quickbus.json")
    _base_ext(conf)["mapping"]["lowcan"]["0211"] = 2

    with caplog.at_level(logging.WARNING):
        ios = _io_all(_parse(conf))

    assert ios["211"]["quickbus"] == {"device_id": "2", "name": "MT Bad"}
    assert "Quickbus mapping" not in caplog.text


def test_repeated_identifier_within_one_device_is_left_out(caplog: pytest.LogCaptureFixture) -> None:
    """Their Web-IO command names would collide — neither one gets a command or lands in "io"."""
    conf = load_json_fixture("config_quickbus.json")
    ext = _base_ext(conf)
    ext["inoutput"]["205"] = {**ext["inoutput"]["201"], "Id": 205, "Identifier": "g1"}
    ext["mapping"]["lowcan"]["205"] = 1

    with caplog.at_level(logging.WARNING):
        result = _parse(conf)

    active = {io["id"] for io in result["io"]}
    assert {"201", "205"}.isdisjoint(active)
    assert {"202", "211"} <= active
    assert _io_all(result)["205"]["quickbus"] == {"device_id": "1", "name": "MT Flur"}
    names = [cmd["Name"] for cmd in build_webio_commands(_HOOK, result, WebioClass.QUICKBUS)]
    assert len(names) == len(set(names))
    assert "HA QB1 MT Flur G1" not in names
    assert (
        "1 IO identifiers of extension 1 repeat within their device, their 2 active IOs are left out of the active "
        "IOs (no entity, no Web-IO command): MT Flur 'G1'"
    ) in caplog.text


def test_identifier_repeated_on_other_devices_is_no_conflict(caplog: pytest.LogCaptureFixture) -> None:
    """G1 on every Multitouch is the normal case — only a repeat within one device is left out."""
    with caplog.at_level(logging.WARNING):
        result = _parse()

    assert {"201", "211"} <= {io["id"] for io in result["io"]}
    assert "repeat within their device" not in caplog.text


def test_repeated_identifier_of_plain_ios_is_left_out(caplog: pytest.LogCaptureFixture) -> None:
    """The extension is the device of a plain IO — "HA IO <ext> I1" would name both."""
    conf = load_json_fixture("config_quickbus.json")
    ext = _base_ext(conf)
    ext["inoutput"]["19"] = {**ext["inoutput"]["11"], "Id": 19, "Identifier": "i1"}

    with caplog.at_level(logging.WARNING):
        result = _parse(conf)

    active = {io["id"] for io in result["io"]}
    assert {"11", "19"}.isdisjoint(active)
    assert {"11", "19"} <= set(_io_all(result))
    # The same identifier on another extension is no conflict, extension 2 stays untouched.
    assert {"301", "302"} <= active
    assert "extension 2" not in caplog.text
    ext_name = result["extensions"]["1"]["name"]
    assert "1 IO identifiers of extension 1 repeat within their device, their 2 active IOs" in caplog.text
    assert f"(no entity, no Web-IO command): {ext_name} 'I1'" in caplog.text


def test_plain_io_sharing_an_identifier_with_a_quickbus_io_is_no_conflict(caplog: pytest.LogCaptureFixture) -> None:
    """Their command names differ ("HA IO <ext> G1" / "HA QB1 MT Flur G1")."""
    conf = load_json_fixture("config_quickbus.json")
    ext = _base_ext(conf)
    ext["inoutput"]["19"] = {**ext["inoutput"]["11"], "Id": 19, "Identifier": "G1"}

    with caplog.at_level(logging.WARNING):
        result = _parse(conf)

    assert {"19", "201", "211"} <= {io["id"] for io in result["io"]}
    assert "repeat within their device" not in caplog.text


@pytest.mark.parametrize("mapping", [{}, {"lowcan": []}])
def test_ios_of_devices_without_a_mapping_that_repeat_identifiers_are_left_out(
    mapping: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """Without the map, G1 of both Multitouch devices is a plain IO of the same extension."""
    conf = load_json_fixture("config_quickbus.json")
    _base_ext(conf)["mapping"] = mapping

    with caplog.at_level(logging.WARNING):
        result = _parse(conf)

    active = {io["id"] for io in result["io"]}
    assert {"201", "211", "203", "213"}.isdisjoint(active)
    assert {"11", "202", "204"} <= active
    names = [cmd["Name"] for cmd in build_webio_commands(_HOOK, result)]
    assert len(names) == len(set(names))
    assert "Extension 1 lists 2 Quickbus devices but no usable IO mapping" in caplog.text
    assert "2 IO identifiers of extension 1 repeat within their device, their 4 active IOs" in caplog.text
    # The no-mapping warning already names the case, no second coverage warning.
    assert "have no IO in the mapping" not in caplog.text


def test_repeated_identifier_of_an_inactive_io_is_no_conflict(caplog: pytest.LogCaptureFixture) -> None:
    conf = load_json_fixture("config_quickbus.json")
    ext = _base_ext(conf)
    ext["inoutput"]["205"] = {**ext["inoutput"]["201"], "Id": 205, "Active": False}
    ext["mapping"]["lowcan"]["205"] = 1

    with caplog.at_level(logging.WARNING):
        result = _parse(conf)

    active = {io["id"] for io in result["io"]}
    assert "201" in active
    assert "205" not in active
    assert "205" in _io_all(result)
    assert "repeat within their device" not in caplog.text


@pytest.mark.parametrize("mapping", [{}, {"wireless": []}, {"lowcan": []}, ["kaputt"]])
def test_devices_without_a_mapping_are_reported(mapping: Any, caplog: pytest.LogCaptureFixture) -> None:
    conf = load_json_fixture("config_quickbus.json")
    conf["FubModules"]["1"]["2"]["mapping"] = mapping

    with caplog.at_level(logging.WARNING):
        ios = _io_all(_parse(conf))

    assert ios["301"]["quickbus"] is None
    assert "Extension 2 lists 1 Quickbus devices but no usable IO mapping" in caplog.text


def test_mapping_entry_of_an_io_the_extension_does_not_list_is_reported(caplog: pytest.LogCaptureFixture) -> None:
    conf = load_json_fixture("config_quickbus.json")
    _base_ext(conf)["mapping"]["lowcan"]["299"] = 2

    with caplog.at_level(logging.WARNING):
        ios = _io_all(_parse(conf))

    assert ios["211"]["quickbus"] == {"device_id": "2", "name": "MT Bad"}
    assert "Quickbus mapping of extension 1: 1 entries point to IOs the extension does not list: 299->2" in (
        caplog.text
    )


def test_device_without_a_mapped_io_is_reported(caplog: pytest.LogCaptureFixture) -> None:
    conf = load_json_fixture("config_quickbus.json")
    lowcan = _base_ext(conf)["mapping"]["lowcan"]
    for io_id in ("211", "212", "213"):
        del lowcan[io_id]

    with caplog.at_level(logging.WARNING):
        ios = _io_all(_parse(conf))

    assert ios["211"]["quickbus"] is None
    assert ios["201"]["quickbus"] == {"device_id": "1", "name": "MT Flur"}
    assert (
        "1 Quickbus devices of extension 1 have no IO in the mapping, their IOs are treated as plain IOs: 2 MT Bad"
    ) in caplog.text


def test_device_whose_only_mapping_entries_are_stale_is_reported(caplog: pytest.LogCaptureFixture) -> None:
    conf = load_json_fixture("config_quickbus.json")
    lowcan = _base_ext(conf)["mapping"]["lowcan"]
    for io_id in ("211", "212", "213"):
        del lowcan[io_id]
    lowcan["299"] = 2

    with caplog.at_level(logging.WARNING):
        _parse(conf)

    assert "1 entries point to IOs the extension does not list: 299->2" in caplog.text
    assert (
        "1 Quickbus devices of extension 1 have no IO in the mapping, their IOs are treated as plain IOs: 2 MT Bad"
        in caplog.text
    )


def test_devices_left_without_an_io_by_unresolved_entries_are_reported(caplog: pytest.LogCaptureFixture) -> None:
    """No entry resolves, so no IO is mapped — the unresolved warning alone would not name the devices."""
    conf = load_json_fixture("config_quickbus.json")
    _base_ext(conf)["mapping"]["lowcan"] = {"201": 99}

    with caplog.at_level(logging.WARNING):
        _parse(conf)

    assert f"Quickbus mapping of extension 1: 1 {_UNRESOLVED}" in caplog.text
    assert (
        "2 Quickbus devices of extension 1 have no IO in the mapping, their IOs are treated as plain IOs: "
        "1 MT Flur, 2 MT Bad"
    ) in caplog.text


def test_complete_mapping_reports_no_coverage_gap(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        _parse()

    assert "does not list" not in caplog.text
    assert "have no IO in the mapping" not in caplog.text


def test_long_unresolved_mapping_list_is_capped(caplog: pytest.LogCaptureFixture) -> None:
    conf = load_json_fixture("config_quickbus.json")
    _base_ext(conf)["mapping"]["lowcan"].update({str(900 + n): 9 for n in range(8)})

    with caplog.at_level(logging.WARNING):
        _parse(conf)

    assert "8 entries point to an unknown device" in caplog.text
    assert "'904'->9 and 3 more" in caplog.text
    assert "'905'" not in caplog.text


def test_huge_raw_values_are_cut_off_in_the_log(caplog: pytest.LogCaptureFixture) -> None:
    conf = load_json_fixture("config_quickbus.json")
    _base_ext(conf)["lowcan"]["2"]["Id"] = "9" * 5000
    conf["FubModules"]["1"]["2"]["mapping"] = {"lowcan": "x" * 5000}

    with caplog.at_level(logging.WARNING):
        _parse(conf)

    assert "Skipping 1 malformed Quickbus device entries of extension 1" in caplog.text
    assert "no usable IO mapping" in caplog.text
    assert all(len(record.getMessage()) < 1000 for record in caplog.records)


def test_huge_integer_labels_fall_back_without_crashing() -> None:
    """str() refuses such an int: the device gets its fallback name, the IO its id as identifier."""
    conf = load_json_fixture("config_quickbus.json")
    ext = _base_ext(conf)
    ext["lowcan"]["2"]["Name"] = 10**5000
    ext["inoutput"]["213"]["Identifier"] = 10**5000

    result = _parse(conf)

    assert result["quickbus_devices"]["2"]["name"] == "QB2"
    assert _io_all(result)["213"]["identifier"] == "213"
    assert _io_all(result)["211"]["quickbus"] == {"device_id": "2", "name": "QB2"}


def test_io_entries_without_a_valid_id_are_skipped(caplog: pytest.LogCaptureFixture) -> None:
    """The Id feeds unique_id/entity_id — a missing one would give "None", a huge int crash str()."""
    conf = load_json_fixture("config_quickbus.json")
    ios = _base_ext(conf)["inoutput"]
    ios["204"]["Id"] = 10**5000
    ios["11"]["Identifier"] = 10**5000
    ios["11"]["Id"] = None

    with caplog.at_level(logging.WARNING):
        result = _parse(conf)

    assert {"11", "204"}.isdisjoint(_io_all(result))
    assert {"201", "202", "203"} <= _io_all(result).keys()
    assert "Skipping 2 IO entries of extension 1 without a valid Id: 11: None, 204: <int too large to print>" in (
        caplog.text
    )


def test_quickbus_ios_without_a_type_row_are_left_out(caplog: pytest.LogCaptureFixture) -> None:
    """G1 says nothing about binary or analog — a guessed kind would give a wrong command range."""
    io_types = {k: v for k, v in _IO_TYPES.items() if k != "134"}

    with caplog.at_level(logging.WARNING):
        result = parse_config(
            load_json_fixture("config_quickbus.json"), io_types=io_types, io_input_types=_IO_INPUT_TYPES
        )

    active = {io["id"] for io in result["io"]}
    assert {"201", "202", "211"}.isdisjoint(active)
    assert {"201", "202", "211"} <= _io_all(result).keys()
    # Typed Quickbus IOs and the plain IO stay.
    assert {"11", "203", "204", "213"} <= active
    assert (
        "3 active Quickbus IOs of extension 1 have no row in the IO type table, so binary or analog is unknown — "
        "they are left out of the active IOs (no entity, no Web-IO command): MT Flur G1 (type 134), "
        "MT Flur G18 (type 134), MT Bad G1 (type 134)"
    ) in caplog.text


@pytest.mark.parametrize("io_types", [None, {}])
def test_missing_type_table_keeps_quickbus_ios(io_types: Any, caplog: pytest.LogCaptureFixture) -> None:
    """A failed scrape must not drop every Quickbus entity for one poll — parse_config warns instead."""
    with caplog.at_level(logging.WARNING):
        result = parse_config(load_json_fixture("config_quickbus.json"), io_types=io_types)

    assert {io["id"] for io in result["io"] if io["quickbus"]} == {
        "201",
        "202",
        "203",
        "204",
        "211",
        "213",
        "301",
        "302",
    }
    assert "no row in the IO type table" not in caplog.text


def test_inactive_or_plain_ios_without_a_type_row_give_no_type_warning(caplog: pytest.LogCaptureFixture) -> None:
    """212 is an inactive gesture, 11 a plain input that falls back to its identifier."""
    conf = load_json_fixture("config_quickbus.json")
    for io_id in ("201", "202", "211"):
        _base_ext(conf)["inoutput"][io_id]["InOutputTypeId"] = 12
    io_types = {k: v for k, v in _IO_TYPES.items() if k not in ("1", "134")}

    with caplog.at_level(logging.WARNING):
        result = parse_config(conf, io_types=io_types, io_input_types=_IO_INPUT_TYPES)

    assert _io_all(result)["11"]["is_binary"] is True
    assert "11" in {io["id"] for io in result["io"]}
    assert "no row in the IO type table" not in caplog.text


def test_huge_integer_type_id_names_no_type_row(caplog: pytest.LogCaptureFixture) -> None:
    conf = load_json_fixture("config_quickbus.json")
    _base_ext(conf)["inoutput"]["203"]["InOutputTypeId"] = 10**5000

    with caplog.at_level(logging.WARNING):
        result = _parse(conf)

    assert "203" not in {io["id"] for io in result["io"]}
    assert "1 active Quickbus IOs of extension 1 have no row in the IO type table" in caplog.text
    assert "MT Flur TL1 (type <int too large to print>)" in caplog.text


def test_typed_quickbus_ios_give_no_type_warning(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        _parse()

    assert "no row in the IO type table" not in caplog.text


def test_scraped_names_cannot_forge_log_lines(caplog: pytest.LogCaptureFixture) -> None:
    conf = load_json_fixture("config_quickbus.json")
    ext = _base_ext(conf)
    ext["lowcan"]["2"]["Name"] = "MT Bad\nERROR forged line\r\u2028"
    del ext["mapping"]["lowcan"]["211"], ext["mapping"]["lowcan"]["212"], ext["mapping"]["lowcan"]["213"]

    with caplog.at_level(logging.WARNING):
        _parse(conf)

    coverage = next(r.getMessage() for r in caplog.records if "have no IO in the mapping" in r.getMessage())
    assert "\n" not in coverage
    assert "\r" not in coverage
    assert "\u2028" not in coverage
    assert coverage.endswith(r"2 MT Bad\nERROR forged line\r\u2028")


def test_long_scraped_values_are_cut_before_they_are_logged(caplog: pytest.LogCaptureFixture) -> None:
    conf = load_json_fixture("config_quickbus.json")
    ext = _base_ext(conf)
    ext["lowcan"]["2"]["Name"] = "N" * 100_000
    del ext["mapping"]["lowcan"]["211"], ext["mapping"]["lowcan"]["212"], ext["mapping"]["lowcan"]["213"]
    ext["inoutput"]["11"]["Id"] = ["x" * 100_000] * 1000

    with caplog.at_level(logging.WARNING):
        result = _parse(conf)

    assert result["quickbus_devices"]["2"]["name"] == "N" * 100_000
    assert max(len(r.getMessage()) for r in caplog.records) < 2000
    assert "have no IO in the mapping, their IOs are treated as plain IOs: 2 NNN" in caplog.text
    assert "Skipping 1 IO entries of extension 1 without a valid Id: 11: ['xxx" in caplog.text


def test_extensions_with_a_malformed_id_are_skipped(caplog: pytest.LogCaptureFixture) -> None:
    """The id lands in every warning about the extension, a line break in it would forge a log line."""
    conf = load_json_fixture("config_quickbus.json")
    io_module = conf["FubModules"]["1"]
    io_module["1" + chr(10) + "ERROR forged"] = io_module.pop("2")
    skipped_ios = set(io_module["1" + chr(10) + "ERROR forged"]["inoutput"])

    with caplog.at_level(logging.WARNING):
        result = _parse(conf)

    assert set(result["extensions"]) == {"1"}
    assert skipped_ios == {"301", "302"}
    assert skipped_ios.isdisjoint(_io_all(result))
    assert skipped_ios.isdisjoint(io["id"] for io in result["io"])
    assert set(result["quickbus_devices"]) == {"1", "2"}
    assert not any(chr(10) in r.getMessage() for r in caplog.records)
    assert "Skipping 1 extensions with a malformed id or content: id '1" in caplog.text


def test_extensions_with_a_non_canonical_id_or_no_dict_content_are_skipped(caplog: pytest.LogCaptureFixture) -> None:
    conf = load_json_fixture("config_quickbus.json")
    io_module = conf["FubModules"]["1"]
    io_module["01"] = io_module.pop("2")
    io_module["3"] = [10**5000]

    with caplog.at_level(logging.WARNING):
        result = _parse(conf)

    assert set(result["extensions"]) == {"1"}
    assert {io["ext_id"] for io in result["io"]} == {"1"}
    assert {"301", "302"}.isdisjoint(_io_all(result))
    assert set(result["quickbus_devices"]) == {"1", "2"}
    assert "Skipping 2 extensions with a malformed id or content: id '01', 3: " in caplog.text


def test_unprintable_container_falls_back_without_crashing(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Older reprlib versions let the int-to-str ValueError of a nested huge int through."""

    def _raise(_value: Any) -> str:
        raise ValueError("Exceeds the limit (4300 digits) for integer string conversion")

    monkeypatch.setattr(config_module._LOG_REPR, "repr", _raise)
    conf = load_json_fixture("config_quickbus.json")
    _base_ext(conf)["inoutput"]["11"]["Id"] = [10**5000]

    with caplog.at_level(logging.WARNING):
        _parse(conf)

    assert "without a valid Id: 11: <list with a value too large to print>" in caplog.text


def test_huge_integer_ids_are_rejected_without_crashing(caplog: pytest.LogCaptureFixture) -> None:
    """Python refuses str() of an int beyond its digit limit — such an id is malformed, not fatal."""
    conf = load_json_fixture("config_quickbus.json")
    ext = _base_ext(conf)
    ext["lowcan"]["2"]["Id"] = 10**5000
    ext["mapping"]["lowcan"]["211"] = 10**5000
    conf["FubModules"]["1"]["2"]["lowcan"]["3"]["Id"] = 10**18

    with caplog.at_level(logging.WARNING):
        result = _parse(conf)

    assert set(result["quickbus_devices"]) == {"1"}
    assert _io_all(result)["211"]["quickbus"] is None
    assert "<int too large to print>" in caplog.text
    assert "Skipping 1 malformed Quickbus device entries of extension 2" in caplog.text


def test_many_malformed_device_entries_give_one_capped_warning(caplog: pytest.LogCaptureFixture) -> None:
    conf = load_json_fixture("config_quickbus.json")
    _base_ext(conf)["lowcan"].update({str(50 + n): "kaputt" for n in range(8)})

    with caplog.at_level(logging.WARNING):
        _parse(conf)

    skipped = [r for r in caplog.records if "malformed Quickbus device entries" in r.getMessage()]
    assert len(skipped) == 1
    assert "Skipping 8 malformed Quickbus device entries of extension 1" in skipped[0].getMessage()
    assert "and 3 more" in skipped[0].getMessage()


@pytest.mark.parametrize(
    ("field", "value", "key", "expected"),
    [
        ("Identifier", 1234, "serial", ""),
        ("TypeId", "21", "type_id", None),
        ("TypeId", True, "type_id", None),
    ],
)
def test_malformed_device_field_is_reported(
    field: str, value: Any, key: str, expected: Any, caplog: pytest.LogCaptureFixture
) -> None:
    conf = load_json_fixture("config_quickbus.json")
    _base_ext(conf)["lowcan"]["1"][field] = value

    with caplog.at_level(logging.WARNING):
        result = _parse(conf)

    assert result["quickbus_devices"]["1"][key] == expected
    assert f"1 malformed Quickbus device fields, they are left empty: device 1 of extension 1 {field} " in (caplog.text)


def test_malformed_device_fields_of_all_extensions_give_one_warning(caplog: pytest.LogCaptureFixture) -> None:
    conf = load_json_fixture("config_quickbus.json")
    _base_ext(conf)["lowcan"]["1"].update({"Identifier": 1234, "TypeId": "21"})
    conf["FubModules"]["1"]["2"]["lowcan"]["3"]["TypeId"] = "22"

    with caplog.at_level(logging.WARNING):
        _parse(conf)

    records = [r.getMessage() for r in caplog.records if "malformed Quickbus device fields" in r.getMessage()]
    assert records == [
        "3 malformed Quickbus device fields, they are left empty: device 1 of extension 1 Identifier 1234, "
        "device 1 of extension 1 TypeId '21', device 3 of extension 2 TypeId '22'"
    ]


@pytest.mark.parametrize("field", ["Identifier", "TypeId"])
def test_missing_device_field_is_not_reported(field: str, caplog: pytest.LogCaptureFixture) -> None:
    conf = load_json_fixture("config_quickbus.json")
    del _base_ext(conf)["lowcan"]["1"][field]

    with caplog.at_level(logging.WARNING):
        _parse(conf)

    assert "malformed Quickbus device fields" not in caplog.text


@pytest.mark.parametrize("bad_id", [None, True, -1, 1.0, "1a", "", "٣", "9" * 5000])
def test_malformed_device_entry_is_skipped(bad_id: Any, caplog: pytest.LogCaptureFixture) -> None:
    conf = load_json_fixture("config_quickbus.json")
    _base_ext(conf)["lowcan"]["2"]["Id"] = bad_id

    with caplog.at_level(logging.WARNING):
        result = _parse(conf)

    assert "2" not in result["quickbus_devices"]
    assert _io_all(result)["211"]["quickbus"] is None
    assert "Skipping 1 malformed Quickbus device entries of extension 1: 2: " in caplog.text


def test_non_dict_device_entry_and_non_dict_mapping_do_not_crash(caplog: pytest.LogCaptureFixture) -> None:
    conf = load_json_fixture("config_quickbus.json")
    _base_ext(conf)["lowcan"]["2"] = "kaputt"
    conf["FubModules"]["1"]["2"]["mapping"] = ["kaputt"]

    with caplog.at_level(logging.WARNING):
        ios = _io_all(_parse(conf))

    assert ios["201"]["quickbus"] == {"device_id": "1", "name": "MT Flur"}
    assert ios["211"]["quickbus"] is None
    assert ios["301"]["quickbus"] is None
    assert "Skipping 1 malformed Quickbus device entries of extension 1: 2: 'kaputt'" in caplog.text


def test_device_id_string_is_canonical() -> None:
    conf = load_json_fixture("config_quickbus.json")
    _base_ext(conf)["lowcan"]["2"]["Id"] = "002"

    result = _parse(conf)

    assert result["quickbus_devices"]["2"]["id"] == "2"
    assert _io_all(result)["211"]["quickbus"] == {"device_id": "2", "name": "MT Bad"}


def test_io_id_string_is_canonical() -> None:
    """The entry id, the live-state key and the pushed id use the canonical form the mapping uses."""
    conf = load_json_fixture("config_quickbus.json")
    _base_ext(conf)["inoutput"]["211"]["Id"] = "00211"
    _base_ext(conf)["inoutput"]["213"]["Id"] = "00213"
    del _base_ext(conf)["inoutput"]["213"]["Identifier"]

    result = _parse(conf, io_live_states={"211": "1"})

    ios = _io_all(result)
    assert "00211" not in ios
    assert (ios["211"]["quickbus"], ios["211"]["value"]) == ({"device_id": "2", "name": "MT Bad"}, 1)
    # Without an identifier the id stands in for it, canonical as well.
    assert (ios["213"]["identifier"], ios["213"]["name"]) == ("213", "MT Bad 213")
    commands = {cmd["Name"]: cmd for cmd in build_webio_commands(_HOOK, result, WebioClass.QUICKBUS)}
    assert 'id="211"' in commands["HA QB2 MT Bad G1"]["Data"]


def test_plain_io_id_string_is_canonical() -> None:
    conf = load_json_fixture("config_quickbus.json")
    _base_ext(conf)["inoutput"]["11"]["Id"] = "0011"

    ios = _io_all(_parse(conf, io_live_states={"11": "1"}))

    assert "0011" not in ios
    assert (ios["11"]["id"], ios["11"]["value"], ios["11"]["quickbus"]) == ("11", 1, None)


def test_ios_whose_ids_repeat_once_canonical_are_left_out(caplog: pytest.LogCaptureFixture) -> None:
    """ "211" and "00211" would share one live state and one pushed id — neither of them is meant."""
    conf = load_json_fixture("config_quickbus.json")
    _base_ext(conf)["inoutput"]["202"]["Id"] = "00211"

    with caplog.at_level(logging.WARNING):
        result = _parse(conf)

    active_ids = [io["id"] for io in result["io"]]
    assert "211" not in active_ids
    assert {"201", "203", "213"} <= set(active_ids)
    assert (
        "1 IO ids repeat, their 2 IOs are left out of the active IOs (no entity, no Web-IO command): 211 (ext 1)"
        in caplog.text
    )


def test_ios_whose_ids_repeat_across_extensions_are_left_out(caplog: pytest.LogCaptureFixture) -> None:
    """get_io_live_states refuses one IO id under two extension ids, so a poll of both would fail."""
    conf = load_json_fixture("config_quickbus.json")
    conf["FubModules"]["1"]["2"]["inoutput"]["301"]["Id"] = "0211"

    with caplog.at_level(logging.WARNING):
        result = _parse(conf)

    active_ids = [io["id"] for io in result["io"]]
    assert "211" not in active_ids
    assert {"201", "213"} <= set(active_ids)
    assert [io["ext_id"] for io in result["io_all"] if io["id"] == "211"] == ["1", "2"]
    assert (
        "1 IO ids repeat, their 2 IOs are left out of the active IOs (no entity, no Web-IO command): 211 (ext 1, 2)"
        in caplog.text
    )


def test_unnamed_device_gets_a_fallback_name() -> None:
    conf = load_json_fixture("config_quickbus.json")
    _base_ext(conf)["lowcan"]["2"]["Name"] = ""

    result = _parse(conf)

    assert result["quickbus_devices"]["2"]["name"] == "QB2"
    assert _io_all(result)["211"]["name"] == "QB2 G1"


@pytest.mark.parametrize("flag", [None, True, False, 2, 0.0, "yes"])
def test_unusable_int_is_output_makes_a_read_only_input(flag: Any, caplog: pytest.LogCaptureFixture) -> None:
    """Not the type table: its row 134 says "input: false" and would make the gesture an output."""
    conf = load_json_fixture("config_quickbus.json")
    _base_ext(conf)["inoutput"]["201"]["IntIsOutput"] = flag
    _base_ext(conf)["inoutput"]["204"]["IntIsOutput"] = flag

    with caplog.at_level(logging.WARNING):
        ios = _io_all(_parse(conf))

    assert ios["201"]["is_input"] is True
    assert ios["204"]["is_input"] is True
    assert (
        "2 Quickbus IOs of extension 1 have no usable IntIsOutput flag and are treated as read-only inputs: "
        f"MT Flur 'G1': {flag!r}, MT Flur 'R': {flag!r}"
    ) in caplog.text


@pytest.mark.parametrize(("flag", "is_input"), [("0", True), ("1", False)])
def test_int_is_output_as_digit_string(flag: str, is_input: bool) -> None:
    conf = load_json_fixture("config_quickbus.json")
    _base_ext(conf)["inoutput"]["201"]["IntIsOutput"] = flag

    assert _io_all(_parse(conf))["201"]["is_input"] is is_input


def test_int_is_output_does_not_change_plain_ios() -> None:
    """Outside the Quickbus the identifier prefix keeps deciding, whatever IntIsOutput says."""
    conf = load_json_fixture("config_quickbus.json")
    _base_ext(conf)["inoutput"]["11"]["IntIsOutput"] = 1

    assert _io_all(_parse(conf))["11"]["is_input"] is True


def test_legacy_hint_does_not_list_the_quickbus_class(caplog: pytest.LogCaptureFixture) -> None:
    """Pre-split installs never had a Quickbus class — the migration hint must not ask for one."""
    conf = load_json_fixture("config_quickbus.json")
    conf["WebDevices"] = {"30": {"Name": "HomeAssistant", "Ip": "192.168.1.20:8123", "WebDeviceBaseId": 7}}

    with caplog.at_level(logging.WARNING):
        _parse(conf)

    assert "missing class(es): Marker, IO, KNX." in caplog.text


def test_no_quickbus_class_is_not_reported_as_missing(caplog: pytest.LogCaptureFixture) -> None:
    """An install set up before Quickbus support has every class but [QB] — no warning for that."""
    conf = load_json_fixture("config_quickbus.json")
    conf["WebDevices"] = {
        str(30 + n): {"Name": f"HomeAssistant{suffix}", "Ip": "192.168.1.20:8123", "WebDeviceBaseId": 7 + n}
        for n, suffix in enumerate((" [M]", " [IO]", " [KNX]"))
    }

    with caplog.at_level(logging.WARNING):
        result = _parse(conf)

    assert result["webio_devices"][WebioClass.QUICKBUS] == {"device_id": None, "device_ip": None, "base_id": None}
    assert "missing class" not in caplog.text


def test_existing_quickbus_commands_are_parsed_into_their_class() -> None:
    """The consumer's audit compares these against build_webio_commands — names and class must line up."""
    conf = load_json_fixture("config_quickbus.json")
    result = _parse(conf)
    expected = build_webio_commands(_HOOK, result, WebioClass.QUICKBUS)
    conf["FubModules"]["10"] = {
        "31": {
            str(500 + n): {"Name": cmd["Name"], "TypeId": cmd["TypeId"], "WebCommandId": 900 + n}
            for n, cmd in enumerate(expected)
        }
    }

    commands = _parse(conf)["webio_commands"]

    assert set(commands) == {cmd["Name"] for cmd in expected}
    assert commands["HA QB1 MT Flur G1"] == {
        "webIoId": "500",
        "cmdId": 900,
        "typeId": 1,
        "webioClass": WebioClass.QUICKBUS,
    }
    assert commands["HA QB1 MT Flur TL1"]["typeId"] == 2


def test_quickbus_webio_device_is_found() -> None:
    devices = _parse()["webio_devices"]

    assert devices[WebioClass.QUICKBUS] == {"device_id": "31", "device_ip": "192.168.1.20:8123", "base_id": "8"}


# --- Web-IO commands ---


def test_io_class_leaves_quickbus_ios_out() -> None:
    names = [cmd["Name"] for cmd in build_webio_commands(_HOOK, _parse(), WebioClass.IO)]

    assert names == ["HA IO BASE I1"]


def test_quickbus_class_has_one_command_per_active_quickbus_io() -> None:
    commands = build_webio_commands(_HOOK, _parse(), WebioClass.QUICKBUS)

    assert [cmd["Name"] for cmd in commands] == [
        "HA QB1 MT Flur G1",
        "HA QB1 MT Flur G18",
        "HA QB1 MT Flur TL1",
        "HA QB1 MT Flur R",
        "HA QB2 MT Bad G1",
        "HA QB2 MT Bad TL1",
        "HA QB3 BM Diele PIR1",
        "HA QB3 BM Diele TL1",
    ]


def test_io_builder_gives_a_quickbus_io_its_own_command() -> None:
    """Ext + identifier would address G1 of every Multitouch of the extension."""
    io = next(io for io in _parse()["io"] if io["id"] == "211")

    cmd = build_io_webio_command(io, _HOOK)

    assert cmd["Name"] == "HA QB2 MT Bad G1"
    assert 'type="quickbus"' in cmd["Data"]
    assert "ext=" not in cmd["Data"]


def test_stored_commands_of_one_name_are_all_kept(caplog: pytest.LogCaptureFixture) -> None:
    """A second command of a name must not overwrite the first, or the audit never cleans it up."""
    conf = load_json_fixture("config_quickbus.json")
    conf["FubModules"]["10"] = {
        "30": {"5": {"Name": "HA X", "WebCommandId": 50, "TypeId": 1}},
        "31": {"6": {"Name": "HA X", "WebCommandId": 60, "TypeId": 2}, "7": {"Name": "HA Y", "TypeId": 1}},
    }

    with caplog.at_level(logging.WARNING):
        result = _parse(conf)

    assert result["webio_commands"]["HA X"]["webIoId"] == "5"
    assert result["webio_commands"]["HA Y"]["webioClass"] == WebioClass.QUICKBUS
    assert result["webio_command_duplicates"] == [
        {"name": "HA X", "webIoId": "6", "cmdId": 60, "typeId": 2, "webioClass": WebioClass.QUICKBUS}
    ]
    assert (
        "1 stored Web-IO commands repeat the name of another command, only the first one of each name is in "
        "webio_commands: HA X (webIoId 6, quickbus)"
    ) in caplog.text


def test_unique_stored_commands_have_no_duplicates(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        result = _parse()

    assert result["webio_command_duplicates"] == []
    assert "repeat the name of another command" not in caplog.text


def test_quickbus_command_pushes_the_io_id() -> None:
    commands = {cmd["Name"]: cmd for cmd in build_webio_commands(_HOOK, _parse(), WebioClass.QUICKBUS)}

    gesture = commands["HA QB2 MT Bad G1"]
    # The whole body: no ext/io pair next to the id that a consumer could route by.
    assert gesture["Data"] == (
        'function data(a)\r\n  local d = { id="211", value=a, type="quickbus" }\r\n  return json_stringify(d)\r\nend'
    )
    assert (gesture["TypeId"], gesture["Min"], gesture["Max"]) == (1, 0, 1)
    assert gesture["Parameter"] == _HOOK
    temperature = commands["HA QB1 MT Flur TL1"]
    assert (temperature["TypeId"], temperature["Min"], temperature["Max"]) == (2, 0, 100)


def test_quickbus_command_names_stay_unique_for_same_named_devices() -> None:
    conf = load_json_fixture("config_quickbus.json")
    _base_ext(conf)["lowcan"]["2"]["Name"] = "MT Flur"

    names = [cmd["Name"] for cmd in build_webio_commands(_HOOK, _parse(conf))]

    assert len(names) == len(set(names))
    # Both devices keep their commands — the collision guard must not be what makes them unique.
    assert {"HA QB1 MT Flur G1", "HA QB2 MT Flur G1", "HA QB1 MT Flur TL1", "HA QB2 MT Flur TL1"} <= set(names)


def test_all_classes_together_cover_every_active_io_once() -> None:
    result = _parse()

    commands = build_webio_commands(_HOOK, result)
    per_class = build_webio_commands(_HOOK, result, WebioClass.IO) + build_webio_commands(
        _HOOK, result, WebioClass.QUICKBUS
    )

    assert len(commands) == len(result["io"])
    assert commands == per_class
    assert len({cmd["Name"] for cmd in commands}) == len(commands)


@pytest.mark.parametrize("webio_class", [None, WebioClass.MARKER, WebioClass.QUICKBUS])
def test_command_names_colliding_across_classes_are_left_out(
    webio_class: WebioClass | None, caplog: pytest.LogCaptureFixture
) -> None:
    """A marker schema without {MarkerId} can render the name of a Quickbus command."""
    result = _parse()
    result["markers"] = [
        {"id": "7", "name": "qb2 MT Bad G1", "type": "digital"},
        {"id": "8", "name": "M8 Licht", "type": "digital"},
    ]

    with caplog.at_level(logging.WARNING):
        names = [cmd["Name"] for cmd in build_webio_commands(_HOOK, result, webio_class)]

    # Exactly the commands without the two colliding ones — not renamed, not kept under another name.
    without_collision = {**result, "markers": [{"id": "8", "name": "M8 Licht", "type": "digital"}]}
    expected = [cmd["Name"] for cmd in build_webio_commands(_HOOK, without_collision, webio_class)]
    assert names == [name for name in expected if name != "HA QB2 MT Bad G1"]
    assert ("HA M8 Licht" in names) == (webio_class in (None, WebioClass.MARKER))
    assert ("HA QB1 MT Flur G18" in names) == (webio_class in (None, WebioClass.QUICKBUS))
    assert (
        "2 Web-IO command names are generated more than once, those commands are left out (rename the item in "
        "Comexio or change the naming schema): HA QB2 MT Bad G1, HA qb2 MT Bad G1"
    ) in caplog.text


@pytest.mark.parametrize("bad_id", ["", "abc", "-3", "1" * 5000, None, 2.5])
def test_malformed_marker_and_knx_ids_do_not_abort_the_build(bad_id: Any) -> None:
    """Such an id matches no ignored id; a per-class upload of an unrelated class still works."""
    result = _parse()
    result["markers"] = [{"id": bad_id, "name": "M Kaputt", "type": "digital"}]
    result["knx"] = [{"id": bad_id, "name": "K Kaputt", "type": "digital"}]

    names = [cmd["Name"] for cmd in build_webio_commands(_HOOK, result, None, {7}, {7})]
    quickbus = build_webio_commands(_HOOK, result, WebioClass.QUICKBUS, {7}, {7})

    assert {"HA M Kaputt", "HA K Kaputt"} <= set(names)
    assert quickbus == build_webio_commands(_HOOK, _parse(), WebioClass.QUICKBUS)


def test_ignored_items_do_not_collide(caplog: pytest.LogCaptureFixture) -> None:
    """An item the consumer does not expose gets no command, so it must not knock out another one."""
    result = _parse()
    result["markers"] = [{"id": "7", "name": "QB2 MT Bad G1", "type": "digital"}]
    result["knx"] = [{"id": "9", "name": "QB1 MT Flur G1", "type": "digital"}]

    with caplog.at_level(logging.WARNING):
        names = [
            cmd["Name"] for cmd in build_webio_commands(_HOOK, result, ignored_marker_ids={7}, ignored_knx_ids={9})
        ]

    assert {"HA QB2 MT Bad G1", "HA QB1 MT Flur G1"} <= set(names)
    assert len(names) == len(set(names))
    assert "generated more than once" not in caplog.text


def test_unique_command_names_give_no_collision_warning(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        build_webio_commands(_HOOK, _parse())

    assert "generated more than once" not in caplog.text


def test_quickbus_device_name_is_lua_escaped_in_name_only() -> None:
    """The payload carries only the numeric IO id — a quote in the device name cannot break the Lua."""
    conf = load_json_fixture("config_quickbus.json")
    _base_ext(conf)["lowcan"]["1"]["Name"] = 'MT "Flur"'

    commands = build_webio_commands(_HOOK, _parse(conf), WebioClass.QUICKBUS)

    assert commands[0]["Name"] == 'HA QB1 MT "Flur" G1'
    assert 'id="201", value=a, type="quickbus"' in commands[0]["Data"]
    assert "Flur" not in commands[0]["Data"]


def test_generate_webio_json_for_the_quickbus_class() -> None:
    result = _parse()

    payload = json.loads(generate_webio_json(_HOOK, "HomeAssistant [QB]", result, WebioClass.QUICKBUS))

    assert payload["base"]["Identifier"] == "HomeAssistant [QB]"
    assert payload["commands"] == build_webio_commands(_HOOK, result, WebioClass.QUICKBUS)


# --- Function plan labels ---


def test_io_owner() -> None:
    ios = _io_all(_parse())

    assert io_owner(ios["211"]) == "MT Bad"
    assert io_owner(ios["11"]) == "BASE"
    assert io_owner({"quickbus": {"device_id": "1", "name": 7}, "ext_name": "BASE"}) == "BASE"
    assert io_owner({"ext_name": None}) == ""


def test_quickbus_io_is_addressed_by_its_device_in_plans() -> None:
    ios = _io_all(_parse())
    ref = {"reference": {"type": 1, "ref_id": 211}}

    assert element_search_id(ref, ios) == "MT Bad#G1"
    assert _pill_parts_io("202", ios) == ("MT Flur  #G18", "Licht Flur", "0")


def test_quickbus_output_is_not_writable_from_a_plan() -> None:
    """The plan card must not offer set_value for "MT Flur#R" — that address is unknown to the API."""
    ios = _io_all(_parse())
    plain_output = copy.deepcopy(ios["204"])
    plain_output["quickbus"] = None
    geo: dict[str, Any] = {"inactive": False}
    plain_geo: dict[str, Any] = {"inactive": False}

    _apply_geometry_target_fields(geo, 1, "204", {}, ios)
    _apply_geometry_target_fields(plain_geo, 1, "204", {}, {"204": plain_output})

    assert geo == {"inactive": False, "target": "MT Flur#R", "io_input": False, "writable": False}
    assert plain_geo["writable"] is True
