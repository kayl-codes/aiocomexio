"""Paper format, resolution, orientation and canvas size of a function plan.

Work on the scraped $Fubs (plan metadata, keyed by fub id) and $Paper (paper formats, keyed by
paper id) of the function module page.
"""

import logging
from collections.abc import Mapping
from typing import Any

_LOGGER = logging.getLogger(__name__)

# Reference canvas bounds: A4 landscape at 90 DPI (empirically measured on a live Comexio).
_CANVAS_REF_X = 870.0
_CANVAS_REF_Y = 720.0
_CANVAS_REF_MM_LONG = 297  # A4 long side (landscape width)
_CANVAS_REF_MM_SHORT = 210  # A4 short side (landscape height)
_CANVAS_REF_RES = 90
# Paper name -> (long side mm, short side mm) for an explicit format override.
_PAPER_MM_BY_NAME: dict[str, tuple[int, int]] = {
    "A2": (594, 420),
    "A3": (420, 297),
    "A4": (297, 210),
    "A5": (210, 148),
}


def _plan(fubs: Mapping[str, Any], fub_id: int) -> Mapping[str, Any]:
    plan = fubs.get(str(fub_id))
    return plan if isinstance(plan, Mapping) else {}


def _paper(papers: Mapping[str, Any], plan: Mapping[str, Any]) -> Mapping[str, Any]:
    paper = papers.get(str(plan.get("Paper", "")))
    return paper if isinstance(paper, Mapping) else {}


def _int(value: Any, default: int) -> int:
    """int(value), or default for a missing/non-numeric field."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def plan_paper_format(fubs: Mapping[str, Any], papers: Mapping[str, Any], fub_id: int) -> str:
    """Paper format name (e.g. 'A4') of a function plan, defaulting to 'A4'."""
    return str(_paper(papers, _plan(fubs, fub_id)).get("Name", "A4"))


def plan_dpi(fubs: Mapping[str, Any], fub_id: int) -> int:
    """Configured resolution (DPI) of a function plan, defaulting to 90."""
    return _int(_plan(fubs, fub_id).get("Resolution"), _CANVAS_REF_RES)


def plan_orientation(fubs: Mapping[str, Any], fub_id: int) -> str:
    """'portrait' or 'landscape' for a function plan, defaulting to 'landscape'."""
    return "portrait" if _int(_plan(fubs, fub_id).get("Orientation"), 0) == 1 else "landscape"


def plan_active(fubs: Mapping[str, Any], fub_id: int) -> bool | None:
    """A function plan's active flag, or None if the plan is unknown."""
    if str(fub_id) not in fubs:
        return None
    return bool(_int(_plan(fubs, fub_id).get("Active"), 0))


def plan_canvas_bounds(
    fubs: Mapping[str, Any],
    papers: Mapping[str, Any],
    fub_id: int,
    paper_name: str | None = None,
    orientation: str | None = None,
) -> tuple[float, float]:
    """Estimated (x_max, y_max) canvas bounds of a function plan in Studio units.

    Scales proportionally from the A4-landscape-90-DPI reference (870x720). DPI is always taken
    from the plan. paper_name overrides the format (A2/A3/A4/A5); None = the plan's own paper.
    orientation overrides as "landscape"/"portrait" — needed for a plan that was just created
    and is not in $Fubs yet; None = the plan's own, where 0 = landscape (long side -> X) and
    1 = portrait (long side -> Y).
    """
    plan = _plan(fubs, fub_id)
    res = _int(plan.get("Resolution"), _CANVAS_REF_RES)
    if orientation is None:
        orient_id = _int(plan.get("Orientation"), 0)
    else:
        orient_id = 1 if orientation.lower() == "portrait" else 0

    if paper_name and paper_name in _PAPER_MM_BY_NAME:
        mm_long, mm_short = _PAPER_MM_BY_NAME[paper_name]
    else:
        if paper_name:
            _LOGGER.debug("Unknown paper format %r for plan %s — using the plan's own paper", paper_name, fub_id)
        paper = _paper(papers, plan)
        mm_long = paper.get("MMX", _CANVAS_REF_MM_LONG)
        mm_short = paper.get("MMY", _CANVAS_REF_MM_SHORT)

    width_mm, height_mm = (mm_long, mm_short) if orient_id == 0 else (mm_short, mm_long)
    x_max = _CANVAS_REF_X * (width_mm / _CANVAS_REF_MM_LONG) * (res / _CANVAS_REF_RES)
    y_max = _CANVAS_REF_Y * (height_mm / _CANVAS_REF_MM_SHORT) * (res / _CANVAS_REF_RES)
    return x_max, y_max
