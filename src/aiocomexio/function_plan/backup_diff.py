"""Renumbering-tolerant hashing and semantic diffing of Function Plan backup snapshots.

A snapshot is one plan's ``{"elements": ..., "connections": ...}`` as loaded from Comexio, optionally
with the ``"labels"`` captured by :func:`referenced_label_metadata`. Comexio provides no modification
timestamps for plans and renumbers the plan-local FubElementIds during some bulk operations without
the wiring itself changing, so everything here compares elements by a stable identity instead of
their raw ids. Storing and rotating the snapshots is the caller's job.
"""

import hashlib
import json
from collections.abc import Mapping
from typing import Any

__all__ = [
    "build_source_id_translation",
    "diff_snapshots",
    "plan_hash",
    "referenced_label_metadata",
    "snapshot_label_maps",
]

# (type, ref_id, x, y, name) — see _element_identity.
type _Identity = tuple[Any, Any, Any, Any, Any]
# (element identity, IOPos, inverted)
type _Endpoint = tuple[_Identity, Any, bool]
type _Wire = tuple[_Endpoint, _Endpoint]

# reference.type values with a globally stable ref_id (survives Comexio renumbering the
# plan-local FubElementId): 1=IO, 2=marker, 10=WebIO, 4=time module. Everything else (block
# instances, constants, comments) has no such global identity — fubBase blocks in particular
# share one ref_id across EVERY instance of that block type — so position is used instead.
_STABLE_REF_TYPES = {1, 2, 10, 4}

# reference.type -> key under snapshot["labels"], for the same three ref kinds that get a
# resolvable display name in render_labels.resolve_element_label (marker/WebIO/IO).
_LABEL_REF_TYPES = {2: "markers", 10: "webio", 1: "ios"}


def _reference(elem: Mapping[str, Any]) -> Mapping[str, Any]:
    """An element's "reference", or {} if it has none, it is null or no object."""
    ref = elem.get("reference")
    return ref if isinstance(ref, Mapping) else {}


def _ref_id(ref: Mapping[str, Any]) -> Any:
    """reference.ref_id with a digit string as int — the same id must not differ by its JSON type.

    int, not str, so that the plan_hash of a snapshot with int ids stays what it always was.
    """
    ref_id = ref.get("ref_id")
    return int(ref_id) if isinstance(ref_id, str) and _is_ascii_int(ref_id) else ref_id


def _is_ascii_int(text: str) -> bool:
    """Whether int(text) parses — str.isdigit alone also accepts e.g. "²"."""
    return text.isascii() and text.isdigit()


def _element_identity(elem: Mapping[str, Any]) -> _Identity:
    """Stable cross-snapshot identity for one plan element (see _STABLE_REF_TYPES).

    Always a fixed-shape (type, ref_id, x, y, name) tuple — x/y/name are None for the
    stable-ref-id types, since those need neither for identity. The position-based fallback
    also carries ref_id and name — not needed for equality (position already disambiguates),
    but this is the only place a caller-side label resolver can still get them from, since the
    diff only ever returns identity tuples, not the original element dicts. Carrying them also
    means a block swapped for a different kind at the same spot, or a renamed constant/comment,
    correctly shows up as removed+added instead of being silently treated as unchanged.

    Position itself is NOT rounded/bucketed here — a plain float difference (even a sub-unit
    Comexio re-save drift) is intentional: it lets _split_moved() downstream recognize a wire
    whose endpoint only moved and report it as one "moved" entry, instead of either a
    confusing added+removed pair (raw diff) or silently hiding the move (rounded diff).
    """
    ref = _reference(elem)
    etype = ref.get("type")
    ref_id = _ref_id(ref)
    if etype in _STABLE_REF_TYPES:
        return (etype, ref_id, None, None, None)
    return (etype, ref_id, elem.get("position_x"), elem.get("position_y"), elem.get("name"))


def build_source_id_translation(
    snapshot_elements: Mapping[str, Any], live_elements: Mapping[str, Any]
) -> dict[str, str]:
    """Map each stable-identity element's id in `snapshot_elements` to its id in `live_elements`.

    A stored backup's local FubElementIds can be renumbered on the live plan by a later
    Comexio-side sync (see _element_identity) even though nothing wired to that element
    actually changed. This lets a live per-connection value lookup (keyed by the LIVE plan's
    ids) still be applied while rendering a frozen snapshot whose ids may have since shifted,
    keeping a displayed backup's wiring values live without swapping its structure back to the
    current live plan. Only covers the _STABLE_REF_TYPES kinds (IO/marker/WebIO/time module); a
    block instance, constant or comment has no cross-snapshot identity to match on and is
    simply left untranslated.

    An element placed more than once (the same marker twice) pairs its copies up in id order;
    copies the live plan has fewer of map to its first one — all copies carry the same value.
    """
    live_ids = _stable_ids_by_identity(live_elements)
    translation: dict[str, str] = {}
    for identity, snapshot_ids in _stable_ids_by_identity(snapshot_elements).items():
        if targets := live_ids.get(identity):
            for index, elem_id in enumerate(snapshot_ids):
                translation[elem_id] = targets[index] if index < len(targets) else targets[0]
    return translation


def _stable_ids_by_identity(elements: Mapping[str, Any]) -> dict[_Identity, list[str]]:
    """The ids of the _STABLE_REF_TYPES elements per identity, in id order."""
    ids: dict[_Identity, list[str]] = {}
    for elem_id in sorted(elements, key=_id_order):
        elem = elements[elem_id]
        if _reference(elem).get("type") in _STABLE_REF_TYPES:
            ids.setdefault(_element_identity(elem), []).append(elem_id)
    return ids


def _id_order(elem_id: str) -> tuple[int, int | str]:
    """Sort key for element ids: numeric ids numerically, before any other."""
    return (0, int(elem_id)) if _is_ascii_int(elem_id) else (1, elem_id)


def _strip_position(identity: _Identity) -> tuple[Any, Any, Any]:
    """Position-agnostic form of an element identity, used to pair up moved connections."""
    etype, ref_id, _pos_x, _pos_y, name = identity
    return (etype, ref_id, name)


def _wire_key(wire: _Wire) -> tuple[tuple[Any, ...], tuple[Any, ...]]:
    (src_id, src_pos, src_inv), (dst_id, dst_pos, dst_inv) = wire
    return (_strip_position(src_id), src_pos, src_inv), (_strip_position(dst_id), dst_pos, dst_inv)


def _moved_kinds(older_ids: set[_Identity], newer_ids: set[_Identity]) -> set[tuple[Any, Any, Any]]:
    """The element kinds (see _strip_position) of which a position is gone and another appeared.

    Only such a kind can have moved — two unmoved blocks of the same kind are no move, so
    rewiring from one to the other stays a removed + an added wire. Per kind, not per
    position: blocks shifted together by exactly their spacing land on each other's spots.
    """
    gone = {_strip_position(identity) for identity in older_ids - newer_ids}
    return gone & {_strip_position(identity) for identity in newer_ids - older_ids}


def _is_move(old_wire: _Wire, new_wire: _Wire, moved_kinds: set[tuple[Any, Any, Any]]) -> bool:
    """Whether every end of old_wire is new_wire's end, or an element of a kind that moved."""
    return all(
        old_end[0] == new_end[0] or _strip_position(old_end[0]) in moved_kinds
        for old_end, new_end in zip(old_wire, new_wire, strict=True)
    )


def _split_moved(
    added: set[_Wire], removed: set[_Wire], moved_kinds: set[tuple[Any, Any, Any]]
) -> tuple[list[_Wire], list[_Wire], list[tuple[_Wire, _Wire]]]:
    """Pair up added/removed wires that differ only by a block's position.

    A block instance (fubBase/constant/comment — identified via position since its ref_id is
    shared across every instance of that kind) that gets dragged to a new spot, or drifts by a
    sub-unit float amount on a Comexio re-save, without the wiring itself actually changing,
    would otherwise show up as one bogus "removed" wire plus one bogus "added" wire with an
    identical resolved label. This surfaces it as a single "moved" entry instead
    (moved_kinds: see _moved_kinds).
    """
    added_by_key: dict[tuple[Any, ...], list[_Wire]] = {}
    for wire in added:
        added_by_key.setdefault(_wire_key(wire), []).append(wire)
    removed_by_key: dict[tuple[Any, ...], list[_Wire]] = {}
    for wire in removed:
        removed_by_key.setdefault(_wire_key(wire), []).append(wire)

    moved: list[tuple[_Wire, _Wire]] = []
    for key in added_by_key.keys() & removed_by_key.keys():
        candidates = sorted(added_by_key[key], key=str)
        for old_wire in sorted(removed_by_key[key], key=str):
            new_wire = next((wire for wire in candidates if _is_move(old_wire, wire, moved_kinds)), None)
            if new_wire is None:
                continue
            candidates.remove(new_wire)
            moved.append((old_wire, new_wire))
            removed.discard(old_wire)
            added.discard(new_wire)

    return sorted(added, key=str), sorted(removed, key=str), sorted(moved, key=str)


def _named_identities(elements: Mapping[str, Any], ref_type: int) -> set[_Identity]:
    return {_element_identity(elem) for elem in elements.values() if _reference(elem).get("type") == ref_type}


def _connection_wires(snapshot: Mapping[str, Any]) -> set[_Wire]:
    """Every individual source->sink wire, as a tuple of stable endpoint identities."""
    elements = snapshot.get("elements", {})

    def _endpoint(port: Mapping[str, Any]) -> _Endpoint:
        # An end without an element — Comexio keeps connections without any "input" — gets the
        # empty identity: its raw FubElementId (if any) is exactly what renumbering changes, so
        # two such ends at the same port of the same element hash and diff as one.
        elem = elements.get(str(port.get("FubElementId")), {})
        return (_element_identity(elem), port.get("IOPos"), bool(port.get("Inverted")))

    wires: set[_Wire] = set()
    for conn in snapshot.get("connections", {}).values():
        src = _endpoint(conn.get("input", {}))
        outputs = conn.get("output", [])
        outputs = outputs.values() if isinstance(outputs, Mapping) else outputs
        wires.update((src, _endpoint(out)) for out in outputs)
    return wires


def _canonical_plan_content(plan_data: Mapping[str, Any]) -> dict[str, list[Any]]:
    """Renumbering-tolerant content of a plan for plan_hash(): every element keyed by its own
    stable identity (see _element_identity) instead of Comexio's raw, renumberable
    FubElementId, plus every wire keyed by its endpoints' stable identities (see
    _connection_wires) instead of the raw connection dict.
    """
    elements = plan_data.get("elements", {})
    return {
        "elements": sorted((_element_identity(elem) for elem in elements.values()), key=str),
        "wires": sorted(_connection_wires(plan_data), key=str),
    }


def plan_hash(plan_data: Mapping[str, Any]) -> str:
    """Return a canonical, renumbering-tolerant SHA-256 over a plan's elements + wiring.

    Hashed over each element/wire's stable identity (see _canonical_plan_content), not
    Comexio's raw FubElementIds — those get renumbered wholesale by some Comexio-side sync
    operations without the wiring itself actually changing (see diff_snapshots for the
    confirmed 2026-07-13 case), which would otherwise make an auto-backup treat such a sync as
    a real content change on every single run.
    """
    canonical = json.dumps(_canonical_plan_content(plan_data), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def diff_snapshots(newer: Mapping[str, Any], older: Mapping[str, Any]) -> dict[str, Any]:
    """Semantic added/removed diff between two snapshots of the SAME plan identity.

    Identity is reference-based (see _element_identity), not the raw FubElementId Comexio
    assigns each plan element — those get renumbered wholesale during some bulk operations
    without the wiring itself actually changing, which a raw-ID diff would otherwise report
    as a wall of false-positive removals/additions (confirmed against a real snapshot pair
    2026-07-13: ~130 elements renumbered by a constant offset within one poll interval).
    Returns raw identity tuples, not human-readable labels — resolving those against the
    marker/IO maps (see snapshot_label_maps) is the caller's job.
    """
    newer_elements, older_elements = newer.get("elements", {}), older.get("elements", {})
    newer_wires, older_wires = _connection_wires(newer), _connection_wires(older)

    def _added_removed(newer_set: set[_Identity], older_set: set[_Identity]) -> dict[str, list[_Identity]]:
        return {"added": sorted(newer_set - older_set, key=str), "removed": sorted(older_set - newer_set, key=str)}

    added_wires, removed_wires = newer_wires - older_wires, older_wires - newer_wires
    moved_kinds = _moved_kinds(
        {_element_identity(elem) for elem in older_elements.values()},
        {_element_identity(elem) for elem in newer_elements.values()},
    )
    added_c, removed_c, moved_c = _split_moved(added_wires, removed_wires, moved_kinds)

    return {
        "markers": _added_removed(_named_identities(newer_elements, 2), _named_identities(older_elements, 2)),
        "ios": _added_removed(_named_identities(newer_elements, 1), _named_identities(older_elements, 1)),
        "connections": {"added": added_c, "removed": removed_c, "moved": moved_c},
    }


def referenced_label_metadata(
    plan_data: Mapping[str, Any],
    markers_by_id: Mapping[str, Any],
    webio_by_id: Mapping[str, Any],
    ios_by_id: Mapping[str, Any],
) -> dict[str, dict[str, str]]:
    """Capture the display name of every marker/WebIO/IO this plan references, right now.

    Meant to be stored alongside the snapshot (as its "labels") so a later diff/preview of THIS
    snapshot can show names as they were at capture time instead of resolving them against
    whatever the live data says later (see snapshot_label_maps) — a renamed or deleted marker
    would otherwise make an old backup look like it was wired to something it never was. Kept
    minimal (only referenced ids, name only) since a snapshot is stored on every wiring change.
    """
    by_type = {2: markers_by_id, 10: webio_by_id, 1: ios_by_id}
    metadata: dict[str, dict[str, str]] = {}
    for elem in plan_data.get("elements", {}).values():
        ref = _reference(elem)
        ref_type: Any = ref.get("type")
        key = _LABEL_REF_TYPES.get(ref_type)
        if key is None:
            continue
        ref_id = str(ref.get("ref_id"))
        entry = by_type[ref_type].get(ref_id)
        if entry and "name" in entry:
            metadata.setdefault(key, {})[ref_id] = entry["name"]
    return metadata


def snapshot_label_maps(
    labels: Mapping[str, Mapping[str, str]] | None,
    live_markers: Mapping[str, Any],
    live_webio: Mapping[str, Any],
    live_ios: Mapping[str, Any],
) -> tuple[Mapping[str, Any], Mapping[str, Any], Mapping[str, Any]]:
    """Overlay a snapshot's captured-at-backup-time names onto the live label maps.

    `labels` is one snapshot's stored metadata (see referenced_label_metadata) — None/empty for
    a snapshot captured before labels were stored, in which case this is a no-op and callers
    keep resolving against live data. Where present, a captured name overrides the live one;
    other fields (e.g. analog/digital classification) are kept from the live entry when the id
    still exists there, so a still-live marker keeps its correct pill styling while showing its
    historical name. An id no longer present live gets a bare {"name": ...} entry instead of
    falling through to resolve_element_label()'s generic "(unknown)" fallback.
    """
    labels = labels or {}

    def _overlay(live: Mapping[str, Any], captured: Mapping[str, str]) -> Mapping[str, Any]:
        if not captured:
            return live
        merged = dict(live)
        for ref_id, name in captured.items():
            merged[ref_id] = {**merged.get(ref_id, {}), "name": name}
        return merged

    return (
        _overlay(live_markers, labels.get("markers", {})),
        _overlay(live_webio, labels.get("webio", {})),
        _overlay(live_ios, labels.get("ios", {})),
    )
