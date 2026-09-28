"""Renumbering-tolerant plan hashing and semantic snapshot diffing."""

import copy
from types import MappingProxyType
from typing import Any

import pytest

from aiocomexio.function_plan import (
    build_source_id_translation,
    diff_snapshots,
    plan_hash,
    referenced_label_metadata,
    snapshot_label_maps,
)
from tests.common import load_json_fixture

_ID_OFFSET = 1000


@pytest.fixture
def fixture() -> dict[str, Any]:
    data: dict[str, Any] = load_json_fixture("function_plan.json")
    return data


@pytest.fixture
def plan(fixture: dict[str, Any]) -> dict[str, Any]:
    return {"elements": fixture["elements"], "connections": fixture["connections"]}


def _renumbered(plan: dict[str, Any]) -> dict[str, Any]:
    """The same plan after Comexio renumbered every FubElementId (and connection id) by an offset."""

    def shift(port: dict[str, Any]) -> dict[str, Any]:
        return {**port, "FubElementId": port["FubElementId"] + _ID_OFFSET}

    return {
        "elements": {str(int(eid) + _ID_OFFSET): elem for eid, elem in plan["elements"].items()},
        "connections": {
            str(int(cid) + _ID_OFFSET): {"input": shift(conn["input"]), "output": [shift(o) for o in conn["output"]]}
            for cid, conn in plan["connections"].items()
        },
    }


def test_plan_hash_ignores_element_renumbering(plan: dict[str, Any]) -> None:
    assert plan_hash(_renumbered(plan)) == plan_hash(plan)


def test_plan_hash_changes_with_wiring(plan: dict[str, Any]) -> None:
    changed = copy.deepcopy(plan)
    changed["connections"]["11"]["output"][0]["Inverted"] = False

    assert plan_hash(changed) != plan_hash(plan)


def test_plan_hash_accepts_output_as_object(plan: dict[str, Any]) -> None:
    """Comexio serializes a gap-free output group as an array and a sparse one as an object."""
    as_object = copy.deepcopy(plan)
    for conn in as_object["connections"].values():
        conn["output"] = {str(i): out for i, out in enumerate(conn["output"])}
    as_proxy = copy.deepcopy(plan)
    for conn in as_proxy["connections"].values():
        conn["output"] = MappingProxyType({str(i): out for i, out in enumerate(conn["output"])})

    assert plan_hash(as_object) == plan_hash(plan)
    assert plan_hash(as_proxy) == plan_hash(plan)


def test_diff_of_renumbered_plan_is_empty(plan: dict[str, Any]) -> None:
    diff = diff_snapshots(_renumbered(plan), plan)

    assert diff == {
        "markers": {"added": [], "removed": []},
        "ios": {"added": [], "removed": []},
        "connections": {"added": [], "removed": [], "moved": []},
    }


def test_diff_reports_added_marker_and_wire(plan: dict[str, Any]) -> None:
    newer = copy.deepcopy(plan)
    newer["elements"]["20"] = {"name": "", "position_x": 420, "position_y": 120, "reference": {"type": 2, "ref_id": 9}}
    newer["connections"]["21"] = {
        "input": {"FubElementId": 7, "IOPos": 0, "Inverted": False},
        "output": [{"FubElementId": 20, "IOPos": 0, "Inverted": False}],
    }

    diff = diff_snapshots(newer, plan)

    assert diff["markers"] == {"added": [(2, 9, None, None, None)], "removed": []}
    assert len(diff["connections"]["added"]) == 1
    assert diff["connections"]["removed"] == diff["connections"]["moved"] == []


def test_diff_reports_removed_io(plan: dict[str, Any]) -> None:
    older = copy.deepcopy(plan)
    del plan["elements"]["3"]

    assert diff_snapshots(plan, older)["ios"] == {"added": [], "removed": [(1, 11, None, None, None)]}


def test_diff_reports_moved_block_as_moved_not_added_and_removed(plan: dict[str, Any]) -> None:
    """A dragged block (position-identified) must not surface as a removed+added wire pair."""
    newer = copy.deepcopy(plan)
    newer["elements"]["4"]["position_y"] = 60

    connections = diff_snapshots(newer, plan)["connections"]

    assert connections["added"] == connections["removed"] == []
    assert len(connections["moved"]) == 3  # the Oder gate's two inputs and its output


def test_source_id_translation_maps_only_stable_reference_types(plan: dict[str, Any]) -> None:
    translation = build_source_id_translation(plan["elements"], _renumbered(plan)["elements"])

    # marker, IO, Web-IO, marker, time module — not the comment, blocks or constant.
    assert translation == {"2": "1002", "3": "1003", "5": "1005", "8": "1008", "9": "1009"}


def test_referenced_label_metadata_captures_names(fixture: dict[str, Any], plan: dict[str, Any]) -> None:
    metadata = referenced_label_metadata(plan, fixture["markers_by_id"], fixture["webio_by_id"], fixture["ios_by_id"])

    assert metadata == {
        "markers": {"1": "M1 Licht Wohnen", "4": "M4 Klingel [TRIG]"},
        "ios": {"11": "BASE I1 Taster Flur"},
        "webio": {"101": "30. HA M1 Licht Wohnen"},
    }


def test_referenced_label_metadata_skips_unknown_ids(plan: dict[str, Any]) -> None:
    assert referenced_label_metadata(plan, {}, {}, {}) == {}


def test_snapshot_label_maps_overlays_captured_names(fixture: dict[str, Any]) -> None:
    labels = {"markers": {"1": "M1 Alter Name", "7": "M7 Gelöscht"}}

    markers, webio, ios = snapshot_label_maps(
        labels, fixture["markers_by_id"], fixture["webio_by_id"], fixture["ios_by_id"]
    )

    assert markers["1"] == {**fixture["markers_by_id"]["1"], "name": "M1 Alter Name"}
    assert markers["7"] == {"name": "M7 Gelöscht"}
    assert markers["4"] == fixture["markers_by_id"]["4"]
    assert webio is fixture["webio_by_id"]
    assert ios is fixture["ios_by_id"]


def test_snapshot_label_maps_without_labels_is_a_no_op(fixture: dict[str, Any]) -> None:
    maps = snapshot_label_maps(None, fixture["markers_by_id"], fixture["webio_by_id"], fixture["ios_by_id"])

    assert maps[0] is fixture["markers_by_id"]
    assert maps[1] is fixture["webio_by_id"]
    assert maps[2] is fixture["ios_by_id"]
