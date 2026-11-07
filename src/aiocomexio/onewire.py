"""1-Wire sensors of the Comexio: the configured sensors ($FubModules["12"]) and their live status.

The address of a sensor is its Prefix plus its Id (OT1, OH3, OVAD6, ...). The status answer of the
1-Wire monitor (read1WireStati) names a sensor by the same address, so the two join on it. A sensor
is digital when its Binary flag is 1 and analog when it is 2 (see the module constants).
"""

import logging
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .config import TYPE_ANALOG, TYPE_DIGITAL, iter_group
from .exceptions import ComexioDataError

_LOGGER = logging.getLogger(__name__)

ONEWIRE_GROUP = "12"

# One counter per extension block of the IO home page (/admin/in_output/home): the 1-Wire count is
# the one with the class number_of_type_30 (the Quickbus and KNX counts carry suffixes _30_2 / _30_3).
_ONEWIRE_COUNT_RE = re.compile(r'<div id="number_of_\w+" class="number_of_type_30">(\d+)</div>', re.ASCII)
# A reading as Comexio writes it: optional sign, digits, optional decimal point or comma, optional exponent.
_READING_RE = re.compile(r"[+-]?\d+(?:[.,]\d+)?(?:[eE][+-]?\d+)?", re.ASCII)
_ANY_TYPE_COUNTER = 'class="number_of_type_'

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
    the reading as a float, None if the answer carried none (or a non-finite one).
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
    return sum(int(count) for count in _ONEWIRE_COUNT_RE.findall(io_home_html))


def _is_flag(value: Any, flag: int) -> bool:
    """True for the integer flag, not for a bool (bool is an int subclass in Python)."""
    return isinstance(value, int) and not isinstance(value, bool) and value == flag


def _kind_of(binary: Any) -> str | None:
    if not isinstance(binary, int) or isinstance(binary, bool):
        return None
    return _KIND_BY_BINARY_FLAG.get(binary)


def _is_address_prefix(prefix: str) -> bool:
    """ASCII letters and digits only, so that Prefix + Id maps to exactly one address."""
    return prefix.isascii() and prefix.isalnum()


def _sensor_from_entry(entry: Mapping[str, Any]) -> OneWireSensor | None:
    """The sensor of one active $FubModules["12"] entry, None if it lacks a usable address."""
    prefix, config_id = entry.get(_PREFIX_KEY), entry.get(_ID_KEY)
    if not isinstance(prefix, str) or not _is_address_prefix(prefix):
        return None
    if isinstance(config_id, bool) or not isinstance(config_id, int):
        return None
    name, hw_id = entry.get(_NAME_KEY), entry.get(_HW_ID_KEY)
    return OneWireSensor(
        address=f"{prefix}{config_id}",
        config_id=config_id,
        prefix=prefix,
        name=name if isinstance(name, str) else "",
        hw_id=hw_id if isinstance(hw_id, str) else "",
        kind=_kind_of(entry.get(_BINARY_KEY)),
    )


def parse_onewire_config(fub_modules: Mapping[str, Any]) -> dict[str, OneWireSensor]:
    """The active 1-Wire sensors of $FubModules, keyed by address.

    Inactive entries are left out. An active entry without a usable Prefix and Id is logged and
    skipped; if active entries exist but none is usable, that is a changed config and raises
    ComexioDataError rather than reporting no sensors. A sensor without a usable Binary flag stays
    with kind None; the caller decides what to do with it.
    """
    sensors: dict[str, OneWireSensor] = {}
    active = 0
    unusable = 0
    for _group_id, entry in iter_group(fub_modules.get(ONEWIRE_GROUP)):
        if not isinstance(entry, Mapping) or not _is_flag(entry.get(_ACTIVE_KEY), 1):
            continue
        active += 1
        sensor = _sensor_from_entry(entry)
        if sensor is None:
            unusable += 1
            continue
        if sensor.address in sensors:
            _LOGGER.warning("Two active 1-Wire entries share the address %r, the later one wins", sensor.address)
        sensors[sensor.address] = sensor
    if unusable:
        _LOGGER.warning("Skipped %d active 1-Wire entries without a usable Prefix and Id", unusable)
    if active and not sensors:
        raise ComexioDataError(f"None of the {active} active 1-Wire entries has a usable Prefix and Id")
    return sensors


def _finite_or_none(raw: Any) -> float | None:
    """The reading as a finite float; None for absent, non-numeric or non-finite answers.

    A JSON number is taken as it is; a string must look like a reading (a decimal comma is
    accepted, underscores and other forms are not).
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
        number = float(raw)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def _status_flag(item: Mapping[str, Any], key: str, address: str) -> bool:
    """A status flag, which must be a JSON boolean: a missing or other value is a changed answer."""
    value = item.get(key)
    if not isinstance(value, bool):
        raise ComexioDataError(f"1-Wire status of {address} has no boolean {key}: {value!r}")
    return value


def _status_from_item(item: Mapping[str, Any]) -> OneWireStatus | None:
    """One status item, None if it carries no address."""
    address = item.get(_STATUS_ID_KEY)
    if not isinstance(address, str) or not address:
        return None
    name, device_id = item.get(_STATUS_NAME_KEY), item.get(_STATUS_DEVICE_KEY)
    return OneWireStatus(
        address=address,
        device_id=device_id if isinstance(device_id, str) else "",
        name=name if isinstance(name, str) else "",
        online=_status_flag(item, _STATUS_ONLINE_KEY, address),
        visible=_status_flag(item, _STATUS_VISIBLE_KEY, address),
        value=_finite_or_none(item.get(_STATUS_VALUE_KEY)),
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
    for item in payload:
        if not isinstance(item, Mapping):
            continue
        status = _status_from_item(item)
        if status is not None:
            if status.address in states:
                _LOGGER.warning("Two 1-Wire status items share the address %r, the later one wins", status.address)
            states[status.address] = status
    if payload and not states:
        raise ComexioDataError(f"1-Wire status answer has {len(payload)} items, none with an address")
    return states
