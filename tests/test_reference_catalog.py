"""Reference catalog reconciliation: block-type ids resolved by stable key, never hard-coded."""

import copy
import json
import logging
from pathlib import Path
from typing import Any

import pytest

from aiocomexio.reference_catalog import (
    KIND_FUB_BASE,
    KIND_FUB_TYPES,
    LIVE_EXTRACTORS,
    REASON_CHECK_FAILED,
    REASON_NO_REFERENCE,
    REFERENCE_DIR,
    EntryStatus,
    ReferenceCatalog,
    build_reference,
    extract_live_fub_base,
    find_unknown_fub_base_refs,
    format_deviations,
    key_family,
    load_reference_catalogs,
    parse_reference,
    reconcile,
    unresolved,
)
from tests.common import load_json_fixture

FLANKE_ID = 113
FLANKE_KEY = "flankenerkenner/daa/ddd"
REQUIRED = ((KIND_FUB_BASE, FLANKE_KEY),)


@pytest.fixture
def raw() -> dict[str, Any]:
    data: dict[str, Any] = load_json_fixture("reference_catalog_raw.json")
    return data


def _references(raw: dict[str, Any], **overrides: int) -> dict[str, ReferenceCatalog]:
    """Reference catalogs built from raw itself, with selected fub_base ids overridden."""
    references = {kind: parse_reference(kind, build_reference(kind, raw, "11.1.4")) for kind in LIVE_EXTRACTORS}
    entries = {**references[KIND_FUB_BASE].entries, **overrides}
    references[KIND_FUB_BASE] = ReferenceCatalog(KIND_FUB_BASE, "11.1.4", entries)
    return references


def _flanke(raw: dict[str, Any]) -> dict[str, Any]:
    flanke: dict[str, Any] = raw["FubModules"]["5"][str(FLANKE_ID)]
    return flanke


def test_key_is_name_plus_port_signature_in_pos_order_without_apps(raw: dict[str, Any]) -> None:
    live = extract_live_fub_base(raw)
    assert live == {
        "or/dd/d": [5],
        "or/ddd/d": [6],
        "average/aa/a": [45],
        FLANKE_KEY: [FLANKE_ID],
    }


def test_port_signature_follows_pos_not_list_order(raw: dict[str, Any]) -> None:
    flanke = _flanke(raw)
    flanke["input"] = list(reversed(flanke["input"]))
    flanke["output"] = {str(i): port for i, port in reversed(list(enumerate(flanke["output"])))}
    live = extract_live_fub_base(raw)
    assert live is not None
    assert live[FLANKE_KEY] == [FLANKE_ID]


def test_live_entries_without_name_or_integer_id_are_skipped(raw: dict[str, Any]) -> None:
    expected = extract_live_fub_base(raw)
    group = raw["FubModules"]["5"]
    group["900"] = {**copy.deepcopy(_flanke(raw)), "Id": 900, "Name": ""}
    group["901"] = {**copy.deepcopy(group["5"]), "Id": "x", "Name": "xor"}
    assert extract_live_fub_base(raw) == expected
    check = reconcile(_references(raw), raw, "11.1.4")
    assert check.fub_base_ids == frozenset({5, 6, 45, 65, FLANKE_ID, 900})  # 901 has no integer id


def test_key_family() -> None:
    assert key_family(FLANKE_KEY) == "flankenerkenner"
    assert key_family("marker") == "marker"


def test_installed_apps_are_not_reference_data_but_known_plan_targets(raw: dict[str, Any]) -> None:
    assert "appaaaa00000_fubbbbb11111/d/d" not in build_reference(KIND_FUB_BASE, raw, "11.1.4")["entries"]
    check = reconcile(_references(raw), raw, "11.1.4")
    assert check.fub_base_ids == frozenset({5, 6, 45, 65, FLANKE_ID})
    assert check.catalogs[KIND_FUB_BASE].new_keys == ()


def test_ports_serialized_as_object_or_garbage(raw: dict[str, Any]) -> None:
    flanke = _flanke(raw)
    flanke["input"] = {str(i): port for i, port in enumerate(flanke["input"])}
    flanke["output"] = 1
    live = extract_live_fub_base(raw)
    assert live is not None
    assert live["flankenerkenner/daa/"] == [FLANKE_ID]


def test_identical_catalog_is_all_ok(raw: dict[str, Any]) -> None:
    check = reconcile(_references(raw), raw, "11.1.4")
    assert all(entry.status is EntryStatus.OK for c in check.catalogs.values() for entry in c.entries.values())
    assert check.resolve(KIND_FUB_BASE, FLANKE_KEY) == FLANKE_ID
    assert check.resolve(KIND_FUB_TYPES, "marker") == 2
    assert unresolved(check, REQUIRED) == []
    assert format_deviations(check) == []
    assert "fub_base 4/4 ok" in check.summary()


def test_moved_id_resolves_to_the_live_id(raw: dict[str, Any]) -> None:
    references = _references(raw, **{FLANKE_KEY: 100113, "or/dd/d": 1005})
    check = reconcile(references, raw, "11.1.4")
    assert check.status(KIND_FUB_BASE, FLANKE_KEY) is EntryStatus.MOVED
    assert check.resolve(KIND_FUB_BASE, FLANKE_KEY) == FLANKE_ID
    assert unresolved(check, REQUIRED) == []
    assert f"fub_base {FLANKE_KEY}: moved ref=100113 live=113" in format_deviations(check)
    assert "2 moved" in check.summary()
    assert not check.has_unusable()


def test_missing_block_is_blocked(raw: dict[str, Any]) -> None:
    references = _references(raw)
    del raw["FubModules"]["5"][str(FLANKE_ID)]
    check = reconcile(references, raw, "11.1.4")
    assert check.status(KIND_FUB_BASE, FLANKE_KEY) is EntryStatus.MISSING
    assert check.resolve(KIND_FUB_BASE, FLANKE_KEY) is None
    assert unresolved(check, REQUIRED) == [f"fub_base:{FLANKE_KEY} (missing)"]
    assert check.has_unusable()


def test_changed_port_layout_is_blocked_even_with_the_same_id(raw: dict[str, Any]) -> None:
    references = _references(raw)
    _flanke(raw)["input"] = _flanke(raw)["input"][:1]
    check = reconcile(references, raw, "11.1.4")
    assert check.status(KIND_FUB_BASE, FLANKE_KEY) is EntryStatus.CHANGED
    assert check.resolve(KIND_FUB_BASE, FLANKE_KEY) is None
    assert check.catalogs[KIND_FUB_BASE].new_keys == ("flankenerkenner/d/ddd",)
    assert "1 new" in check.summary()


def test_duplicate_live_key_is_ambiguous(raw: dict[str, Any]) -> None:
    references = _references(raw)
    raw["FubModules"]["5"]["213"] = {**copy.deepcopy(_flanke(raw)), "Id": 213}
    check = reconcile(references, raw, "11.1.4")
    assert check.status(KIND_FUB_BASE, FLANKE_KEY) is EntryStatus.AMBIGUOUS
    assert check.resolve(KIND_FUB_BASE, FLANKE_KEY) is None


def test_key_missing_from_the_reference_file_is_blocked_not_used(raw: dict[str, Any]) -> None:
    references = _references(raw)
    entries = {k: v for k, v in references[KIND_FUB_BASE].entries.items() if k != FLANKE_KEY}
    references[KIND_FUB_BASE] = ReferenceCatalog(KIND_FUB_BASE, "11.1.4", entries)
    check = reconcile(references, raw, "11.1.4")
    assert check.resolve(KIND_FUB_BASE, FLANKE_KEY) is None  # live has it, but unchecked
    assert unresolved(check, REQUIRED) == [f"fub_base:{FLANKE_KEY} (not checked)"]


def test_unresolved_names_why_there_is_no_result() -> None:
    assert unresolved(None, REQUIRED, REASON_CHECK_FAILED) == [f"fub_base:{FLANKE_KEY} ({REASON_CHECK_FAILED})"]


def test_summary_without_duration_is_stable_across_polls(raw: dict[str, Any]) -> None:
    references = _references(raw)
    first, second = (reconcile(references, raw, "11.1.4") for _ in range(2))
    object.__setattr__(second, "duration_ms", first.duration_ms + 5)
    assert first.summary(include_duration=False) == second.summary(include_duration=False)
    assert " ms)" not in first.summary(include_duration=False)
    assert first.summary().endswith(" ms)")


def test_live_catalog_unavailable(raw: dict[str, Any]) -> None:
    references = _references(raw)
    raw["FubModules"]["5"] = []  # PHP serializes an empty mapping as an array
    check = reconcile(references, raw, "11.1.4")
    assert not check.catalogs[KIND_FUB_BASE].live_available
    assert check.fub_base_ids == frozenset()
    assert check.resolve(KIND_FUB_BASE, FLANKE_KEY) is None
    assert "fub_base live data unavailable" in check.summary()


def test_fub_types_unavailable_blocks_only_fub_types(raw: dict[str, Any]) -> None:
    references = _references(raw)
    del raw["FubTypes"]
    check = reconcile(references, raw, "11.1.4")
    assert not check.catalogs[KIND_FUB_TYPES].live_available
    assert check.resolve(KIND_FUB_TYPES, "marker") is None
    assert check.resolve(KIND_FUB_BASE, FLANKE_KEY) == FLANKE_ID


def test_unloadable_reference_file_is_reported_not_unchecked(raw: dict[str, Any]) -> None:
    references = _references(raw)
    del references[KIND_FUB_BASE]
    check = reconcile(references, raw, "11.1.4")
    assert check.missing_references() == [KIND_FUB_BASE]
    assert unresolved(check, REQUIRED) == [f"fub_base:{FLANKE_KEY} ({REASON_NO_REFERENCE})"]
    assert check.has_unusable()
    assert "fub_base reference unavailable" in check.summary()


def test_format_deviations_is_capped(raw: dict[str, Any]) -> None:
    check = reconcile(_references(raw, **{FLANKE_KEY: 1, "or/dd/d": 2}), raw, "11.1.4")
    lines = format_deviations(check, limit=1)
    assert len(lines) == 2
    assert lines[1] == "... 1 more"


def test_fingerprint_changes_only_with_the_result(raw: dict[str, Any]) -> None:
    references = _references(raw)
    first = reconcile(references, raw, "11.1.4").fingerprint()
    assert reconcile(references, raw, "11.1.4").fingerprint() == first
    assert reconcile(_references(raw, **{FLANKE_KEY: 1}), raw, "11.1.4").fingerprint() != first


def test_fingerprint_tells_apart_deviations_with_equal_counts(raw: dict[str, Any]) -> None:
    flanke_moved = reconcile(_references(raw, **{FLANKE_KEY: 1}), raw, "11.1.4")
    or_moved = reconcile(_references(raw, **{"or/dd/d": 1}), raw, "11.1.4")
    assert flanke_moved.catalogs[KIND_FUB_BASE].counts() == or_moved.catalogs[KIND_FUB_BASE].counts()
    assert flanke_moved.fingerprint() != or_moved.fingerprint()


@pytest.mark.parametrize(
    "data",
    [
        {"format": 2, "kind": KIND_FUB_BASE, "entries": {"a/d/d": 1}},
        {"format": 1, "kind": KIND_FUB_TYPES, "entries": {"a/d/d": 1}},
        {"format": 1, "kind": KIND_FUB_BASE, "entries": {}},
        {"format": 1, "kind": KIND_FUB_BASE, "entries": {"a/d/d": "x"}},
        {"format": 1, "kind": KIND_FUB_BASE, "entries": {"a/d/d": 1.5}},
        {"format": 1, "kind": KIND_FUB_BASE, "entries": {"a/d/d": True}},
        {"format": 1, "kind": KIND_FUB_BASE, "entries": {"a/d/d": "12"}},
        [],
    ],
)
def test_malformed_reference_is_rejected(data: Any) -> None:
    with pytest.raises(ValueError):
        parse_reference(KIND_FUB_BASE, data)


def test_build_reference_refuses_an_empty_catalog(raw: dict[str, Any]) -> None:
    raw["FubModules"]["5"] = []
    with pytest.raises(ValueError, match="no live entries"):
        build_reference(KIND_FUB_BASE, raw, "11.1.4")


def test_build_reference_refuses_non_unique_keys(raw: dict[str, Any]) -> None:
    raw["FubModules"]["5"]["213"] = {**copy.deepcopy(_flanke(raw)), "Id": 213}
    with pytest.raises(ValueError, match="not unique"):
        build_reference(KIND_FUB_BASE, raw, "11.1.4")


def test_shipped_reference_files_cover_every_extractor_and_the_flanke() -> None:
    references = load_reference_catalogs(REFERENCE_DIR)
    assert set(references) == set(LIVE_EXTRACTORS)
    assert references[KIND_FUB_BASE].entries[FLANKE_KEY] == FLANKE_ID
    assert references[KIND_FUB_TYPES].entries["fubBase"] == 5


def test_unusable_reference_files_are_logged_and_skipped(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    (tmp_path / f"{KIND_FUB_BASE}.json").write_text("{not json", encoding="utf-8")
    (tmp_path / "unknown_kind.json").write_text("{}", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        assert load_reference_catalogs(tmp_path) == {}
    messages = [(record.levelno, record.getMessage()) for record in caplog.records]
    assert (logging.WARNING, "Reference catalog unknown_kind.json has no live extractor — ignored") in messages
    assert (logging.ERROR, f"Reference catalog {KIND_FUB_BASE}.json unusable") in messages
    for kind in LIVE_EXTRACTORS:
        assert (
            logging.ERROR,
            f"Reference catalog {kind}.json missing or unusable — its entries can't be resolved",
        ) in messages


def test_built_reference_round_trips_through_a_file(tmp_path: Path, raw: dict[str, Any]) -> None:
    for kind in LIVE_EXTRACTORS:
        content = build_reference(kind, raw, "11.1.4")
        (tmp_path / f"{kind}.json").write_text(json.dumps(content), encoding="utf-8")
    references = load_reference_catalogs(tmp_path)
    assert all(
        entry.status is EntryStatus.OK
        for c in reconcile(references, raw, "11.1.4").catalogs.values()
        for entry in c.entries.values()
    )


def test_unknown_plan_refs_are_found() -> None:
    plans: dict[str, Any] = {
        "19": {
            "elements": {
                "593": {"reference": {"type": "5", "ref_id": "113"}},
                "594": {"reference": {"type": "2", "ref_id": "999"}},
                "595": {"reference": {"type": "5", "ref_id": "5"}},
            }
        },
        "20": {"elements": []},
        "21": {"elements": [{"reference": {"type": "5", "ref_id": "113"}}]},
        "22": {"elements": {"1": None, "2": [], "3": {"reference": "5"}, "4": {"reference": {"type": "5"}}}},
        "25": {"elements": {"8": {"reference": {"type": "5", "ref_id": None}}}},  # no ref_id: malformed
        "23": None,
        "24": {"elements": {"7": {"reference": {"type": 5, "ref_id": 114}}}},  # numbers, not strings
    }
    assert find_unknown_fub_base_refs(plans, [5, 6, 45]) == [("19", "593", "113"), ("24", "7", "114")]
    assert find_unknown_fub_base_refs(plans, [5, 6, 45, 113, 114]) == []
