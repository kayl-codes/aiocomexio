"""1-Wire sensors: configuration ($FubModules["12"]) and live status (read1WireStati)."""

import json
import logging
from typing import Any

import pytest

from aiocomexio import ComexioAuthenticationError, ComexioClient, ComexioDataError
from aiocomexio.config import TYPE_ANALOG, TYPE_DIGITAL
from aiocomexio.onewire import _bounded_repr, parse_onewire_config, parse_onewire_device_count, parse_onewire_status
from tests.fake_comexio import FUNCTION_MODULE_PATH, FakeComexio

ONEWIRE_STATUS_PATH = "/admin/knx_one_wire/read1WireStati/"


def _entry(config_id: int, prefix: str, *, binary: Any = 2, active: Any = 1, name: str = "Sensor") -> dict[str, Any]:
    return {
        "Id": config_id,
        "Name": name,
        "HwId": f"26-synthetic-{config_id}",
        "Prefix": prefix,
        "Active": active,
        "Binary": binary,
        "Multisensor": "",
    }


def _status_item(address: str, *, online: bool = True, visible: bool = True, value: str = "21.5") -> dict[str, Any]:
    return {
        "id": address,
        "longname": f"Sensor {address}",
        "device_id": f"28-synthetic-{address}",
        "online": online,
        "visible": visible,
        "value": value,
    }


def states_for(overrides: dict[str, Any]) -> Any:
    """The parsed status of one sensor OT1 with the given fields replaced."""
    return parse_onewire_status([{**_status_item("OT1"), **overrides}])["OT1"]


# --- configuration -------------------------------------------------------------------------


def test_config_address_is_prefix_plus_id_and_kind_follows_binary_flag() -> None:
    modules = {"12": {"1": _entry(1, "OT", binary=2), "2": _entry(2, "OIB", binary=1)}}

    sensors = parse_onewire_config(modules)

    assert list(sensors) == ["OT1", "OIB2"]
    assert sensors["OT1"].kind == TYPE_ANALOG
    assert sensors["OIB2"].kind == TYPE_DIGITAL
    assert sensors["OIB2"].config_id == 2
    assert sensors["OIB2"].prefix == "OIB"


def test_config_inactive_entries_are_left_out() -> None:
    modules = {"12": {"1": _entry(1, "OT", active=0), "2": _entry(2, "OT", active=1)}}

    assert list(parse_onewire_config(modules)) == ["OT2"]


def test_config_binary_flag_other_than_1_or_2_gives_no_kind() -> None:
    modules = {"12": {"1": _entry(1, "OT", binary=3), "2": _entry(2, "OT", binary=True)}}

    sensors = parse_onewire_config(modules)

    assert list(sensors) == ["OT1", "OT2"]
    assert sensors["OT1"].kind is None
    assert sensors["OT2"].kind is None


def test_config_array_shaped_group_gives_the_same_sensors() -> None:
    as_object = {"12": {"1": _entry(1, "OT"), "2": _entry(2, "OH")}}
    as_array = {"12": [None, _entry(1, "OT"), _entry(2, "OH")]}

    sensors = parse_onewire_config(as_array)

    assert list(sensors) == ["OT1", "OH2"]
    assert sensors["OH2"].config_id == 2
    assert sensors == parse_onewire_config(as_object)


def test_config_malformed_entries_are_skipped() -> None:
    broken = [
        "not an object",
        {**_entry(1, "OT"), "Id": "1.5"},
        {**_entry(2, "OT"), "Id": True},
        {**_entry(3, "OT"), "Prefix": ""},
        {**_entry(4, "OT"), "Prefix": None},
        {**_entry(6, "OT"), "Id": -1},
        {**_entry(7, "OT"), "Id": 10**18},
        _entry(5, "OT"),
    ]
    modules = {"12": {str(index): entry for index, entry in enumerate(broken)}}

    assert list(parse_onewire_config(modules)) == ["OT5"]


@pytest.mark.parametrize(("config_id", "address"), [("7", "OT7"), ("007", "OT7"), (100_000, "OT100000")])
def test_config_digit_string_or_large_id_is_accepted(config_id: object, address: str) -> None:
    modules = {"12": {"1": {**_entry(7, "OT"), "Id": config_id}}}

    sensors = parse_onewire_config(modules)

    assert list(sensors) == [address]
    assert sensors[address].config_id == int(address.removeprefix("OT"))


def test_config_same_id_in_int_and_digit_string_is_one_sensor(caplog: pytest.LogCaptureFixture) -> None:
    modules = {"12": {"1": {**_entry(7, "OT"), "Id": "007"}, "2": _entry(7, "OT")}}

    with caplog.at_level(logging.WARNING, logger="aiocomexio.onewire"):
        sensors = parse_onewire_config(modules)

    assert list(sensors) == ["OT7"]
    assert [record.getMessage() for record in caplog.records] == [
        "Two active 1-Wire entries share the address 'OT7', the later one wins"
    ]


def test_config_same_id_under_two_prefixes_keeps_both_with_a_warning(caplog: pytest.LogCaptureFixture) -> None:
    """A function plan names a sensor by Id alone, so a shared Id is logged; both sensors keep their address."""
    modules = {"12": {"1": _entry(3, "OT"), "2": _entry(3, "OH"), "3": _entry(4, "OT"), "4": _entry(3, "OP")}}

    with caplog.at_level(logging.WARNING, logger="aiocomexio.onewire"):
        sensors = parse_onewire_config(modules)

    assert list(sensors) == ["OT3", "OH3", "OT4", "OP3"]
    # One warning per shared Id, not one per pair.
    assert [record.getMessage() for record in caplog.records] == [
        "1-Wire sensors OH3, OP3, OT3 share the Id 3, a function plan element shows only one of them"
    ]


def test_config_with_distinct_ids_logs_nothing(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="aiocomexio.onewire"):
        parse_onewire_config({"12": {"1": _entry(3, "OT"), "2": _entry(4, "OH")}})

    assert caplog.records == []


@pytest.mark.parametrize("config_id", ["", " 7", "-1", "٧", "9" * 19, 10**18])
def test_config_unusable_id_is_skipped_without_raising(config_id: object) -> None:
    modules = {"12": {"1": {**_entry(1, "OT"), "Id": config_id}, "2": _entry(2, "OT")}}

    assert list(parse_onewire_config(modules)) == ["OT2"]


def test_config_active_true_counts_as_active() -> None:
    modules = {"12": {"1": _entry(1, "OT", active=True)}}

    assert list(parse_onewire_config(modules)) == ["OT1"]


@pytest.mark.parametrize("active", ["1", 2, 1.0])
def test_config_active_other_values_count_as_inactive(active: object) -> None:
    modules = {"12": {"1": _entry(1, "OT", active=active)}}

    assert parse_onewire_config(modules) == {}


@pytest.mark.parametrize("config_id", [0, 99_999])
def test_config_id_bounds_are_inclusive(config_id: int) -> None:
    modules = {"12": {"1": _entry(config_id, "OT")}}

    assert list(parse_onewire_config(modules)) == [f"OT{config_id}"]


@pytest.mark.parametrize(("shape", "type_name"), [("text", "str"), (7, "int"), (True, "bool"), ((1, 2), "tuple")])
def test_config_group_of_unsupported_shape_raises(shape: object, type_name: str) -> None:
    with pytest.raises(ComexioDataError) as info:
        parse_onewire_config({"12": shape})

    assert str(info.value) == f"$FubModules group 12 has an unsupported shape: {type_name}"


@pytest.mark.parametrize("modules", [{}, {"12": None}, {"12": []}, {"12": [None, None]}])
def test_config_absent_group_is_empty(modules: dict[str, object]) -> None:
    assert parse_onewire_config(modules) == {}


@pytest.mark.parametrize(("group", "count"), [(["text", "x"], 2), (["text", None], 1), ([7], 1), ({"1": "text"}, 1)])
def test_config_group_of_only_non_objects_raises(group: object, count: int) -> None:
    with pytest.raises(ComexioDataError) as info:
        parse_onewire_config({"12": group})

    assert str(info.value) == f"$FubModules group 12 holds {count} entries, none an object"


def test_config_group_mixing_objects_and_non_objects_does_not_raise() -> None:
    assert list(parse_onewire_config({"12": ["text", _entry(1, "OT")]})) == ["OT1"]
    assert parse_onewire_config({"12": ["text", _entry(1, "OT", active=0)]}) == {}


def test_config_with_only_unusable_active_entries_raises() -> None:
    modules = {"12": {"1": {**_entry(1, "OT"), "Prefix": None}}}

    with pytest.raises(ComexioDataError):
        parse_onewire_config(modules)


def test_config_without_group_12_is_empty() -> None:
    assert parse_onewire_config({"2": {"1": {"Name": "Marker"}}}) == {}


# --- status --------------------------------------------------------------------------------


def test_status_reads_value_and_error_flags_per_address() -> None:
    payload = [
        _status_item("OT1", value="21.5"),
        _status_item("OT2", online=False),
        _status_item("OT3", visible=False, value="0"),
    ]

    states = parse_onewire_status(payload)

    assert list(states) == ["OT1", "OT2", "OT3"]
    assert states["OT1"].ok
    assert states["OT1"].value == pytest.approx(21.5)
    assert not states["OT2"].ok
    assert not states["OT2"].online
    assert not states["OT3"].visible
    assert not states["OT3"].ok


def test_status_value_accepts_decimal_comma_and_rejects_non_finite() -> None:
    payload = [
        _status_item("OT1", value="21,5"),
        _status_item("OT2", value="nan"),
        _status_item("OT3", value="inf"),
        _status_item("OT4", value=""),
    ]

    states = parse_onewire_status(payload)

    assert list(states) == ["OT1", "OT2", "OT3", "OT4"]
    assert states["OT1"].value == pytest.approx(21.5)
    assert states["OT2"].value is None
    assert states["OT3"].value is None
    assert states["OT4"].value is None


def test_status_item_without_address_is_skipped() -> None:
    payload = [{"longname": "no id"}, _status_item("OT1"), "junk"]

    assert list(parse_onewire_status(payload)) == ["OT1"]


def test_status_answer_that_is_not_a_list_raises() -> None:
    with pytest.raises(ComexioDataError) as info:
        parse_onewire_status({"id": "OT1"})

    assert str(info.value) == "1-Wire status answer is not a list: dict"


# --- client --------------------------------------------------------------------------------


async def test_get_onewire_status_posts_all_with_monitor_referer(
    logged_in: ComexioClient, comexio: FakeComexio
) -> None:
    comexio.serve_json("POST", ONEWIRE_STATUS_PATH, [_status_item("OT1"), _status_item("OIB10", online=False)])

    states = await logged_in.get_onewire_status()

    assert sorted(states) == ["OIB10", "OT1"]
    assert states["OIB10"].online is False
    [request] = comexio.received_at("POST", ONEWIRE_STATUS_PATH)[-1:]
    assert request.form == {"all": "all"}
    assert request.referer is not None
    assert request.referer.endswith("/admin/knx_one_wire/one_wire/")


async def test_get_onewire_status_on_expired_session_raises_authentication_error(
    logged_in: ComexioClient, comexio: FakeComexio
) -> None:
    page = '<html><form>Anmeldung<input id="loginsubmit"></form></html>'
    comexio.serve_text("POST", ONEWIRE_STATUS_PATH, page)

    with pytest.raises(ComexioAuthenticationError) as info:
        await logged_in.get_onewire_status()

    assert str(info.value) == (
        f"Comexio served the login form, the session is not logged in (1-Wire status: response is not JSON: '{page}')"
    )


async def test_get_onewire_sensors_reads_the_configuration_page(logged_in: ComexioClient, comexio: FakeComexio) -> None:
    modules = {"12": {"1": _entry(1, "OT"), "2": _entry(2, "OIB", binary=1)}}
    comexio.serve_text("GET", FUNCTION_MODULE_PATH, f"<script>var $FubModules = {json.dumps(modules)};</script>")

    sensors = await logged_in.get_onewire_sensors()

    assert list(sensors) == ["OT1", "OIB2"]
    assert sensors["OT1"].kind == TYPE_ANALOG
    assert sensors["OIB2"].kind == TYPE_DIGITAL
    assert len(comexio.received_at("GET", FUNCTION_MODULE_PATH)) == 1


# --- device count (IO home page) -----------------------------------------------------------

IO_HOME_PATH = "/admin/in_output/home"


# Anchored, so the whole message is compared and not only a fragment of it.
_UNKNOWN_MARKUP = r"^IO home page counters have an unknown markup, the 1-Wire count is unknown$"


def _counter(extension: str, css_class: str, value: str) -> str:
    return f'<div id="number_of_{extension}" class="{css_class}">{value}</div>'


def test_device_count_sums_the_1wire_counter_of_every_extension() -> None:
    html = "".join(
        [
            _counter("1", "number_of_type_30", "10"),
            _counter("1_2", "number_of_type_30_2", "7"),
            _counter("1_3", "number_of_type_30_3", "57"),
            _counter("5", "number_of_type_30", "3"),
            _counter("6", "number_of_type_3", "2"),
        ]
    )

    assert parse_onewire_device_count(html) == 13


def test_device_count_tolerates_attribute_order_whitespace_and_extra_classes() -> None:
    html = (
        '<div class="box number_of_type_30 wide" id="number_of_2"  >\n 4 </div>'
        '<div id = "number_of_3" class="number_of_type_30">2</div>'
    )

    assert parse_onewire_device_count(html) == 6


@pytest.mark.parametrize("digits", ["1234567", "9" * 40])
def test_device_count_with_implausibly_long_number_raises(digits: str) -> None:
    html = _counter("1", "number_of_type_30", digits)

    with pytest.raises(ComexioDataError):
        parse_onewire_device_count(html)


def test_device_count_accepts_six_digits() -> None:
    assert parse_onewire_device_count(_counter("1", "number_of_type_30", "999999")) == 999999


def test_device_count_ignores_decoy_classes_and_missing_id() -> None:
    html = (
        _counter("1", "number_of_type_300", "5")
        + '<div class="xnumber_of_type_30">6</div>'
        + '<div class="number_of_type_30">7</div>'
        + _counter("1_2", "a number_of_type_30_2", "8")
        + _counter("2", "number_of_type_30 wide", "3")
    )

    assert parse_onewire_device_count(html) == 3


def test_device_count_ignores_long_quickbus_number_next_to_a_1wire_counter() -> None:
    html = _counter("1_2", "number_of_type_30_2", "9" * 40) + _counter("1", "number_of_type_30", "2")

    assert parse_onewire_device_count(html) == 2


def test_device_count_with_malformed_counter_next_to_a_valid_one_raises() -> None:
    html = _counter("1", "number_of_type_30", "10") + _counter("2", "number_of_type_30", "n/a")

    with pytest.raises(ComexioDataError, match=_UNKNOWN_MARKUP):
        parse_onewire_device_count(html)


def test_device_count_ignores_a_broken_counter_of_another_type() -> None:
    html = _counter("1", "number_of_type_30", "10") + _counter("2", "number_of_type_3", "n/a")

    assert parse_onewire_device_count(html) == 10


def test_device_count_matches_the_1wire_class_as_a_whole_token() -> None:
    html = _counter("1", "foo number_of_type_30 bar", "3") + '<div id="number_of_2">4</div>'

    assert parse_onewire_device_count(html) == 3


def test_device_count_reads_single_quoted_markup() -> None:
    html = "<div id='number_of_1' class='number_of_type_30'>3</div>" + _counter("2", "number_of_type_30", "4")

    assert parse_onewire_device_count(html) == 7


def test_device_count_reads_multi_token_single_quoted_class() -> None:
    html = "<div id=\"number_of_1\" class='number_of_type_30 extra'>3</div>"

    assert parse_onewire_device_count(html) == 3


def test_device_count_with_mismatched_quotes_is_not_a_1wire_counter() -> None:
    html = '<div id="number_of_1" class="number_of_type_30\'>3</div>' + _counter("2", "number_of_type_30", "4")

    assert parse_onewire_device_count(html) == 4


def test_device_count_with_single_quoted_malformed_1wire_counter_raises() -> None:
    with pytest.raises(ComexioDataError, match=_UNKNOWN_MARKUP):
        parse_onewire_device_count("<div id='number_of_1' class='number_of_type_30'>n/a</div>")


def test_device_count_ignores_quickbus_and_knx_counters() -> None:
    html = _counter("1_2", "number_of_type_30_2", "7") + _counter("1_3", "number_of_type_30_3", "57")

    assert parse_onewire_device_count(html) == 0


def test_device_count_without_any_counter_raises() -> None:
    with pytest.raises(ComexioDataError):
        parse_onewire_device_count("<html><body>no devices</body></html>")


def test_device_count_with_only_other_counters_is_zero() -> None:
    assert parse_onewire_device_count(_counter("5", "number_of_type_3", "2")) == 0


def test_device_count_with_non_ascii_digits_raises() -> None:
    html = _counter("1", "number_of_type_30", "٤")

    with pytest.raises(ComexioDataError, match=_UNKNOWN_MARKUP):
        parse_onewire_device_count(html)


async def test_get_onewire_device_count_reads_the_io_home_page(logged_in: ComexioClient, comexio: FakeComexio) -> None:
    comexio.serve_text("GET", IO_HOME_PATH, _counter("1", "number_of_type_30", "4"))

    assert await logged_in.get_onewire_device_count() == 4
    assert len(comexio.received_at("GET", IO_HOME_PATH)) == 1


async def test_get_onewire_device_count_on_expired_session_raises_authentication_error(
    logged_in: ComexioClient, comexio: FakeComexio
) -> None:
    comexio.serve_text("GET", IO_HOME_PATH, '<html><form><input id="loginsubmit"></form></html>')

    with pytest.raises(ComexioAuthenticationError) as info:
        await logged_in.get_onewire_device_count()

    assert str(info.value) == "Comexio served the login form, the session is not logged in (IO home page)"


def test_status_value_accepts_json_numbers() -> None:
    assert states_for({"value": 21.5}).value == 21.5
    assert states_for({"value": 7}).value == 7.0


@pytest.mark.parametrize("raw", [True, "1_0", "-inf", "Infinity", "1e999", None])
def test_status_value_rejects_unparseable_or_non_finite(raw: object) -> None:
    assert states_for({"value": raw}).value is None


def test_status_value_of_unusable_type_is_logged_once_and_read_as_missing(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="aiocomexio.onewire"):
        assert states_for({"value": {"reading": 21.5}}).value is None

    assert [record.getMessage() for record in caplog.records] == [
        "1-Wire reading of 'OT1' is not a number (type dict), shown as missing"
    ]
    assert caplog.records[0].levelno == logging.WARNING


@pytest.mark.parametrize("raw", [float("nan"), float("inf"), 10**400, ""])
def test_status_non_finite_or_empty_reading_is_missing_without_a_warning(
    caplog: pytest.LogCaptureFixture, raw: object
) -> None:
    with caplog.at_level(logging.WARNING, logger="aiocomexio.onewire"):
        assert states_for({"value": raw}).value is None

    assert caplog.text == ""


def test_reading_warning_keeps_the_content_out_of_warning_level(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG, logger="aiocomexio.onewire"):
        states_for({"value": {"marker": "zz9"}})

    warnings = [record.getMessage() for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "zz9" not in warnings[0]
    assert "zz9" in caplog.text


def test_log_lines_escape_a_line_break_in_the_address(caplog: pytest.LogCaptureFixture) -> None:
    payload = [{**_status_item("OT1"), "id": "OT1\nWARNING forged", "value": {"x": 1}}]

    with caplog.at_level(logging.WARNING, logger="aiocomexio.onewire"):
        parse_onewire_status(payload)

    assert [record.getMessage() for record in caplog.records] == [
        "1-Wire reading of 'OT1\\nWARNING forged' is not a number (type dict), shown as missing"
    ]


def test_debug_records_are_bounded_and_escaped(caplog: pytest.LogCaptureFixture) -> None:
    payload = [{**_status_item("OT1\nWARNING " + "A" * 5000), "value": "line\n" + "B" * 5000}]

    with caplog.at_level(logging.DEBUG, logger="aiocomexio.onewire"):
        parse_onewire_status(payload)

    debug_records = [record for record in caplog.records if record.levelno == logging.DEBUG]
    assert debug_records
    assert all("\n" not in record.getMessage() for record in debug_records)
    assert all(len(record.getMessage()) < 300 for record in debug_records)


def test_duplicate_address_warning_is_bounded_and_escaped(caplog: pytest.LogCaptureFixture) -> None:
    address = "OT1\nWARNING " + "A" * 5000
    payload = [{**_status_item(address), "value": "1"}, {**_status_item(address), "value": "2"}]

    with caplog.at_level(logging.WARNING, logger="aiocomexio.onewire"):
        parse_onewire_status(payload)

    assert len(caplog.records) == 1
    assert "\n" not in caplog.records[0].getMessage()
    assert len(caplog.records[0].getMessage()) < 300


def test_reading_warning_bounds_a_long_address(caplog: pytest.LogCaptureFixture) -> None:
    payload = [{**_status_item("A" * 5000), "value": {"x": 1}}]

    with caplog.at_level(logging.WARNING, logger="aiocomexio.onewire"):
        parse_onewire_status(payload)

    assert caplog.records
    assert all(len(record.getMessage()) < 300 for record in caplog.records)


def test_config_mixed_group_skips_non_objects_with_a_warning(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="aiocomexio.onewire"):
        sensors = parse_onewire_config({"12": [None, "text", _entry(1, "OT")]})

    assert list(sensors) == ["OT1"]
    assert [record.getMessage() for record in caplog.records] == ["Skipped 1 non-object 1-Wire config entries"]


def test_status_flags_must_be_booleans() -> None:
    payload = [{**_status_item("OT1"), "online": 1}]

    with pytest.raises(ComexioDataError):
        parse_onewire_status(payload)


def test_status_error_message_bounds_the_external_value() -> None:
    payload = [{**_status_item("OT1"), "online": "x" * 5000}]

    with pytest.raises(ComexioDataError) as info:
        parse_onewire_status(payload)

    assert len(str(info.value)) < 300


def test_status_error_message_bounds_a_long_address() -> None:
    payload = [{**_status_item("A" * 5000), "online": "x"}]

    with pytest.raises(ComexioDataError) as info:
        parse_onewire_status(payload)

    assert len(str(info.value)) < 300
    assert "..." in str(info.value)


def test_non_text_name_is_shown_blank_and_logged(caplog: pytest.LogCaptureFixture) -> None:
    entry = _entry(1, "OT")
    entry["Name"] = 5

    with caplog.at_level(logging.WARNING):
        sensors = parse_onewire_config({"12": {"1": entry}})

    assert list(sensors) == ["OT1"]
    assert sensors["OT1"].name == ""
    assert [record.getMessage() for record in caplog.records] == [
        "1-Wire field Name has the unexpected type int, shown blank"
    ]


def test_non_text_status_device_id_is_shown_blank_and_logged(caplog: pytest.LogCaptureFixture) -> None:
    payload = [{**_status_item("OT1"), "device_id": ["28-x"]}]

    with caplog.at_level(logging.WARNING):
        states = parse_onewire_status(payload)

    assert list(states) == ["OT1"]
    assert states["OT1"].device_id == ""
    assert [record.getMessage() for record in caplog.records] == [
        "1-Wire field device_id has the unexpected type list, shown blank"
    ]


def test_absent_or_null_text_fields_are_blank_without_a_warning(caplog: pytest.LogCaptureFixture) -> None:
    entry = _entry(1, "OT")
    entry["Name"] = None
    del entry["HwId"]

    with caplog.at_level(logging.WARNING):
        sensors = parse_onewire_config({"12": {"1": entry}})

    assert list(sensors) == ["OT1"]
    assert sensors["OT1"].name == ""
    assert sensors["OT1"].hw_id == ""
    assert caplog.records == []


def test_status_error_repr_renders_only_a_few_elements() -> None:
    calls = 0

    class Spy:
        def __repr__(self) -> str:
            nonlocal calls
            calls += 1
            return "spy"

    payload = [{**_status_item("OT1"), "online": [Spy() for _ in range(1000)]}]

    with pytest.raises(ComexioDataError) as info:
        parse_onewire_status(payload)

    assert calls < 20
    assert len(str(info.value)) < 300


def test_status_items_without_address_are_logged(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        states = parse_onewire_status([{"longname": "no id"}, _status_item("OT1"), "junk"])

    assert list(states) == ["OT1"]
    assert [record.getMessage() for record in caplog.records] == ["Skipped 2 1-Wire status items without an address"]


def test_duplicate_sensor_address_keeps_the_later_entry() -> None:
    modules = {"12": {"1": _entry(1, "OT", name="first"), "2": _entry(1, "OT", name="second")}}

    sensors = parse_onewire_config(modules)

    assert list(sensors) == ["OT1"]
    assert sensors["OT1"].name == "second"


def test_sensor_prefix_must_be_ascii() -> None:
    modules = {"12": {"1": _entry(1, "ÖT")}}

    with pytest.raises(ComexioDataError):
        parse_onewire_config(modules)


def test_bounded_repr_of_a_repr_that_raises_shows_the_type_only(monkeypatch: pytest.MonkeyPatch) -> None:
    class RaisingRepr:
        def repr(self, _value: object) -> str:
            raise ValueError("Exceeds the limit (4300 digits) for integer string conversion")

    monkeypatch.setattr("aiocomexio.onewire._EXCERPT_REPR", RaisingRepr())

    assert _bounded_repr(10**5000) == "<int with an integer beyond the digit limit>"
