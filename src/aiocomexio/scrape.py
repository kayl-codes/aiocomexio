"""Scraping of Comexio's inline `var $Name = {...}` JS object literals out of admin pages.

The admin interface embeds its configuration as JS object literals in `<script>` blocks.
These helpers turn a fetched page into plain dicts; they never touch the network.
"""

import html as html_lib
import json
import logging
import re
from dataclasses import dataclass
from datetime import timedelta
from html.parser import HTMLParser
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
# Web-IO add page: plain assignments (no `var`, no `$`) of the class and device lists.
_WEBIO_DEVICE_BASE_LIST_RE = re.compile(r"\bDeviceBaseList\s*=\s*")
_WEBIO_DEVICE_LIST_RE = re.compile(r"\bDeviceList\s*=\s*")
_VAR_DECL_RE = re.compile(r"var\s+\$(\w+)\s*=\s*", re.DOTALL)
_LITERAL_START_RE = re.compile(r"\s*([{\[])")
_LITERAL_CLOSERS = {"{": "}", "[": "]"}
_TRAILING_COMMA_RE = re.compile(r",\s*[}\]]")
# System dashboard page: the general system info is a table of plain two-cell rows, label and value,
# e.g. <tr><td >Freier Speicherplatz:</td><td >43%</td></tr>. The label is in the UI language;
# $I18N.module.free_flash on the same page carries it, so the row is found in every language.
_TWO_CELL_ROW_RE = re.compile(r"<tr[^>]*>\s*<td[^>]*>([^<]*)</td>\s*<td[^>]*>([^<]*)</td>\s*</tr>", re.IGNORECASE)
_PERCENT_RE = re.compile(r"(\d{1,3}(?:[.,]\d{1,6})?)\s*%", re.ASCII)
_FREE_STORAGE_I18N_KEY = "free_flash"
# admin_dashboard/run_duration: "12 Tage 21:16:16" — the day count with a word in the UI language
# (left out below one day is assumed, not observed), then hours:minutes:seconds.
_MAX_HOUR_WITH_DAYS = 23
_RUN_DURATION_RE = re.compile(r"(?:(\d{1,5})\s+[^\s\d:]+\s+)?(\d{1,3}):([0-5]\d):([0-5]\d)", re.ASCII)


# System dashboard page: the time settings form, by element id.
_USE_TIME_SERVER_ID = "use_timeserver"
_TIME_SERVER_ID = "timeserver"
_TIME_SERVER_RATE_ID = "timeserver_update_rate"
_INTERVAL_RE = re.compile(r"\d{1,7}", re.ASCII)
_SELECT_CONTENT_TAGS = frozenset({"option", "optgroup"})


@dataclass(frozen=True, slots=True)
class TimeServerSettings:
    """The time server settings of the system dashboard's date & time form.

    enabled is the "use time server" checkbox, server the configured host with surrounding
    whitespace removed ("" if the field is empty or blank), update_interval the query interval in
    seconds (None if no option is selected).
    """

    enabled: bool
    server: str
    update_interval: int | None


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
    return _extract_balanced(script_text, start_index, "{")


def _extract_balanced(script_text: str, start_index: int, opener: str) -> tuple[str | None, int]:
    """extract_js_object_literal for an object ('{') or array ('[') literal."""
    closer = _LITERAL_CLOSERS[opener]
    if start_index >= len(script_text) or script_text[start_index] != opener:
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
        elif ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return script_text[start_index : i + 1], i + 1
    return None, start_index


def _literal_at(text: str, pos: int) -> str | None:
    """The object or array literal assigned at pos, or None if the value is neither.

    Only whitespace may sit between `=` and the literal — searching on for the next "{" would
    hand a `var $A = 5;` the NEXT variable's object.
    """
    match = _LITERAL_START_RE.match(text, pos)
    return None if match is None else _extract_balanced(text, match.start(1), match.group(1))[0]


def _as_id_mapping(decoded: dict[str, Any] | list[Any], var_name: str) -> dict[str, Any] | None:
    """A decoded object or array literal as a dict keyed by id, or None if it is no id group.

    PHP's json_encode renders an array with the keys 0..n-1 as a JSON array (`[]` when empty,
    e.g. $Fubs without any plan) — the index then IS the id, as in config.iter_group. An array
    counts as such an id group only if every member is an object whose "Id", where it has one,
    equals its position: a plain list would otherwise pass off positions as ids, and e.g. a
    list-valued legacy $ioTypes would hide the real $IOTypesBinary table.
    """
    if isinstance(decoded, dict):
        return decoded
    for index, member in enumerate(decoded):
        if not isinstance(member, dict) or str(member.get("Id", index)) != str(index):
            _LOGGER.debug("Skipping %s: an array, but no id group (member %d)", var_name, index)
            return None
    return {str(index): member for index, member in enumerate(decoded)}


def scrape_js_vars(html: str, *, page_label: str) -> dict[str, Any]:
    """Extract every top-level `var $Name = {...}` object literal, or id-group array literal, from an HTML page.

    Keys are the variable names without the `$`. An array literal that is an id group (PHP's
    json_encode of one with the ids 0..n-1, `[]` when empty) becomes a dict keyed by index. Any
    other value is skipped; one that isn't valid JSON (after stripping trailing commas) with a
    warning — page_label only names the page in that warning.
    """
    # Restrict search to script tags to avoid scanning entire HTML with a single DOTALL regex
    script_blocks = _SCRIPT_BLOCK_RE.findall(html)

    result: dict[str, Any] = {}
    for script in script_blocks:
        for m in _VAR_DECL_RE.finditer(script):
            var_name = m.group(1)
            raw_obj = _literal_at(script, m.end())
            if raw_obj is None:
                _LOGGER.debug("Skipping $%s on %s page: not an object or array literal", var_name, page_label)
                continue

            try:
                decoded = json.loads(normalize_js_like_object(raw_obj))
            except ValueError as exc:  # JSONDecodeError, or an int beyond Python's int-to-str digit limit
                _LOGGER.warning("Failed to decode JSON for variable $%s on %s page: %s", var_name, page_label, exc)
                continue
            if (mapping := _as_id_mapping(decoded, f"${var_name}")) is not None:
                result[var_name] = mapping
    return result


def parse_comexio_version(html: str) -> str | None:
    """Comexio firmware version (e.g. "11.0.2") from an admin page's static asset paths, or None."""
    if match := COMEXIO_VERSION_RE.search(html):
        return match.group(1)
    return None


def parse_free_storage_percent(html: str) -> float | None:
    """Free storage in percent (0-100) from the system dashboard page, or None if the page does not show it.

    The row is found by its label as $I18N.module.free_flash on the same page spells it, so it does
    not depend on the UI language. None if that label is missing, no row or more than one row
    carries it, or the value is no percentage within 0-100.
    """
    label = _free_storage_label(html)
    if label is None:
        return None
    values = [
        value
        for cell_label, value in _TWO_CELL_ROW_RE.findall(html)
        if _bare_label(html_lib.unescape(cell_label)) == label
    ]
    if len(values) != 1:
        _LOGGER.debug("System dashboard: %d rows labelled %r, expected exactly one", len(values), label[:40])
        return None
    match = _PERCENT_RE.fullmatch(html_lib.unescape(values[0]).strip())
    if match is None:
        _LOGGER.debug("System dashboard: free storage value %r is no percentage", values[0][:40])
        return None
    percent = float(match.group(1).replace(",", "."))
    if percent > 100:
        _LOGGER.debug("System dashboard: free storage %r is out of range", percent)
        return None
    return percent


def parse_run_duration(text: str) -> timedelta | None:
    """Uptime from admin_dashboard/run_duration's "duration" text (e.g. "12 Tage 21:16:16"), or None if unparsable.

    The day word is in the UI language and therefore not checked, only its count. With a day count
    the hours stay below 24; a bare "HH:MM:SS" may carry more.
    """
    match = _RUN_DURATION_RE.fullmatch(text.strip())
    if match is None:
        return None
    days, hours, minutes, seconds = match.groups()
    if days is not None and int(hours) > _MAX_HOUR_WITH_DAYS:
        return None
    return timedelta(days=int(days or 0), hours=int(hours), minutes=int(minutes), seconds=int(seconds))


def parse_time_server(html: str) -> TimeServerSettings | None:
    """Time server settings from the system dashboard page, or None if the page does not show them.

    None unless the page carries exactly one "use time server" checkbox and one time server field;
    an interval that is no positive whole number of seconds, or no single selected option, is None.
    """
    parser = _TimeServerFormParser()
    parser.feed(html)
    parser.close()
    if len(parser.enabled) != 1 or len(parser.server) != 1:
        _LOGGER.debug(
            "System dashboard: %d time server checkboxes and %d fields, expected one each",
            len(parser.enabled),
            len(parser.server),
        )
        return None
    interval = None
    if len(parser.interval) == 1 and _INTERVAL_RE.fullmatch(parser.interval[0]) and int(parser.interval[0]) > 0:
        interval = int(parser.interval[0])
    elif parser.interval:
        _LOGGER.debug("System dashboard: time server interval is unclear: %r", [v[:20] for v in parser.interval[:3]])
    return TimeServerSettings(enabled=parser.enabled[0], server=parser.server[0].strip(), update_interval=interval)


class _TimeServerFormParser(HTMLParser):
    """Collects the time server checkbox state, field value and selected interval option(s)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.enabled: list[bool] = []
        self.server: list[str] = []
        self.interval: list[str] = []
        self._in_rate_select = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        element_id = attributes.get("id")
        if tag == "input" and element_id == _USE_TIME_SERVER_ID:
            self.enabled.append("checked" in attributes)
        elif tag == "input" and element_id == _TIME_SERVER_ID:
            self.server.append(attributes.get("value") or "")
        elif tag == "select":
            self._in_rate_select = element_id == _TIME_SERVER_RATE_ID
        elif tag == "option" and self._in_rate_select and "selected" in attributes:
            self.interval.append(attributes.get("value") or "")
        elif tag not in _SELECT_CONTENT_TAGS:
            # Only options belong in a select: anything else means it was left unclosed.
            self._in_rate_select = False

    def handle_endtag(self, tag: str) -> None:
        if tag == "select":
            self._in_rate_select = False


def _free_storage_label(html: str) -> str | None:
    """The free storage label from $I18N.module.free_flash, or None if the page carries none."""
    i18n = scrape_js_vars(html, page_label="system dashboard").get("I18N")
    module = i18n.get("module") if isinstance(i18n, dict) else None
    label = module.get(_FREE_STORAGE_I18N_KEY) if isinstance(module, dict) else None
    if not isinstance(label, str) or not _bare_label(label):
        _LOGGER.debug("System dashboard: no $I18N.module.%s label", _FREE_STORAGE_I18N_KEY)
        return None
    return _bare_label(label)


def _bare_label(label: str) -> str:
    """A row label without surrounding whitespace and its trailing colon, as cell and $I18N may spell it."""
    return label.strip().removesuffix(":").rstrip()


def _decode_js_var(html: str, decl_re: re.Pattern[str], var_name: str) -> dict[str, Any] | None:
    """Decode the object or array literal assigned by the first match of decl_re, or None if absent/invalid."""
    assign_match = decl_re.search(html)
    if assign_match is None:
        return None
    raw_object = _literal_at(html, assign_match.end())
    if raw_object is None:
        _LOGGER.debug("Skipping %s: not an object or array literal", var_name)
        return None
    try:
        decoded = json.loads(normalize_js_like_object(raw_object))
    except ValueError as exc:  # JSONDecodeError, or an int beyond Python's int-to-str digit limit
        _LOGGER.warning("Failed to decode %s: %s", var_name, exc)
        return None
    return _as_id_mapping(decoded, var_name)


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


def parse_webio_add_page(html: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """(DeviceBaseList, DeviceList) from the Web-IO add page, or None unless the page carries both.

    The add page embeds both as JSON (`DeviceBaseList={id: {Id, Identifier, ...}}` for the
    classes, `DeviceList={id: {Id, Name, ...}}` for the devices). Unlike the rendered <option>
    and tab labels they need no HTML unescaping, and their presence proves the page really is
    the add page: None lets a caller tell "not that page" from "no such class/device".
    """
    lists = []
    for decl_re, var_name in ((_WEBIO_DEVICE_BASE_LIST_RE, "DeviceBaseList"), (_WEBIO_DEVICE_LIST_RE, "DeviceList")):
        # A second assignment (an empty initializer, a reset) leaves open which one holds the data.
        if len(decl_re.findall(html)) != 1:
            _LOGGER.debug("Web-IO add page: %s is not assigned exactly once", var_name)
            return None
        if (decoded := _decode_js_var(html, decl_re, var_name)) is None:
            return None
        lists.append(decoded)
    return lists[0], lists[1]
