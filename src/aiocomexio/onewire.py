"""1-Wire sensors of the Comexio: the configured sensors ($FubModules["12"]) and their live status.

The address of a sensor is its Prefix plus its Id (OT1, OH3, OVAD6, ...). The status answer of the
1-Wire monitor (read1WireStati) names a sensor by the same address, so the two join on it. A sensor
is digital when its Binary flag is 1 and analog when it is 2 (see the module constants).
"""

import logging
import math
import re
import reprlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .config import TYPE_ANALOG, TYPE_DIGITAL, _strict_id, iter_group
from .exceptions import ComexioDataError

_LOGGER = logging.getLogger(__name__)

ONEWIRE_GROUP = "12"

# One counter per extension block of the IO home page (/admin/in_output/home): every <div> with an id
# number_of_<ext> and a class number_of_type_<n>. The 1-Wire count is the class number_of_type_30
# (the Quickbus and KNX counts carry suffixes _30_2 / _30_3). Attribute order, quote style, whitespace
# and extra classes do not matter, so the tag is matched first and its attributes checked separately.
_COUNTER_TAG_RE = re.compile(r"<div\b([^>]*)>\s*(\d+)\s*</div>", re.ASCII)
_OPENING_DIV_RE = re.compile(r"<div\b([^>]*)>", re.ASCII)
_COUNTER_ID_RE = re.compile(r"""\sid\s*=\s*(["'])number_of_\w+\1""", re.ASCII)
_CLASS_ATTR_RE = re.compile(r"""\sclass\s*=\s*(["'])([^"']*)\1""", re.ASCII)
_ONEWIRE_CLASS = "number_of_type_30"
_ANY_TYPE_COUNTER = "number_of_type_"
# A count with more digits than this is a changed page, not a device count.
_MAX_COUNT_DIGITS = 6
# A reading as Comexio writes it: optional sign, digits, optional decimal point or comma, optional exponent.
_READING_RE = re.compile(r"[+-]?\d+(?:[.,]\d+)?(?:[eE][+-]?\d+)?", re.ASCII)
# Longest excerpt of an external value that goes into an error message.
_EXCERPT_MAX_CHARS = 80
# Builds a bounded repr: a large or deeply nested value is never rendered in full first.
_EXCERPT_REPR = reprlib.Repr()
_EXCERPT_REPR.maxstring = _EXCERPT_MAX_CHARS
_EXCERPT_REPR.maxother = _EXCERPT_MAX_CHARS

# Value of the Binary flag of a configured 1-Wire sensor. Observed on a Comexio 11.1.5 with one
# digital (flag 1) and nine analog (flag 2) sensors; in the logic editor the digital one shows a
# triangle pin, the analog ones round pins. Only one digital sample so far.
BINARY_FLAG_DIGITAL = 1
BINARY_FLAG_ANALOG = 2
_KIND_BY_BINARY_FLAG = {BINARY_FLAG_DIGITAL: TYPE_DIGITAL, BINARY_FLAG_ANALOG: TYPE_ANALOG}

_ACTIVE_KEY = "Active"
_ID_KEY = "Id"
_PREFIX_KEY = "Prefix"
_NAME_KEY = "Name"
_BINARY_KEY = "Binary"
_HW_ID_KEY = "HwId"

_STATUS_ID_KEY = "id"
_STATUS_DEVICE_KEY = "device_id"
_STATUS_NAME_KEY = "longname"
_STATUS_ONLINE_KEY = "online"
_STATUS_VISIBLE_KEY = "visible"
_STATUS_VALUE_KEY = "value"


@dataclass(frozen=True, slots=True, kw_only=True)
class OneWireSensor:
    """One active 1-Wire sensor of the configuration.

    address is Prefix + Id, the name the status answer uses too. kind is TYPE_DIGITAL or
    TYPE_ANALOG from the Binary flag, None if the flag is neither 1 nor 2.
    """

    address: str
    config_id: int
    prefix: str
    name: str
    hw_id: str
    kind: str | None


@dataclass(frozen=True, slots=True, kw_only=True)
class OneWireStatus:
    """The live state of one 1-Wire sensor from read1WireStati.

    online False means a read error (CRC), visible False that the sensor was not found. value is
    the reading as a float, None if the answer carried none, a non-finite one, or one that is not
    a number at all (logged).
    """

    address: str
    device_id: str
    name: str
    online: bool
    visible: bool
    value: float | None

    @property
    def ok(self) -> bool:
        """True when the sensor is found and answers without a read error."""
        return self.visible and self.online


def parse_onewire_device_count(io_home_html: str) -> int:
    """The number of 1-Wire devices the IO home page counts, summed over all extension blocks.

    0 means there is no 1-Wire device, so the status need not be polled. A page without any
    device counter is a changed page, not an installation without devices: that raises
    ComexioDataError, so a caller never stops polling on a silent 0.
    """
    if _ANY_TYPE_COUNTER not in io_home_html:
        raise ComexioDataError("IO home page carries no device counters, the 1-Wire count is unknown")
    counters = [
        (attrs, digits) for attrs, digits in _COUNTER_TAG_RE.findall(io_home_html) if _COUNTER_ID_RE.search(attrs)
    ]
    onewire_counters = [(attrs, digits) for attrs, digits in counters if _is_onewire_counter(attrs)]
    # A 1-Wire counter whose content is not a plain number is not in onewire_counters; it must not be dropped
    # silently. Counters of other types are not read, so their markup does not matter.
    onewire_tags = [
        attrs
        for attrs in _OPENING_DIV_RE.findall(io_home_html)
        if _COUNTER_ID_RE.search(attrs) and _is_onewire_counter(attrs)
    ]
    if not counters or len(onewire_counters) != len(onewire_tags):
        raise ComexioDataError("IO home page counters have an unknown markup, the 1-Wire count is unknown")
    return sum(_count_of(digits) for _attrs, digits in onewire_counters)


def _is_onewire_counter(attrs: str) -> bool:
    """True when the class attribute holds the token number_of_type_30 (not a suffixed Quickbus or KNX class)."""
    match = _CLASS_ATTR_RE.search(attrs)
    return match is not None and _ONEWIRE_CLASS in match.group(2).split()


def _count_of(digits: str) -> int:
    """The counter value; an implausibly long number is a changed page, not a count."""
    if len(digits) > _MAX_COUNT_DIGITS:
        raise ComexioDataError(f"IO home page counter has {len(digits)} digits, not a device count")
    return int(digits)


def _bounded_repr(value: Any) -> str:
    """repr of an external value, cut to _EXCERPT_MAX_CHARS so an error message stays short."""
    text = _EXCERPT_REPR.repr(value)
    return text if len(text) <= _EXCERPT_MAX_CHARS else f"{text[:_EXCERPT_MAX_CHARS]}..."


def _is_flag(value: Any, flag: int) -> bool:
    """True for the integer flag, not for a bool (bool is an int subclass in Python)."""
    return isinstance(value, int) and not isinstance(value, bool) and value == flag


def _is_active(value: Any) -> bool:
    """Active is 1 or the boolean True."""
    return value is True or _is_flag(value, 1)


def _kind_of(binary: Any) -> str | None:
    if not isinstance(binary, int) or isinstance(binary, bool):
        return None
    return _KIND_BY_BINARY_FLAG.get(binary)


def _is_address_prefix(prefix: str) -> bool:
    """ASCII letters and digits only, so that Prefix + Id maps to exactly one address."""
    return prefix.isascii() and prefix.isalnum()


def _text_of(item: Mapping[str, Any], key: str) -> str:
    """A display text field: absent or null is "", a string is taken, any other type is logged and shown blank."""
    value = item.get(key)
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    _LOGGER.warning("1-Wire field %s has the unexpected type %s, shown blank", key, type(value).__name__)
    return ""


def _sensor_from_entry(entry: Mapping[str, Any]) -> OneWireSensor | None:
    """The sensor of one active $FubModules["12"] entry, None if it lacks a usable address."""
    prefix, canonical = entry.get(_PREFIX_KEY), _strict_id(entry.get(_ID_KEY))
    if not isinstance(prefix, str) or not _is_address_prefix(prefix) or canonical is None:
        return None
    return OneWireSensor(
        address=f"{prefix}{canonical}",
        config_id=int(canonical),
        prefix=prefix,
        name=_text_of(entry, _NAME_KEY),
        hw_id=_text_of(entry, _HW_ID_KEY),
        kind=_kind_of(entry.get(_BINARY_KEY)),
    )


def _group_mappings(group: Any) -> list[Mapping[str, Any]]:
    """The object entries of group 12. A null is a placeholder; other non-objects are logged and skipped.

    A group that holds entries but no object at all is a changed config and raises.
    """
    # A null is a placeholder and not counted as a changed entry.
    entries = [entry for _group_id, entry in iter_group(group) if entry is not None]
    mappings = [entry for entry in entries if isinstance(entry, Mapping)]
    others = len(entries) - len(mappings)
    if others and not mappings:
        raise ComexioDataError(f"$FubModules group {ONEWIRE_GROUP} holds {others} entries, none an object")
    if others:
        _LOGGER.warning("Skipped %d non-object 1-Wire config entries", others)
    return mappings


def parse_onewire_config(fub_modules: Mapping[str, Any]) -> dict[str, OneWireSensor]:
    """The active 1-Wire sensors of $FubModules, keyed by address.

    Inactive entries are left out. An active entry without a usable Prefix and Id is logged and
    skipped; if active entries exist but none is usable, that is a changed config and raises
    ComexioDataError rather than reporting no sensors. A group that holds entries but no mapping at
    all is a changed config too and raises. A sensor without a usable Binary flag stays with kind
    None; the caller decides what to do with it.
    """
    group = fub_modules.get(ONEWIRE_GROUP)
    if group is not None and not isinstance(group, Mapping | list):
        raise ComexioDataError(f"$FubModules group {ONEWIRE_GROUP} has an unsupported shape: {type(group).__name__}")
    sensors: dict[str, OneWireSensor] = {}
    active = 0
    unusable = 0
    for entry in _group_mappings(group):
        if not _is_active(entry.get(_ACTIVE_KEY)):
            continue
        active += 1
        sensor = _sensor_from_entry(entry)
        if sensor is None:
            unusable += 1
            _LOGGER.debug(
                "Unusable active 1-Wire entry: Prefix %s, Id %s",
                _bounded_repr(entry.get(_PREFIX_KEY)),
                _bounded_repr(entry.get(_ID_KEY)),
            )
            continue
        if sensor.address in sensors:
            _LOGGER.warning(
                "Two active 1-Wire entries share the address %s, the later one wins", _bounded_repr(sensor.address)
            )
        sensors[sensor.address] = sensor
    if unusable:
        _LOGGER.warning("Skipped %d active 1-Wire entries without a usable Prefix and Id", unusable)
    if active and not sensors:
        raise ComexioDataError(f"None of the {active} active 1-Wire entries has a usable Prefix and Id")
    return sensors


def _number_of(raw: Any) -> float | None:
    """The raw reading as a float; None if its type or text is not a number Comexio writes.

    A JSON number is taken as it is; a string must look like a reading (a decimal comma is
    accepted, underscores and other forms are not). A number too large for a float is infinite.
    """
    if isinstance(raw, bool):
        return None
    if isinstance(raw, str):
        if _READING_RE.fullmatch(raw) is None:
            return None
        raw = raw.replace(",", ".")
    elif not isinstance(raw, int | float):
        return None
    try:
        return float(raw)
    except OverflowError:
        return math.inf


def _finite_or_none(raw: Any, address: str) -> float | None:
    """The reading as a finite float; None for an absent or non-finite reading.

    An empty string is the "no value yet" form and passes silently. A present value that is not a
    number at all is logged with its sensor and type (its content only at DEBUG) and read as None,
    so a changed answer shows up in the log instead of passing as a silently missing value.
    Non-finite numbers (NaN, Infinity, too large) are None by design and are not logged.
    """
    if raw is None or raw == "":
        return None
    number = _number_of(raw)
    if number is None:
        _LOGGER.warning(
            "1-Wire reading of %s is not a number (type %s), shown as missing",
            _bounded_repr(address),
            type(raw).__name__,
        )
        _LOGGER.debug("1-Wire reading of %s: %s", _bounded_repr(address), _bounded_repr(raw))
        return None
    return number if math.isfinite(number) else None


def _status_flag(item: Mapping[str, Any], key: str, address: str) -> bool:
    """A status flag, which must be a JSON boolean: a missing or other value is a changed answer."""
    value = item.get(key)
    if not isinstance(value, bool):
        raise ComexioDataError(
            f"1-Wire status of {_bounded_repr(address)} has no boolean {key}: {_bounded_repr(value)}"
        )
    return value


def _status_from_item(item: Mapping[str, Any]) -> OneWireStatus | None:
    """One status item, None if it carries no address."""
    address = item.get(_STATUS_ID_KEY)
    if not isinstance(address, str) or not address:
        return None
    return OneWireStatus(
        address=address,
        device_id=_text_of(item, _STATUS_DEVICE_KEY),
        name=_text_of(item, _STATUS_NAME_KEY),
        online=_status_flag(item, _STATUS_ONLINE_KEY, address),
        visible=_status_flag(item, _STATUS_VISIBLE_KEY, address),
        value=_finite_or_none(item.get(_STATUS_VALUE_KEY), address),
    )


def parse_onewire_status(payload: Any) -> dict[str, OneWireStatus]:
    """The read1WireStati answer (a JSON list of sensor objects), keyed by address.

    Raises ComexioDataError if the answer is not a list at all, if a flag is not a boolean, or if
    the list is not empty but no item carries an address. An empty list is a valid answer (no
    sensors answered).
    """
    if not isinstance(payload, list):
        raise ComexioDataError(f"1-Wire status answer is not a list: {type(payload).__name__}")
    states: dict[str, OneWireStatus] = {}
    skipped = 0
    for item in payload:
        status = _status_from_item(item) if isinstance(item, Mapping) else None
        if status is None:
            skipped += 1
            continue
        if status.address in states:
            _LOGGER.warning(
                "Two 1-Wire status items share the address %s, the later one wins", _bounded_repr(status.address)
            )
        states[status.address] = status
    if skipped:
        _LOGGER.warning("Skipped %d 1-Wire status items without an address", skipped)
    if payload and not states:
        raise ComexioDataError(f"1-Wire status answer has {len(payload)} items, none with an address")
    return states
