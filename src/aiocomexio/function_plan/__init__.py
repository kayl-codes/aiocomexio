"""Pure Function Plan logic: SVG preview, signal-flow diagram, read-only health check and backup diff.

No I/O and no Comexio API calls: every function works on already-loaded plan data (elements,
connections, element-type catalog and the marker/Web-IO/IO lookups), so a live plan and a stored
backup snapshot render identically.
"""

from .analysis import analyze_function_plan
from .backup_diff import (
    build_source_id_translation,
    diff_snapshots,
    plan_hash,
    referenced_label_metadata,
    snapshot_label_maps,
)
from .canvas import plan_active, plan_canvas_bounds, plan_dpi, plan_orientation, plan_paper_format
from .render import render_plan_svg
from .render_flow import render_flow_svg
from .render_labels import resolve_element_label
from .render_selfreset import detect_self_reset_cycles
from .render_values import element_search_id

__all__ = [
    "analyze_function_plan",
    "build_source_id_translation",
    "detect_self_reset_cycles",
    "diff_snapshots",
    "element_search_id",
    "plan_active",
    "plan_canvas_bounds",
    "plan_dpi",
    "plan_hash",
    "plan_orientation",
    "plan_paper_format",
    "referenced_label_metadata",
    "render_flow_svg",
    "render_plan_svg",
    "resolve_element_label",
    "snapshot_label_maps",
]
