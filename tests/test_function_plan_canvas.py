"""Paper format, orientation and canvas size of a function plan from $Fubs / $Paper."""

from typing import Any

import pytest

from aiocomexio.function_plan import plan_active, plan_canvas_bounds, plan_dpi, plan_orientation, plan_paper_format
from tests.common import load_json_fixture


@pytest.fixture
def conf() -> dict[str, Any]:
    conf: dict[str, Any] = load_json_fixture("config_basic.json")
    return conf


def test_plan_metadata(conf: dict[str, Any]) -> None:
    fubs, papers = conf["Fubs"], conf["Paper"]

    assert plan_paper_format(fubs, papers, 1) == "A4"
    assert plan_paper_format(fubs, papers, 99) == "A4"
    assert plan_dpi(fubs, 2) == 120
    assert plan_dpi(fubs, 99) == 90
    assert plan_active(fubs, 1) is True
    assert plan_active(fubs, 2) is False
    assert plan_active(fubs, 99) is None
    assert plan_orientation(fubs, 1) == "landscape"
    assert plan_orientation(fubs, 2) == "portrait"


def test_plan_canvas_bounds(conf: dict[str, Any]) -> None:
    fubs, papers = conf["Fubs"], conf["Paper"]

    assert plan_canvas_bounds(fubs, papers, 1) == pytest.approx((870.0, 720.0))
    # A3 portrait at 120 DPI: long side -> Y, scaled by paper size and resolution.
    assert plan_canvas_bounds(fubs, papers, 2) == pytest.approx(
        (870.0 * 297 / 297 * 120 / 90, 720.0 * 420 / 210 * 120 / 90)
    )


def test_plan_canvas_bounds_overrides_for_a_plan_not_yet_in_fubs() -> None:
    assert plan_canvas_bounds({}, {}, 5, paper_name="A3", orientation="Portrait") == pytest.approx(
        (870.0 * 297 / 297, 720.0 * 420 / 210)
    )
    # An unknown paper name falls back to the plan's own paper (here: the A4 default).
    assert plan_canvas_bounds({}, {}, 5, paper_name="Letter", orientation="landscape") == pytest.approx((870.0, 720.0))


def test_malformed_plan_and_paper_entries_fall_back_to_defaults() -> None:
    fubs = {"1": {"Resolution": None, "Orientation": "x", "Paper": 9, "Active": "x"}, "2": "garbage"}
    papers = {"9": "garbage"}

    assert plan_dpi(fubs, 1) == 90
    assert plan_orientation(fubs, 1) == "landscape"
    assert plan_paper_format(fubs, papers, 1) == "A4"
    assert plan_active(fubs, 1) is False
    assert plan_active(fubs, 2) is False
    assert plan_canvas_bounds(fubs, papers, 1) == pytest.approx((870.0, 720.0))


def test_malformed_paper_fields_fall_back_to_defaults() -> None:
    fubs = {"1": {"Paper": 9}}
    papers = {"9": {"Name": None, "MMX": None, "MMY": "x"}, "10": {"MMX": "nan", "MMY": "inf"}}

    assert plan_paper_format(fubs, papers, 1) == "A4"
    assert plan_canvas_bounds(fubs, papers, 1) == pytest.approx((870.0, 720.0))
    assert plan_canvas_bounds({"1": {"Paper": 10}}, papers, 1) == pytest.approx((870.0, 720.0))
