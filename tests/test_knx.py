"""KNX objects: DPT resolution chain, DPT metadata and DPT3.x composite pairing in parse_config."""

from typing import Any

import pytest

from aiocomexio.config import parse_config
from aiocomexio.knx import KNX_DPT_ANALOG_RANGES, resolve_knx_dpt
from tests.common import load_json_fixture


@pytest.fixture
def catalog() -> dict[str, Any]:
    catalog: dict[str, Any] = load_json_fixture("knx_dpt_catalog.json")
    return catalog


def _parse(**kwargs: Any) -> dict[str, dict[str, Any]]:
    result = parse_config(load_json_fixture("config_basic.json"), io_types=load_json_fixture("io_types.json"), **kwargs)
    return {item["id"]: item for item in result["knx"]}


@pytest.fixture
def knx_items(catalog: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return _parse(knx_live_states={"1": "19,5"}, knx_dpt_catalog=catalog)


def test_resolve_knx_dpt_follows_point_device_dpt_chain(catalog: dict[str, Any]) -> None:
    assert resolve_knx_dpt(catalog, "1") == (9, 1)
    assert resolve_knx_dpt(catalog, "3") == (3, 8)


@pytest.mark.parametrize(
    "broken",
    [
        {},
        {"KnxPoints": [], "KnxDevices": {}, "KnxDpt": {}},
        {"KnxPoints": {"1": {"KnxDeviceId": 10}}, "KnxDevices": {}, "KnxDpt": {}},
        {"KnxPoints": {"1": {"KnxDeviceId": 10}}, "KnxDevices": {"10": {"KnxDptId": 20}}, "KnxDpt": {}},
        {
            "KnxPoints": {"1": {"KnxDeviceId": 10}},
            "KnxDevices": {"10": {"KnxDptId": 20}},
            "KnxDpt": {"20": {"KnxBaseTypeId": "9", "KnxSubId": 1}},
        },
    ],
)
def test_resolve_knx_dpt_returns_none_for_broken_chain(broken: dict[str, Any]) -> None:
    assert resolve_knx_dpt(broken, "1") is None


def test_resolve_knx_dpt_unknown_point(catalog: dict[str, Any]) -> None:
    assert resolve_knx_dpt(catalog, "99") is None


def test_unnamed_knx_object_is_not_imported(knx_items: dict[str, dict[str, Any]]) -> None:
    assert sorted(knx_items) == ["1", "2", "3", "4"]


def test_analog_knx_object_gets_dpt_range_and_live_value(knx_items: dict[str, dict[str, Any]]) -> None:
    item = knx_items["1"]

    assert (item["type"], item["value"], item["ha_name"]) == ("analog", 19.5, "K1 Wohnen Temperatur")
    dpt_range = (item["dpt_min"], item["dpt_max"], item["dpt_unit"], item["dpt_step"])
    assert dpt_range == KNX_DPT_ANALOG_RANGES[(9, 1)]
    assert item["dpt_device_class"] == "temperature"


def test_dpt3_pair_is_tagged_as_cover_composite(knx_items: dict[str, dict[str, Any]]) -> None:
    assert knx_items["2"]["knx_composite"] == {"role": "direction", "domain": "cover", "partner_id": "3"}
    assert knx_items["3"]["knx_composite"] == {"role": "stepcode", "domain": "cover", "partner_id": "2"}


def test_knx_type_without_io_type_entry_is_flagged_ambiguous(knx_items: dict[str, dict[str, Any]]) -> None:
    item = knx_items["4"]

    assert (item["type"], item["dpt_ambiguous"]) == ("digital", True)
    assert "dpt_type_unresolved" not in item
    # An ambiguous item must not also be auto-classified from the DPT chain.
    assert "dpt_min" not in item
    assert "dpt_device_class" not in item


def test_knx_without_catalog_has_no_dpt_metadata() -> None:
    items = _parse()

    assert "dpt_min" not in items["1"]
    assert "knx_composite" not in items["2"]
    assert items["4"]["dpt_ambiguous"] is True


def test_knx_live_values_are_not_mixed_with_marker_values() -> None:
    """Markers and KNX objects share one numeric id space — their live values must stay apart."""
    result = parse_config(
        load_json_fixture("config_basic.json"),
        io_types=load_json_fixture("io_types.json"),
        live_states={"1": "1"},
        knx_live_states={"1": "19,5"},
    )

    assert result["markers"][0]["value"] == 1.0
    assert result["knx"][0]["value"] == 19.5


def _knx_item(k_id: int, name: str, type_raw: int) -> dict[str, Any]:
    return {"Id": k_id, "Name": name, "Type": type_raw}


def _catalog(points: dict[int, int], devices: dict[int, int], dpts: dict[int, tuple[int, int]]) -> dict[str, Any]:
    """Synthetic KNX catalog: point -> device, device -> dpt id, dpt id -> (base type, sub id)."""
    return {
        "KnxPoints": {str(p): {"KnxDeviceId": d} for p, d in points.items()},
        "KnxDevices": {str(d): {"KnxDptId": t} for d, t in devices.items()},
        "KnxDpt": {str(t): {"KnxBaseTypeId": b, "KnxSubId": s} for t, (b, s) in dpts.items()},
    }


def _parse_knx(items: list[dict[str, Any]], catalog: dict[str, Any]) -> dict[str, dict[str, Any]]:
    conf = {"FubModules": {"11": {str(i["Id"]): i for i in items}}}
    result = parse_config(conf, io_types=load_json_fixture("io_types.json"), knx_dpt_catalog=catalog)
    return {item["id"]: item for item in result["knx"]}


def test_digital_knx_dpt_metadata() -> None:
    """DPT1.019 gets its device class, DPT1.001 is flagged ambiguous, DPT1.011 stays plain."""
    items = _parse_knx(
        [_knx_item(1, "Fenster", 1), _knx_item(2, "Schalter", 1), _knx_item(3, "Status", 1)],
        _catalog({1: 10, 2: 11, 3: 12}, {10: 20, 11: 21, 12: 22}, {20: (1, 19), 21: (1, 1), 22: (1, 11)}),
    )

    assert items["1"]["dpt_device_class"] == "door"
    assert items["2"]["dpt_ambiguous"] is True
    assert "dpt_device_class" not in items["3"]
    assert "dpt_ambiguous" not in items["3"]


def test_analog_knx_without_range_entry_or_chain_keeps_generic_fallback() -> None:
    items = _parse_knx(
        [_knx_item(1, "Unbekannte Groesse", 5), _knx_item(2, "Ohne Punkt", 5)],
        _catalog({1: 10}, {10: 20}, {20: (9, 3)}),
    )

    assert "dpt_min" not in items["1"]
    assert "dpt_min" not in items["2"]


@pytest.mark.parametrize(
    ("items", "catalog"),
    [
        # Three points share one KnxDeviceId.
        (
            [_knx_item(1, "A", 1), _knx_item(2, "B", 121), _knx_item(3, "C", 121)],
            _catalog({1: 10, 2: 10, 3: 10}, {10: 20}, {20: (3, 8)}),
        ),
        # Two points, but the DPT is no DPT3.x composite.
        ([_knx_item(1, "A", 1), _knx_item(2, "B", 121)], _catalog({1: 10, 2: 10}, {10: 20}, {20: (5, 1)})),
        # DPT3.008, but both halves are digital.
        ([_knx_item(1, "A", 1), _knx_item(2, "B", 1)], _catalog({1: 10, 2: 10}, {10: 20}, {20: (3, 8)})),
    ],
)
def test_dpt3_composite_needs_exactly_one_digital_and_one_analog_point(
    items: list[dict[str, Any]], catalog: dict[str, Any]
) -> None:
    assert all("knx_composite" not in item for item in _parse_knx(items, catalog).values())


def test_dpt3_composite_skipped_without_point_table() -> None:
    items = _parse_knx([_knx_item(1, "A", 1)], {"KnxDevices": {}, "KnxDpt": {}})

    assert "knx_composite" not in items["1"]
