"""Plan payload shape checks and normalization."""

from typing import Any

import pytest

from aiocomexio import ComexioDataError
from aiocomexio.function_plan import normalize_plan_payload, plan_payload_has_elements


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
