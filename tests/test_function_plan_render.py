"""Function plan SVG preview rendering and the read-only plan health check."""

import os
import subprocess
import sys
from typing import Any

import pytest
from syrupy.assertion import SnapshotAssertion
from syrupy.extensions.single_file import SingleFileSnapshotExtension, WriteMode

from aiocomexio.function_plan import (
    analyze_function_plan,
    detect_self_reset_cycles,
    element_search_id,
    render_flow_svg,
    render_plan_svg,
)
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


def test_render_flow_diagram_is_independent_of_hash_seed() -> None:
    # Regression: box positions and edge order followed set iteration order, so the self-reset
    # cycle's members swapped places between interpreter runs (seeds 1 and 2 differed).
    outputs = set()
    for seed in ("1", "2", "3"):
        result = subprocess.run(
            [sys.executable, "-c", _FLOW_SCRIPT, str(FIXTURES_DIR / "function_plan.json")],
            env={**os.environ, "PYTHONHASHSEED": seed},
            capture_output=True,
            check=True,
        )
        outputs.add(result.stdout)

    assert len(outputs) == 1


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
