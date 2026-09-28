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
