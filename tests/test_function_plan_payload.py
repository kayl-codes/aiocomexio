"""Plan payload shape checks and normalization."""

from typing import Any

import pytest

from aiocomexio import ComexioDataError
from aiocomexio.function_plan import build_run_payload, normalize_plan_payload, plan_payload_has_elements


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"elements": {}}, True),
        ({"elements": []}, True),
        ({"elements": None}, False),
        ({"connections": {}}, False),
        (["elements"], False),
        (None, False),
    ],
)
def test_plan_payload_has_elements(payload: Any, expected: bool) -> None:
    assert plan_payload_has_elements(payload) is expected


def test_normalize_rekeys_elements_by_id_and_connections_by_position() -> None:
    data = {"elements": [{"id": 12}, {"name": "no id"}], "connections": [{"input": {}}, {"input": {}}]}

    normalized = normalize_plan_payload(data)

    assert list(normalized["elements"]) == ["12", "1"]
    assert list(normalized["connections"]) == ["0", "1"]
    assert isinstance(data["elements"], list)  # input left untouched


def test_normalize_keeps_objects_and_fills_missing() -> None:
    elements = {"5": {"id": 5}}

    assert normalize_plan_payload({"elements": elements}) == {"elements": elements, "connections": {}}


@pytest.mark.parametrize("value", [5, "x", True])
def test_normalize_rejects_scalar_collection(value: Any) -> None:
    with pytest.raises(ComexioDataError, match="elements"):
        normalize_plan_payload({"elements": value})


@pytest.mark.parametrize(
    "elements",
    [
        [{"id": 1}, {"name": "no id, position 1"}],
        [{"id": 4}, {"id": 4}],
        [{"id": None}, {"id": 0}],
    ],
)
def test_normalize_rejects_colliding_keys(elements: list[dict[str, Any]]) -> None:
    with pytest.raises(ComexioDataError, match=r"elements\[1\] repeats"):
        normalize_plan_payload({"elements": elements})


def test_normalize_rejects_non_object_list_item() -> None:
    with pytest.raises(ComexioDataError, match=r"connections\[1\]"):
        normalize_plan_payload({"elements": {}, "connections": [{}, 3]})


# --- run payload ---------------------------------------------------------------------------------


_IN = {"FubElementId": 10, "IOPos": 0}


def test_build_run_payload_turns_output_lists_into_indexed_objects() -> None:
    plan: dict[str, Any] = {
        "elements": {"10": {"id": 10}},
        "connections": {
            "1": {"id": 1, "input": _IN, "output": [{"FubElementId": 11}, {"FubElementId": 12}]},
            "2": {"id": 2, "input": _IN, "output": {"0": {"FubElementId": 13}}},
            "3": {"id": 3, "input": _IN, "output": []},
        },
    }

    payload = build_run_payload(plan)

    assert payload == {
        "elements": {"10": {"id": 10}},
        "connections": {
            "1": {"id": 1, "input": _IN, "output": {"0": {"FubElementId": 11}, "1": {"FubElementId": 12}}},
            "2": {"id": 2, "input": _IN, "output": {"0": {"FubElementId": 13}}},
            "3": {"id": 3, "input": _IN, "output": {}},
        },
    }
    assert plan["connections"]["1"]["output"] == [{"FubElementId": 11}, {"FubElementId": 12}]


@pytest.mark.parametrize(
    ("plan", "match"),
    [
        ({"connections": {}}, "elements and connections"),
        ({"elements": {}}, "elements and connections"),
        ({"elements": [], "connections": {}}, "elements and connections"),
        ({"elements": {}, "connections": {"1": []}}, "not an object"),
        ({"elements": {}, "connections": {"1": {"input": _IN, "output": "x"}}}, "not a list or object"),
        ({"elements": {}, "connections": {"1": {"id": 1, "input": _IN}}}, "no output collection"),
        ({"elements": {}, "connections": {"1": {"id": 1, "output": []}}}, "no input endpoint"),
        ({"elements": {}, "connections": {"1": {"input": None, "output": []}}}, "no input endpoint"),
        ({"elements": {}, "connections": {"1": {"input": {"IOPos": 0}, "output": []}}}, "no input endpoint"),
        ({"elements": {}, "connections": {"1": {"input": _IN, "output": [None]}}}, "no endpoint"),
        ({"elements": {}, "connections": {"1": {"input": _IN, "output": {"0": 5}}}}, "no endpoint"),
        ({"elements": {}, "connections": {"1": {"input": _IN, "output": [{"IOPos": 0}]}}}, "no endpoint"),
        (
            {"elements": {}, "connections": {1: {"input": _IN, "output": []}, "1": {"input": _IN, "output": []}}},
            "collide",
        ),
        ({"elements": {}, "connections": {"1": {"input": _IN, "output": {0: _IN, "0": _IN}}}}, "Output ids .* collide"),
        ({"elements": {"10": None}, "connections": {}}, "Element '10'"),
        ({"elements": {1: {}, "1": {}}, "connections": {}}, "Element ids collide"),
    ],
)
def test_build_run_payload_never_turns_a_broken_plan_into_an_empty_one(plan: dict[str, Any], match: str) -> None:
    with pytest.raises(ValueError, match=match):
        build_run_payload(plan)


@pytest.mark.parametrize("plan", [None, [], "plan"])
def test_build_run_payload_refuses_a_non_mapping(plan: Any) -> None:
    with pytest.raises(TypeError, match="must be a mapping"):
        build_run_payload(plan)
