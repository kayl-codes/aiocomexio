"""Parsing of the scraped Comexio configuration into markers, IOs, KNX objects and Web-IO commands.

Pure logic: the inputs are the dicts the scrape helpers return ($FubModules, $WebDevices, ...
from the function module page, the IO type tables from the main admin page, the KNX DPT
catalog) plus optional live values. Nothing here touches the network.
"""

import logging
import math
import re
import reprlib
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, fields
from typing import Any

from .const import (
    DEFAULT_SCHEMA_IO,
    DEFAULT_SCHEMA_KNX,
    DEFAULT_SCHEMA_MARKER,
    DEFAULT_SERVER_ALIAS,
    DEFAULT_WEBIO_NAME,
    MARKER_KNX_BRIDGE_SUFFIX_RE,
    MARKER_READ_ONLY_SUFFIX,
    MARKER_TRIGGER_SUFFIXES,
    NO_NAME_TITLE,
    WEBIO_CLASS_LABEL,
    WEBIO_CLASSES,
    MarkerKind,
    WebioClass,
    webio_class_name,
)
from .knx import (
    KNX_DPT3_COMPOSITE_DOMAIN,
    KNX_DPT_ANALOG_RANGES,
    KNX_DPT_DEVICE_CLASS,
    KNX_DPT_DIGITAL_AMBIGUOUS,
    KNX_DPT_DIGITAL_DEVICE_CLASS,
    resolve_knx_dpt,
)

__all__ = [
    "TYPE_ANALOG",
    "TYPE_DIGITAL",
    "ParseOptions",
    "SafeDict",
    "clean_value",
    "io_schema_title",
    "is_extension_offline",
    "iter_group",
    "log_excerpt",
    "marker_kind",
    "normalize_io_unit",
    "parse_config",
]

_LOGGER = logging.getLogger(__name__)

# Value of a marker's/KNX object's "type" key.
TYPE_ANALOG = "analog"
TYPE_DIGITAL = "digital"

# parse_config result/item keys used more than once here.
_KEY_MARKERS = "markers"
_KEY_WEBIO_COMMANDS = "webio_commands"
_KEY_WEBIO_DUPLICATES = "webio_command_duplicates"
_KEY_DPT_AMBIGUOUS = "dpt_ambiguous"
_KEY_QUICKBUS_DEVICES = "quickbus_devices"

# $FubModules keys of the three source categories and of the Web-IO command groups.
_MODULE_IO = "1"
_MODULE_MARKER = "2"
_MODULE_WEBIO = "10"
_MODULE_KNX = "11"

# Key of an extension's Quickbus device table in $FubModules["1"], and of its IO-Id -> device-Id
# map under the extension's "mapping" (Comexio's internal name for the Quickbus is "lowcan").
_QUICKBUS_KEY = "lowcan"

# Entries named in one aggregated warning before the rest is only counted.
_MAX_LOGGED_ENTRIES = 5

# Longest digit string _strict_id accepts as an id. A Comexio id is a database key, far below
# this; the cap is a sanity limit (always within a signed 64-bit integer, leading zeros counted),
# not the full int64 range: anything longer is treated as malformed.
_MAX_ID_DIGITS = 18

# Longest repr of a raw value quoted in a log line before it is cut off.
_MAX_LOGGED_REPR = 80

# Longest entry of an aggregated warning (log_excerpt) before it is cut off.
_MAX_LOGGED_ENTRY = 2 * _MAX_LOGGED_REPR

# Cuts strings and containers while rendering them, so a huge raw value is never rendered in full.
_LOG_REPR = reprlib.Repr(
    maxlevel=2,
    maxtuple=_MAX_LOGGED_ENTRIES,
    maxlist=_MAX_LOGGED_ENTRIES,
    maxdict=_MAX_LOGGED_ENTRIES,
    maxset=_MAX_LOGGED_ENTRIES,
    maxstring=_MAX_LOGGED_REPR,
    maxother=_MAX_LOGGED_REPR,
)

# Line breaks and other control characters of a scraped name would forge log lines.
_LOG_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")

_OUTPUT_IO_RE = re.compile(r"^Q\d+$")
_INPUT_IO_RE = re.compile(r"^(?:I|AI|QI)\d+$")
_ANALOG_OUTPUT_IO_RE = re.compile(r"^QI\d+$")
_BINARY_IO_RE = re.compile(r"^[QI]\d+$")


@dataclass(frozen=True, kw_only=True)
class ParseOptions:
    """Consumer settings that shape parse_config's output.

    webio_name: base name of the consumer's Web-IO device classes (see const.webio_class_name).
    server_alias: {ServerAlias} placeholder of the entity-name schemas.
    schema_marker / schema_io / schema_knx: str.format_map entity-name templates; unknown
    {keys} are left unchanged.
    """

    webio_name: str = DEFAULT_WEBIO_NAME
    server_alias: str = DEFAULT_SERVER_ALIAS
    schema_marker: str = DEFAULT_SCHEMA_MARKER
    schema_io: str = DEFAULT_SCHEMA_IO
    schema_knx: str = DEFAULT_SCHEMA_KNX

    def __post_init__(self) -> None:
        """Reject a malformed schema here, naming the field, instead of mid-parse without context."""
        for schema_field in fields(self):
            if schema_field.name.startswith("schema_"):
                _validate_schema(schema_field.name, getattr(self, schema_field.name))


def _validate_schema(field_name: str, schema: str) -> None:
    """Probe-render schema; raise ValueError naming field_name if str.format_map rejects it."""
    try:
        schema.format_map(SafeDict())
    except (ValueError, AttributeError, IndexError, TypeError) as exc:
        raise ValueError(f"Invalid {field_name} {schema!r}: {exc}") from exc


@dataclass(frozen=True, kw_only=True)
class _SourceSpec:
    """Per-category parameters of the shared Marker/KNX item build."""

    module_key: str
    schema: str
    id_prefix: str
    id_placeholder: str
    title_placeholder: str


class SafeDict(dict[str, Any]):
    """Format mapping that leaves unknown `{keys}` unchanged instead of raising KeyError."""

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def clean_value(val: Any) -> float:
    """Convert a Comexio value to float, accepting a German decimal comma; 0.0 if not numeric.

    "nan" / "inf" count as not numeric: float() accepts them, but no Comexio reading is one.
    """
    if val is None:
        return 0.0
    if isinstance(val, str):
        val = val.replace(",", ".")
    try:
        result = float(val)
    except (ValueError, TypeError):
        result = math.nan
    if not math.isfinite(result):
        _LOGGER.warning("Failed to clean value: %s", val)
        return 0.0
    return result


def is_extension_offline(identifier: str | None) -> bool:
    """Return True when an extension's Identifier indicates an offline module.

    Online extensions report a serial number in 'XXXX-XXXX-XXXX' format; offline ones carry
    only a short model code without dashes (e.g. '5010'). An empty or missing identifier is
    also treated as offline.
    """
    return not isinstance(identifier, str) or "-" not in identifier


def _label(value: Any) -> str:
    """A scraped label as str: a number is stringified, anything else non-string (or empty) is "".

    So is an int beyond Python's int-to-str digit limit — str() refuses it.
    """
    if not value or not isinstance(value, (str, int, float)):
        return ""
    try:
        return str(value)
    except ValueError:
        return ""


def _table_entry(table: Mapping[str, Any], key: Any) -> Mapping[str, Any]:
    """table[str(key)] if that is a mapping, else {} — a malformed type-table row must not crash parsing."""
    try:
        entry = table.get(str(key))
    except ValueError:
        # An int key beyond Python's int-to-str digit limit names no row.
        return {}
    return entry if isinstance(entry, Mapping) else {}


def _io_type_row(io_types: Mapping[str, Any], type_id: Any) -> Mapping[str, Any]:
    """The IO type table row of type_id, or {} if it has none or its "binary" is no bool.

    A row with "binary": null (or "false", 0, ...) cannot tell binary from analog; kept, it would
    put a None or a misread kind into an active IO and its Web-IO command. Treated as no row, the
    IO falls back like one without a type row.
    """
    row = _table_entry(io_types, type_id)
    return row if isinstance(row.get("binary", False), bool) else {}


def _warn_malformed_type_rows(io_types: Mapping[str, Any]) -> None:
    """Warn once per parse about IO type rows _io_type_row drops — a scraper or firmware problem, not a missing row."""
    malformed = [
        f"{_short_repr(type_id)}: {_short_repr(row.get('binary'))}"
        for type_id, row in io_types.items()
        if isinstance(row, Mapping) and not isinstance(row.get("binary", False), bool)
    ]
    if malformed:
        _LOGGER.warning(
            '%d IO type table rows have no boolean "binary" and are ignored — their IOs and KNX objects are '
            "classified as if the type had no row: %s",
            len(malformed),
            log_excerpt(malformed),
        )


def iter_group(group: Any) -> Iterable[tuple[str, Any]]:
    """Iterate a Comexio id group as (id, member) pairs, ids normalized to str.

    Comexio serializes a gap-free id group as a JSON array instead of an object (observed for
    both $FubModules groups and Web-IO command groups) — the array index then IS the id, so
    both shapes yield the same (id, member) pairs.
    """
    items: Iterable[tuple[Any, Any]]
    if isinstance(group, Mapping):
        items = group.items()
    elif isinstance(group, list):
        items = enumerate(group)
    else:
        items = ()
    return ((str(gid), member) for gid, member in items)


def _io_description(desc: str, ident: str) -> str:
    """The IO's description, stripped, or "" if it is empty or merely repeats the identifier."""
    title = (desc or "").strip()
    return "" if title.casefold() == ident.strip().casefold() else title


def io_schema_title(desc: str, ident: str) -> str:
    """{IoTitle} for the entity-name schema: the IO's description, or "#nn" if there is none.

    A description that is empty or merely repeats the identifier carries no information —
    using it would render "AI1 AI1" under the default schema — so both get the placeholder
    unnamed markers use.
    """
    return _io_description(desc, ident) or NO_NAME_TITLE


def marker_kind(title: str, *, is_marker: bool = True) -> MarkerKind:
    """Derive an item's exposure kind from its Comexio-side title suffix.

    The KNX bridge suffix "[K<id>]" only classifies markers (is_marker=True): a KNX object can
    never itself be a bridge, only be fed by one, so a KNX object whose own title happens to
    end in the same shape is treated as normal instead of being swept into KNX_BRIDGE.
    "[RO]" wins over a simultaneous "[TRIG]"/"[TP]" suffix.
    """
    title = title.rstrip()
    if MARKER_KNX_BRIDGE_SUFFIX_RE.search(title):
        if is_marker:
            return MarkerKind.KNX_BRIDGE
        _LOGGER.warning(
            "KNX object '%s' has a title ending in '[K<id>]' — that suffix is reserved for "
            "auto-created write-path bridge Markers and is ignored here (treating as normal).",
            title,
        )
    if title.endswith(MARKER_READ_ONLY_SUFFIX):
        if any(suffix in title for suffix in MARKER_TRIGGER_SUFFIXES):
            _LOGGER.warning("Marker '%s' has both [RO] and a trigger suffix — treating as read-only.", title)
        return MarkerKind.READ_ONLY
    if title.endswith(MARKER_TRIGGER_SUFFIXES):
        return MarkerKind.TRIGGER
    return MarkerKind.NORMAL


def normalize_io_unit(unit: Any) -> str:
    """Normalize Comexio IO unit strings ("\\u00b0C", "0/1", "?", ...); a non-string unit becomes ""."""
    if not isinstance(unit, str):
        return ""
    if unit in ("\\u00b0C", "°C", "°C", "C"):
        return "°C"
    return "" if unit in ("0/1", "1/0", "?") else unit


def parse_config(
    conf: Mapping[str, Any],
    *,
    io_types: Mapping[str, Any] | None = None,
    io_input_types: Mapping[str, Any] | None = None,
    options: ParseOptions | None = None,
    live_states: Mapping[str, Any] | None = None,
    referenced_markers: set[str] | None = None,
    knx_live_states: Mapping[str, Any] | None = None,
    knx_dpt_catalog: Mapping[str, Any] | None = None,
    io_live_states: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Parse the scraped function module page config into markers, IOs, KNX objects and Web-IO commands.

    conf: the scraped function module page (scrape.scrape_js_vars), with FubModules and WebDevices.
    io_types / io_input_types: the main admin page's type tables (scrape.parse_io_types /
    parse_io_input_types); without io_types, IOs are classified by their identifier.
    live_states / knx_live_states: id-keyed live values of markers and KNX objects. Kept as two
    separate mappings because markers and KNX objects share the same plain numeric id space.
    io_live_states: live values of physical IOs keyed by IO "Id", as ComexioClient.get_io_live_states
    returns them. An IO missing from it or mapped to None keeps the config's "Value"; a value present but not numeric
    reads as 0 (with a warning), like a marker's. Parsing keeps no memory of earlier polls: a caller
    polling between config fetches merges each result into its last known values before passing them.
    referenced_markers: ids of unnamed markers wired into a function plan — imported anyway
    with a "#nn" title.
    knx_dpt_catalog: the scraped KNX admin page; attaches DPT range/unit/device class metadata
    and DPT3.x composite pairing to KNX items. Without it (None or {}) KNX items carry none of
    that — no knx_composite tags either. A consumer that builds entities from those tags should
    keep passing its last good catalog when a catalog fetch fails, or the pairs fall apart.

    Returns a dict with the keys markers, io (active IOs), io_all (every IO, for labels; see below), knx,
    webio_commands (the consumer's own classes, keyed by command name), webio_command_duplicates
    (the further stored commands of a name webio_commands already holds, same fields plus "name";
    the first one of a name wins, in WebioClass order — a consumer can delete the others by
    webIoId, its audit recreates a command that turns out missing), webio_names (label
    lexicon over ALL Web-IO devices), webio_devices (one entry per WebioClass), extensions and
    quickbus_devices (Quickbus devices keyed by device id; an IO of one carries
    "quickbus": {"device_id", "name"}, every other IO "quickbus": None). A Quickbus IO is
    addressed by its unique "id" only — its ext_name/identifier pair repeats across devices, so
    ComexioClient.set_io_value cannot reach it: treat it as read-only even when is_input is False.
    Active IOs that repeat an identifier of their own device (Quickbus device or extension), and
    active Quickbus IOs whose type has no usable row in a non-empty io_types, land only in "io_all" —
    the latter with "is_binary" None (kind unknown), as do inactive Quickbus IOs of such a type.
    IOs whose canonical id repeats are left out of "io" (when two active ones share it) and of
    "io_all" (when any two IOs share it, inactive and left-out ones included: a label looked up
    by that id would be arbitrary) — then an active IO can keep its entity but lose its label.
    """
    options = options or ParseOptions()
    fub_modules = conf.get("FubModules")
    if not isinstance(fub_modules, Mapping):
        fub_modules = {}
    data: dict[str, Any] = {
        _KEY_MARKERS: [],
        "io": [],
        "io_all": [],
        "knx": [],
        _KEY_WEBIO_COMMANDS: {},
        _KEY_WEBIO_DUPLICATES: [],
        "webio_names": {},
        "webio_devices": {cls: {"device_id": None, "device_ip": None, "base_id": None} for cls in WEBIO_CLASSES},
        # Per-extension identity (name + stable serial), lets a consumer detect a Comexio-side rename.
        "extensions": {},
        _KEY_QUICKBUS_DEVICES: {},
    }
    if io_types is None and (fub_modules.get(_MODULE_IO) or fub_modules.get(_MODULE_KNX)):
        _LOGGER.warning(
            "parse_config called without io_types — IOs are classified by their identifier only (a Quickbus "
            "IO, whose identifier says nothing, as analog) and every KNX object is flagged dpt_ambiguous"
        )
    io_types = io_types or {}
    _warn_malformed_type_rows(io_types)

    _process_device_info(conf, data, options.webio_name, fub_modules)
    data[_KEY_MARKERS].extend(
        _process_source_items(
            fub_modules,
            _SourceSpec(
                module_key=_MODULE_MARKER,
                schema=options.schema_marker,
                id_prefix="M",
                id_placeholder="MarkerId",
                title_placeholder="MarkerTitle",
            ),
            io_types=io_types,
            server_alias=options.server_alias,
            live_states=live_states or {},
            referenced_ids=referenced_markers,
        )
    )
    _process_ios(data, fub_modules, io_types, io_input_types or {}, options, io_live_states or {})
    # No "wired but unnamed" import for KNX: marker and KNX ids share a numeric space, so the
    # marker reference set cannot be reused here. A KNX object is imported only when named.
    data["knx"].extend(_process_knx(fub_modules, io_types, options, knx_live_states or {}, knx_dpt_catalog))

    _LOGGER.debug(
        "Parsed %d markers, %d IOs, %d KNX objects, %d Web-IO commands for %s",
        len(data[_KEY_MARKERS]),
        len(data["io"]),
        len(data["knx"]),
        len(data[_KEY_WEBIO_COMMANDS]),
        options.webio_name,
    )
    return data


def _process_device_info(
    conf: Mapping[str, Any], data: dict[str, Any], webio_name: str, fub_modules: Mapping[str, Any]
) -> None:
    """Fill webio_devices/webio_commands for every managed Web-IO class, then the name lexicon."""
    web_devices = {d_id: d for d_id, d in iter_group(conf.get("WebDevices")) if isinstance(d, Mapping)}
    fub_10 = fub_modules.get(_MODULE_WEBIO, {})
    fub_10_by_dev_id = dict(iter_group(fub_10))

    missing_classes = []
    for webio_class in WEBIO_CLASSES:
        target_dev_id = _assign_webio_device_id(web_devices, data, webio_name, webio_class)
        if not target_dev_id:
            missing_classes.append(webio_class)
            continue
        commands = fub_10_by_dev_id.get(target_dev_id)
        if commands is None:
            continue
        for w_id, w_obj in iter_group(commands):
            if not isinstance(w_obj, dict):
                _LOGGER.debug(
                    "Skipping non-dict Web-IO command entry %s in device %s (webio_class=%s): %r",
                    w_id,
                    target_dev_id,
                    webio_class,
                    w_obj,
                )
                continue
            _add_webhook_command(data, w_id, w_obj, webio_class)

    if duplicates := data[_KEY_WEBIO_DUPLICATES]:
        _LOGGER.warning(
            "%d stored Web-IO commands repeat the name of another command, only the first one of each name "
            "is in webio_commands: %s",
            len(duplicates),
            log_excerpt([f"{d['name']} (webIoId {d['webIoId']}, {d['webioClass']})" for d in duplicates]),
        )
    missing_classes = [c for c in missing_classes if c != WebioClass.QUICKBUS]
    if missing_classes and any(d.get("Name") == webio_name for d in web_devices.values()):
        # Pre-split installs have a single Web-IO device named exactly `webio_name`; it won't
        # match the "<name> [M]"/"<name> [IO]" class names, so the classes look missing.
        _LOGGER.warning(
            "Found a legacy Web-IO device named '%s' without a Marker/IO class suffix. "
            "Web-IO is split into separate classes ('%s' / '%s'); missing class(es): %s.",
            webio_name,
            webio_class_name(webio_name, WebioClass.MARKER),
            webio_class_name(webio_name, WebioClass.IO),
            ", ".join(WEBIO_CLASS_LABEL[c] for c in missing_classes),
        )

    _build_webio_name_lexicon(data, fub_10)


def _assign_webio_device_id(
    web_devices: Mapping[str, Any], data: dict[str, Any], webio_name: str, webio_class: WebioClass
) -> str | None:
    """Find the WebDevices entry for one Web-IO class, populate its device info, return its id."""
    class_name = webio_class_name(webio_name, webio_class)
    for d_id, d_data in web_devices.items():
        if d_data.get("Name") != class_name:
            continue
        target_dev_id = str(d_id)
        dev_info = data["webio_devices"][webio_class]
        dev_info["device_id"] = target_dev_id
        # Comexio has been observed to scrape a leading space into the Ip field.
        raw_ip = d_data.get("Ip")
        dev_info["device_ip"] = raw_ip.strip() if isinstance(raw_ip, str) else raw_ip
        # The class id is WebDeviceBaseId in $WebDevices — "BaseId" only exists in the
        # upload/save payloads.
        raw_base_id = d_data.get("WebDeviceBaseId")
        dev_info["base_id"] = str(raw_base_id) if raw_base_id is not None else None
        return target_dev_id
    return None


def _build_webio_name_lexicon(data: dict[str, Any], fub_10: Any) -> None:
    """Build the webio_names label lexicon over ALL Web-IO devices (read-only, for plan rendering).

    Plans may wire commands of foreign Web-IO devices. Names mirror Comexio Studio's pill
    labels: '{deviceId}. {commandName}' — Studio does NOT include the device name. Kept
    separate from webio_commands on purpose — that dict must only contain the consumer's own
    classes.
    """
    for dev_id, dev_commands in iter_group(fub_10):
        prefix = f"{dev_id}. "
        for w_id, w_obj in iter_group(dev_commands):
            if not isinstance(w_obj, dict):
                continue
            name = w_obj.get("Name")
            if name:
                data["webio_names"][w_id] = {
                    "name": f"{prefix}{name}",
                    "analog": w_obj.get("TypeId") in {2, "2"},
                }


def _add_webhook_command(data: dict[str, Any], w_id: str, w_obj: dict[str, Any], webio_class: WebioClass) -> None:
    """Add one Web-IO command of a managed class to webio_commands."""
    raw_type = w_obj.get("TypeId")
    try:
        val_type = int(raw_type) if raw_type is not None else 1
    except (ValueError, TypeError, OverflowError):  # OverflowError: JSON Infinity
        val_type = 1

    name = w_obj.get("Name")
    if not isinstance(name, str):
        # An empty-string name is kept on purpose: the consumer's audit sees it as a surplus
        # command in its own class and can delete it. A non-string one has no usable key.
        _LOGGER.warning("Skipping Web-IO command %s (webio_class=%s) with non-string name %r", w_id, webio_class, name)
        return

    entry = {
        "webIoId": w_id,
        "cmdId": w_obj.get("WebCommandId"),
        "typeId": val_type,
        "webioClass": webio_class,
    }
    # The consumer keys its commands by name across all classes; a second command of a name
    # must not overwrite the first one, or it vanishes from the audit and is never cleaned up.
    if name in data[_KEY_WEBIO_COMMANDS]:
        data[_KEY_WEBIO_DUPLICATES].append({"name": name, **entry})
        return
    data[_KEY_WEBIO_COMMANDS][name] = entry


def _process_source_items(
    fub_modules: Mapping[str, Any],
    spec: _SourceSpec,
    *,
    io_types: Mapping[str, Any],
    server_alias: str,
    live_states: Mapping[str, Any],
    referenced_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Shared Marker/KNX item processing over one $FubModules category.

    An item without a Comexio label is skipped unless its id is in referenced_ids (wired into
    a function plan) — then it is imported with the "#nn" title.
    """
    referenced_ids = referenced_ids or set()
    items: list[dict[str, Any]] = []
    for _gid, raw in iter_group(fub_modules.get(spec.module_key)):
        if not isinstance(raw, dict) or raw.get("Id") is None:
            continue
        item_id = str(raw.get("Id"))
        raw_name = raw.get("Name")
        has_name = bool(_label(raw_name))
        if raw_name and not has_name:
            _LOGGER.warning("%s%s has a non-string name %r — treating it as unnamed", spec.id_prefix, item_id, raw_name)
        if not has_name and item_id not in referenced_ids:
            continue
        items.append(_build_source_item(raw, spec, item_id, has_name, io_types, server_alias, live_states))
    return items


def _build_source_item(
    raw: dict[str, Any],
    spec: _SourceSpec,
    item_id: str,
    has_name: bool,
    io_types: Mapping[str, Any],
    server_alias: str,
    live_states: Mapping[str, Any],
) -> dict[str, Any]:
    """Build one Marker/KNX item dict from its raw $FubModules entry."""
    type_raw = raw.get("Type", 1)
    type_str, type_unresolved = _source_item_type(spec.module_key, type_raw, io_types)
    title = _label(raw.get("Name")) or NO_NAME_TITLE

    ha_name = spec.schema.format_map(
        SafeDict(ServerAlias=server_alias, **{spec.id_placeholder: item_id, spec.title_placeholder: title})
    )
    item: dict[str, Any] = {
        "id": item_id,
        "ha_name": " ".join(ha_name.split()),
        "name": f"{spec.id_prefix}{item_id} {title}",
        # Bare Comexio title, without the id prefix "name" carries.
        "title": title,
        # Unnamed-but-referenced item ("#nn").
        "no_name": not has_name,
        "type": type_str,
        "type_raw": type_raw,
        "value": clean_value(live_states.get(item_id, 0)),
        "kind": marker_kind(title, is_marker=spec.module_key == _MODULE_MARKER),
    }
    if type_unresolved:
        # Only ever True for KNX items — popped again by _process_knx.
        item["dpt_type_unresolved"] = True
    return item


def _source_item_type(module_key: str, type_raw: Any, io_types: Mapping[str, Any]) -> tuple[str, bool]:
    """digital/analog classification for one raw Marker/KNX Type value -> (type, unresolved).

    KNX Type is a rich catalog code in the same value space as IOs' $IOTypesBinary, not the
    simple {1,2,3} marker scale. A KNX Type without a usable io_types entry (missing, null, no
    object, an empty one or a non-bool "binary") is reported unresolved and defaults to "digital"
    — the safer of the two, it never exposes a possibly-binary datapoint to analog writes.
    """
    if module_key == _MODULE_KNX:
        if not (entry := _io_type_row(io_types, type_raw)):
            return TYPE_DIGITAL, True
        return (TYPE_DIGITAL if entry.get("binary", False) else TYPE_ANALOG), False
    return (TYPE_ANALOG if type_raw in [2, 3] else TYPE_DIGITAL), False


def _process_knx(
    fub_modules: Mapping[str, Any],
    io_types: Mapping[str, Any],
    options: ParseOptions,
    live_states: Mapping[str, Any],
    knx_dpt_catalog: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    """KNX objects from $FubModules["11"], modeled 1:1 on markers plus DPT metadata.

    With a knx_dpt_catalog, analog items get dpt_min/dpt_max/dpt_unit/dpt_step (and
    dpt_device_class where one fits), digital items a dpt_device_class or dpt_ambiguous=True
    for a physically ambivalent DPT1.x subtype, and DPT3.x pairs a knx_composite tag. An item
    whose raw Type has no io_types entry at all is always dpt_ambiguous=True.
    """
    items = _process_source_items(
        fub_modules,
        _SourceSpec(
            module_key=_MODULE_KNX,
            schema=options.schema_knx,
            id_prefix="K",
            id_placeholder="KnxId",
            title_placeholder="KnxTitle",
        ),
        io_types=io_types,
        server_alias=options.server_alias,
        live_states=live_states,
    )
    for item in items:
        if item.pop("dpt_type_unresolved", False):
            # Its digital/analog split is genuinely unknown — flag it for human review
            # regardless of whether a DPT catalog is available.
            item[_KEY_DPT_AMBIGUOUS] = True
    if knx_dpt_catalog:
        for item in items:
            _apply_knx_dpt_metadata(item, knx_dpt_catalog)
        _attach_knx_dpt3_composites(items, knx_dpt_catalog)
    return items


def _apply_knx_dpt_metadata(item: dict[str, Any], knx_dpt_catalog: Mapping[str, Any]) -> None:
    """Resolve one KNX item's DPT and attach unit/device class/ambiguous metadata in place."""
    if item.get(_KEY_DPT_AMBIGUOUS):
        # Already queued for human review (unresolved Type) — a device class resolved from the
        # DPT chain is not corroborating evidence worth auto-tagging on.
        return
    dpt = resolve_knx_dpt(knx_dpt_catalog, item["id"])
    if dpt is None:
        _LOGGER.debug("KNX item %s: could not resolve DPT chain, using generic fallback", item["id"])
        return
    if item["type"] != TYPE_ANALOG:
        if device_class := KNX_DPT_DIGITAL_DEVICE_CLASS.get(dpt):
            item["dpt_device_class"] = device_class
        elif dpt in KNX_DPT_DIGITAL_AMBIGUOUS:
            item[_KEY_DPT_AMBIGUOUS] = True
        return
    dpt_range = KNX_DPT_ANALOG_RANGES.get(dpt)
    if dpt_range is None:
        _LOGGER.debug(
            "KNX item %s: resolved DPT%s.%s has no entry in KNX_DPT_ANALOG_RANGES, using generic fallback range",
            item["id"],
            dpt[0],
            dpt[1],
        )
        return
    item["dpt_min"], item["dpt_max"], item["dpt_unit"], item["dpt_step"] = dpt_range
    if device_class := KNX_DPT_DEVICE_CLASS.get(dpt):
        item["dpt_device_class"] = device_class


def _attach_knx_dpt3_composites(items: list[dict[str, Any]], knx_dpt_catalog: Mapping[str, Any]) -> None:
    """Tag DPT3.x (Dimmer 3.007 / Blinds 3.008) K-element pairs with knx_composite metadata.

    Comexio splits each DPT3.x KNX object into two K-elements sharing one KnxDeviceId — a
    digital control bit (direction) and an analog 3-bit step code. Each half gets
    {"role": "direction"|"stepcode", "domain": "light"|"cover", "partner_id": <other id>}.
    """
    points = knx_dpt_catalog.get("KnxPoints")
    if not isinstance(points, dict):
        return

    by_device: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in items:
        point = points.get(item["id"])
        device_id = point.get("KnxDeviceId") if isinstance(point, dict) else None
        if device_id is not None:
            by_device[str(device_id)].append(item)

    for device_id, pair in by_device.items():
        _tag_knx_dpt3_pair(device_id, pair, knx_dpt_catalog)


def _tag_knx_dpt3_pair(device_id: str, pair: list[dict[str, Any]], knx_dpt_catalog: Mapping[str, Any]) -> None:
    """Tag one KnxDeviceId's 2-point group with knx_composite metadata, if it qualifies."""
    if len(pair) != 2:
        if len(pair) > 2:
            _LOGGER.debug(
                "KNX device %s has %d points sharing one KnxDeviceId (expected at most 2), skipping composite grouping",
                device_id,
                len(pair),
            )
        return
    dpt = resolve_knx_dpt(knx_dpt_catalog, pair[0]["id"])
    domain = KNX_DPT3_COMPOSITE_DOMAIN.get(dpt) if dpt else None
    if dpt is None or domain is None:
        _LOGGER.debug(
            "KNX device %s has 2 points but resolved DPT %s isn't a DPT3.x composite, skipping composite grouping",
            device_id,
            dpt,
        )
        return
    direction_item = next((i for i in pair if i["type"] == TYPE_DIGITAL), None)
    stepcode_item = next((i for i in pair if i["type"] == TYPE_ANALOG), None)
    if direction_item is None or stepcode_item is None:
        _LOGGER.debug(
            "KNX device %s resolved to DPT%s.%s but its 2 points aren't one digital + "
            "one analog K-element, skipping composite grouping",
            device_id,
            dpt[0],
            dpt[1],
        )
        return
    direction_item["knx_composite"] = {"role": "direction", "domain": domain, "partner_id": stepcode_item["id"]}
    stepcode_item["knx_composite"] = {"role": "stepcode", "domain": domain, "partner_id": direction_item["id"]}


def _extension_meta(ext_id: str, ext_content: Mapping[str, Any]) -> tuple[str, str]:
    """(name, serial) of one extension; a missing/malformed Identifier yields serial "" (= offline)."""
    ext_meta = ext_content.get("extension")
    if not isinstance(ext_meta, Mapping):
        ext_meta = {}
    ext_name = ext_meta.get("Name")
    ext_serial = ext_meta.get("Identifier")
    return (
        ext_name if isinstance(ext_name, str) else f"Ext{ext_id}",
        ext_serial if isinstance(ext_serial, str) else "",
    )


def _strict_id(value: Any) -> str | None:
    """A Comexio id as a canonical str: a non-negative int (not bool) or an ASCII digit string; else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        # The same digit cap as for strings — str() of a huge int would even raise.
        return str(value) if 0 <= value < 10**_MAX_ID_DIGITS else None
    # The length cap keeps int() below its digit limit — a longer string would raise instead.
    if isinstance(value, str) and value.isascii() and value.isdigit() and len(value) <= _MAX_ID_DIGITS:
        return str(int(value))
    return None


def _log_safe(text: str) -> str:
    """text cut after _MAX_LOGGED_ENTRY characters, its control characters escaped (a line break as \\n)."""
    cut = text if len(text) <= _MAX_LOGGED_ENTRY else f"{text[:_MAX_LOGGED_ENTRY]}…"
    return _LOG_CONTROL_CHAR_RE.sub(lambda m: repr(m.group())[1:-1], cut)


def log_excerpt(items: list[str]) -> str:
    """The first _MAX_LOGGED_ENTRIES items joined for a log line, with the rest counted.

    Each item is cut and its control characters escaped — items carry scraped names.
    """
    shown = ", ".join(_log_safe(item) for item in items[:_MAX_LOGGED_ENTRIES])
    more = len(items) - _MAX_LOGGED_ENTRIES
    return f"{shown} and {more} more" if more > 0 else shown


def _short_repr(value: Any) -> str:
    """repr() of a raw value for a log line, cut off after _MAX_LOGGED_REPR characters.

    Strings and containers are cut while rendering (_LOG_REPR), not after.
    """
    if isinstance(value, int) and not isinstance(value, bool):
        try:
            text = repr(value)
        except ValueError:
            # Beyond Python's int-to-str digit limit.
            return f"<{type(value).__name__} too large to print>"
    else:
        try:
            text = _LOG_REPR.repr(value)
        except ValueError:
            # A container holding such an int, on a Python whose reprlib does not catch it itself.
            return f"<{type(value).__name__} with a value too large to print>"
    return text if len(text) <= _MAX_LOGGED_REPR else f"{text[:_MAX_LOGGED_REPR]}…"


def _quickbus_device(dev_id: str, raw: Mapping[str, Any], ext_id: str, problems: list[str]) -> dict[str, Any]:
    """One Quickbus device entry; a malformed field is added to problems and replaced by its empty value."""
    name = _label(raw.get("Name"))
    if not name:
        _LOGGER.debug(
            "Quickbus device %s of extension %s has no name (%s)", dev_id, ext_id, _short_repr(raw.get("Name"))
        )
    serial = raw.get("Identifier")
    if serial is not None and not isinstance(serial, str):
        problems.append(f"device {dev_id} of extension {ext_id} Identifier {_short_repr(serial)}")
    type_id = raw.get("TypeId")
    type_ok = isinstance(type_id, int) and not isinstance(type_id, bool)
    if type_id is not None and not type_ok:
        problems.append(f"device {dev_id} of extension {ext_id} TypeId {_short_repr(type_id)}")
    return {
        "id": dev_id,
        "name": name or f"QB{dev_id}",
        # Empty for a virtual device (created in Comexio Studio without hardware).
        "serial": serial if isinstance(serial, str) else "",
        "type_id": type_id if type_ok else None,
        "ext_id": ext_id,
    }


def _quickbus_entries(ext_id: str, ext_content: Mapping[str, Any]) -> list[tuple[str, Mapping[str, Any]]]:
    """(device id, raw entry) of every well-formed Quickbus device entry of one extension."""
    entries: list[tuple[str, Mapping[str, Any]]] = []
    malformed: list[str] = []
    for key, raw in iter_group(ext_content.get(_QUICKBUS_KEY)):
        dev_id = _strict_id(raw.get("Id")) if isinstance(raw, Mapping) else None
        if dev_id is None:
            malformed.append(f"{key}: {_short_repr(raw)}")
            continue
        entries.append((dev_id, raw))
    if malformed:
        _LOGGER.warning(
            "Skipping %d malformed Quickbus device entries of extension %s: %s",
            len(malformed),
            ext_id,
            log_excerpt(malformed),
        )
    return entries


def _extension_groups(fub_modules: Mapping[str, Any]) -> list[tuple[str, Mapping[str, Any]]]:
    """The (ext id, content) groups of $FubModules["1"] with a canonical numeric id and a dict content.

    The id feeds the consumer's data (extensions, an IO's ext_id) and every warning about the
    extension, so a malformed one — even one carrying a line break, or "01" next to "1" — is
    skipped, like a group that is no dict, with one warning.
    """
    groups: list[tuple[str, Mapping[str, Any]]] = []
    skipped: list[str] = []
    for ext_id, ext_content in iter_group(fub_modules.get(_MODULE_IO)):
        if _strict_id(ext_id) != ext_id:
            skipped.append(f"id {_short_repr(ext_id)}")
        elif isinstance(ext_content, Mapping):
            groups.append((ext_id, ext_content))
        else:
            skipped.append(f"{ext_id}: {_short_repr(ext_content)}")
    if skipped:
        _LOGGER.warning("Skipping %d extensions with a malformed id or content: %s", len(skipped), log_excerpt(skipped))
    return groups


def _collect_quickbus_devices(
    extensions: list[tuple[str, Mapping[str, Any]]],
) -> tuple[dict[str, dict[str, Any]], set[str]]:
    """The Quickbus devices of every extension (ext id -> device id -> device), and the dropped ids.

    A device id that is not unique — listed twice in one extension or on two extensions — is
    dropped everywhere, so no IO is merged into the wrong device; the IOs of all its devices
    stay plain IOs. Collected before any IO is built, so the outcome does not depend on the
    order of the extensions.
    """
    by_ext: dict[str, dict[str, Any]] = {}
    seen: set[str] = set()
    dropped: set[str] = set()
    problems: list[str] = []
    for ext_id, ext_content in extensions:
        devices: dict[str, Any] = {}
        for dev_id, raw in _quickbus_entries(ext_id, ext_content):
            if dev_id in seen:
                dropped.add(dev_id)
                continue
            seen.add(dev_id)
            devices[dev_id] = _quickbus_device(dev_id, raw, ext_id, problems)
        by_ext[ext_id] = devices
    if problems:
        _LOGGER.warning(
            "%d malformed Quickbus device fields, they are left empty: %s", len(problems), log_excerpt(problems)
        )
    if dropped:
        _LOGGER.warning(
            "Quickbus device ids are not unique, their devices are dropped and their IOs treated as plain IOs: %s",
            log_excerpt(sorted(dropped, key=int)),
        )
        by_ext = {
            ext_id: {dev_id: dev for dev_id, dev in devices.items() if dev_id not in dropped}
            for ext_id, devices in by_ext.items()
        }
    return by_ext, dropped


def _resolve_quickbus_mapping(
    raw_map: Any, devices: Mapping[str, Any], dropped: set[str]
) -> tuple[dict[str, str], list[str]]:
    """IO id -> device id of the trusted mapping entries, and the log entries of all others.

    One doubtful entry — a second, unknown, malformed or dropped device under any spelling of
    the IO id ("5", "05") — makes the whole IO doubtful: none of its entries is trusted, the
    ones trusted so far are reported along with it. Entries of a dropped device id are not
    reported again, the non-unique ids are warned about on their own.
    """
    result: dict[str, str] = {}
    first_entry: dict[str, str] = {}
    conflicting: set[str] = set()
    unresolved: list[str] = []
    for io_key, dev_ref in iter_group(raw_map):
        io_id = _strict_id(io_key)
        dev_id = _strict_id(dev_ref)
        entry = f"{_short_repr(io_key)}->{_short_repr(dev_ref)}"
        if io_id is None:
            if dev_id not in dropped:
                unresolved.append(entry)
            continue
        if (
            io_id not in conflicting
            and dev_id is not None
            and dev_id in devices
            and result.setdefault(io_id, dev_id) == dev_id
        ):
            first_entry.setdefault(io_id, entry)
            continue
        conflicting.add(io_id)
        if result.pop(io_id, None) is not None:
            unresolved.append(first_entry.pop(io_id))
        if dev_id not in dropped:
            unresolved.append(entry)
    return result, unresolved


def _report_quickbus_coverage(
    ext_id: str, ext_content: Mapping[str, Any], devices: Mapping[str, Any], io_map: Mapping[str, str]
) -> None:
    """Warn about mapping entries of IOs the extension does not list and devices without a mapped IO.

    The IOs of such a device are plain IOs; the ones repeating an identifier are left out
    later (_ambiguous_ios).
    """
    io_ids = {
        _strict_id(item.get("Id")) for _, item in iter_group(ext_content.get("inoutput")) if isinstance(item, Mapping)
    }
    stale = sorted((io_id for io_id in io_map if io_id not in io_ids), key=int)
    if stale:
        _LOGGER.warning(
            "Quickbus mapping of extension %s: %d entries point to IOs the extension does not list: %s",
            ext_id,
            len(stale),
            log_excerpt([f"{io_id}->{io_map[io_id]}" for io_id in stale]),
        )
    # A device whose only entries are stale has no IO either.
    mapped = {dev_id for io_id, dev_id in io_map.items() if io_id in io_ids}
    unmapped = sorted(set(devices) - mapped, key=int)
    if unmapped:
        _LOGGER.warning(
            "%d Quickbus devices of extension %s have no IO in the mapping, their IOs are treated as plain IOs: %s",
            len(unmapped),
            ext_id,
            log_excerpt([f"{dev_id} {devices[dev_id]['name']}" for dev_id in unmapped]),
        )


def _quickbus_io_map(
    ext_id: str, ext_content: Mapping[str, Any], devices: Mapping[str, Any], dropped: set[str]
) -> dict[str, str]:
    """IO id -> Quickbus device id of one extension, from its "mapping" (only devices of this extension).

    Doubtful entries: see _resolve_quickbus_mapping.
    """
    mapping = ext_content.get("mapping")
    raw_map = mapping.get(_QUICKBUS_KEY) if isinstance(mapping, Mapping) else None
    result, unresolved = _resolve_quickbus_mapping(raw_map, devices, dropped)
    if devices and (result or unresolved):
        # Also when no entry resolves: the unresolved warning does not name the devices left without an IO.
        _report_quickbus_coverage(ext_id, ext_content, devices, result)
    if unresolved:
        _LOGGER.warning(
            "Quickbus mapping of extension %s: %d entries point to an unknown device, are malformed or "
            "contradict each other, those IOs are treated as plain IOs: %s",
            ext_id,
            len(unresolved),
            log_excerpt(unresolved),
        )
    elif devices and not result:
        # Without the map every device IO would be a plain IO again — the identifier collisions.
        _LOGGER.warning(
            "Extension %s lists %d Quickbus devices but no usable IO mapping (%s) — their IOs are treated as plain IOs",
            ext_id,
            len(devices),
            _short_repr(raw_map),
        )
    return result


def _quickbus_io_direction(io_item: Mapping[str, Any]) -> bool | None:
    """is_input of a Quickbus IO from its own IntIsOutput flag; None if that is malformed.

    $IOInputTypes cannot be used for these: it marks e.g. the Multitouch gestures (type 134) as
    not an input, which would make every gesture a writable output.
    """
    raw = io_item.get("IntIsOutput")
    if isinstance(raw, int) and not isinstance(raw, bool) and raw in (0, 1):
        return raw == 0
    if isinstance(raw, str) and raw in ("0", "1"):
        return raw == "0"
    return None


@dataclass
class _QuickbusContext:
    """The Quickbus devices and IO map of one extension, plus what its IOs report afterwards."""

    ext_id: str
    devices: Mapping[str, Any]
    io_map: Mapping[str, str]
    bad_flags: list[str] = field(default_factory=list)

    def resolve(self, io_item: Mapping[str, Any]) -> tuple[Mapping[str, Any] | None, bool | None]:
        """The Quickbus device of an IO and its is_input; (None, None) for a plain IO."""
        dev_id = self.io_map.get(_strict_id(io_item.get("Id")) or "")
        if dev_id is None:
            return None, None
        device = self.devices[dev_id]
        is_input = _quickbus_io_direction(io_item)
        if is_input is None:
            # Read-only is the safe side: a wrong guess must never turn a gesture into an output.
            ident = _short_repr(io_item.get("Identifier"))
            self.bad_flags.append(f"{device['name']} {ident}: {_short_repr(io_item.get('IntIsOutput'))}")
            is_input = True
        return device, is_input

    def report(self) -> None:
        if self.bad_flags:
            _LOGGER.warning(
                "%d Quickbus IOs of extension %s have no usable IntIsOutput flag and are treated as read-only "
                "inputs: %s",
                len(self.bad_flags),
                self.ext_id,
                log_excerpt(self.bad_flags),
            )


def _ambiguous_ios(ext_id: str, entries: list[dict[str, Any]]) -> set[int]:
    """Positions of the active IOs whose device lists their identifier more than once.

    The device of a Quickbus IO is its Quickbus device, that of a plain IO the extension — this
    also catches the IOs of a Quickbus device without a usable mapping, which repeat G1, TL1, ...
    Their Web-IO command names (and a consumer's ids) would collide, and no rule says which
    one is meant — so all of them are left out. Comexio should never list such a pair.
    """
    by_key: dict[tuple[str | None, str], list[int]] = {}
    for pos, entry in enumerate(entries):
        if not entry["inactive"]:
            qb = entry["quickbus"]
            key = (qb["device_id"] if qb else None, str(entry["identifier"]).upper())
            by_key.setdefault(key, []).append(pos)
    repeated = [positions for positions in by_key.values() if len(positions) > 1]
    if repeated:
        labels = []
        for positions in repeated:
            first = entries[positions[0]]
            owner = first["quickbus"]["name"] if first["quickbus"] else first["ext_name"]
            labels.append(f"{owner} {_short_repr(first['identifier'])}")
        _LOGGER.warning(
            "%d IO identifiers of extension %s repeat within their device, their %d active IOs are left out of "
            "the active IOs (no entity, no Web-IO command): %s",
            len(repeated),
            ext_id,
            sum(map(len, repeated)),
            log_excerpt(labels),
        )
    return {pos for positions in repeated for pos in positions}


def _repeated_io_ids(active: list[dict[str, Any]]) -> set[str]:
    """The canonical IO "Id"s more than one active IO has, across all extensions ("211" and "00211").

    The Id keys the live states and the Quickbus payload (ComexioClient.get_io_live_states even
    refuses one IO id under two extension ids), so the IOs would read and push each other's
    values — all of them are left out. Only active IOs count: an inactive one is neither polled
    nor pushed. Comexio should never list such a pair.
    """
    repeated = _ext_ids_of_repeated_ids(active)
    if repeated:
        _LOGGER.warning(
            "%d IO ids repeat, their %d active IOs are left out (no entity, no Web-IO command, no plan label): %s",
            len(repeated),
            sum(map(len, repeated.values())),
            _repeat_labels(repeated),
        )
    return set(repeated)


def _unlabelled_io_ids(every_io: list[dict[str, Any]], warned: set[str]) -> set[str]:
    """The canonical IO "Id"s more than one IO has, inactive and left-out ones included; no plan label.

    A plan renderer looks an IO up by its id alone, so it would show an arbitrary one of them —
    the plan falls back to its bare reference instead. Ids _repeated_io_ids already warned about
    are not logged again.
    """
    repeated = _ext_ids_of_repeated_ids(every_io)
    if unwarned := {io_id: ext_ids for io_id, ext_ids in repeated.items() if io_id not in warned}:
        _LOGGER.warning(
            "%d IO ids repeat among all IOs (inactive and left-out ones included), none of them gets a plan label: %s",
            len(unwarned),
            _repeat_labels(unwarned),
        )
    return set(repeated)


def _ext_ids_of_repeated_ids(entries: list[dict[str, Any]]) -> dict[str, list[str]]:
    """Canonical IO id -> the extension ids of its IOs, for every id more than one entry has."""
    by_id: dict[str, list[str]] = {}
    for entry in entries:
        by_id.setdefault(entry["id"], []).append(entry["ext_id"])
    return {io_id: ext_ids for io_id, ext_ids in by_id.items() if len(ext_ids) > 1}


def _repeat_labels(repeated: dict[str, list[str]]) -> str:
    labels = [f"{io_id} (ext {', '.join(dict.fromkeys(repeated[io_id]))})" for io_id in sorted(repeated, key=int)]
    return log_excerpt(labels)


def _type_bound(value: Any) -> float | None:
    """A min/max from the IO type table if it is a finite int or float, else None.

    The table is scraped JSON: a text, bool or infinite bound would break the range math of a
    Web-IO command (abs()) and with it a whole upload, unrelated classes included.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        return value if math.isfinite(value) else None
    except OverflowError:  # an int beyond float's range
        return None


def _type_bounds(type_info: Mapping[str, Any], ext_name: str, ident: str) -> tuple[float | None, float | None]:
    """(min, max) of an IO from its type row; both None (the Web-IO defaults) if one is unusable.

    One bound alone does not make a range: a kept min of 200 with a defaulted max of 100 would
    turn it upside down. A missing or null bound stays as before (min 0, max 1 or None).
    """
    raw = (type_info.get("min", 0), type_info.get("max", 1))
    bounds = (_type_bound(raw[0]), _type_bound(raw[1]))
    if any(bound is None and value is not None for bound, value in zip(bounds, raw, strict=True)):
        _LOGGER.warning(
            "IO %s %s: the IO type table has an unusable min/max (%s, %s), the Web-IO command gets the default range",
            _log_safe(ext_name),
            _short_repr(ident),
            _short_repr(raw[0]),
            _short_repr(raw[1]),
        )
        return None, None
    return bounds


def _untyped_quickbus_ios(
    ext_id: str, entries: list[dict[str, Any]], type_ids: list[Any], io_types: Mapping[str, Any]
) -> set[int]:
    """Positions of the Quickbus IOs whose type has no usable row in the IO type table (_io_type_row).

    A plain IO falls back to its identifier (I1, Q1, QI1, ...); G1, TL1 or R of a Quickbus
    device say nothing about binary or analog, so its entity and Web-IO command would get a
    guessed kind and range — those IOs are left out instead. Inactive ones are included too
    (io_all keeps them for plan labels, so their kind is unknown as well), but only the active
    ones, which lose their entity, are warned about. Not when the whole table is missing (a
    failed scrape, warned about by parse_config): leaving out every Quickbus IO for one poll
    would make a consumer drop their entities.
    """
    if not io_types:
        return set()
    untyped = {
        pos
        for pos, (entry, type_id) in enumerate(zip(entries, type_ids, strict=True))
        if entry["quickbus"] and not _io_type_row(io_types, type_id)
    }
    if active := [pos for pos in sorted(untyped) if not entries[pos]["inactive"]]:
        labels = [
            f"{entries[pos]['quickbus']['name']} {entries[pos]['identifier']} (type {_short_repr(type_ids[pos])})"
            for pos in active
        ]
        _LOGGER.warning(
            "%d active Quickbus IOs of extension %s have no usable row in the IO type table, so binary or analog is "
            "unknown — they are left out of the active IOs (no entity, no Web-IO command): %s",
            len(active),
            _log_safe(ext_id),
            log_excerpt(labels),
        )
    return untyped


def _process_extension_ios(
    data: dict[str, Any],
    ext_id: str,
    ext_content: Mapping[str, Any],
    builder: "_IoEntryBuilder",
    qb: _QuickbusContext,
) -> None:
    """The IOs of one extension, incl. the IOs of its Quickbus devices."""
    ext_name, ext_serial = _extension_meta(ext_id, ext_content)
    data["extensions"][ext_id] = {"name": ext_name, "serial": ext_serial}
    ext_offline = is_extension_offline(ext_serial)

    entries: list[dict[str, Any]] = []
    type_ids: list[Any] = []
    bad_ids: list[str] = []
    for io_id, io_item in iter_group(ext_content.get("inoutput")):
        if not isinstance(io_item, Mapping) or not io_item:
            _LOGGER.debug(
                "Skipping empty/non-dict IO entry %s of extension %s: %s", io_id, ext_id, _short_repr(io_item)
            )
            continue
        if (strict_id := _strict_id(io_item.get("Id"))) is None:
            # The Id feeds unique_id/entity_id and the Quickbus payload; str() of a huge int even raises.
            bad_ids.append(f"{io_id}: {_short_repr(io_item.get('Id'))}")
            continue
        qb_device, is_input = qb.resolve(io_item)
        entry = builder.build(io_item, strict_id, ext_name, qb_device, is_input)
        entry["offline"] = ext_offline
        # The extension group key; ComexioClient.get_io_live_states needs it as "ExtId".
        entry["ext_id"] = ext_id
        entries.append(entry)
        type_ids.append(io_item.get("InOutputTypeId"))
    if bad_ids:
        _LOGGER.warning(
            "Skipping %d IO entries of extension %s without a valid Id: %s", len(bad_ids), ext_id, log_excerpt(bad_ids)
        )
    qb.report()
    untyped = _untyped_quickbus_ios(ext_id, entries, type_ids, builder.io_types)
    for pos in untyped:
        entries[pos]["is_binary"] = None  # unknown kind: a plan renders its pins as unknown, not analog
    left_out = _ambiguous_ios(ext_id, entries) | untyped
    data["io_all"].extend(entries)
    data["io"].extend(e for pos, e in enumerate(entries) if not e["inactive"] and pos not in left_out)


def _process_ios(
    data: dict[str, Any],
    fub_modules: Mapping[str, Any],
    io_types: Mapping[str, Any],
    io_input_types: Mapping[str, Any],
    options: ParseOptions,
    io_live_states: Mapping[str, Any],
) -> None:
    """Physical IOs from $FubModules["1"] (one group per extension), incl. the IOs of Quickbus devices.

    Inactive IOs (Active=False) land only in "io_all" — Comexio itself refuses to wire a
    connection to an inactive IO, but a plan renderer still needs their label.
    """
    builder = _IoEntryBuilder(io_types, io_input_types, options, io_live_states)
    extensions = _extension_groups(fub_modules)
    qb_by_ext, qb_dropped = _collect_quickbus_devices(extensions)
    for ext_id, ext_content in extensions:
        qb_devices = qb_by_ext.get(ext_id, {})
        data[_KEY_QUICKBUS_DEVICES].update(qb_devices)
        qb_map = _quickbus_io_map(ext_id, ext_content, qb_devices, qb_dropped)
        _process_extension_ios(data, ext_id, ext_content, builder, _QuickbusContext(ext_id, qb_devices, qb_map))
    if repeated := _repeated_io_ids(data["io"]):
        data["io"] = [entry for entry in data["io"] if entry["id"] not in repeated]
    if unlabelled := _unlabelled_io_ids(data["io_all"], repeated):
        data["io_all"] = [entry for entry in data["io_all"] if entry["id"] not in unlabelled]


def _io_direction(ident_upper: str, type_id_raw: int, io_input_types: Mapping[str, Any]) -> bool:
    """is_input for an IO: True -> read-only, False -> writable.

    The identifier prefix is the reliable source: Q* are relay/dimmer outputs, I*/AI*/QI* are
    inputs. $IOInputTypes is only a fallback for other identifiers, because the same TypeId
    (e.g. 2 = binary 0/1) is shared by inputs and outputs.
    """
    if _OUTPUT_IO_RE.match(ident_upper):
        return False
    if _INPUT_IO_RE.match(ident_upper):
        return True
    if io_input_types:
        return bool(_table_entry(io_input_types, type_id_raw).get("input", True))
    return True


@dataclass(frozen=True)
class _IoEntryBuilder:
    """The parse-wide inputs of _build_io_entry, bound once per parse_config call."""

    io_types: Mapping[str, Any]
    io_input_types: Mapping[str, Any]
    options: ParseOptions
    io_live_states: Mapping[str, Any]

    def build(
        self,
        io_item: Mapping[str, Any],
        io_id: str,
        ext_name: str,
        qb_device: Mapping[str, Any] | None,
        is_input: bool | None,
    ) -> dict[str, Any]:
        return _build_io_entry(
            io_item,
            io_id,
            ext_name,
            self.io_types,
            self.io_input_types,
            self.options,
            self.io_live_states,
            qb_device=qb_device,
            is_input=is_input,
        )


def _build_io_entry(
    io_item: Mapping[str, Any],
    io_id: str,
    ext_name: str,
    io_types: Mapping[str, Any],
    io_input_types: Mapping[str, Any],
    options: ParseOptions,
    io_live_states: Mapping[str, Any],
    *,
    qb_device: Mapping[str, Any] | None = None,
    is_input: bool | None = None,
) -> dict[str, Any]:
    """Build one IO entry (without the extension's offline flag).

    io_id is the IO's "Id" in _strict_id's canonical form ("00211" -> "211"), the key the Quickbus
    mapping and the live states use as well.

    An IO of a Quickbus device (qb_device) is labelled with the device name instead of the
    extension name, like Comexio Studio's "MT1 # G20"; the {ExtName} schema placeholder then
    renders that device name as well. is_input overrides the direction derived from the
    identifier and $IOInputTypes (a Quickbus IO brings its own, see _quickbus_io_direction).
    """
    # A numeric identifier is stringified, not replaced — it feeds unique_id/entity_id.
    ident = _label(io_item.get("Identifier")) or io_id
    raw_desc = io_item.get("Description")
    description = _io_description(raw_desc if isinstance(raw_desc, str) else "", ident)
    type_info = _io_type_row(io_types, io_item.get("InOutputTypeId"))
    is_binary = type_info.get("binary", False)
    v_min, v_max = _type_bounds(type_info, ext_name, ident)
    ident_upper = ident.upper()

    try:
        type_id_raw = int(io_item.get("InOutputTypeId", 1))
    except (ValueError, TypeError, OverflowError):  # OverflowError: JSON Infinity
        type_id_raw = 1

    # Fallback classification when the IO type table has no usable row for this IO — not only
    # when the whole table is unavailable: a missing row would otherwise make a QI1 "analog, max 1".
    if not type_info:
        if io_types:
            _LOGGER.debug(
                "IO %s %s: no usable row for type %s in the IO type table — classified by identifier",
                _log_safe(ext_name),
                _log_safe(ident),
                _short_repr(io_item.get("InOutputTypeId")),
            )
        if _ANALOG_OUTPUT_IO_RE.match(ident_upper):
            is_binary, v_max = False, 0
        elif _BINARY_IO_RE.match(ident_upper):
            is_binary, v_max = True, 1

    owner = qb_device["name"] if qb_device else ext_name
    io_name = f"{owner} {ident} {description}" if description else f"{owner} {ident}"

    ha_name = options.schema_io.format_map(
        SafeDict(ServerAlias=options.server_alias, ExtName=owner, IoId=ident, IoTitle=description or NO_NAME_TITLE)
    )
    if is_input is None:
        is_input = _io_direction(ident_upper, type_id_raw, io_input_types)
    return {
        "id": io_id,
        "ext_name": ext_name,
        "identifier": ident,
        "ha_name": " ".join(ha_name.split()),
        "name": io_name,
        "is_binary": is_binary,
        "is_input": is_input,
        "unit": normalize_io_unit(type_info.get("unit", "")),
        "min": v_min,
        "max": v_max,
        "type_id_raw": type_id_raw,
        # None is "no reading" (like an absent IO), not a non-numeric one: the config's "Value" stays.
        "value": clean_value(live if (live := io_live_states.get(io_id)) is not None else io_item.get("Value", 0)),
        # Inactive IOs carry this flag so a plan renderer can grey the pill.
        "inactive": not io_item.get("Active"),
        # Its identifier (G1, TL1, ...) repeats per device, so only the IO "Id" is unique.
        "quickbus": {"device_id": qb_device["id"], "name": qb_device["name"]} if qb_device else None,
    }
