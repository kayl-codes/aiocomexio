"""Constants and enums shared by the config parser and (later) the client."""

import re
from enum import StrEnum

# Default entity-name schemas (str.format_map templates, see config.parse_config).
DEFAULT_SCHEMA_MARKER = "M{MarkerId} {MarkerTitle}"
# {ExtName} is left out on purpose: a consumer that groups IOs by extension already shows
# the extension name next to the entity name.
DEFAULT_SCHEMA_IO = "{IoId} {IoTitle}"
DEFAULT_SCHEMA_KNX = "K{KnxId} {KnxTitle}"

DEFAULT_WEBIO_NAME = "HomeAssistant"
DEFAULT_SERVER_ALIAS = "comexio"

# Title given to an unnamed marker/KNX object that is still wired into a function plan, and
# the {IoTitle} of an IO without a description — never empty, so no schema renders an empty
# name or a dangling separator.
NO_NAME_TITLE = "#nn"


class WebioClass(StrEnum):
    """The Web-IO device classes a consumer manages on the Comexio server, one per source category.

    The webIoId (the per-command key inside $FubModules["10"]) is a global counter across ALL
    Web-IO devices on a Comexio server, never reused per device, so commands from every class
    can share one flat lookup without ambiguity.
    """

    MARKER = "marker"
    IO = "io"
    KNX = "knx"


WEBIO_CLASSES = tuple(WebioClass)

# Suffix appended to the configured Web-IO name to build each class's device name.
WEBIO_CLASS_NAME_SUFFIX: dict[WebioClass, str] = {
    WebioClass.MARKER: " [M]",
    WebioClass.IO: " [IO]",
    WebioClass.KNX: " [KNX]",
}

# Short display label per class, used in log messages.
WEBIO_CLASS_LABEL: dict[WebioClass, str] = {
    WebioClass.MARKER: "Marker",
    WebioClass.IO: "IO",
    WebioClass.KNX: "KNX",
}


def webio_class_name(webio_name: str, webio_class: WebioClass) -> str:
    """Comexio Web-IO device name for one managed class, e.g. "HomeAssistant [M]"."""
    return f"{webio_name}{WEBIO_CLASS_NAME_SUFFIX[webio_class]}"


class MarkerKind(StrEnum):
    """How a marker/KNX object is meant to be exposed, derived from its Comexio-side title suffix."""

    NORMAL = "normal"
    READ_ONLY = "read_only"
    TRIGGER = "trigger"
    KNX_BRIDGE = "knx_bridge"


# Title suffix of a read-only marker (e.g. "Boiler Temp [RO]").
MARKER_READ_ONLY_SUFFIX = "[RO]"

# "Virtual push button" markers. "[TP]" (Time Pulse) is the legacy alias, "[TRIG]" the current name.
MARKER_TRIGGER_SUFFIXES = ("[TRIG]", "[TP]")

# Auto-created KNX write-path bridge markers are titled "<KNX title> [K<k_id>]".
MARKER_KNX_BRIDGE_SUFFIX_RE = re.compile(r"\[K\d+\]$")

# Analog markers have no configurable value range on the Comexio side, so their Web-IO
# datapoints must not clamp. Comexio's Web-IO push has two server-side bugs (reproduced
# 2026-08-30): (1) json_stringify() rounds numeric values to 6 significant digits, and
# (2) whenever a command's own Min/Max sit at/near the signed 16-bit boundary (~±32767/32768),
# EVERY pushed value is silently clamped to Max. ±500,000 dodges both: it clears the int16
# danger zone by a wide margin and never exceeds 6 significant digits.
WEBIO_MARKER_ANALOG_MIN = -500_000
WEBIO_MARKER_ANALOG_MAX = 500_000

# Any Web-IO range whose Min or Max falls in this band gets widened to
# WEBIO_MARKER_ANALOG_MIN/MAX (see bug (2) above).
WEBIO_INT16_DANGER_ZONE = (30_000, 40_000)

# KNX "API loopback": a second Web-IO command per K-element whose Lua script GETs the Comexio
# server's own /api/?action=set endpoint to write the K-element's bridge marker directly,
# closing the loop entirely inside Comexio. One class + one device per server; every
# K-element gets one command in it.
WEBIO_CLASS_NAME_KNX_LOOPBACK = "ComexioAPI"
WEBIO_DEVICE_NAME_KNX_LOOPBACK = "Comexio API - KNX Loopback"


def knx_loopback_command_name(k_id: int, marker_id: int) -> str:
    """Web-IO command name for one K-element's API-loopback command, e.g. "KNX K1 to M300"."""
    return f"KNX K{k_id} to M{marker_id}"
