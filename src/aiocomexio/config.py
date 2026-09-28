"""Parsing of the scraped Comexio configuration into markers, IOs, KNX objects and Web-IO commands.

Pure logic: the inputs are the dicts the scrape helpers return ($FubModules, $WebDevices, ...
from the function module page, the IO type tables from the main admin page, the KNX DPT
catalog) plus optional live values. Nothing here touches the network.
"""

import logging
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, fields
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
_KEY_DPT_AMBIGUOUS = "dpt_ambiguous"

# $FubModules keys of the three source categories and of the Web-IO command groups.
_MODULE_IO = "1"
_MODULE_MARKER = "2"
_MODULE_WEBIO = "10"
_MODULE_KNX = "11"

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
        for field in fields(self):
            if field.name.startswith("schema_"):
                _validate_schema(field.name, getattr(self, field.name))


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
    """Convert a Comexio value to float, accepting a German decimal comma; 0.0 if not numeric."""
    if val is None:
        return 0.0
    if isinstance(val, str):
        val = val.replace(",", ".")
    try:
        return float(val)
    except (ValueError, TypeError):
        _LOGGER.warning("Failed to clean value: %s", val)
        return 0.0


def is_extension_offline(identifier: str | None) -> bool:
    """Return True when an extension's Identifier indicates an offline module.

    Online extensions report a serial number in 'XXXX-XXXX-XXXX' format; offline ones carry
    only a short model code without dashes (e.g. '5010'). An empty or missing identifier is
    also treated as offline.
    """
    return not isinstance(identifier, str) or "-" not in identifier


def _label(value: Any) -> str:
    """A scraped label as str: a number is stringified, anything else non-string (or empty) is ""."""
    return str(value) if value and isinstance(value, (str, int)) else ""


def _table_entry(table: Mapping[str, Any], key: Any) -> Mapping[str, Any]:
    """table[str(key)] if that is a mapping, else {} — a malformed type-table row must not crash parsing."""
    entry = table.get(str(key))
    return entry if isinstance(entry, Mapping) else {}


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


def io_schema_title(desc: str, ident: str) -> str:
    """{IoTitle} for the entity-name schema: the IO's description, or "#nn" if there is none.

    A description that is empty or merely repeats the identifier carries no information —
    using it would render "AI1 AI1" under the default schema — so both get the placeholder
    unnamed markers use.
    """
    title = (desc or "").strip()
    if not title or title.casefold() == ident.strip().casefold():
        return NO_NAME_TITLE
    return title


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


def normalize_io_unit(unit: str) -> str:
    """Normalize Comexio IO unit strings ("\\u00b0C", "0/1", "?", ...)."""
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
) -> dict[str, Any]:
    """Parse the scraped function module page config into markers, IOs, KNX objects and Web-IO commands.

    conf: the scraped function module page (scrape.scrape_js_vars), with FubModules and WebDevices.
    io_types / io_input_types: the main admin page's type tables (scrape.parse_io_types /
    parse_io_input_types); without io_types, IOs are classified by their identifier.
    live_states / knx_live_states: id-keyed live values of markers and KNX objects. Kept as two
    separate mappings because markers and KNX objects share the same plain numeric id space.
    referenced_markers: ids of unnamed markers wired into a function plan — imported anyway
    with a "#nn" title.
    knx_dpt_catalog: the scraped KNX admin page; attaches DPT range/unit/device class metadata
    and DPT3.x composite pairing to KNX items. Without it (None or {}) KNX items carry none of
    that — no knx_composite tags either. A consumer that builds entities from those tags should
    keep passing its last good catalog when a catalog fetch fails, or the pairs fall apart.

    Returns a dict with the keys markers, io (active IOs), io_all (every IO, for labels), knx,
    webio_commands (the consumer's own classes, keyed by command name), webio_names (label
    lexicon over ALL Web-IO devices), webio_devices (one entry per WebioClass) and extensions.
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
        "webio_names": {},
        "webio_devices": {cls: {"device_id": None, "device_ip": None, "base_id": None} for cls in WEBIO_CLASSES},
        # Per-extension identity (name + stable serial), lets a consumer detect a Comexio-side rename.
        "extensions": {},
    }
    if io_types is None and (fub_modules.get(_MODULE_IO) or fub_modules.get(_MODULE_KNX)):
        _LOGGER.warning(
            "parse_config called without io_types — IOs are classified by their identifier only "
            "and every KNX object is flagged dpt_ambiguous"
        )
    io_types = io_types or {}

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
    _process_ios(data, fub_modules, io_types, io_input_types or {}, options)
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
    except (ValueError, TypeError):
        val_type = 1

    name = w_obj.get("Name")
    if not isinstance(name, str):
        # An empty-string name is kept on purpose: the consumer's audit sees it as a surplus
        # command in its own class and can delete it. A non-string one has no usable key.
        _LOGGER.warning("Skipping Web-IO command %s (webio_class=%s) with non-string name %r", w_id, webio_class, name)
        return

    # webIoId (w_id) is a global counter across ALL Web-IO devices on the server — safe to key
    # this single flat dict by command name regardless of webio_class.
    data[_KEY_WEBIO_COMMANDS][name] = {
        "webIoId": w_id,
        "cmdId": w_obj.get("WebCommandId"),
        "typeId": val_type,
        "webioClass": webio_class,
    }


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
    simple {1,2,3} marker scale. A KNX Type without any io_types entry is reported unresolved
    and defaults to "digital" — the safer of the two, it never exposes a possibly-binary
    datapoint to analog writes.
    """
    if module_key == _MODULE_KNX:
        if str(type_raw) not in io_types:
            return TYPE_DIGITAL, True
        return (TYPE_DIGITAL if _table_entry(io_types, type_raw).get("binary", False) else TYPE_ANALOG), False
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
    ext_serial = ext_meta.get("Identifier")
    return ext_meta.get("Name", f"Ext{ext_id}"), ext_serial if isinstance(ext_serial, str) else ""


def _process_ios(
    data: dict[str, Any],
    fub_modules: Mapping[str, Any],
    io_types: Mapping[str, Any],
    io_input_types: Mapping[str, Any],
    options: ParseOptions,
) -> None:
    """Physical IOs from $FubModules["1"] (one group per extension).

    Inactive IOs (Active=False) land only in "io_all" — Comexio itself refuses to wire a
    connection to an inactive IO, but a plan renderer still needs their label.
    """
    for ext_id, ext_content in iter_group(fub_modules.get(_MODULE_IO)):
        if not isinstance(ext_content, Mapping):
            _LOGGER.debug("Skipping non-dict extension entry %s: %r", ext_id, ext_content)
            continue
        ext_name, ext_serial = _extension_meta(ext_id, ext_content)
        data["extensions"][ext_id] = {"name": ext_name, "serial": ext_serial}
        ext_offline = is_extension_offline(ext_serial)

        for io_id, io_item in iter_group(ext_content.get("inoutput")):
            if not isinstance(io_item, Mapping) or not io_item:
                _LOGGER.debug("Skipping empty/non-dict IO entry %s of extension %s: %r", io_id, ext_name, io_item)
                continue
            entry = _build_io_entry(io_item, ext_name, io_types, io_input_types, options)
            entry["offline"] = ext_offline
            data["io_all"].append(entry)
            if not entry["inactive"]:
                data["io"].append(entry)


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


def _build_io_entry(
    io_item: Mapping[str, Any],
    ext_name: str,
    io_types: Mapping[str, Any],
    io_input_types: Mapping[str, Any],
    options: ParseOptions,
) -> dict[str, Any]:
    """Build one IO entry (without the extension's offline flag)."""
    # A numeric identifier is stringified, not replaced — it feeds unique_id/entity_id.
    ident = _label(io_item.get("Identifier")) or str(io_item.get("Id", "unknown"))
    desc = io_item.get("Description")
    if not isinstance(desc, str) or not desc:
        desc = ident
    type_info = _table_entry(io_types, io_item.get("InOutputTypeId"))
    is_binary = type_info.get("binary", False)
    v_max = type_info.get("max", 1)
    ident_upper = ident.upper()

    try:
        type_id_raw = int(io_item.get("InOutputTypeId", 1))
    except (ValueError, TypeError):
        type_id_raw = 1

    # Fallback classification when the IO type table is unavailable.
    if not io_types:
        if _ANALOG_OUTPUT_IO_RE.match(ident_upper):
            is_binary, v_max = False, 0
        elif _BINARY_IO_RE.match(ident_upper):
            is_binary, v_max = True, 1

    has_desc = bool(desc.strip()) and desc != ident
    io_name = f"{ext_name} {ident} {desc.strip()}" if has_desc else f"{ext_name} {ident}"

    ha_name = options.schema_io.format_map(
        SafeDict(ServerAlias=options.server_alias, ExtName=ext_name, IoId=ident, IoTitle=io_schema_title(desc, ident))
    )
    return {
        "id": str(io_item.get("Id")),
        "ext_name": ext_name,
        "identifier": ident,
        "ha_name": " ".join(ha_name.split()),
        "name": io_name,
        "is_binary": is_binary,
        "is_input": _io_direction(ident_upper, type_id_raw, io_input_types),
        "unit": normalize_io_unit(type_info.get("unit", "")),
        "min": type_info.get("min", 0),
        "max": v_max,
        "type_id_raw": type_id_raw,
        "value": clean_value(io_item.get("Value", 0)),
        # Inactive IOs carry this flag so a plan renderer can grey the pill.
        "inactive": not io_item.get("Active"),
    }
