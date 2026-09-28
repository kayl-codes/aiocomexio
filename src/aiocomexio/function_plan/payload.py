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
    live one, so a missing or malformed collection raises ValueError instead. The same holds for
    anything that would run as a partial element or wire: an element that is no object, a
    connection without its input or output collection, an endpoint without its FubElementId, or
    two connection ids that collapse into one key. A plan that is no mapping at all raises TypeError.
    """
    if not isinstance(plan, Mapping):
        raise TypeError(f"A plan to run must be a mapping, not {type(plan).__name__}")
    elements, connections = plan.get("elements"), plan.get("connections")
    if not isinstance(elements, Mapping) or not isinstance(connections, Mapping):
        raise ValueError("A plan to run needs its elements and connections as objects")
    for element_id, element in elements.items():
        if not isinstance(element, Mapping):
            raise ValueError(f"Element {element_id!r} is {type(element).__name__}, not an object")
    run_connections: dict[str, Any] = {}
    for conn_id, conn in connections.items():
        if str(conn_id) in run_connections:
            raise ValueError(f"Connection ids collide on {str(conn_id)!r}")
        run_connections[str(conn_id)] = _run_connection(conn_id, conn)
    return {"elements": dict(elements), "connections": run_connections}


def _run_connection(conn_id: Any, conn: Any) -> dict[str, Any]:
    """One connection as run_fup takes it; ValueError for anything that would run as a partial wire."""
    if not isinstance(conn, Mapping):
        raise ValueError(f"Connection {conn_id!r} is {type(conn).__name__}, not an object")
    if not _is_endpoint(conn.get("input")):
        raise ValueError(f"Connection {conn_id!r} has no input endpoint")
    # A missing output would run as a wire without sinks and drop them from the live plan.
    if "output" not in conn:
        raise ValueError(f"Connection {conn_id!r} has no output collection")
    outputs = conn["output"]
    if isinstance(outputs, list):
        outputs = {str(i): output for i, output in enumerate(outputs)}
    elif not isinstance(outputs, Mapping):
        raise ValueError(f"Outputs of connection {conn_id!r} are {type(outputs).__name__}, not a list or object")
    run_outputs: dict[str, Any] = {}
    for output_id, output in outputs.items():
        if not _is_endpoint(output):
            raise ValueError(f"Connection {conn_id!r} has an output that is no endpoint")
        # 0 and "0" become one JSON key; json.dumps would silently keep only one of the two sinks.
        if str(output_id) in run_outputs:
            raise ValueError(f"Output ids of connection {conn_id!r} collide on {str(output_id)!r}")
        run_outputs[str(output_id)] = output
    return {**conn, "output": run_outputs}


def _is_endpoint(endpoint: Any) -> bool:
    """Whether endpoint names the element it attaches to; without that the wire has no end."""
    return isinstance(endpoint, Mapping) and endpoint.get("FubElementId") is not None
