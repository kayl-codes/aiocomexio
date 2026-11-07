"""1-Wire sensors: configuration ($FubModules["12"]) and live status (read1WireStati)."""

import json
from typing import Any

import pytest

from aiocomexio import ComexioAuthenticationError, ComexioClient, ComexioDataError
from aiocomexio.config import TYPE_ANALOG, TYPE_DIGITAL
from aiocomexio.onewire import parse_onewire_config, parse_onewire_device_count, parse_onewire_status
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

    assert sensors["OT1"].kind is None
    assert sensors["OT2"].kind is None


def test_config_array_shaped_group_gives_the_same_sensors() -> None:
    as_object = {"12": {"1": _entry(1, "OT"), "2": _entry(2, "OH")}}
    as_array = {"12": [None, _entry(1, "OT"), _entry(2, "OH")]}

    assert parse_onewire_config(as_array) == parse_onewire_config(as_object)


def test_config_malformed_entries_are_skipped() -> None:
    broken = [
        "not an object",
        {**_entry(1, "OT"), "Id": "1"},
        {**_entry(2, "OT"), "Id": True},
        {**_entry(3, "OT"), "Prefix": ""},
        {**_entry(4, "OT"), "Prefix": None},
        {**_entry(6, "OT"), "Id": -1},
        {**_entry(7, "OT"), "Id": 100_000},
        _entry(5, "OT"),
    ]
    modules = {"12": {str(index): entry for index, entry in enumerate(broken)}}

    assert list(parse_onewire_config(modules)) == ["OT5"]


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


@pytest.mark.parametrize("shape", ["text", 7, True, (1, 2)])
def test_config_group_of_unsupported_shape_raises(shape: object) -> None:
    with pytest.raises(ComexioDataError, match="unsupported shape"):
        parse_onewire_config({"12": shape})


@pytest.mark.parametrize("modules", [{}, {"12": None}])
def test_config_absent_group_is_empty(modules: dict[str, object]) -> None:
    assert parse_onewire_config(modules) == {}


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

    assert states["OT1"].ok
    assert states["OT1"].value == pytest.approx(21.5)
    assert not states["OT2"].ok
    assert not states["OT2"].online
    assert not states["OT3"].visible


def test_status_value_accepts_decimal_comma_and_rejects_non_finite() -> None:
    payload = [
        _status_item("OT1", value="21,5"),
        _status_item("OT2", value="nan"),
        _status_item("OT3", value="inf"),
        _status_item("OT4", value=""),
    ]

    states = parse_onewire_status(payload)

    assert states["OT1"].value == pytest.approx(21.5)
    assert states["OT2"].value is None
    assert states["OT3"].value is None
    assert states["OT4"].value is None


def test_status_item_without_address_is_skipped() -> None:
    payload = [{"longname": "no id"}, _status_item("OT1"), "junk"]

    assert list(parse_onewire_status(payload)) == ["OT1"]


def test_status_answer_that_is_not_a_list_raises() -> None:
    with pytest.raises(ComexioDataError, match="not a list"):
        parse_onewire_status({"id": "OT1"})


# --- client --------------------------------------------------------------------------------


async def test_get_onewire_status_posts_all_with_monitor_referer(
    logged_in: ComexioClient, comexio: FakeComexio
) -> None:
    comexio.serve_json("POST", ONEWIRE_STATUS_PATH, [_status_item("OT1"), _status_item("OIB10", online=False)])

    states = await logged_in.get_onewire_status()

    assert set(states) == {"OT1", "OIB10"}
    assert states["OIB10"].online is False
    [request] = comexio.received_at("POST", ONEWIRE_STATUS_PATH)[-1:]
    assert request.form == {"all": "all"}
    assert request.referer is not None
    assert request.referer.endswith("/admin/knx_one_wire/one_wire/")


async def test_get_onewire_status_on_expired_session_raises_authentication_error(
    logged_in: ComexioClient, comexio: FakeComexio
) -> None:
    comexio.serve_text("POST", ONEWIRE_STATUS_PATH, '<html><form>Anmeldung<input id="loginsubmit"></form></html>')

    with pytest.raises(ComexioAuthenticationError, match="login form"):
        await logged_in.get_onewire_status()


async def test_get_onewire_sensors_reads_the_configuration_page(logged_in: ComexioClient, comexio: FakeComexio) -> None:
    modules = {"12": {"1": _entry(1, "OT"), "2": _entry(2, "OIB", binary=1)}}
    comexio.serve_text("GET", FUNCTION_MODULE_PATH, f"<script>var $FubModules = {json.dumps(modules)};</script>")

    sensors = await logged_in.get_onewire_sensors()

    assert sensors["OT1"].kind == TYPE_ANALOG
    assert sensors["OIB2"].kind == TYPE_DIGITAL


# --- device count (IO home page) -----------------------------------------------------------

IO_HOME_PATH = "/admin/in_output/home"


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


def test_device_count_with_single_quoted_markup_raises() -> None:
    with pytest.raises(ComexioDataError, match="unknown markup"):
        parse_onewire_device_count("<div id='number_of_1' class='number_of_type_30'>3</div>")


def test_device_count_ignores_quickbus_and_knx_counters() -> None:
    html = _counter("1_2", "number_of_type_30_2", "7") + _counter("1_3", "number_of_type_30_3", "57")

    assert parse_onewire_device_count(html) == 0


def test_device_count_without_any_counter_raises() -> None:
    with pytest.raises(ComexioDataError):
        parse_onewire_device_count("<html><body>no devices</body></html>")


def test_device_count_with_only_other_counters_is_zero() -> None:
    assert parse_onewire_device_count(_counter("5", "number_of_type_3", "2")) == 0


def test_device_count_with_non_ascii_digits_raises() -> None:
    with pytest.raises(ComexioDataError, match="unknown markup"):
        parse_onewire_device_count(_counter("1", "number_of_type_30", "٤"))


async def test_get_onewire_device_count_reads_the_io_home_page(logged_in: ComexioClient, comexio: FakeComexio) -> None:
    comexio.serve_text("GET", IO_HOME_PATH, _counter("1", "number_of_type_30", "4"))

    assert await logged_in.get_onewire_device_count() == 4


async def test_get_onewire_device_count_on_expired_session_raises_authentication_error(
    logged_in: ComexioClient, comexio: FakeComexio
) -> None:
    comexio.serve_text("GET", IO_HOME_PATH, '<html><form><input id="loginsubmit"></form></html>')

    with pytest.raises(ComexioAuthenticationError, match="login form"):
        await logged_in.get_onewire_device_count()


def test_status_value_accepts_json_numbers() -> None:
    assert states_for({"value": 21.5}).value == 21.5
    assert states_for({"value": 7}).value == 7.0


@pytest.mark.parametrize("raw", [True, "1_0", "-inf", "Infinity", "1e999", "", None])
def test_status_value_rejects_unparseable_or_non_finite(raw: object) -> None:
    assert states_for({"value": raw}).value is None


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


def test_duplicate_sensor_address_keeps_the_later_entry() -> None:
    modules = {"12": {"1": _entry(1, "OT", name="first"), "2": _entry(1, "OT", name="second")}}

    assert parse_onewire_config(modules)["OT1"].name == "second"


def test_sensor_prefix_must_be_ascii() -> None:
    modules = {"12": {"1": _entry(1, "ÖT")}}

    with pytest.raises(ComexioDataError):
        parse_onewire_config(modules)
