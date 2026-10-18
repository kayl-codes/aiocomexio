"""Builders for the Web-IO commands and class templates a consumer uploads to the Comexio server.

Each marker, IO and KNX object gets one Web-IO command that pushes its value to a webhook on
change; each K-element wired to a bridge marker additionally gets an API-loopback command
(see build_knx_loopback_webio_command). Pure logic: nothing here touches the network.
"""

import json
import logging
from collections import Counter
from collections.abc import Collection, Mapping
from typing import Any

from .config import TYPE_ANALOG, log_excerpt
from .const import (
    WEBIO_CLASS_NAME_KNX_LOOPBACK,
    WEBIO_INT16_DANGER_ZONE,
    WEBIO_MARKER_ANALOG_MAX,
    WEBIO_MARKER_ANALOG_MIN,
    WebioClass,
    knx_loopback_command_name,
)
from .knx import KNX_DPT_ANALOG_RANGES, resolve_knx_dpt

__all__ = [
    "CONTENT_TYPE_JSON",
    "WEBIO_PROTOCOL_HTTP",
    "WEBIO_PROTOCOL_HTTPS",
    "build_io_webio_command",
    "build_knx_loopback_webio_command",
    "build_marker_webio_command",
    "build_quickbus_webio_command",
    "build_webio_commands",
    "generate_webio_json",
    "knx_loopback_class_json",
    "knx_loopback_range",
    "knx_webio_range",
    "lua_escape",
    "quickbus_command_name",
    "safe_webio_range",
]

_LOGGER = logging.getLogger(__name__)

# HeaderModifier of every JSON-POST Web-IO command (the default of ComexioClient.save_webio_command).
CONTENT_TYPE_JSON = "Content-Type: application/json"

# Values of a Web-IO command's "Protocol" field (the form's protocol <select>): HTTP and HTTPS, both
# HTTP/1.1. Comexio also offers TCP (2), HTTP/2 (4) and HTTPS/2 (5); the values are not consecutive.
WEBIO_PROTOCOL_HTTP = 0
WEBIO_PROTOCOL_HTTPS = 1


def lua_escape(value: Any) -> str:
    """Escape a value for embedding in a double-quoted Lua string literal."""
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def _webio_data_lua(payload: str) -> str:
    """Build the Lua `data(a)` webhook body for a Web-IO command, given its JSON payload fields."""
    return f"function data(a)\r\n  local d = {{ {payload} }}\r\n  return json_stringify(d)\r\nend"


def _webio_command(
    *, name: str, type_id: int, min_v: float, max_v: float, data: str, webhook_path: str, protocol: int
) -> dict[str, Any]:
    """Build a webhook Web-IO command dict, filling in the fields shared by markers and IOs."""
    return {
        "Name": name,
        "TypeId": type_id,
        "Min": min_v,
        "Max": max_v,
        "Parameter": webhook_path,
        "HeaderModifier": CONTENT_TYPE_JSON,
        "Data": data,
        "Protocol": protocol,
        "PostGet": 1,
        "WebDeviceId": 0,
        "Authentication": 0,
        "Input": 1,
        "ReqFreq": "",
        "ReplyInterpreter": "",
        "Port": "",
        "SendOnOne": 0,
        "Changed": 1,
        "BaseId": 0,
        "DefaultValue": "",
        "DefaultActive": 1,
        "io": [],
    }


def safe_webio_range(v_min: float, v_max: float) -> tuple[float, float]:
    """Widen a Web-IO Min/Max pair whose bounds land in the int16 clamp-bug danger zone.

    See WEBIO_INT16_DANGER_ZONE / WEBIO_MARKER_ANALOG_MIN for the underlying Comexio bug.
    """
    lo, hi = WEBIO_INT16_DANGER_ZONE
    if lo <= abs(v_min) <= hi or lo <= abs(v_max) <= hi:
        return WEBIO_MARKER_ANALOG_MIN, WEBIO_MARKER_ANALOG_MAX
    return v_min, v_max


def knx_webio_range(dpt_min: float | None, dpt_max: float | None) -> tuple[float, float]:
    """Min/Max to embed in an analog KNX Web-IO command, from a resolved DPT range.

    The real DPT range is used verbatim — only the safe_webio_range int16 guard applies — and
    deliberately not capped to WEBIO_MARKER_ANALOG_MIN/MAX: Comexio still has an open firmware
    bug that rounds analog values above ~1,000,000, but capping here would hide it silently
    and need undoing once Comexio ships a fix. An unresolved DPT (None) falls back to the
    generic WEBIO_MARKER_ANALOG_MIN/MAX range.
    """
    if dpt_min is None or dpt_max is None:
        return WEBIO_MARKER_ANALOG_MIN, WEBIO_MARKER_ANALOG_MAX
    return safe_webio_range(dpt_min, dpt_max)


def knx_loopback_range(
    k_id: int, marker_id: int, *, knx_dpt_catalog: Mapping[str, Any] | None, catalog_stale: bool
) -> tuple[float, float]:
    """Resolve the Min/Max for one analog K-element's API-loopback Web-IO command.

    Unlike build_marker_webio_command's KNX branch (dpt_min/dpt_max already resolved by
    parse_config), the loopback command is built on demand, so it resolves the DPT itself
    against whatever catalog the caller holds. A missing catalog (None), a stale one
    (catalog_stale: the firmware version moved on since it was fetched) and an unresolvable
    DPT all degrade to the generic WEBIO_MARKER_ANALOG_MIN/MAX range; each case is logged
    separately, since the resulting command is permanent and nothing re-checks it later.
    """
    dpt = None
    if knx_dpt_catalog is not None and not catalog_stale:
        dpt = resolve_knx_dpt(knx_dpt_catalog, str(k_id))
    if dpt is None:
        if knx_dpt_catalog is None:
            reason = "catalog not yet fetched"
        elif catalog_stale:
            reason = "catalog stale (comexio_version changed since last fetch)"
        else:
            reason = "DPT chain resolution failed"
        _LOGGER.debug(
            "KNX loopback command K%s->M%s: could not resolve DPT (%s), using generic fallback range",
            k_id,
            marker_id,
            reason,
        )
        return WEBIO_MARKER_ANALOG_MIN, WEBIO_MARKER_ANALOG_MAX

    dpt_range = KNX_DPT_ANALOG_RANGES.get(dpt)
    if dpt_range is None:
        _LOGGER.debug(
            "KNX loopback command K%s->M%s: resolved DPT%s.%s has no entry in "
            "KNX_DPT_ANALOG_RANGES, using generic fallback range",
            k_id,
            marker_id,
            dpt[0],
            dpt[1],
        )
        return WEBIO_MARKER_ANALOG_MIN, WEBIO_MARKER_ANALOG_MAX

    return knx_webio_range(dpt_range[0], dpt_range[1])


def build_marker_webio_command(
    m: Mapping[str, Any],
    webhook_path: str,
    source_type: WebioClass = WebioClass.MARKER,
    *,
    protocol: int = WEBIO_PROTOCOL_HTTP,
) -> dict[str, Any]:
    """Build the Web-IO command dict for a single marker or KNX object.

    source_type is the literal written into the webhook Lua payload's ``type=`` field
    ("marker" or "knx"); it tells the receiver which kind of item the pushed value belongs to.
    A KNX object may carry dpt_min/dpt_max (set by parse_config) — see knx_webio_range for how
    those replace the generic ±500,000 range with the real DPT range. protocol is how the command
    reaches the webhook (WEBIO_PROTOCOL_HTTP or WEBIO_PROTOCOL_HTTPS).
    """
    is_ana = m["type"] == TYPE_ANALOG
    min_v: float
    max_v: float
    if is_ana and source_type == WebioClass.KNX:
        min_v, max_v = knx_webio_range(m.get("dpt_min"), m.get("dpt_max"))
    elif is_ana:
        min_v, max_v = WEBIO_MARKER_ANALOG_MIN, WEBIO_MARKER_ANALOG_MAX
    else:
        min_v, max_v = 0, 1
    safe_id = lua_escape(m["id"])
    safe_type = lua_escape(source_type)
    lua = _webio_data_lua(f'id="{safe_id}", value=a, type="{safe_type}"')
    return _webio_command(
        name=f"HA {m['name']}",
        type_id=2 if is_ana else 1,
        min_v=min_v,
        max_v=max_v,
        data=lua,
        webhook_path=webhook_path,
        protocol=protocol,
    )


def _io_command_range(io_item: Mapping[str, Any]) -> tuple[bool, float, float]:
    """(is_analog, Min, Max) of an IO's Web-IO command.

    Uses the authentic min/max from the Comexio IO type definition, unless it lands in the
    int16 danger zone (see safe_webio_range). io_item["min"/"max"] can be present but None
    (scraped type value missing), so None is coerced explicitly rather than via .get()'s default.
    """
    is_ana = not io_item.get("is_binary", False)
    raw_min = io_item.get("min")
    raw_max = io_item.get("max")
    v_min = 0 if raw_min is None else raw_min
    default_max = 100 if is_ana else 1
    v_max = default_max if raw_max is None else raw_max
    v_min, v_max = safe_webio_range(v_min, v_max)
    return is_ana, v_min, v_max


def build_io_webio_command(
    io_item: Mapping[str, Any], webhook_path: str, *, protocol: int = WEBIO_PROTOCOL_HTTP
) -> dict[str, Any]:
    """Build the Web-IO command dict for a single physical IO (min/max: see _io_command_range).

    protocol: see build_marker_webio_command.

    An IO of a Quickbus device gets its own command (build_quickbus_webio_command): ext +
    identifier would not tell it apart from the same identifier on another device.
    """
    if io_item.get("quickbus"):
        return build_quickbus_webio_command(io_item, webhook_path, protocol=protocol)
    is_ana, v_min, v_max = _io_command_range(io_item)
    safe_ext = lua_escape(io_item["ext_name"])
    safe_io_id = lua_escape(io_item["identifier"])
    lua = _webio_data_lua(f'ext="{safe_ext}", io="{safe_io_id}", value=a, type="io"')
    return _webio_command(
        name=f"HA IO {io_item['ext_name']} {io_item['identifier']}",
        type_id=2 if is_ana else 1,
        min_v=v_min,
        max_v=v_max,
        data=lua,
        webhook_path=webhook_path,
        protocol=protocol,
    )


def quickbus_command_name(io_item: Mapping[str, Any]) -> str:
    """Web-IO command name of a Quickbus IO, e.g. "HA QB1 MT1 G18".

    The device id keeps names unique even when two Quickbus devices share a name; the device
    name keeps them readable in Comexio Studio.
    """
    qb = io_item["quickbus"]
    return f"HA QB{qb['device_id']} {qb['name']} {io_item['identifier']}"


def build_quickbus_webio_command(
    io_item: Mapping[str, Any], webhook_path: str, *, protocol: int = WEBIO_PROTOCOL_HTTP
) -> dict[str, Any]:
    """Build the Web-IO command dict for a single IO of a Quickbus device.

    Its identifier (G1, TL1, ...) repeats on every device of the same kind, so the payload
    carries the IO's unique "Id" instead of ext + identifier (type "quickbus"). Min/max: see
    _io_command_range; protocol: see build_marker_webio_command.
    """
    is_ana, v_min, v_max = _io_command_range(io_item)
    safe_id = lua_escape(io_item["id"])
    lua = _webio_data_lua(f'id="{safe_id}", value=a, type="{WebioClass.QUICKBUS}"')
    return _webio_command(
        name=quickbus_command_name(io_item),
        type_id=2 if is_ana else 1,
        min_v=v_min,
        max_v=v_max,
        data=lua,
        webhook_path=webhook_path,
        protocol=protocol,
    )


def build_knx_loopback_webio_command(
    *,
    k_id: int,
    marker_id: int,
    is_analog: bool,
    knx_dpt_catalog: Mapping[str, Any] | None,
    catalog_stale: bool,
) -> dict[str, Any]:
    """Build the Web-IO command dict for one K-element's API-loopback command.

    Unlike the webhook commands (POST, JSON body, no auth), this command GETs the Comexio
    server's own /api/?action=set endpoint on every change of the wired K-element's output,
    writing the bridge marker directly. Field combination confirmed live:
      - PostGet=0 (GET, not POST)
      - Authentication=1 — without it the device's Basic-Auth credentials never attach to
        this command's request, even with the class Login=3 and correct device credentials
        (401 Unauthorized).
    Min/Max of an analog command: see knx_loopback_range. knx_dpt_catalog/catalog_stale are
    required (not defaulted) so a caller cannot skip the staleness check by omission; they are
    ignored for a digital command.
    """
    min_v: float
    max_v: float
    if is_analog:
        min_v, max_v = knx_loopback_range(k_id, marker_id, knx_dpt_catalog=knx_dpt_catalog, catalog_stale=catalog_stale)
    else:
        min_v, max_v = 0, 1
    param_lua = f'function parameter(a)\r\n  return "/api/?action=set&marker=M{marker_id}&value="..a\r\nend'
    return {
        "Name": knx_loopback_command_name(k_id, marker_id),
        "TypeId": 2 if is_analog else 1,
        "Min": min_v,
        "Max": max_v,
        "Parameter": param_lua,
        "HeaderModifier": "",
        "Data": "",
        "Protocol": WEBIO_PROTOCOL_HTTP,  # Comexio's own API, reached through the device
        "PostGet": 0,
        "WebDeviceId": 0,
        "Authentication": 1,
        "Input": 1,
        "ReqFreq": "",
        "ReplyInterpreter": "",
        "Port": "",
        "SendOnOne": 0,
        "Changed": 1,
        "BaseId": 0,
        "DefaultValue": "",
        "DefaultActive": 1,
        "io": [],
    }


def build_webio_commands(
    webhook_path: str,
    parsed_data: Mapping[str, Any],
    webio_class: WebioClass | None = None,
    ignored_marker_ids: Collection[int] | None = None,
    ignored_knx_ids: Collection[int] | None = None,
    *,
    protocol: int = WEBIO_PROTOCOL_HTTP,
) -> list[dict[str, Any]]:
    """Build the Web-IO command dicts for a parse_config result.

    webhook_path is the URL path every command POSTs to, over protocol (WEBIO_PROTOCOL_HTTP or
    WEBIO_PROTOCOL_HTTPS). webio_class restricts the result to
    one Web-IO class, for a per-class bulk upload (generate_webio_json); None returns all.
    ignored_marker_ids / ignored_knx_ids exclude items the consumer does not expose — they
    need no command pushing their values.
    """
    commands: list[tuple[WebioClass, dict[str, Any]]] = []
    for m in parsed_data.get("markers", []):
        if not _is_ignored(m["id"], ignored_marker_ids):
            commands.append((WebioClass.MARKER, build_marker_webio_command(m, webhook_path, protocol=protocol)))
    for io in parsed_data.get("io", []):
        if io.get("quickbus"):
            commands.append((WebioClass.QUICKBUS, build_quickbus_webio_command(io, webhook_path, protocol=protocol)))
        else:
            commands.append((WebioClass.IO, build_io_webio_command(io, webhook_path, protocol=protocol)))
    for knx_item in parsed_data.get("knx", []):
        if not _is_ignored(knx_item["id"], ignored_knx_ids):
            commands.append(
                (WebioClass.KNX, build_marker_webio_command(knx_item, webhook_path, WebioClass.KNX, protocol=protocol))
            )
    # Collisions are found across all classes, so a per-class upload leaves out the same ones.
    return [cmd for cls, cmd in _without_colliding_names(commands) if webio_class in (None, cls)]


def _is_ignored(item_id: Any, ignored_ids: Collection[int] | None) -> bool:
    """Whether item_id is one of ignored_ids; an id int() cannot read is never ignored.

    parse_config keeps a marker/KNX id as it comes, so a malformed one must not abort the whole
    build (and with it a per-class upload of an unrelated class). int() also raises ValueError for
    a digit string beyond Python's int-to-str limit, and OverflowError for an infinite float.
    """
    if not ignored_ids:
        return False
    try:
        return int(item_id) in ignored_ids
    except (TypeError, ValueError, OverflowError):
        return False


def _without_colliding_names(
    commands: list[tuple[WebioClass, dict[str, Any]]],
) -> list[tuple[WebioClass, dict[str, Any]]]:
    """The commands whose name no other command shares (ignoring case); the others are logged.

    A consumer keys its Web-IO commands by name across all classes (parse_config's
    "webio_commands"), so two commands of one name would overwrite each other there, and no
    rule says which one is meant. A marker schema without its MarkerId can e.g. render a
    marker name that equals an IO command name.
    """
    counts = Counter(cmd["Name"].casefold() for _, cmd in commands)
    colliding = sorted({cmd["Name"] for _, cmd in commands if counts[cmd["Name"].casefold()] > 1})
    if colliding:
        _LOGGER.warning(
            "%d Web-IO command names are generated more than once, those commands are left out "
            "(rename the item in Comexio or change the naming schema): %s",
            len(colliding),
            log_excerpt(colliding),
        )
    return [(cls, cmd) for cls, cmd in commands if counts[cmd["Name"].casefold()] == 1]


def generate_webio_json(
    webhook_path: str,
    webio_name: str,
    parsed_data: Mapping[str, Any],
    webio_class: WebioClass | None = None,
    ignored_marker_ids: Collection[int] | None = None,
    ignored_knx_ids: Collection[int] | None = None,
    *,
    protocol: int = WEBIO_PROTOCOL_HTTP,
) -> str:
    """Upload-ready JSON string for the Comexio Web-IO class importer.

    webio_name is the class-specific name (see const.webio_class_name). The remaining
    arguments are forwarded to build_webio_commands.
    """
    return json.dumps(
        {
            "data": "web_io",
            "format": 1,
            "base": {"Identifier": webio_name, "UseCookies": 0, "Login": 2, "BaseId": 0},
            "commands": build_webio_commands(
                webhook_path, parsed_data, webio_class, ignored_marker_ids, ignored_knx_ids, protocol=protocol
            ),
        }
    )


def knx_loopback_class_json(commands: list[dict[str, Any]] | None = None) -> str:
    """Upload-ready JSON for the KNX API-loopback Web-IO class.

    Login=3 ("depends on the device") is required so the device's own username/password get
    attached as Basic-Auth to this class' commands — gated additionally by each command's own
    Authentication=1 (see build_knx_loopback_webio_command). commands is the initial command
    set to embed; commands added later go through the single-command save path.
    """
    return json.dumps(
        {
            "data": "web_io",
            "format": 1,
            "base": {"Identifier": WEBIO_CLASS_NAME_KNX_LOOPBACK, "UseCookies": 0, "Login": 3, "BaseId": 0},
            "commands": commands or [],
        }
    )
