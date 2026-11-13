"""Comexio API settings: what the page /admin/api/home says about access through /api/.

The page is the one form behind Comexio's "Api" menu. It holds the on/off switch (`api_active`), the TCP interface
(`use_tcp`, `tcp_port`), the login (`use_login`, `username`, `password`) and, per data type, what /api/ may do:
`in_output` (IOs), `flag` (markers) and `one_wire` (1-Wire) each take 0 forbid, 1 read, 3 read and write; the markers
also 4, single selection, where the hidden input `api_special_2` lists the markers by id with a bit mask (1 read,
2 write, `null` for an id without a marker; its `edit_list` in api.js reads it so).

Reading the page is the client's job (a browser-like html.parser, see ComexioClient.get_api_settings); this module
turns what the parser found into an ApiSettings and says whether a write to a target is allowed. The page carries the
API password in clear text. It is never kept: the client compares it with the password it was created with, and the
result has only that verdict.
"""

import hmac
import json
import logging
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any

from .exceptions import ComexioDataError

_LOGGER = logging.getLogger(__name__)

# The ids and names of the form elements this module reads; a parser keeps only these.
API_FORM_ID = "apiform"
ACTIVE_RADIO = "api_active"
USE_TCP_INPUT = "use_tcp"
TCP_PORT_INPUT = "tcp_port"
USE_LOGIN_INPUT = "use_login"
USERNAME_INPUT = "username"
PASSWORD_INPUT = "password"  # nosec B105 - the id of a form field, not a password
MARKER_LIST_INPUT = "api_special_2"
IN_OUTPUT_SELECT = "in_output"
FLAG_SELECT = "flag"
ONE_WIRE_SELECT = "one_wire"
API_INPUT_IDS = (USE_TCP_INPUT, TCP_PORT_INPUT, USE_LOGIN_INPUT, USERNAME_INPUT, PASSWORD_INPUT, MARKER_LIST_INPUT)
API_SELECT_IDS = (IN_OUTPUT_SELECT, FLAG_SELECT, ONE_WIRE_SELECT)

ACTIVE_VALUE = "active"
INACTIVE_VALUE = "inactive"

# What a data type's select says /api/ may do with it.
ACCESS_FORBIDDEN = 0
ACCESS_READ = 1
ACCESS_READ_WRITE = 3
ACCESS_SELECTION = 4  # the markers only: the list of api_special_2 decides per marker
_ACCESS_NAMES = {
    ACCESS_FORBIDDEN: "forbidden",
    ACCESS_READ: "read only",
    ACCESS_READ_WRITE: "read and write",
    ACCESS_SELECTION: "single selection",
}

# The bits of an entry of the marker list.
RIGHT_READ = 1
RIGHT_WRITE = 2
_MAX_RIGHTS = RIGHT_READ | RIGHT_WRITE

# The targets of a write through /api/ (ComexioClient.set_marker_value, set_knx_value, set_io_value).
WRITE_TARGET_MARKER = "marker"
WRITE_TARGET_KNX = "knx"
WRITE_TARGET_IO = "io"

_WRITE_TARGETS = (WRITE_TARGET_MARKER, WRITE_TARGET_KNX, WRITE_TARGET_IO)
# What each select may be set to: the single selection exists for the markers only.
_PLAIN_ACCESS = (ACCESS_FORBIDDEN, ACCESS_READ, ACCESS_READ_WRITE)

_MAX_PORT = 65535
# Longest id of a marker in the list; a longer key is not an id.
_MAX_ID_DIGITS = 9
_UTF8 = "utf-8"
# What the client encodes the Basic Auth credentials in (ComexioClient._basic_auth_header).
_BASIC_AUTH_ENCODING = "iso-8859-1"

_MSG_INACTIVE = "the API is switched off (access: inactive)"
_MSG_CREDENTIALS_UNUSABLE = (
    "the API requires a login and the configured API credentials cannot be sent "
    "(empty user, ':' in the user, or characters outside ISO-8859-1)"
)
_MSG_USER_DIFFERS = "the API requires a login and its user differs from the configured API user"
_MSG_CREDENTIAL_DIFFERS = "the API requires a login and its password differs from the configured API password"


class _MarkerRights(Mapping[int, int]):
    """The marker list as a read-only mapping: a caller of the frozen ApiSettings cannot change what it reports.

    A class of its own, not a MappingProxyType: dataclasses.asdict() deep-copies it, which a mappingproxy refuses.
    """

    __slots__ = ("_rights",)

    def __init__(self, rights: Mapping[int, int]) -> None:
        self._rights = dict(rights)

    def __getitem__(self, marker_id: int) -> int:
        return self._rights[marker_id]

    def __iter__(self) -> Iterator[int]:
        return iter(self._rights)

    def __len__(self) -> int:
        return len(self._rights)

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self._rights!r})"


@dataclass(slots=True)
class ApiPageFields:
    """What a parser found on the API settings page; api_settings_from_fields turns it into an ApiSettings.

    active_choice is the value of the checked api_active radio (None: none is checked), inputs the attributes of the
    first input of each id in API_INPUT_IDS and selected the value of the selected option of each select in
    API_SELECT_IDS.
    """

    active_choice: str | None = None
    # No repr: the attributes of the password input hold the API password in clear text.
    inputs: dict[str, Mapping[str, str]] = field(default_factory=dict, repr=False)
    selected: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True, kw_only=True)
class ApiSettings:
    """The API settings of the IO-Server, from its API page.

    active is the on/off switch. tcp_enabled and tcp_port describe the TCP interface (tcp_port None if the field
    holds no usable port); whether the port matters for /api/ over HTTP is not known. login_required says /api/ asks
    for the login; username is its user. The password is not kept: username_matches and password_matches compare the
    login with the API credentials the client was created with (both True without a login, nothing to compare).
    in_output, flag and one_wire are the access of the data types (ACCESS_*); marker_rights maps a marker id to its
    bits (RIGHT_READ, RIGHT_WRITE) and is None if the page has no readable list.
    """

    active: bool
    tcp_enabled: bool
    tcp_port: int | None
    login_required: bool
    username: str = field(repr=False)
    username_matches: bool
    password_matches: bool
    in_output: int
    flag: int
    one_wire: int
    marker_rights: Mapping[int, int] | None
    # False if the configured API credentials give no Authorization header (see _credentials_usable).
    credentials_usable: bool = True

    def write_blockers(self, target: str, marker_id: int | None = None) -> tuple[str, ...]:
        """The settings that stop /api/ writing the target, each as a sentence; none: the settings allow it.

        target is WRITE_TARGET_MARKER (marker_id names the marker, if known), WRITE_TARGET_IO or WRITE_TARGET_KNX; any
        other text raises ValueError (an unknown target must not read as "nothing blocks it"). The page has no access
        setting for KNX objects, so only the switch and the login can stop those. WRITE_TARGET_IO is for an IO written
        over /api/ (a digital output Q* or DG*): the others go over the admin session, which these settings do not
        govern.
        """
        if target not in _WRITE_TARGETS:
            raise ValueError(f"unknown write target {target!r}")
        blockers = []
        if not self.active:
            blockers.append(_MSG_INACTIVE)
        blockers.extend(self._login_blockers())
        if target == WRITE_TARGET_MARKER:
            blockers.extend(self._marker_blockers(marker_id))
        elif target == WRITE_TARGET_IO:
            blockers.extend(_access_blockers(self.in_output, "access to IOs"))
        return tuple(blockers)

    def _login_blockers(self) -> list[str]:
        if not self.login_required:
            return []
        if not self.credentials_usable:
            return [_MSG_CREDENTIALS_UNUSABLE]
        if not self.username_matches:
            return [_MSG_USER_DIFFERS]
        return [] if self.password_matches else [_MSG_CREDENTIAL_DIFFERS]

    def _marker_blockers(self, marker_id: int | None) -> list[str]:
        if self.flag != ACCESS_SELECTION:
            return _access_blockers(self.flag, "access to markers")
        name = "the marker" if marker_id is None else f"M{marker_id}"
        if self.marker_rights is None:
            return [f"access to markers is single selection and its list could not be read ({name} is unknown)"]
        if marker_id is None or not self.marker_rights.get(marker_id, 0) & RIGHT_WRITE:
            return [f"access to markers is single selection and {name} is not released for writing"]
        return []

    def summary(self) -> str:
        """The settings in one line for a log or an error message: no user name, no password."""
        parts = [
            "active" if self.active else "inactive",
            _tcp_summary(self),
        ]
        if self.login_required and not self.credentials_usable:
            parts.append("login required (configured credentials cannot be sent)")
        elif self.login_required:
            parts.append(
                f"login required (user {'matches' if self.username_matches else 'differs'}, "
                f"password {'matches' if self.password_matches else 'differs'})"
            )
        else:
            parts.append("no login")
        parts.append(
            f"IOs {_ACCESS_NAMES.get(self.in_output, self.in_output)}, "
            f"markers {_ACCESS_NAMES.get(self.flag, self.flag)}, "
            f"1-Wire {_ACCESS_NAMES.get(self.one_wire, self.one_wire)}"
        )
        return "API " + "; ".join(parts)


def _tcp_summary(settings: ApiSettings) -> str:
    if not settings.tcp_enabled:
        return "TCP interface off"
    if settings.tcp_port is None:
        return "TCP interface on, port unreadable"
    return f"TCP interface on port {settings.tcp_port}"


def _access_blockers(access: int, what: str) -> list[str]:
    """The sentence for a data type whose access does not allow writing, none if it does."""
    if access == ACCESS_READ_WRITE:
        return []
    return [f"{what} is {_ACCESS_NAMES.get(access, access)}"]


def api_settings_from_fields(fields: ApiPageFields, *, api_username: str, api_password: str) -> ApiSettings:
    """The ApiSettings of the fields a parser found; api_username and api_password are what the client logs in with.

    A page without the on/off switch, a checked choice other than active or inactive, without the login or TCP
    switch, the user or password field, or whose data type select has no recognised selected option is no API page
    of a Comexio this reads: ComexioDataError. The marker list is the one part that may be missing or malformed:
    marker_rights is None then.
    """
    active = _active(fields.active_choice)
    login_required = _checked(fields, USE_LOGIN_INPUT)
    username = _value(fields, USERNAME_INPUT)
    password = _value(fields, PASSWORD_INPUT)
    return ApiSettings(
        active=active,
        tcp_enabled=_checked(fields, USE_TCP_INPUT),
        tcp_port=_port(fields.inputs.get(TCP_PORT_INPUT, {}).get("value")),
        login_required=login_required,
        username=username,
        username_matches=not login_required or _same(username, api_username),
        password_matches=not login_required or _same(password, api_password),
        credentials_usable=_credentials_usable(api_username, api_password),
        in_output=_access(fields, IN_OUTPUT_SELECT, _PLAIN_ACCESS),
        flag=_access(fields, FLAG_SELECT, (*_PLAIN_ACCESS, ACCESS_SELECTION)),
        one_wire=_access(fields, ONE_WIRE_SELECT, _PLAIN_ACCESS),
        marker_rights=_marker_rights(fields.inputs.get(MARKER_LIST_INPUT, {}).get("value")),
    )


def _active(choice: str | None) -> bool:
    if choice == ACTIVE_VALUE:
        return True
    if choice == INACTIVE_VALUE:
        return False
    shown = choice if choice is None else choice[:20]  # page text, capped like the access values
    raise ComexioDataError(f"API page: the {ACTIVE_RADIO} choice is {shown!r}, expected active or inactive")


def _input(fields: ApiPageFields, input_id: str) -> Mapping[str, str]:
    try:
        return fields.inputs[input_id]
    except KeyError:
        raise ComexioDataError(f"API page has no {input_id} input: changed page") from None


def _checked(fields: ApiPageFields, input_id: str) -> bool:
    attributes = _input(fields, input_id)
    if attributes.get("type", "").lower() != "checkbox":
        raise ComexioDataError(f"API page: the {input_id} input is no checkbox: changed page")
    return "checked" in attributes


def _value(fields: ApiPageFields, input_id: str) -> str:
    return _input(fields, input_id).get("value", "")


def _access(fields: ApiPageFields, select_id: str, allowed: tuple[int, ...]) -> int:
    value = fields.selected.get(select_id)
    if value is None:
        raise ComexioDataError(f"API page has no selected option for {select_id}: changed page")
    for access in allowed:
        if value == str(access):
            return access
    raise ComexioDataError(f"API page: {select_id} is set to an unknown access {value[:20]!r}")


def _port(text: str | None) -> int | None:
    """The TCP port of the field, None for anything but a whole number of 1 to 65535."""
    if text is None or not text.isascii() or not text.isdigit():
        return None
    digits = text.lstrip("0")  # leading zeros do not make the number longer (and the bound keeps int() cheap)
    if len(digits) > len(str(_MAX_PORT)):
        return None
    port = int(digits or "0")
    return port if 1 <= port <= _MAX_PORT else None


def _same(page_value: str, configured: str) -> bool:
    """Whether the two texts are equal, compared in constant time."""
    return hmac.compare_digest(page_value.encode(_UTF8, "surrogatepass"), configured.encode(_UTF8, "surrogatepass"))


def _credentials_usable(username: str, password: str) -> bool:
    """Whether the client sends these as a Basic Auth header: it sends none for an empty user and refuses the rest."""
    if not username or ":" in username:
        return False
    try:
        f"{username}:{password}".encode(_BASIC_AUTH_ENCODING)
    except UnicodeEncodeError:
        return False
    return True


def _marker_rights(raw: str | None) -> Mapping[int, int] | None:
    """The marker list of api_special_2 as {id: bits}, read as the page's own script reads it; None if unreadable.

    The script swaps the single quotes for double ones and parses JSON. An id without a marker is `null` and left
    out. One malformed key or value makes the whole list untrustworthy: None, with a debug line.
    """
    if raw is None:
        return None
    try:
        decoded: Any = json.loads(raw.replace("'", '"'))
    except (ValueError, RecursionError):  # JSONDecodeError, an int beyond the digit limit, nesting too deep
        _LOGGER.debug("API page: the marker list is no JSON")
        return None
    if not isinstance(decoded, dict):
        _LOGGER.debug("API page: the marker list is no object")
        return None
    rights: dict[int, int] = {}
    for key, value in decoded.items():
        # An id has one spelling: "05" is another key to the page's script, but would be the same marker here.
        if not (key.isascii() and key.isdigit() and len(key) <= _MAX_ID_DIGITS and key == str(int(key))):
            _LOGGER.debug("API page: the marker list has a key that is no id")
            return None
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= _MAX_RIGHTS:
            _LOGGER.debug("API page: the marker list has a value that is no access")
            return None
        rights[int(key)] = value
    return _MarkerRights(rights)
