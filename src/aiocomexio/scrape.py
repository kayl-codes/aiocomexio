"""Scraping of Comexio's inline `var $Name = {...}` JS object literals out of admin pages.

The admin interface embeds its configuration as JS object literals in `<script>` blocks.
These helpers turn a fetched page into plain dicts; they never touch the network.
"""

import json
import logging
import re
from typing import Any

_LOGGER = logging.getLogger(__name__)

# $ioTypes = legacy (pre-v11); $IOTypesBinary = v11+ replacement with identical structure.
_IO_TYPES_DECL_RE = re.compile(r"var\s+\$ioTypes\s*=\s*")
_IO_BINARY_TYPES_DECL_RE = re.compile(r"var\s+\$IOTypesBinary\s*=\s*")
_IO_INPUT_TYPES_DECL_RE = re.compile(r"var\s+\$IOInputTypes\s*=\s*")
_SCRIPT_BLOCK_RE = re.compile(r"<script[^>]*>(.*?)</script[^>]{0,32}>", re.DOTALL | re.IGNORECASE)
# Comexio's own firmware/frontend version (e.g. "11.0.2"), from static asset paths
# (cache-busting), e.g. src="/11.0.2/js/cmb_admin.js" — cmb_admin.js is the generic
# admin-wide script, cmb_function_function_module.js is specific to the function module page;
# matching either is redundancy against a future filename change.
COMEXIO_VERSION_RE = re.compile(
    r'src="/(\d+\.\d+\.\d+)/(?:js/cmb_admin\.js|'
    r'module/admin/function_function_module/js/cmb_function_function_module\.js)"'
)
_VAR_DECL_RE = re.compile(r"var\s+\$(\w+)\s*=\s*", re.DOTALL)
_EMPTY_JS_ARRAY_RE = re.compile(r"\[\s*\]")
_OBJECT_START_RE = re.compile(r"\s*\{")
_TRAILING_COMMA_RE = re.compile(r",\s*[}\]]")


def normalize_js_like_object(obj_str: str) -> str:
    """Remove trailing commas before closing braces/brackets to make JS objects JSON-compatible.

    Commas inside quoted strings are kept — a label like "A,}" must survive unchanged.
    """
    out: list[str] = []
    in_string: str | None = None
    escape = False
    for i, ch in enumerate(obj_str):
        if in_string:
            in_string, escape = _string_state(ch, in_string, escape)
        elif ch in ("'", '"'):
            in_string = ch
        elif ch == "," and _TRAILING_COMMA_RE.match(obj_str, i):
            continue
        out.append(ch)
    return "".join(out)


def _string_state(ch: str, in_string: str, escape: bool) -> tuple[str | None, bool]:
    """Next (in_string, escape) state for one character inside a quoted JS string."""
    if escape:
        return in_string, False
    if ch == "\\":
        return in_string, True
    return (None if ch == in_string else in_string), False


def extract_js_object_literal(script_text: str, start_index: int) -> tuple[str | None, int]:
    """Extract a JS object literal starting at start_index (pointing at '{').

    Returns (literal, end index), or (None, start_index) if there is no balanced literal there.
    Braces inside single- or double-quoted strings are ignored.
    """
    if start_index >= len(script_text) or script_text[start_index] != "{":
        return None, start_index

    depth = 0
    in_string: str | None = None
    escape = False

    for i in range(start_index, len(script_text)):
        ch = script_text[i]
        if in_string:
            in_string, escape = _string_state(ch, in_string, escape)
        elif ch in ("'", '"'):
            in_string = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return script_text[start_index : i + 1], i + 1
    return None, start_index


def _object_start(text: str, pos: int) -> int | None:
    """Index of the '{' opening the value assigned at pos, or None if that value isn't an object literal.

    Only whitespace may sit between `=` and `{` — searching on for the next "{" would hand a
    `var $A = 5;` (or `[]`, PHP's json_encode of an empty array) the NEXT variable's object.
    """
    match = _OBJECT_START_RE.match(text, pos)
    return match.end() - 1 if match else None


def scrape_js_vars(html: str, *, page_label: str) -> dict[str, Any]:
    """Extract every top-level `var $Name = {...}` JS object literal from an HTML page.

    Keys are the variable names without the `$`. An empty array `[]` (PHP's json_encode of an
    empty id group) becomes {}. A value that isn't an object literal is skipped; one that isn't
    valid JSON (after stripping trailing commas) is skipped with a warning — page_label only
    names the page in that warning.
    """
    # Restrict search to script tags to avoid scanning entire HTML with a single DOTALL regex
    script_blocks = _SCRIPT_BLOCK_RE.findall(html)

    result: dict[str, Any] = {}
    for script in script_blocks:
        for m in _VAR_DECL_RE.finditer(script):
            var_name = m.group(1)
            if _EMPTY_JS_ARRAY_RE.match(script, m.end()):
                # PHP json_encode renders an empty array as `[]` (e.g. $Fubs without any plan).
                result[var_name] = {}
                continue
            brace_index = _object_start(script, m.end())
            raw_obj = None if brace_index is None else extract_js_object_literal(script, brace_index)[0]
            if raw_obj is None:
                _LOGGER.debug("Skipping $%s on %s page: not an object literal", var_name, page_label)
                continue

            try:
                result[var_name] = json.loads(normalize_js_like_object(raw_obj))
            except json.JSONDecodeError as exc:
                _LOGGER.warning("Failed to decode JSON for variable $%s on %s page: %s", var_name, page_label, exc)
    return result


def parse_comexio_version(html: str) -> str | None:
    """Comexio firmware version (e.g. "11.0.2") from an admin page's static asset paths, or None."""
    if match := COMEXIO_VERSION_RE.search(html):
        return match.group(1)
    return None


def _decode_js_var(html: str, decl_re: re.Pattern[str], var_name: str) -> dict[str, Any] | None:
    """Decode the object literal assigned by the first match of decl_re, or None if absent/invalid."""
    assign_match = decl_re.search(html)
    if assign_match is None:
        return None
    if _EMPTY_JS_ARRAY_RE.match(html, assign_match.end()):
        return {}
    brace_index = _object_start(html, assign_match.end())
    raw_object = None if brace_index is None else extract_js_object_literal(html, brace_index)[0]
    if raw_object is None:
        _LOGGER.debug("Skipping %s: not an object literal", var_name)
        return None
    try:
        decoded = json.loads(normalize_js_like_object(raw_object))
    except json.JSONDecodeError as exc:
        _LOGGER.warning("Failed to decode %s: %s", var_name, exc)
        return None
    return decoded if isinstance(decoded, dict) else None


def parse_io_types(html: str) -> dict[str, Any]:
    """IO type table (TypeId -> {binary, min, max, unit}) from the main admin page.

    Tries the legacy $ioTypes first, then the v11+ $IOTypesBinary (identical structure); an
    empty table falls through to the next. Returns {} if neither yields entries —
    config.parse_config then falls back to classifying IOs by their identifier.
    """
    for decl_re, var_name in (
        (_IO_TYPES_DECL_RE, "$ioTypes"),
        (_IO_BINARY_TYPES_DECL_RE, "$IOTypesBinary"),
    ):
        io_types = _decode_js_var(html, decl_re, var_name)
        if io_types:
            _LOGGER.debug("Loaded %d IO types from %s", len(io_types), var_name)
            return io_types
    _LOGGER.warning("No IO type data found ($ioTypes / $IOTypesBinary) — using identifier fallback")
    return {}


def parse_io_input_types(html: str) -> dict[str, Any]:
    """IO input table (TypeId -> {input: bool}) from $IOInputTypes on the main admin page, or {}."""
    io_input_types = _decode_js_var(html, _IO_INPUT_TYPES_DECL_RE, "$IOInputTypes") or {}
    _LOGGER.debug("Loaded %d IO input types", len(io_input_types))
    return io_input_types
