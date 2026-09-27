"""Pure Function Plan logic: SVG preview, signal-flow diagram and the read-only health check.

No I/O and no Comexio API calls: every function works on already-loaded plan data (elements,
connections, element-type catalog and the marker/Web-IO/IO lookups), so a live plan and a stored
backup snapshot render identically.
"""

from .analysis import analyze_function_plan
from .render import render_plan_svg
from .render_flow import render_flow_svg
from .render_labels import resolve_element_label
from .render_selfreset import detect_self_reset_cycles
from .render_values import element_search_id

__all__ = [
    "analyze_function_plan",
    "detect_self_reset_cycles",
    "element_search_id",
    "render_flow_svg",
    "render_plan_svg",
    "resolve_element_label",
]
