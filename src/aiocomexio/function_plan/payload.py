"""Shape checks and normalization for loadelements/loadallelements plan payloads."""

from collections.abc import Mapping
from typing import Any

from ..exceptions import ComexioDataError

_PLAN_COLLECTIONS = ("elements", "connections")


def plan_payload_has_elements(data: Any) -> bool:
    """True if a plan payload carries a real elements collection (an object or an array).

    An error object or a truncated payload (no "elements", or "elements": null) must not pass as
    a loaded but empty plan wherever the plan's content gates an irreversible action.
    """
    return isinstance(data, dict) and isinstance(data.get("elements"), (dict, list))


def normalize_plan_payload(data: dict[str, Any]) -> dict[str, Any]:
    """Copy of data with elements and connections as id-keyed dicts ({} if missing or null).

    Comexio's PHP backend serializes an associative array as a JSON array whenever its keys
    happen to be exactly 0..N-1 — a shape coincidence, not a sign that the collection is empty.
    A list is re-keyed by each item's own "id" (elements carry one) or else by its list position,
    which for connections IS the server's real connection id. Raises ComexioDataError if a
    collection is neither object, array nor null, or if a list item is not an object.
    """
    normalized = dict(data)
    for key in _PLAN_COLLECTIONS:
        value = data.get(key)
        if isinstance(value, list):
            normalized[key] = _keyed_by_list_position(key, value)
        elif value is None or isinstance(value, dict):
            normalized[key] = value or {}
        else:
            raise ComexioDataError(f"Function plan {key} is {type(value).__name__}, not an object or array")
    return normalized


def _keyed_by_list_position(key: str, items: list[Any]) -> dict[str, Any]:
    """Re-key a list-shaped collection into {id: item}, using the list position where an item has no id."""
    keyed: dict[str, Any] = {}
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            raise ComexioDataError(f"Function plan {key}[{i}] is {type(item).__name__}, not an object")
        item_id = item.get("id")
        item_key = str(i if item_id is None else item_id)
        if item_key in keyed:
            # Silently keeping only one of the two would drop an element from diff, backup and restore.
            raise ComexioDataError(f"Function plan {key}[{i}] repeats the key {item_key!r}")
        keyed[item_key] = item
    return keyed


def build_run_payload(plan: Mapping[str, Any]) -> dict[str, Any]:
    """The "data" run_fup saves: elements as given, each connection's outputs as an index-keyed object.

    plan is a plan as load_function_plan returns it (connection outputs as a list). Both
    collections must be present: a plan without them would run as an empty plan and wipe the
    live one, so a missing or malformed collection raises ValueError instead. A plan that is no
    mapping at all raises TypeError.
    """
    if not isinstance(plan, Mapping):
        raise TypeError(f"A plan to run must be a mapping, not {type(plan).__name__}")
    elements, connections = plan.get("elements"), plan.get("connections")
    if not isinstance(elements, Mapping) or not isinstance(connections, Mapping):
        raise ValueError("A plan to run needs its elements and connections as objects")
    run_connections: dict[str, Any] = {}
    for conn_id, conn in connections.items():
        if not isinstance(conn, Mapping):
            raise ValueError(f"Connection {conn_id!r} is {type(conn).__name__}, not an object")
        outputs = conn.get("output", [])
        if isinstance(outputs, list):
            outputs = {str(i): output for i, output in enumerate(outputs)}
        elif not isinstance(outputs, Mapping):
            raise ValueError(f"Outputs of connection {conn_id!r} are {type(outputs).__name__}, not a list")
        run_connections[str(conn_id)] = {**conn, "output": dict(outputs)}
    return {"elements": dict(elements), "connections": run_connections}
