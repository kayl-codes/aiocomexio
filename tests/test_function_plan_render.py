"""Function plan SVG preview rendering and the read-only plan health check."""

import os
import subprocess
import sys
from types import MappingProxyType
from typing import Any
from xml.etree import ElementTree

import pytest
from syrupy.assertion import SnapshotAssertion
from syrupy.extensions.single_file import SingleFileSnapshotExtension, WriteMode

from aiocomexio.function_plan import (
    analyze_function_plan,
    detect_self_reset_cycles,
    element_id_sort_key,
    element_search_id,
    render_constants,
    render_flow,
    render_flow_svg,
    render_plan_svg,
)
from aiocomexio.function_plan.render_labels import strip_xml_forbidden, with_block_values
from tests.common import FIXTURES_DIR, load_json_fixture


class SvgSnapshotExtension(SingleFileSnapshotExtension):
    """Store each SVG snapshot as its own .svg file, so it can be opened and eyeballed directly."""

    file_extension = "svg"
    _write_mode = WriteMode.TEXT


@pytest.fixture
def svg_snapshot(snapshot: SnapshotAssertion) -> SnapshotAssertion:
    return snapshot.use_extension(SvgSnapshotExtension)


@pytest.fixture
def plan() -> dict[str, Any]:
    plan: dict[str, Any] = load_json_fixture("function_plan.json")
    return plan


def _render(plan: dict[str, Any], **kwargs: Any) -> str:
    return render_plan_svg(
        plan["elements"],
        plan["connections"],
        plan["catalog"],
        plan["markers_by_id"],
        plan["webio_by_id"],
        plan["ios_by_id"],
        title="Test <Plan>",
        **kwargs,
    )


def test_render_fit_to_content(plan: dict[str, Any], svg_snapshot: SnapshotAssertion) -> None:
    assert _render(plan) == svg_snapshot


def test_render_on_paper_canvas(plan: dict[str, Any], svg_snapshot: SnapshotAssertion) -> None:
    assert _render(plan, title_suffix="aktiv · A4", canvas=(870.0, 720.0)) == svg_snapshot


def test_render_escapes_title(plan: dict[str, Any]) -> None:
    svg = _render(plan)

    assert "Test &lt;Plan&gt;" in svg
    assert "<Plan>" not in svg


def test_render_plan_id_marks_root_svg(plan: dict[str, Any]) -> None:
    svg = _render(plan, plan_id='7"<x>')

    assert svg.startswith("<svg ")
    assert 'data-plan-id="7&quot;&lt;x&gt;"' in svg.split(">", 1)[0]
    assert "data-plan-id" not in _render(plan)


def test_render_accepts_output_as_any_mapping(plan: dict[str, Any]) -> None:
    # Regression: only a dict output group was unpacked; a read-only mapping was iterated as keys.
    as_proxy = {
        **plan,
        "connections": {
            cid: {**conn, "output": MappingProxyType({str(i): out for i, out in enumerate(conn["output"])})}
            for cid, conn in plan["connections"].items()
        },
    }

    assert _render(as_proxy) == _render(plan)


def test_render_empty_plan_does_not_crash() -> None:
    svg = render_plan_svg({}, {}, {}, {}, {}, {}, title="Leer")

    assert svg.startswith("<svg")
    assert svg.endswith("</svg>")


def test_self_reset_cycle_is_detected(plan: dict[str, Any]) -> None:
    assert detect_self_reset_cycles(plan["elements"], plan["connections"], plan["catalog"]) == [("8", "9")]


def test_analyze_function_plan(plan: dict[str, Any], snapshot: SnapshotAssertion) -> None:
    findings = analyze_function_plan(
        plan["elements"],
        plan["connections"],
        plan["catalog"],
        plan["markers_by_id"],
        plan["webio_by_id"],
        plan["ios_by_id"],
    )

    assert findings == snapshot


def test_render_flow_diagram(plan: dict[str, Any], svg_snapshot: SnapshotAssertion) -> None:
    svg, skipped = render_flow_svg(
        plan["elements"],
        plan["connections"],
        plan["catalog"],
        plan["markers_by_id"],
        plan["webio_by_id"],
        plan["ios_by_id"],
        title="Test <Plan>",
    )

    # Only the managed-plan comment carries no wiring.
    assert skipped == 1
    assert "1 unverdrahtete Element(e) ausgeblendet" in svg
    assert "Test &lt;Plan&gt;" in svg
    assert svg.count('class="flow-edge"') == 6
    # Both members of the marker/timer self-reset cycle are flagged as such.
    assert "⟲ M4 Klingel [TRIG]</text>" in svg
    assert "⟲ T1 Taster Reset</text>" in svg
    assert svg == svg_snapshot


# Renders the fixture's flow diagram in a fresh interpreter, so PYTHONHASHSEED takes effect.
_FLOW_SCRIPT = """
import json, sys
from aiocomexio.function_plan import render_flow_svg
plan = json.loads(open(sys.argv[1], encoding="utf-8").read())
svg, _ = render_flow_svg(
    plan["elements"], plan["connections"], plan["catalog"],
    plan["markers_by_id"], plan["webio_by_id"], plan["ios_by_id"], title="T",
)
sys.stdout.buffer.write(svg.encode("utf-8"))
"""


def _run_with_hash_seeds(script: str, *args: str) -> set[bytes]:
    """stdout of `script` in fresh interpreters with different PYTHONHASHSEEDs (one per seed)."""
    outputs = set()
    for seed in ("1", "2", "3", "4"):
        result = subprocess.run(
            [sys.executable, "-c", script, *args],
            env={**os.environ, "PYTHONHASHSEED": seed},
            capture_output=True,
            check=True,
        )
        outputs.add(result.stdout)
    return outputs


def test_render_flow_diagram_is_independent_of_hash_seed() -> None:
    # Regression: box positions and edge order followed set iteration order, so the self-reset
    # cycle's members swapped places between interpreter runs (seeds 1 and 2 differed).
    assert len(_run_with_hash_seeds(_FLOW_SCRIPT, str(FIXTURES_DIR / "function_plan.json"))) == 1


# One marker resetting itself through two on_pulse timers: two cycles from one marker.
_TWO_TIMER_SCRIPT = """
from aiocomexio.function_plan import detect_self_reset_cycles
elements = {
    "1": {"reference": {"type": 2, "ref_id": 1}},
    "10": {"reference": {"type": 4, "ref_id": 1}},
    "2": {"reference": {"type": 4, "ref_id": 1}},
}
wires = [("1", "10"), ("1", "2"), ("10", "1"), ("2", "1")]
connections = {
    str(i): {"input": {"FubElementId": int(src)}, "output": [{"FubElementId": int(dst)}]}
    for i, (src, dst) in enumerate(wires)
}
catalog = {"time_modules": {"1": {"kind": "on_pulse"}}}
print(detect_self_reset_cycles(elements, connections, catalog))
"""


def test_self_reset_cycles_are_in_element_id_order_for_any_hash_seed() -> None:
    # Regression: timers were taken from a set, so the cycle order depended on PYTHONHASHSEED.
    assert _run_with_hash_seeds(_TWO_TIMER_SCRIPT) == {b"[('1', '2'), ('1', '10')]" + os.linesep.encode()}


def test_render_flow_empty_plan_does_not_crash() -> None:
    svg, skipped = render_flow_svg({}, {}, {}, {}, {}, {}, title="Leer")

    assert skipped == 0
    assert svg.startswith("<svg")
    assert svg.endswith("</svg>")


@pytest.mark.parametrize(
    ("element_id", "expected"),
    [
        ("2", "M1"),  # marker
        ("3", "BASE#I1"),  # IO: extension + identifier
        ("9", "T1"),  # time module
        ("5", ""),  # Web-IO command: no own object id
        ("1", ""),  # comment
    ],
)
def test_element_search_id(plan: dict[str, Any], element_id: str, expected: str) -> None:
    assert element_search_id(plan["elements"][element_id], plan["ios_by_id"]) == expected


def test_element_search_id_unknown_io_is_empty() -> None:
    assert element_search_id({"reference": {"type": 1, "ref_id": 99}}, {}) == ""


def test_element_id_sort_key_orders_numerically_and_never_raises() -> None:
    ids = ["10", "b", "2", "²", "1"]

    assert sorted(ids, key=element_id_sort_key) == ["1", "2", "10", "b", "²"]


def test_element_id_sort_key_sorts_an_id_beyond_the_int_digit_limit_as_text() -> None:
    huge = "1" * 5000

    assert sorted(["b", huge, "2"], key=element_id_sort_key) == ["2", huge, "b"]


_OR_TITLE = "<title>Oder</title>"


def _flow(plan: dict[str, Any], **kwargs: Any) -> str:
    svg, _skipped = render_flow_svg(
        plan["elements"],
        plan["connections"],
        plan["catalog"],
        plan["markers_by_id"],
        plan["webio_by_id"],
        plan["ios_by_id"],
        title="Test <Plan>",
        **kwargs,
    )
    return svg


def test_block_values_without_values_leave_output_unchanged(plan: dict[str, Any]) -> None:
    assert _render(plan, block_values=None) == _render(plan)
    assert _render(plan, block_values={}) == _render(plan)
    assert _flow(plan, block_values=None) == _flow(plan)


def test_block_values_listed_in_block_hover(plan: dict[str, Any]) -> None:
    values = {"4": {"time_up": "12", "Kühlgrenze": "21.5", "autohide": "1"}}
    svg = _render(plan, block_values=values)
    assert "<title>Oder\nKühlgrenze: 21.5\ntime_up: 12</title>" in svg
    assert "autohide" not in svg
    # The title bar text and data-label keep the plain label; the other block is untouched.
    assert 'data-label="Oder"' in svg
    assert svg.count(_OR_TITLE) == 0
    assert "<title>Nicht</title>" in svg


def test_block_values_listed_in_flow_hover(plan: dict[str, Any]) -> None:
    svg = _flow(plan, block_values={"4": {"b": "2", "a": "1"}})
    assert "<title>Oder\na: 1\nb: 2</title>" in svg
    assert "<title>Nicht</title>" in svg


def test_block_values_only_apply_to_blocks(plan: dict[str, Any]) -> None:
    marker_ids = [k for k, e in plan["elements"].items() if e["reference"]["type"] == 2]
    svg = _render(plan, block_values={marker_ids[0]: {"x": "1"}})
    assert svg == _render(plan)


def test_block_values_are_escaped(plan: dict[str, Any]) -> None:
    svg = _render(plan, block_values={"4": {"<b>&": '"x" <y>'}})
    assert "<b>&:" not in svg
    assert "&lt;b&gt;&amp;: &quot;x&quot; &lt;y&gt;" in svg
    assert "&lt;b&gt;&amp;: &quot;x&quot; &lt;y&gt;" in _flow(plan, block_values={"4": {"<b>&": '"x" <y>'}})


@pytest.mark.parametrize(
    "render",
    [_render, lambda plan: _render(plan, canvas=(870.0, 720.0)), _flow],
    ids=["fit", "paper", "flow"],
)
def test_rendered_svg_is_well_formed_xml(plan: dict[str, Any], render: Any) -> None:
    # A standalone .svg goes through an XML parser: e.g. a literal "<title>" in a CSS comment breaks it.
    ElementTree.fromstring(render(plan))


# Every XML 1.0-forbidden range, with both ends of each range.
_XML_FORBIDDEN = [
    "\x00",
    "\x01",
    "\x08",
    "\x0b",
    "\x0c",
    "\x0e",
    "\x1b",
    "\x1f",
    "\ud800",
    "\udfff",
    "\ufffe",
    "\uffff",
]


@pytest.mark.parametrize("char", _XML_FORBIDDEN)
def test_xml_forbidden_characters_in_any_text_keep_the_svg_parsable(plan: dict[str, Any], char: str) -> None:
    # html.escape keeps U+0001 & co.; an XML parser rejects the whole SVG for one of them.
    for marker in plan["markers_by_id"].values():
        marker["name"] = f"{marker['name']}{char}x"
    values = {"4": {f"a{char}b": f"1{char}2"}}

    for svg in (
        _render(plan, title_suffix=f"s{char}t", block_values=values),
        _flow(plan, title_suffix=f"s{char}t", block_values=values),
    ):
        ElementTree.fromstring(svg)
        assert char not in svg


def test_strip_xml_forbidden_keeps_every_allowed_character() -> None:
    allowed = "\t\n\r \x7f\x85\ud7ff\ue000\ufffd\U00010000\U0001f600\U0010ffff"

    assert strip_xml_forbidden(allowed) == allowed
    assert strip_xml_forbidden("".join(_XML_FORBIDDEN)) == ""


_BLOCK = {"reference": {"type": 5, "ref_id": 1}}


def test_with_block_values_sorts_setting_names_case_insensitively() -> None:
    # A plain sorted() puts "Zeta" before "alpha".
    assert with_block_values("Oder", "4", _BLOCK, {"4": {"Zeta": "1", "alpha": "2"}}) == "Oder\nalpha: 2\nZeta: 1"


def test_with_block_values_only_autohide_keeps_the_plain_label() -> None:
    label = with_block_values("Oder", "4", _BLOCK, {"4": {"autohide": "1"}})

    assert label == "Oder"
    assert not label.endswith("\n")


def test_with_block_values_ignores_unknown_ids_non_blocks_and_non_str_names() -> None:
    values: Any = {"99": {"x": "1"}, "4": {7: "bad", "a": "1"}}

    assert with_block_values("Oder", "4", _BLOCK, values) == "Oder\na: 1"
    assert with_block_values("Oder", "5", _BLOCK, values) == "Oder"
    assert with_block_values("M1", "4", {"reference": {"type": 2}}, values) == "M1"


def test_with_block_values_skips_settings_whose_value_is_not_a_str() -> None:
    values: Any = {"4": {"n": 5, "f": 1.5, "none": None, "flag": True, "ok": "1"}}

    assert with_block_values("Oder", "4", _BLOCK, values) == "Oder\nok: 1"


def test_with_block_values_keeps_a_value_from_forging_extra_hover_lines() -> None:
    values = {"4": {"a": "1\nforged: 9", "b": "x\r\ny", "c": "p\rq", "d\ne": "v"}}

    text = with_block_values("Oder", "4", _BLOCK, values)

    assert text == "Oder\na: 1 forged: 9\nb: x y\nc: p q\nd e: v"
    assert len(text.split("\n")) == 5


@pytest.mark.parametrize("sep", ["", "", "", "", "", "", " ", " "])
def test_with_block_values_replaces_every_unicode_line_boundary(sep: str) -> None:
    values = {"4": {f"a{sep}b": f"1{sep}forged: 9"}}

    text = with_block_values("Oder", "4", _BLOCK, values)

    assert text == "Oder\na b: 1 forged: 9"
    assert len(text.splitlines()) == 2


class _UnprintableKey:
    def __str__(self) -> str:
        raise RuntimeError("no str")


def test_with_block_values_never_stringifies_a_setting_name_that_is_not_a_str() -> None:
    values: Any = {"4": {_UnprintableKey(): "1", "B": "2", "a": "3"}}

    assert with_block_values("Oder", "4", _BLOCK, values) == "Oder\na: 3\nB: 2"


def test_with_block_values_orders_names_that_differ_only_in_case_independent_of_input_order() -> None:
    expected = "Oder\nA: 2\na: 1"

    assert with_block_values("Oder", "4", _BLOCK, {"4": {"a": "1", "A": "2"}}) == expected
    assert with_block_values("Oder", "4", _BLOCK, {"4": {"A": "2", "a": "1"}}) == expected


_SVG_NS = "{http://www.w3.org/2000/svg}"


def test_node_tooltip_covers_the_whole_node_group(plan: dict[str, Any]) -> None:
    # On the group, the tooltip also shows over pins, title bar and texts; nothing needs pointer-events.
    root = ElementTree.fromstring(_render(plan, block_values={"4": {"a": "1"}}))
    groups = [
        g
        for g in root.iter(f"{_SVG_NS}g")
        if "node-g" in g.get("class", "").split() and g[0].get("class") != "node-comment"
    ]

    assert len(groups) > 1
    for group in groups:
        assert group[0].tag == f"{_SVG_NS}title", group.get("data-eid")
        if group.get("data-eid") == "4":
            assert group[0].text == "Oder\na: 1"
        else:
            # An XML parser turns the newline of a multi-line label into a space inside data-label.
            assert " ".join((group[0].text or "").split()) == group.get("data-label")
    assert all(rect.find(f"{_SVG_NS}title") is None for rect in root.iter(f"{_SVG_NS}rect"))
    assert "pointer-events" not in render_constants._STYLE
    assert "pointer-events: none" in render_flow._STYLE.split(".flow-label", 1)[1].split("}", 1)[0]


def test_unconnected_pill_and_block_carry_the_tooltip_on_the_group() -> None:
    elements = {
        "1": {"reference": {"type": 2, "ref_id": 7}, "name": "", "position_x": 0, "position_y": 0},
        "2": {"reference": {"type": 5, "ref_id": 1}, "name": "", "position_x": 300, "position_y": 0},
    }
    svg = render_plan_svg(elements, {}, {}, {"7": {"name": "M7 Frei"}}, {}, {}, title="T")

    groups = {g.get("data-eid"): g for g in ElementTree.fromstring(svg).iter(f"{_SVG_NS}g") if g.get("data-eid")}
    for eid in ("1", "2"):
        assert groups[eid][0].tag == f"{_SVG_NS}title"
        rect = groups[eid].find(f"{_SVG_NS}rect")
        assert rect is not None
        assert rect.get("class") == "node-orphan"
        assert groups[eid].find(f"{_SVG_NS}rect/{_SVG_NS}title") is None
    assert groups["1"][0].text == "M7 Frei"


def test_strip_xml_forbidden_matches_the_xml_char_production_for_every_bmp_character() -> None:
    def forbidden(c: int) -> bool:
        return c <= 0x08 or c in (0x0B, 0x0C, 0xFFFE, 0xFFFF) or 0x0E <= c <= 0x1F or 0xD800 <= c <= 0xDFFF

    text = "".join(map(chr, range(0x10000)))

    assert strip_xml_forbidden(text) == "".join(chr(c) for c in range(0x10000) if not forbidden(c))


def test_comment_node_has_no_tooltip_and_keeps_the_pointer() -> None:
    elements = {"1": {"reference": {"type": 14, "ref_id": 0}, "name": "Hinweis", "position_x": 0, "position_y": 0}}
    svg = render_plan_svg(elements, {}, {}, {}, {}, {}, title="T")

    group = next(g for g in ElementTree.fromstring(svg).iter(f"{_SVG_NS}g") if g.get("data-eid") == "1")
    assert group.find(f"{_SVG_NS}title") is None
    assert group.find(f"{_SVG_NS}text") is not None
    # Neither the style block nor an inline attribute may take the pointer from the comment text.
    assert "pointer-events" not in svg


@pytest.mark.parametrize("block_values", ["x", 5, ["a"], ("a",), True])
def test_with_block_values_ignores_block_values_that_is_not_a_mapping(block_values: Any) -> None:
    assert with_block_values("Oder", "4", _BLOCK, block_values) == "Oder"


@pytest.mark.parametrize("entry", [None, "x", 5, ["a", "b"], ("a",)])
def test_with_block_values_ignores_a_block_values_entry_that_is_not_a_mapping(entry: Any) -> None:
    assert with_block_values("Oder", "4", _BLOCK, {"4": entry}) == "Oder"


@pytest.mark.parametrize("reference", [None, "x", 5, [5], ["type", 5]])
def test_with_block_values_ignores_an_element_reference_that_is_not_a_mapping(reference: Any) -> None:
    assert with_block_values("Oder", "4", {"reference": reference}, {"4": {"a": "1"}}) == "Oder"
