"""Comexio extension: the settings of its edit form (/admin/extension/edit/), above all the IP address.

The edit form of an extension carries its name, serial number, type id and "IP-Adresse (optional)"
(`extension_location`). That field holds `local` for an extension on the IO-Server's own bus and the IP address for
one that has an address of its own, such as a Doorstation (kayl-codes/homeassistant-comexio#180). A Doorstation
serves its camera stream under that address.

Reading the form is the client's job (a browser-like html.parser, see ComexioClient.get_extension); this module turns
the values of its inputs into an ExtensionInfo. The form also has hidden login rows (class `ext_login_data`:
user name and password fields of the extension); they are never read, and no credential reaches ExtensionInfo.
"""

import ipaddress
from collections.abc import Mapping
from dataclasses import dataclass

from .config import _strict_id
from .doorstation import _positive_id
from .exceptions import ComexioDataError

# The ids of the inputs of the edit form this module reads; a parser keeps only these.
NAME_INPUT = "extension_name"
SERIAL_INPUT = "extension_serial"
LOCATION_INPUT = "extension_location"
TYPE_ID_INPUT = "extension_type_id"
EXTENSION_INPUTS = (NAME_INPUT, SERIAL_INPUT, LOCATION_INPUT, TYPE_ID_INPUT)

# The type id of a Doorstation: the hidden input extension_type_id of its edit form, and the class device_type_16 of
# its tab on the IO home page.
DOORSTATION_TYPE_ID = 16

_BROADCAST = ipaddress.IPv4Address("255.255.255.255")


@dataclass(frozen=True, slots=True, kw_only=True)
class ExtensionInfo:
    """The settings of one extension, from its edit form.

    type_id is the numeric type of the extension (DOORSTATION_TYPE_ID for a Doorstation). name and serial are
    stripped and None when the form holds none (an extension learned offline has no serial number; both are
    informational, so an input the form lacks reads like an empty one). location is the raw value of the
    "IP-Adresse (optional)" field, None when the input carries no value; ip is that value if it is a usable IPv4
    address (None otherwise, see extension_ip).
    """

    ext_id: int
    type_id: int
    name: str | None
    serial: str | None
    location: str | None

    @property
    def ip(self) -> str | None:
        """The IPv4 address of the extension; None for `local`, an empty field or anything else (see extension_ip)."""
        return extension_ip(self.location)

    @property
    def is_doorstation(self) -> bool:
        """Whether the extension is a Doorstation (its type id is DOORSTATION_TYPE_ID)."""
        return self.type_id == DOORSTATION_TYPE_ID


def extension_ip(location: str | None) -> str | None:
    """location as an IPv4 address in its canonical form, None if it is none.

    Surrounding whitespace is ignored. `local`, an empty value, a host name, an IPv6 address, a malformed address
    (a missing octet, an octet above 255, one with a leading zero, non-ASCII digits) and an address no device can
    have (0.0.0.0, loopback, multicast, the broadcast address) give None: None means "no usable IPv4 address", so a
    caller that must tell `local` from a typo reads ExtensionInfo.location, which keeps the raw value.
    """
    if location is None:
        return None
    try:
        address = ipaddress.IPv4Address(location.strip())
    except ValueError:
        return None
    if address.is_unspecified or address.is_loopback or address.is_multicast or address == _BROADCAST:
        return None
    return str(address)


def require_ext_id(ext_id: object) -> int:
    """ext_id if it is a positive Comexio id; a non-int (a bool too) raises TypeError, any other value ValueError."""
    if isinstance(ext_id, bool) or not isinstance(ext_id, int):
        raise TypeError(f"ext_id must be an int, not {type(ext_id).__name__}")
    if _strict_id(ext_id) is None or ext_id <= 0:
        raise ValueError("ext_id must be a positive Comexio id")
    return ext_id


def extension_from_inputs(ext_id: int, inputs: Mapping[str, str | None]) -> ExtensionInfo:
    """The ExtensionInfo of an edit form whose inputs (by id, see EXTENSION_INPUTS) have these values.

    A missing key means the form has no such input, a None value an input without a value attribute. A form with
    no usable type id is no edit form of an existing extension: Comexio answers an unknown id with the form for a new
    extension (no type id), so that raises ComexioDataError, as does a form without the IP address field.
    """
    raw_type_id = inputs.get(TYPE_ID_INPUT)
    type_id = _positive_id(raw_type_id)
    if type_id is None:
        found = "none" if raw_type_id is None else repr(raw_type_id)
        raise ComexioDataError(
            f"Extension {ext_id} edit form names no extension type (found {found}): unknown id or changed page"
        )
    if LOCATION_INPUT not in inputs:
        raise ComexioDataError(f"Extension {ext_id} edit form has no IP address field: changed page")
    return ExtensionInfo(
        ext_id=ext_id,
        type_id=type_id,
        name=_text_or_none(inputs.get(NAME_INPUT)),
        serial=_text_or_none(inputs.get(SERIAL_INPUT)),
        location=inputs[LOCATION_INPUT],
    )


def _text_or_none(value: str | None) -> str | None:
    """value without surrounding whitespace, None if nothing is left (or there was no value)."""
    return (value or "").strip() or None
