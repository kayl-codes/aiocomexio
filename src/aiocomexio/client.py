"""Async client for the Comexio IO-Server: admin login, reads, value writes, Web-IO and function plans.

The client works on an aiohttp.ClientSession the caller creates and owns (see
session.session_kwargs for the settings it needs). It never closes that session. Every call
either returns real data or raises a ComexioError subclass.
"""

import base64
import json
import logging
import math
import re
import secrets
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from http import HTTPStatus
from typing import Any, TypeIs
from urllib.parse import urlsplit

import aiohttp
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from .config import iter_group
from .exceptions import (
    ComexioAuthenticationError,
    ComexioConnectionError,
    ComexioDataError,
    ComexioError,
    ComexioRequestRejectedError,
    ComexioResponseError,
)
from .function_plan.payload import build_run_payload, normalize_plan_payload, plan_payload_has_elements
from .scrape import parse_comexio_version, parse_io_input_types, parse_io_types, scrape_js_vars
from .session import is_local_address
from .webio import CONTENT_TYPE_JSON

__all__ = ["ComexioClient", "LiveStates", "RawConfig", "WebioBaseInfo"]

_LOGGER = logging.getLogger(__name__)

_ADMIN_PATH = "/admin/"
_LOGIN_PATH = "/board/home/login/"
_FUNCTION_MODULE_PATH = "/admin/function_function_module/home"
_KNX_CATALOG_PATH = "/admin/knx_one_wire/knx/"
_DASHBOARD_REFRESH_PATH = "/board/dashboard/refresh/"
_LOAD_ELEMENTS_PATH = "/admin/function_function_module/loadelements/"
_LOAD_ALL_ELEMENTS_PATH = "/admin/function_function_module/loadallelements"
_BUS_WORKLOAD_PATH = "/admin/in_output/inoutputinfo"
_BUS_WORKLOAD_REFERER_PATH = "/admin/in_output/home"
_EXTENSION_FIRMWARE_PATH = "/admin/extension/checkextension_fwupdate/"
_SYSTEM_DASHBOARD_PATH = "/admin/admin_dashboard/home/"
_API_PATH = "/api/"
_WEBIO_HOME_PATH = "/admin/web_io/home"
_WEBIO_ADD_PATH = "/admin/web_io/add"
_WEBIO_BASE_WINDOW_PATH = "/admin/web_io/baseDeviceWindow/"
_WEBIO_DELETE_DEVICE_PATH = "/admin/web_io/delete_device/"
_WEBIO_DELETE_BASE_PATH = "/admin/web_io/delete_web_device_base/"
_WEBIO_SAVE_DEVICE_PATH = "/admin/web_io/save"
_WEBIO_CREATE_DEVICE_PATH = "/admin/web_io/saveDeviceWindow"
_WEBIO_UPLOAD_PATH = "/admin/web_io/upload_device_settings"
_WEBIO_SAVE_COMMAND_PATH = "/admin/web_io/save_command"
_WEBIO_DELETE_COMMAND_PATH = "/admin/web_io/delete_web_command/"
_WEBIO_EDIT_COMMAND_PATH = "/admin/web_io/edit_command/"
_UNIQUE_CHECK_PATH = "/admin/_helper/isunique"
_MARKER_HOME_PATH = "/admin/flag/home"
_MARKER_ADD_PATH = "/admin/flag/add/"
_MARKER_SAVE_PATH = "/admin/flag/saveOne"
_KNX_HOME_PATH = "/admin/knx_one_wire/home"
_KNX_SAVE_PATH = "/admin/knx_one_wire/saveKnx/"
_DELETE_ELEMENT_PATH = "/admin/function_function_module/delete_element/"
_PLAN_SAVE_PATH = "/admin/function_function_module/save_fub"
_PLAN_DELETE_PATH = "/admin/function_function_module/delete/"
_PLAN_RUN_PATH = "/admin/function_function_module/run_fup/"
_PLAN_STOP_PATH = "/admin/function_function_module/stop_fup/"
_PLAN_ADD_ELEMENT_PATH = "/admin/function_function_module/add_element/"
_PLAN_SAVE_CONNECTION_PATH = "/admin/function_function_module/saveconnection/"
_PLAN_SAVE_POSITIONS_PATH = "/admin/function_function_module/saveelementspos/"
_PLAN_DELETE_ELEMENTS_PATH = "/admin/function_function_module/deleteelements/"
_PLAN_SAVE_COMMENT_PATH = "/admin/function_function_module/savefupcommentelement/"

# The admin page still shows the login form ("Anmeldung") when the session is not logged in.
_LOGIN_PAGE_MARKER = "Anmeldung"
# The login form's submit button. An admin request without a logged-in session answers HTTP 200
# with the login form (checked against a live server), so endpoints whose answer carries no data
# to check look for this — more specific than _LOGIN_PAGE_MARKER, which a Web-IO dialog could
# carry as a label.
_LOGIN_FORM_SUBMIT_MARKER = 'id="loginsubmit"'
# The login form is ISO-8859-1; RSA blocks are encrypted over its byte encoding.
_LOGIN_ENCODING = "iso-8859-1"
_LOGIN_NONCE_LENGTH = 20
# PKCS#1 v1.5 padding takes 11 bytes of every RSA block.
_PKCS1_PADDING_BYTES = 11

# dashboard/refresh request/response key prefix for KNX objects ($FubModules type "11"). KNX
# objects and markers share one plain numeric id space, so the prefix is what splits the
# response back apart.
_KNX_LIVE_KEY_PREFIX = "knxIo_11_"
_MESSAGES_KEY = "messages"
_RESULT_KEY = "result"

# $FubModules type of markers; delete_element needs it to know which object list the id is in.
_MARKER_FUB_MODULE_TYPE = "2"
# Marker type in the flag forms: 1 = digital, 2 = analog.
_MARKER_TYPE_BINARY = "1"
_MARKER_TYPE_ANALOG = "2"
# The Web-IO delete answer carries a jQuery UI error box when the device is still used in a plan.
_WEBIO_IN_USE_MARKER = "ui-state-error"
# The min/max <input> tags on a Web-IO command's edit form, e.g.
# <input type="text" id="min_cmd_io_0" name="min_cmd_io_0" value="-500000">.
_WEBIO_CMD_INPUT_RE = re.compile(r'<input\b[^>]*\bid="(min|max)_cmd_io_0"[^>]*>', re.IGNORECASE)
_WEBIO_CMD_VALUE_RE = re.compile(r'\bvalue="([^"]*)"')

# Plain-text answer of fupValueData for a plan that is not running (instead of a JSON value dict).
_JSON_START_CHARS = ("{", "[")

# save_fub form values: paper format ids and orientation ids as Comexio's plan dialog sends them.
_PLAN_PAPER_IDS = {"A3": "2", "A4": "3", "A5": "4"}
_PLAN_ORIENTATION_IDS = {"landscape": "0", "portrait": "1"}
_PLAN_DPI_RANGE = range(45, 121)
# save_fub / delete answer with a redirect whose query carries the verdict.
_REDIRECT_STATUSES = frozenset({HTTPStatus.MOVED_PERMANENTLY, HTTPStatus.FOUND, HTTPStatus.SEE_OTHER})
_PLAN_ADDED_CONFIRMATION = "added=1"
_PLAN_SAVED_CONFIRMATION = "saved=1"
_PLAN_DELETED_CONFIRMATION = "delete=ok"
# Element types and the catalog ref_id add_element needs for blocks that have no catalog entry.
_ELEMENT_TYPE_COMMENT = 14
_ELEMENT_TYPE_CONSTANT = 16
_COMMENT_REF_ID = "3"
# Comexio normalizes a constant's ref_id to 0 once saved, but refuses ref_id 0 on create.
_CONSTANT_REF_ID = "1"
_COMMENT_WIDTHS = range(1, 6)
_CONNECTION_VALUE_TYPES = frozenset({"binary", "analog"})
_ID_KEY = "id"
# Explicitly empty fupValueData answers; any other value (0, False, ...) must parse or raise.
_EMPTY_CONNECTION_VALUES: tuple[Any, ...] = (None, "", [], {})


@dataclass(frozen=True, slots=True)
class RawConfig:
    """The scraped admin configuration, input for config.parse_config.

    variables holds every `var $Name = {...}` object literal of the function module page, keyed
    without the "$" (FubModules, Fubs, WebDevices, ...). io_types / io_input_types are the IO
    type tables from the main admin page ({} if absent). comexio_version is the firmware version
    from the page's asset paths, None if the page did not reveal it.
    """

    variables: dict[str, Any]
    io_types: dict[str, Any]
    io_input_types: dict[str, Any]
    comexio_version: str | None


@dataclass(frozen=True, slots=True)
class WebioBaseInfo:
    """A Web-IO class (Comexio's "device base") and whether Comexio offers to delete it.

    deletable is False while a device of the class still exists.
    """

    base_id: str
    deletable: bool


@dataclass(frozen=True, slots=True)
class LiveStates:
    """Live values from one dashboard refresh, keyed by plain numeric id per source.

    Markers and KNX objects share one numeric id space (marker 5 and KNX object 5 both exist),
    so they are kept apart instead of merged into one id-keyed dict.
    """

    markers: dict[str, Any]
    knx: dict[str, Any]


class ComexioClient:
    """Admin-session client for one Comexio IO-Server.

    host is "hostname", "ip" or either with ":port". The server only speaks plain HTTP. session
    must have its own cookie jar (session.session_kwargs), since the admin login lives in it.
    Call login() before any other admin method. api_username / api_password are the separate
    Comexio API user the set_*_value methods send as Basic Auth; without api_username they send
    no credentials at all.
    """

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        *,
        session: aiohttp.ClientSession,
        api_username: str = "",
        api_password: str = "",  # nosec B107
    ) -> None:
        self._host = host
        self._username = username
        self._password = password
        self._api_username = api_username
        self._api_password = api_password
        self._session = session
        # The IO-Server has no HTTPS endpoint; _warn_plain_http flags non-local hosts before credentials go out.
        self._base_url = f"http://{host}"  # NOSONAR
        self._plain_http_warned = False

    @property
    def host(self) -> str:
        """The host this client talks to."""
        return self._host

    async def login(self) -> None:
        """RSA admin login (Comexio v11 scheme: PKCS#1 v1.5 blocks over salt + nonce + password).

        Raises ComexioAuthenticationError if the server rejects the credentials (or the password
        cannot be encoded for the login form), ComexioConnectionError / ComexioResponseError /
        ComexioDataError if the login could not be carried out at all.
        """
        self._warn_plain_http()
        _LOGGER.debug("Starting RSA login for %s", self._host)
        login_url = f"{self._base_url}{_LOGIN_PATH}"
        # A still valid session cookie from an earlier login would make the admin page below look
        # logged in even if the server rejects these credentials. The jar belongs to this client
        # alone (see session.session_kwargs), so every login starts from an empty one.
        self._session.cookie_jar.clear()
        self._session.cookie_jar.update_cookies({"comexio-client-time": str(int(time.time()))})

        keys = await self._request_json("POST", _LOGIN_PATH, what="Login key request", data={"login_keys": "true"})
        encrypted_password = self._encrypt_password(keys)
        form = {
            "target": "/board/home/login",
            "username": self._username,
            "password": encrypted_password,
            "loginsubmit": "Anmelden",
            "encryption": "rsa",
        }
        # The login POST's own status is not meaningful; the admin page below is the verdict.
        await self._request_text(
            "POST", _LOGIN_PATH, what="Login", check_status=False, data=form, headers={"Referer": login_url}
        )
        html = await self._request_text("GET", _ADMIN_PATH, what="Login check")
        if not html or _LOGIN_PAGE_MARKER in html:
            raise ComexioAuthenticationError(f"Comexio at {self._host} rejected the admin login")
        _LOGGER.debug("Logged into the Comexio admin interface at %s", self._host)

    def _encrypt_password(self, keys: Any) -> str:
        """The login form's password field: two RSA blocks, with and without the password."""
        try:
            salt = base64.b64decode(keys["salt"]).decode(_LOGIN_ENCODING)
            modulus, exponent = int(keys["modulus"], 16), int(keys["exponent"], 16)
            public_key = rsa.RSAPublicNumbers(exponent, modulus).public_key()
        except (KeyError, TypeError, ValueError) as err:
            raise ComexioDataError("Login key response carries no usable RSA key") from err
        nonce = "".join(secrets.choice("0123456789ABCDEF") for _ in range(_LOGIN_NONCE_LENGTH))
        without_password = (salt + nonce).encode(_LOGIN_ENCODING)
        try:
            with_password = without_password + self._password.encode(_LOGIN_ENCODING)
        except UnicodeEncodeError as err:
            raise ComexioAuthenticationError("The password contains characters outside ISO-8859-1") from err
        capacity = (public_key.key_size + 7) // 8 - _PKCS1_PADDING_BYTES
        if len(without_password) > capacity:
            raise ComexioDataError("Login key response carries an RSA key too small for salt and nonce")
        if len(with_password) > capacity:
            raise ComexioAuthenticationError(
                f"The password is too long for the RSA login (at most {capacity - len(without_password)} bytes)"
            )
        return f"{_rsa_block(public_key, with_password)} {_rsa_block(public_key, without_password)}"

    async def get_raw_config(self) -> RawConfig:
        """Scrape the admin page (IO type tables) and the function module page (all config objects).

        Raises ComexioAuthenticationError if the session is no longer logged in (the server then
        serves the login form) and ComexioDataError if the function module page carries no
        parseable $FubModules, so a broken scrape never passes as an empty installation.
        """
        main_html = await self._request_text("GET", _ADMIN_PATH, what="Admin page")
        html = await self._request_text("GET", _FUNCTION_MODULE_PATH, what="Function module page")
        variables = scrape_js_vars(html, page_label="function module")
        if not isinstance(variables.get("FubModules"), dict):
            raise _missing_data_error(html, "Function module page carries no parseable $FubModules")
        return RawConfig(
            variables=variables,
            io_types=parse_io_types(main_html),
            io_input_types=parse_io_input_types(main_html),
            comexio_version=parse_comexio_version(html),
        )

    async def get_knx_dpt_catalog(self) -> dict[str, Any]:
        """$KnxPoints / $KnxDevices / $KnxDpt from the KNX admin page (input for knx.resolve_knx_dpt).

        The data only changes on an ETS import or a firmware update, so a consumer should cache
        it (e.g. per comexio_version) instead of fetching it on every poll. Raises
        ComexioDataError if the page carries no parseable data.
        """
        html = await self._request_text("GET", _KNX_CATALOG_PATH, what="KNX DPT catalog")
        catalog = scrape_js_vars(html, page_label="KNX DPT catalog")
        if not catalog:
            raise _missing_data_error(html, "KNX DPT catalog page carries no parseable data")
        _LOGGER.debug(
            "KNX DPT catalog: %d points, %d devices, %d dpt entries",
            len(catalog.get("KnxPoints", {})),
            len(catalog.get("KnxDevices", {})),
            len(catalog.get("KnxDpt", {})),
        )
        return catalog

    async def get_live_states(self, marker_count: int, knx_max_id: int = 0) -> LiveStates:
        """Live values of markers 1..marker_count and KNX objects 1..knx_max_id, in one request.

        An id the server leaves out of its answer is absent from the result, not 0.
        """
        refresh: dict[str, Any] = {str(i): {"action": "get", "MarkerName": f"M{i}"} for i in range(1, marker_count + 1)}
        refresh |= {
            f"{_KNX_LIVE_KEY_PREFIX}{i}": {"action": "get", "KnxIo": f"K{i}", "Unit": "any"}
            for i in range(1, knx_max_id + 1)
        }
        refresh[_MESSAGES_KEY] = {"action": "messages"}

        data = await self._dashboard_refresh(refresh, what="Live states")
        result = data.get(_RESULT_KEY) if isinstance(data, dict) else None
        if not isinstance(result, dict):
            raise ComexioDataError(f"Live states response has no result object: {_excerpt(data)}")
        knx = {
            key.removeprefix(_KNX_LIVE_KEY_PREFIX): value
            for key, value in result.items()
            if key.startswith(_KNX_LIVE_KEY_PREFIX)
        }
        markers = {
            key: value
            for key, value in result.items()
            if key != _MESSAGES_KEY and not key.startswith(_KNX_LIVE_KEY_PREFIX)
        }
        return LiveStates(markers=markers, knx=knx)

    async def get_function_plan_connection_values(self, fub_id: int) -> dict[str, list[Any]]:
        """Live output values per source element of one running plan (Studio's fupValueData).

        Returns {source FubElementId: [value per output row]}; a block with several outputs
        reports one list for all of them, indexed by output IOPos. Returns {} if the plan is not
        running (Comexio then answers with a plain-text sentinel such as "0:not_found") or reports
        an empty value set. Raises ComexioDataError if the answer carries no connection entry.
        """
        payload = {"connection": {"action": "fupValueData", "fupId": fub_id}}
        data = await self._dashboard_refresh(payload, what=f"Connection values of plan {fub_id}")
        result = data.get(_RESULT_KEY) if isinstance(data, dict) else None
        if not isinstance(result, dict) or "connection" not in result:
            raise ComexioDataError(f"Connection values of plan {fub_id}: no result.connection in {_excerpt(data)}")
        raw = result["connection"]
        if isinstance(raw, str) and raw and not raw.lstrip().startswith(_JSON_START_CHARS):
            _LOGGER.debug("No connection values for plan %s (not running: %s)", fub_id, raw)
            return {}
        return {} if raw in _EMPTY_CONNECTION_VALUES else _connection_values(raw, fub_id)

    async def load_function_plan(self, fub_id: int, *, strict: bool = False) -> dict[str, Any]:
        """Elements and connections of one plan, both normalized to id-keyed dicts.

        strict=True raises ComexioDataError for a payload without a real elements or connections
        collection instead of returning it as an empty one — for callers whose next step is
        irreversible, such as run_function_plan, which would wipe what the plan was missing.
        """
        data = await self._load_plan_payload(fub_id)
        if strict:
            _require_plan_collections(data, fub_id)
        plan = normalize_plan_payload(data)
        _LOGGER.debug(
            "Loaded function plan %s: %d elements, %d connections",
            fub_id,
            len(plan["elements"]),
            len(plan["connections"]),
        )
        return plan

    async def load_all_function_plans(
        self, fub_ids: Iterable[int] | None = None, *, strict: bool = False
    ) -> dict[int, dict[str, Any]]:
        """Every plan in one bulk request: {fub_id: normalized plan}, optionally limited to fub_ids.

        Comexio serializes requests server-side, so one bulk call beats N per-plan calls. A
        malformed entry is skipped with a warning; strict=True also skips entries without a real
        elements collection. Unlike load_function_plan(strict=True) it does not check connections,
        so a result here is no source for run_function_plan. A requested plan the server leaves
        out is absent from the result.
        """
        wanted = None if fub_ids is None else set(fub_ids)
        if wanted is not None and not wanted:
            return {}
        started = time.monotonic()
        raw = await self._request_json(
            "GET",
            _LOAD_ALL_ELEMENTS_PATH,
            what="Loading all function plans",
            headers=self._xhr_headers(_FUNCTION_MODULE_PATH),
        )
        if raw == []:
            raw = {}  # PHP's json_encode of an empty array: a server without any plan
        if not isinstance(raw, dict):
            raise ComexioDataError(f"Bulk function plan payload is not an object: {_excerpt(raw)}")

        plans: dict[int, dict[str, Any]] = {}
        seen: set[int] = set()
        for fid_str, data in raw.items():
            entry = _bulk_plan_entry(fid_str, data, wanted, seen, strict=strict)
            if entry is not None:
                plans[entry[0]] = entry[1]
        _LOGGER.debug("Loaded %d function plans in %.2fs (bulk request)", len(plans), time.monotonic() - started)
        if wanted is not None and (missing := wanted - plans.keys()):
            _LOGGER.debug("Requested function plans missing from the bulk result: %s", sorted(missing))
        return plans

    async def fetch_marker_titles(self) -> dict[int, str]:
        """Current marker titles {id: title} from a fresh config scrape."""
        config = await self.get_raw_config()
        titles: dict[int, str] = {}
        for marker_id, marker in iter_group(config.variables["FubModules"].get("2")):
            try:
                titles[int(marker_id)] = str((marker or {}).get("Name") or "")
            except (AttributeError, TypeError, ValueError):
                _LOGGER.warning("Skipping malformed marker entry %r", marker_id)
        return titles

    async def get_bus_workload(self) -> dict[str, Any]:
        """Internal bus workload and SD card presence (the admin in/output info)."""
        data = await self._request_json(
            "POST", _BUS_WORKLOAD_PATH, what="Bus workload", headers=self._xhr_headers(_BUS_WORKLOAD_REFERER_PATH)
        )
        if not isinstance(data, dict):
            raise ComexioDataError(f"Bus workload payload is not an object: {_excerpt(data)}")
        return data

    async def check_extension_firmware(self) -> list[dict[str, Any]]:
        """Available firmware updates for the base module and every extension on the local bus.

        Comexio documents that this check can briefly interrupt extension outputs — call it
        rarely (e.g. nightly), never on a regular poll.
        """
        payload = await self._request_json(
            "POST",
            _EXTENSION_FIRMWARE_PATH,
            what="Extension firmware check",
            data={"pos": "local"},
            headers=self._xhr_headers(_ADMIN_PATH),
        )
        if not isinstance(payload, dict) or payload.get("ok") != "ok":
            raise ComexioDataError(f"Extension firmware check returned an error payload: {_excerpt(payload)}")
        data = payload.get("data")
        if not isinstance(data, list) or not all(isinstance(entry, dict) for entry in data):
            raise ComexioDataError(f"Extension firmware data is not a list of objects: {_excerpt(data)}")
        return data

    # --- value writes (/api/, Basic Auth) ---

    async def set_marker_value(self, marker_id: int, value: float) -> None:
        """Write a marker's value through the /api/ interface."""
        await self._api_set({"marker": f"M{marker_id}"}, value, what=f"Writing marker M{marker_id}")

    async def set_knx_value(self, knx_id: int, value: float) -> None:
        """Write a KNX object's value through the /api/ interface (query parameter knx=K<id>)."""
        await self._api_set({"knx": f"K{knx_id}"}, value, what=f"Writing KNX object K{knx_id}")

    async def set_io_value(self, ext: str, io: str, value: float) -> None:
        """Write an IO's value through the /api/ interface; ext is the extension name, io its identifier."""
        await self._api_set({"ext": ext, "io": io}, value, what=f"Writing IO {io} of {ext}")

    # --- Web-IO lifecycle (admin session) ---

    async def get_webio_base_info(self, class_name: str) -> WebioBaseInfo | None:
        """The Web-IO class named class_name, or None if the server has no such class.

        None is only returned for a page that was fetched and really lists no such class — a
        failed fetch raises, so a caller never uploads a duplicate class next to one that is
        still there. Raises ComexioAuthenticationError if the session is not logged in.
        """
        html = await self._request_admin_text("GET", _WEBIO_ADD_PATH, what="Web-IO add page")
        pattern = rf'<option value="(\d+)"[^>]*>{re.escape(class_name)}</option>'
        if not (match := re.search(pattern, html, re.IGNORECASE)):
            return None
        base_id = match[1]
        window = await self._request_admin_text("GET", _WEBIO_BASE_WINDOW_PATH, what="Web-IO base window")
        return WebioBaseInfo(base_id=base_id, deletable=f"delete_web_device_base/?id={base_id}" in window)

    async def get_webio_device_id(self, device_name: str) -> str | None:
        """Id of the Web-IO device named device_name, or None if the server has no such device.

        Same contract as get_webio_base_info: None only for a fetched page without that device.
        """
        html = await self._request_admin_text("GET", _WEBIO_HOME_PATH, what="Web-IO home page")
        pattern = rf'<a id="tab-link-(\d+)"[^>]*>{re.escape(device_name)}</a>'
        return match[1] if (match := re.search(pattern, html, re.IGNORECASE)) else None

    async def delete_webio_device(self, device_id: str | int) -> bool:
        """Delete a Web-IO device. False if Comexio refuses because a function plan still uses it.

        True means Comexio reported no in-use error; its answer carries no other verdict, so
        get_webio_device_id shows whether the device is really gone where it matters.
        """
        what = f"Deleting Web-IO device {device_id}"
        html = await self._request_admin_text(
            "GET",
            _WEBIO_DELETE_DEVICE_PATH,
            what=what,
            params={"id": str(device_id)},
            headers=self._xhr_headers(_WEBIO_HOME_PATH),
        )
        if _WEBIO_IN_USE_MARKER in html:
            _LOGGER.warning("Web-IO device %s is used in a function plan and cannot be deleted", device_id)
            return False
        _LOGGER.debug("%s: %s", what, _excerpt(html))
        return True

    async def delete_webio_base(self, base_id: str | int) -> None:
        """Delete a Web-IO class. Comexio only allows this once no device of the class is left.

        Comexio's answer carries no verdict, so returning only means the request was accepted —
        check with get_webio_base_info where it matters.
        """
        what = f"Deleting Web-IO class {base_id}"
        body = await self._request_admin_text(
            "GET",
            _WEBIO_DELETE_BASE_PATH,
            what=what,
            params={"id": str(base_id)},
            headers=self._xhr_headers(_WEBIO_HOME_PATH),
        )
        _LOGGER.debug("%s: %s", what, _excerpt(body))

    async def upload_webio_class(self, class_json: str, *, class_name: str, filename: str) -> str:
        """Upload a Web-IO class template (webio.generate_webio_json) under class_name; returns its base id.

        filename is the name of the uploaded file as Comexio records it (e.g. "ha_<server>.json").
        Raises ComexioRequestRejectedError if Comexio answers without "ok".
        """
        form = aiohttp.FormData()
        form.add_field("file", class_json.encode(), filename=filename, content_type="application/json")
        form.add_field("set_name", class_name)
        what = f"Uploading Web-IO class {class_name!r}"
        result = _expect_object(
            await self._request_json(
                "POST", _WEBIO_UPLOAD_PATH, what=what, data=form, headers=self._xhr_headers(_WEBIO_HOME_PATH)
            ),
            what,
        )
        _LOGGER.debug("%s: %s", what, _excerpt(result))
        if not result.get("ok"):
            raise ComexioRequestRejectedError(f"{what} was refused: {_excerpt(result)}")
        base_id = result.get("base_id")
        if base_id is None or base_id == "":
            raise ComexioDataError(f"{what}: answer carries no base_id: {_excerpt(result)}")
        return str(base_id)

    async def create_webio_device(
        self,
        name: str,
        base_id: str | int,
        address: str,
        *,
        username: str = "",
        password: str = "",  # nosec B107
    ) -> None:
        """Create a device of the Web-IO class base_id that sends to address ("host:port").

        username / password are the Basic Auth credentials the device attaches to commands that
        ask for authentication; leave them empty for a target without login. Comexio's answer
        carries no verdict, so returning only means the request was accepted — check with
        get_webio_device_id(name) where it matters.
        """
        payload = {
            "name": name,
            "ip": address,
            "web_device_base": str(base_id),
            "username": username,
            "password": password,
            "web_device_base_sample": "none",
            "identifier": "",
            "form_login": "2",
        }
        body = await self._request_admin_text(
            "POST",
            _WEBIO_CREATE_DEVICE_PATH,
            what=f"Creating Web-IO device {name!r}",
            data=payload,
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        # The answer carries no verdict to check; logged so a silent refusal can be traced.
        _LOGGER.debug("Creating Web-IO device %r (class %s): %s", name, base_id, _excerpt(body))

    async def update_webio_device_address(self, device_id: str | int, address: str, device_name: str) -> None:
        """Point an existing Web-IO device at a new address ("host:port").

        Comexio's save handler takes the whole device form, so device_name must be the device's
        current name (it is saved along), and the device's Basic Auth credentials, TLS checks and
        login mode are reset to none — only use it on a device without credentials. Raises
        ComexioRequestRejectedError if Comexio does not confirm the save.
        """
        device_data = {
            "web_device_id": str(device_id),
            f"name_{device_id}": device_name,
            f"ip_{device_id}": address,
            f"username_{device_id}": "",
            f"password_{device_id}": "",  # nosec B105
            f"checkca_{device_id}": "0",
            f"pinnedpubkey_{device_id}": "",
            f"form_login_{device_id}": "2",
        }
        what = f"Updating the address of Web-IO device {device_id}"
        result = _expect_object(
            await self._request_json(
                "POST",
                _WEBIO_SAVE_DEVICE_PATH,
                what=what,
                data={"no_reload": "true", "JSON": json.dumps(device_data)},
                headers=self._xhr_headers(_WEBIO_HOME_PATH),
            ),
            what,
        )
        if result.get("save") != 1:
            raise ComexioRequestRejectedError(f"{what} was not confirmed: {_excerpt(result)}")

    async def save_webio_command(
        self,
        device_id: str | int,
        command: Mapping[str, Any],
        *,
        base_id: str | int | None = None,
        command_id: str | int | None = None,
    ) -> None:
        """Add a command to a Web-IO device (command_id None) or update the existing command command_id.

        command is one entry of webio.build_webio_commands (Name, Parameter, Data, TypeId, Min,
        Max, optionally HeaderModifier / PostGet / Authentication — the KNX loopback command
        needs its own values for those three). base_id is the device's class, only used for a
        new command. command_id must be numeric (ValueError otherwise). Comexio's answer carries
        no verdict, so returning only means the request was accepted — the next config scrape
        shows whether the command is there.
        """
        payload: dict[str, Any] = {
            "dlg_web_device_id": str(device_id),
            "protocol": 0,
            "parameter": command["Parameter"],
            "header_modifier": command.get("HeaderModifier", CONTENT_TYPE_JSON),
            "data": command["Data"],
            "port": "",
            "post_get": command.get("PostGet", 1),
            "authentication": command.get("Authentication", 0),
            "req_freq": "",
            "reply_interpreter": "",
            "id_cmd_io_0": _command_ref(command_id),
            "name_cmd_io_0": command["Name"],
            "function_cmd_io_0": "1_1_0",
            "input_cmd_io_0": 1,
            "type_cmd_io_0": command["TypeId"],
            "send_on_one_cmd_io_0": 0,
            "min_cmd_io_0": command["Min"],
            "max_cmd_io_0": command["Max"],
            "default_value_cmd_io_0": "",
            # Comexio's form always posts its hidden template row along.
            "id_cmd_io_sample": "",
            "name_cmd_io_sample": "",
            "function_cmd_io_sample": "0_1_0",
            "input_cmd_io_sample": 1,
            "type_cmd_io_sample": 2,
            "send_on_one_cmd_io_sample": 0,
            "min_cmd_io_sample": 0,
            "max_cmd_io_sample": 1,
            "default_value_cmd_io_sample": "",
            "DefaultActive": 1,
        }
        if command_id is not None:
            payload["id"] = str(command_id)
        else:
            payload["deviceBaseId"] = "0" if base_id is None else str(base_id)
        await self._request_admin_text(
            "POST",
            _WEBIO_SAVE_COMMAND_PATH,
            what=f"Saving Web-IO command {command['Name']!r}",
            data=payload,
            headers=self._xhr_headers(_WEBIO_HOME_PATH),
        )

    async def delete_webio_command(self, command_id: str | int, device_id: str | int) -> None:
        """Delete one command of a Web-IO device.

        Comexio's answer carries no verdict, so returning only means the request was accepted —
        the next config scrape shows whether the command is gone.
        """
        what = f"Deleting Web-IO command {command_id}"
        body = await self._request_admin_text(
            "GET",
            _WEBIO_DELETE_COMMAND_PATH,
            what=what,
            params={"id": str(command_id), "dev": str(device_id)},
        )
        _LOGGER.debug("%s: %s", what, _excerpt(body))

    async def get_webio_command_range(
        self, command_id: str | int, device_id: str | int
    ) -> tuple[float | None, float | None]:
        """(Min, Max) of one Web-IO command, read from its edit form.

        The config scrape never carries Min/Max of Web-IO commands, so the edit form is the only
        source. A field that is missing or not numeric comes back as None; a form without any
        min/max field raises ComexioDataError (or ComexioAuthenticationError for the login form).
        """
        html = await self._request_admin_text(
            "GET",
            _WEBIO_EDIT_COMMAND_PATH,
            what=f"Web-IO command {command_id} edit form",
            params={"Id": str(command_id), "TestDevice": str(device_id)},
        )
        return _command_range(html, command_id)

    # --- markers and KNX objects (admin session) ---

    async def create_marker(self, *, binary: bool) -> int:
        """Create an untitled marker (value 0) and return its id.

        Comexio always hands out the next free id; there is no way to ask for a specific one.
        """
        what = f"Creating a {'digital' if binary else 'analog'} marker"
        result = _expect_object(
            await self._request_json(
                "POST",
                _MARKER_ADD_PATH,
                what=what,
                data={"type": _marker_type(binary)},
                headers=self._xhr_headers(_MARKER_HOME_PATH),
            ),
            what,
        )
        if not result.get("ok"):
            raise ComexioRequestRejectedError(f"{what} was refused: {_excerpt(result)}")
        try:
            marker_id = int(result["saved"])
        except (KeyError, TypeError, ValueError) as err:
            raise ComexioDataError(f"{what}: answer carries no marker id: {_excerpt(result)}") from err
        _LOGGER.debug("Created marker M%s (binary=%s)", marker_id, binary)
        return marker_id

    async def rename_marker(self, marker_id: int, name: str, *, binary: bool) -> None:
        """Set a marker's title (after Comexio's own uniqueness check).

        Comexio's marker save takes the whole form, so this also resets the marker's default
        value and "store in memory" flag to 0 — only use it on a marker whose state is known,
        e.g. one just made with create_marker. binary must match the marker's real type.
        Raises ComexioRequestRejectedError if the name is taken or the save is not confirmed.
        """
        what = f"Renaming marker M{marker_id}"
        await self._check_name_unique("memory", marker_id, name, what=what)
        marker_type = _marker_type(binary)
        payload = {
            "id": str(marker_id),
            "default_default": "0",
            "default_type": marker_type,
            "name": name,
            "type": marker_type,
            f"value_{marker_id}": "0",
            "default": "0",
            "store_memory": "0",
        }
        result = _expect_object(
            await self._request_json(
                "POST", _MARKER_SAVE_PATH, what=what, data=payload, headers=self._xhr_headers(_MARKER_HOME_PATH)
            ),
            what,
        )
        if str(result.get("saved")) != str(marker_id):
            raise ComexioRequestRejectedError(f"{what} was not confirmed: {_excerpt(result)}")

    async def rename_knx_object(self, knx_id: str | int, name: str) -> None:
        """Set a KNX object's title (after Comexio's own uniqueness check); nothing else changes.

        Raises ComexioRequestRejectedError if the name is taken or the save is not confirmed.
        """
        what = f"Renaming KNX object K{knx_id}"
        await self._check_name_unique("oneWire", knx_id, name, what=what)
        result = _expect_object(
            await self._request_json(
                "POST",
                _KNX_SAVE_PATH,
                what=what,
                data={"id": str(knx_id), "field": "name", "value": name},
                headers=self._xhr_headers(_KNX_HOME_PATH),
            ),
            what,
        )
        if str(result.get("Ok")) != "1":
            raise ComexioRequestRejectedError(f"{what} was not confirmed: {_excerpt(result)}")

    async def delete_marker(self, marker_id: int) -> bool:
        """Delete a marker from Comexio's marker list (not an element in a function plan).

        True if Comexio reports it deleted. False if Comexio answered but did not delete it —
        most often because the id does not exist (any more), but Comexio reports a refusal the
        same way, so a caller that just saw the marker should treat False as suspicious.
        """
        payload = {
            "elementId": str(marker_id),
            "type": _MARKER_FUB_MODULE_TYPE,
            "full": "true",
            "timestamp": _js_timestamp(),
        }
        what = f"Deleting marker M{marker_id}"
        result = _expect_object(
            await self._request_json(
                "POST", _DELETE_ELEMENT_PATH, what=what, data=payload, headers=self._xhr_headers(_FUNCTION_MODULE_PATH)
            ),
            what,
        )
        if str(result.get(_RESULT_KEY)) == "1":
            return True
        _LOGGER.debug("%s: not deleted, answer: %s", what, _excerpt(result))
        return False

    # --- function plans --------------------------------------------------------------------------

    async def create_function_plan(
        self,
        name: str,
        *,
        comment: str = "",
        paper_format: str = "A4",
        orientation: str = "landscape",
        dpi: int = 90,
    ) -> int:
        """Create an empty, inactive function plan and return its id.

        paper_format is "A3", "A4" or "A5", orientation "landscape" or "portrait", dpi 45-120.
        Comexio's answer carries no id, so the new plan is looked up by name in a fresh config
        scrape. Raises ComexioRequestRejectedError if the name is taken or Comexio does not
        confirm the plan.
        """
        what = f"Creating function plan {name!r}"
        form = _plan_form(name, comment, paper_format, orientation, dpi)
        await self._check_name_unique("fub", None, name, what=what)
        form.update({"fub_position": "-1", "fub_active": "0", "fub_reset_on_close": "0", "fub_create": "Erzeugen"})
        await self._post_plan_form(form, _PLAN_ADDED_CONFIRMATION, what=what)
        try:
            config = await self.get_raw_config()
        except ComexioError as err:
            err.add_note(f"{what}: Comexio confirmed the plan, only reading back its id failed")
            raise
        return _plan_id_by_name(config.variables.get("Fubs"), name, what=what)

    async def update_function_plan(
        self,
        fub_id: int,
        *,
        name: str,
        comment: str,
        position: int,
        active: bool,
        paper_format: str,
        orientation: str,
        dpi: int,
        reset_outputs_on_stop: bool = False,
    ) -> None:
        """Save a plan's settings (name, comment, position, active flag, paper, orientation, dpi).

        Comexio's save takes the whole settings form, so pass the current value of every setting
        that should stay. $Fubs does not reveal "reset outputs on stop", so it is sent as given
        (default off, Comexio's own default for new plans). Values as for create_function_plan.
        Raises ComexioRequestRejectedError if Comexio does not confirm the save.
        """
        _require_ints(fub_id=fub_id, position=position)
        _require_bools(active=active, reset_outputs_on_stop=reset_outputs_on_stop)
        what = f"Saving settings of function plan {fub_id}"
        form = _plan_form(name, comment, paper_format, orientation, dpi)
        form.update(
            {
                "fub_id": str(fub_id),
                "fub_position": str(position),
                "fub_active": "1" if active else "0",
                "fub_reset_on_close": "1" if reset_outputs_on_stop else "0",
                "fub_save": "Speichern",
            }
        )
        await self._post_plan_form(form, _PLAN_SAVED_CONFIRMATION, what=what)

    async def delete_function_plan(self, fub_id: int) -> None:
        """Delete a whole function plan with everything on it.

        Raises ComexioRequestRejectedError if Comexio does not confirm the deletion.
        """
        _require_ints(fub_id=fub_id)
        what = f"Deleting function plan {fub_id}"
        location = await self._request_redirect(
            "GET",
            _PLAN_DELETE_PATH,
            what=what,
            params={"id": str(fub_id)},
            headers=self._xhr_headers(_FUNCTION_MODULE_PATH),
        )
        if _PLAN_DELETED_CONFIRMATION not in location:
            raise ComexioRequestRejectedError(f"{what} was not confirmed (redirect to {location!r})")

    async def run_function_plan(self, fub_id: int, plan: Mapping[str, Any] | None = None) -> None:
        """Save a plan's elements and connections and activate the plan (Comexio's run_fup).

        plan is a plan as load_function_plan(..., strict=True) returns it; its content replaces
        what is on the plan, so an empty plan wipes the live one. None activates the plan as it
        is: its current state is loaded first and must carry both elements and connections, so a
        broken answer never runs as an empty plan (ComexioDataError). Raises
        ComexioRequestRejectedError if Comexio refuses to run it (e.g. an output used twice).
        """
        _require_ints(fub_id=fub_id)
        if plan is None:
            payload = await self._current_run_payload(fub_id)
        else:
            payload = build_run_payload(plan)
        await self._plan_state_request(
            _PLAN_RUN_PATH, {"id": str(fub_id), "data": json.dumps(payload)}, what=f"Running function plan {fub_id}"
        )

    async def stop_function_plan(self, fub_id: int) -> None:
        """Stop a running plan (Comexio's programming mode); Comexio only edits a stopped plan.

        Raises ComexioRequestRejectedError if Comexio does not confirm the stop.
        """
        _require_ints(fub_id=fub_id)
        await self._plan_state_request(_PLAN_STOP_PATH, {"id": str(fub_id)}, what=f"Stopping function plan {fub_id}")

    async def add_function_plan_element(
        self,
        fub_id: int,
        ref_id: int,
        element_type: int,
        *,
        x: float,
        y: float,
        connection: Mapping[str, Any] | None = None,
    ) -> int:
        """Place a marker, IO, Web-IO command, KNX object or catalog block on a plan; returns its element id.

        ref_id is the id of the placed object within its element type ($FubModules group).
        connection wires the new element in the same call, in Comexio's saveconnection shape
        keyed by connection index — e.g. a Web-IO command fed by an existing marker element:
        {"0": {"id": "new", "fub_id": <fub_id>, "type": "binary",
        "input": {"element": "<marker element id>", "pos": "0", "inverted": False},
        "output": {"0": {"element": "new", "pos": "0", "inverted": False}}}}.
        Comments and constants have their own methods. Raises ComexioRequestRejectedError if
        Comexio refuses the element.
        """
        _require_ints(ref_id=ref_id, element_type=element_type)
        form = {"name": "", "ref_id": str(ref_id), "type": str(element_type), "id": "undefined"}
        if connection is not None:
            form["connection"] = json.dumps(connection, separators=(",", ":"))
        return await self._add_plan_element(
            fub_id, form, x, y, what=f"Placing element type {element_type} ref {ref_id} on function plan {fub_id}"
        )

    async def add_function_plan_constant(self, fub_id: int, value: str, *, x: float, y: float) -> int:
        """Place a constant block with the given value on a plan; returns its element id."""
        _require_strs(value=value)
        form = {"name": value, "ref_id": _CONSTANT_REF_ID, "type": str(_ELEMENT_TYPE_CONSTANT), "id": "undefined"}
        return await self._add_plan_element(fub_id, form, x, y, what=f"Placing a constant on function plan {fub_id}")

    async def add_function_plan_comment(self, fub_id: int, text: str, *, x: float, y: float) -> int:
        """Place a comment block on a plan; returns its element id.

        Comexio places it at its default width; save_function_plan_comment sets another one.
        """
        _require_strs(text=text)
        form = {"name": text, "ref_id": _COMMENT_REF_ID, "type": str(_ELEMENT_TYPE_COMMENT), "id": "0"}
        return await self._add_plan_element(fub_id, form, x, y, what=f"Placing a comment on function plan {fub_id}")

    async def save_function_plan_comment(self, element_id: int, text: str, *, width: int) -> None:
        """Save a comment block's text and width (1 = narrow ... 5 = Comexio's "very wide").

        Raises ComexioRequestRejectedError if Comexio does not confirm the save.
        """
        _require_ints(element_id=element_id)
        _require_strs(text=text)
        if not _is_int(width) or width not in _COMMENT_WIDTHS:
            raise ValueError(f"Comment width must be 1-5, not {width}")
        what = f"Saving comment element {element_id}"
        form = {
            "id": str(element_id),
            "use_base_64": "1",
            "name": base64.b64encode(text.encode()).decode("ascii"),
            "width": str(width),
        }
        result = await self._plan_json(_PLAN_SAVE_COMMENT_PATH, form, what=what)
        if str(result.get(_RESULT_KEY)) != "1":
            raise ComexioRequestRejectedError(f"{what} was not confirmed: {_excerpt(result)}")

    async def save_function_plan_connection(
        self,
        fub_id: int,
        source: int,
        sinks: Sequence[tuple[int, int, bool]],
        *,
        value_type: str,
        source_pos: int = 0,
        source_inverted: bool = False,
        connection_id: int | None = None,
    ) -> int:
        """Save the wire from one source pin to all its sinks; returns the connection id.

        sinks are (element id, input pin, inverted). Comexio keeps ONE connection per source
        pin with all sinks as its outputs, so this call always carries every sink of the pin:
        saving sinks one by one made the wires of IO and constant sources vanish. value_type is
        "binary" or "analog". connection_id must be the pin's existing connection id when the
        pin already has one — resaving it as a new connection makes Comexio merge the outputs
        but drop the connection's source, which leaves a wire without origin.
        Raises ComexioRequestRejectedError if Comexio refuses the connection.
        """
        if value_type not in _CONNECTION_VALUE_TYPES:
            raise ValueError(f"value_type must be 'binary' or 'analog', not {value_type!r}")
        sinks = tuple(sinks)  # a generator would be used up by the checks below
        if not sinks:
            raise ValueError("A connection needs at least one sink")
        _require_ints(fub_id=fub_id, source=source, source_pos=source_pos)
        if connection_id is not None:
            _require_ints(connection_id=connection_id)
        _require_bools(source_inverted=source_inverted)
        for sink in sinks:
            _check_sink(sink)
        connection = {
            _ID_KEY: "new" if connection_id is None else str(connection_id),
            "fub_id": fub_id,
            "input": {"element": str(source), "pos": str(source_pos), "inverted": source_inverted},
            "type": value_type,
            "output": {
                str(i): {"element": str(sink), "pos": str(pin), "inverted": inverted}
                for i, (sink, pin, inverted) in enumerate(sinks)
            },
        }
        what = f"Saving connection from element {source} on function plan {fub_id}"
        form = {"JSON": json.dumps(connection, separators=(",", ":")), "timestamp": _js_timestamp()}
        return _answer_id(await self._plan_json(_PLAN_SAVE_CONNECTION_PATH, form, what=what), what)

    async def move_function_plan_elements(self, positions: Sequence[tuple[int, float, float]]) -> None:
        """Move elements to new canvas positions, given as (element id, x, y).

        Raises ComexioRequestRejectedError if Comexio does not confirm the move.
        """
        positions = tuple(positions)
        if not positions:
            raise ValueError("No element positions given")
        for position in positions:
            _check_position(position)
        what = f"Moving {len(positions)} function plan element(s)"
        moves = {str(i): {"x": x, "y": y, _ID_KEY: element_id} for i, (element_id, x, y) in enumerate(positions)}
        form = {"Json": json.dumps(moves, separators=(",", ":")), "timestamp": _js_timestamp()}
        result = await self._plan_json(_PLAN_SAVE_POSITIONS_PATH, form, what=what)
        if str(result.get(_RESULT_KEY)) != "1":
            raise ComexioRequestRejectedError(f"{what} was not confirmed: {_excerpt(result)}")

    async def delete_function_plan_elements(self, element_ids: Sequence[int]) -> None:
        """Remove elements, and every connection touching them, from their function plan.

        The objects behind them (markers, IOs, ...) stay. Raises ComexioRequestRejectedError if
        Comexio does not confirm the deletion.
        """
        if isinstance(element_ids, (str, bytes, bytearray, memoryview, Mapping)):
            raise TypeError(f"element_ids must be a sequence of ids, not {type(element_ids).__name__}")
        element_ids = tuple(element_ids)
        if not element_ids:
            raise ValueError("No element ids given")
        _require_ints(**{f"element_ids[{i}]": element_id for i, element_id in enumerate(element_ids)})
        what = f"Deleting function plan elements {list(element_ids)}"
        form = {"Json": json.dumps([str(element_id) for element_id in element_ids]), "timestamp": _js_timestamp()}
        result = await self._plan_json(_PLAN_DELETE_ELEMENTS_PATH, form, what=what)
        if result.get("delete") is not True:
            raise ComexioRequestRejectedError(f"{what} was not confirmed: {_excerpt(result)}")

    async def system_emergency_reboot(self) -> None:
        """Reboot the whole Comexio system immediately — no confirmation, no way back.

        Comexio answers before it goes down; success only means the request was accepted.
        """
        await self._request_admin_text(
            "GET", _SYSTEM_DASHBOARD_PATH, what="System reboot", params={"id": "system", "restart": "1"}
        )
        _LOGGER.warning("Comexio at %s accepted the system reboot request", self._host)

    def _warn_plain_http(self) -> None:
        """Warn once that credentials go over plain HTTP to a non-local address."""
        if self._plain_http_warned or is_local_address(self._host):
            return
        _LOGGER.warning(
            "Sending Comexio credentials over plain HTTP to a non-local address (%s). "
            "They may be transmitted in clear text.",
            self._host,
        )
        self._plain_http_warned = True

    async def _api_set(self, target: dict[str, str], value: float, *, what: str) -> None:
        """One /api/?action=set request; HTTP 401 means the API credentials were rejected."""
        headers: dict[str, str] = {}
        if self._api_username:
            self._warn_plain_http()
            headers["Authorization"] = _basic_auth_header(self._api_username, self._api_password)
        params: dict[str, str | float] = {"action": "set", "value": value, **target}
        try:
            body = await self._request_text("GET", _API_PATH, what=what, params=params, headers=headers)
        except ComexioResponseError as err:
            if err.status != HTTPStatus.UNAUTHORIZED:
                raise
            if not self._api_username:
                raise ComexioAuthenticationError(
                    f"Comexio at {self._host} requires API credentials, none are configured (api_username)"
                ) from err
            raise ComexioAuthenticationError(f"Comexio at {self._host} rejected the API credentials") from err
        # The answer carries no verdict to check; logged so an ignored write can be traced.
        _LOGGER.debug("%s: %s", what, _excerpt(body))

    async def _check_name_unique(self, model: str, object_id: str | int | None, name: str, *, what: str) -> None:
        """Comexio's own uniqueness check; ComexioRequestRejectedError if name is taken.

        object_id is the object being renamed (its own name does not count), None for a new one.
        """
        check = f"{what}: name check"
        form = {"model": model, "field": "name", "value": name}
        if object_id is not None:
            form["id"] = str(object_id)
        result = _expect_object(
            await self._request_json("POST", _UNIQUE_CHECK_PATH, what=check, data=form),
            check,
        )
        if _RESULT_KEY not in result:
            raise ComexioDataError(f"{check}: answer carries no result: {_excerpt(result)}")
        if not result[_RESULT_KEY]:
            raise ComexioRequestRejectedError(f"{what}: the name {name!r} is already in use ({_excerpt(result)})")

    async def _load_plan_payload(self, fub_id: int) -> dict[str, Any]:
        """The raw loadelements answer of one plan, checked to be an object."""
        data = await self._request_json(
            "GET",
            _LOAD_ELEMENTS_PATH,
            what=f"Loading function plan {fub_id}",
            params={"fubid": fub_id},
            headers=self._xhr_headers(_FUNCTION_MODULE_PATH),
        )
        if not isinstance(data, dict):
            raise ComexioDataError(f"Function plan {fub_id} payload is not an object: {_excerpt(data)}")
        return data

    async def _current_run_payload(self, fub_id: int) -> dict[str, Any]:
        """run_fup data for a plan's current state; ComexioDataError for anything short of a whole plan."""
        data = await self._load_plan_payload(fub_id)
        _require_plan_collections(data, fub_id)
        try:
            return build_run_payload(normalize_plan_payload(data))
        except ValueError as err:
            raise ComexioDataError(f"Function plan {fub_id} payload cannot be run: {err}") from err

    async def _post_plan_form(self, form: dict[str, str], token: str, *, what: str) -> None:
        """POST the plan settings form; ComexioRequestRejectedError unless the redirect carries token."""
        location = await self._request_redirect(
            "POST", _PLAN_SAVE_PATH, what=what, data=form, headers=self._xhr_headers(_FUNCTION_MODULE_PATH)
        )
        if token not in location:
            raise ComexioRequestRejectedError(f"{what} was not confirmed (redirect to {location!r})")

    async def _plan_state_request(self, path: str, form: dict[str, str], *, what: str) -> None:
        """run_fup / stop_fup; both answer {"result": true, "state": ...} on success."""
        result = await self._plan_json(path, form, what=what)
        if result.get(_RESULT_KEY) is not True:
            raise ComexioRequestRejectedError(f"{what} was refused: {_excerpt(result)}")
        _LOGGER.debug("%s: state %s", what, result.get("state"))

    async def _add_plan_element(self, fub_id: int, form: dict[str, str], x: float, y: float, *, what: str) -> int:
        """add_element with the fields every element shares; returns the new element id."""
        _require_ints(fub_id=fub_id)
        _require_numbers(x=x, y=y)
        form.update({"fubid": str(fub_id), "x": str(x), "y": str(y), "timestamp": _js_timestamp()})
        return _answer_id(await self._plan_json(_PLAN_ADD_ELEMENT_PATH, form, what=what), what)

    async def _plan_json(self, path: str, form: dict[str, str], *, what: str) -> dict[str, Any]:
        """POST one function plan editor action and return its JSON object answer."""
        return _expect_object(
            await self._request_json(
                "POST", path, what=what, data=form, headers=self._xhr_headers(_FUNCTION_MODULE_PATH)
            ),
            what,
        )

    def _xhr_headers(self, referer_path: str) -> dict[str, str]:
        return {"X-Requested-With": "XMLHttpRequest", "Referer": f"{self._base_url}{referer_path}"}

    async def _dashboard_refresh(self, payload: dict[str, Any], *, what: str) -> Any:
        """POST one dashboard/refresh request (form field "json") and return the decoded answer."""
        headers = {**self._xhr_headers(_ADMIN_PATH), "User-Agent": "Mozilla/5.0"}
        return await self._request_json(
            "POST", _DASHBOARD_REFRESH_PATH, what=what, data={"json": json.dumps(payload)}, headers=headers
        )

    async def _request_text(
        self, method: str, path: str, *, what: str, check_status: bool = True, **kwargs: Any
    ) -> str:
        """Body of one request as text, with transport errors and error statuses mapped to ComexioError."""
        try:
            async with self._session.request(method, f"{self._base_url}{path}", **kwargs) as resp:
                if check_status and resp.status != HTTPStatus.OK:
                    raise ComexioResponseError(f"{what} failed: HTTP {resp.status}", status=resp.status)
                try:
                    return await resp.text()
                except UnicodeDecodeError as err:
                    raise ComexioDataError(f"{what}: response is not valid {resp.get_encoding()}") from err
        except (aiohttp.ClientError, TimeoutError) as err:
            raise ComexioConnectionError(f"{what} failed: {err!r}") from err

    async def _request_admin_text(self, method: str, path: str, *, what: str, **kwargs: Any) -> str:
        """_request_text for an admin page or action; the login form raises ComexioAuthenticationError.

        For endpoints whose answer is not JSON — a lapsed session would otherwise read as success.
        """
        body = await self._request_text(method, path, what=what, **kwargs)
        if _LOGIN_FORM_SUBMIT_MARKER in body:
            raise ComexioAuthenticationError(f"Comexio served the login form, the session is not logged in ({what})")
        return body

    async def _request_redirect(self, method: str, path: str, *, what: str, **kwargs: Any) -> str:
        """Location of a form action Comexio answers with a redirect.

        Any other answer is not the confirmation: the login form or a redirect to it raises
        ComexioAuthenticationError, an error status ComexioResponseError, anything else
        ComexioRequestRejectedError.
        """
        try:
            async with self._session.request(
                method, f"{self._base_url}{path}", allow_redirects=False, **kwargs
            ) as resp:
                if resp.status in _REDIRECT_STATUSES:
                    location = resp.headers.get("Location", "")
                    if "login" in urlsplit(location).path.lower():
                        raise ComexioAuthenticationError(
                            f"Comexio redirected to the login page, the session is not logged in ({what})"
                        )
                    return location
                status = resp.status
                body = await resp.text(errors="replace")
        except (aiohttp.ClientError, TimeoutError) as err:
            raise ComexioConnectionError(f"{what} failed: {err!r}") from err
        if _LOGIN_FORM_SUBMIT_MARKER in body:
            raise ComexioAuthenticationError(f"Comexio served the login form, the session is not logged in ({what})")
        if status != HTTPStatus.OK:
            raise ComexioResponseError(f"{what} failed: HTTP {status}", status=status)
        raise ComexioRequestRejectedError(f"{what} was not confirmed: {_excerpt(body)}")

    async def _request_json(self, method: str, path: str, *, what: str, **kwargs: Any) -> Any:
        """Body of one request decoded as JSON (whatever the Content-Type header claims)."""
        body = await self._request_text(method, path, what=what, **kwargs)
        try:
            return json.loads(body)
        except ValueError as err:
            message = f"{what}: response is not JSON: {_excerpt(body)}"
            # Outside the login itself, the login form instead of JSON means the session is gone.
            if path == _LOGIN_PATH:
                raise ComexioDataError(message) from err
            raise _missing_data_error(body, message) from err


def _rsa_block(public_key: rsa.RSAPublicKey, message: bytes) -> str:
    """One PKCS#1 v1.5 block as zero-padded hex, the way Comexio's login form expects it."""
    encrypted = public_key.encrypt(message, padding.PKCS1v15())
    return encrypted.hex().zfill(((public_key.key_size + 7) // 8) * 2)


def _missing_data_error(body: str, message: str) -> ComexioError:
    """ComexioAuthenticationError if body is the login form (session no longer logged in), else ComexioDataError.

    Only called once a page already lacks the expected data, so a marker or plan that happens to
    be named "Anmeldung" never turns a good answer into an authentication error.
    """
    if _LOGIN_PAGE_MARKER in body:
        return ComexioAuthenticationError(f"Comexio served the login form, the session is not logged in ({message})")
    return ComexioDataError(message)


def _basic_auth_header(username: str, password: str) -> str:
    """Authorization header value for Basic Auth, encoded as ISO-8859-1 (aiohttp.BasicAuth's default)."""
    if ":" in username:
        raise ComexioAuthenticationError("The API username must not contain ':' (Basic Auth separator)")
    try:
        credentials = f"{username}:{password}".encode(_LOGIN_ENCODING)
    except UnicodeEncodeError as err:
        raise ComexioAuthenticationError("The API credentials contain characters outside ISO-8859-1") from err
    return f"Basic {base64.b64encode(credentials).decode('ascii')}"


def _expect_object(data: Any, what: str) -> dict[str, Any]:
    """data if it is a JSON object, else ComexioDataError."""
    if not isinstance(data, dict):
        raise ComexioDataError(f"{what}: answer is not an object: {_excerpt(data)}")
    return data


def _plan_form(name: str, comment: str, paper_format: str, orientation: str, dpi: int) -> dict[str, str]:
    """The save_fub fields create and update share; ValueError for a value Comexio does not offer."""
    _require_strs(name=name, comment=comment, paper_format=paper_format, orientation=orientation)
    try:
        paper_id = _PLAN_PAPER_IDS[paper_format.upper()]
    except KeyError:
        raise ValueError(f"paper_format must be one of {sorted(_PLAN_PAPER_IDS)}, not {paper_format!r}") from None
    try:
        orientation_id = _PLAN_ORIENTATION_IDS[orientation.lower()]
    except KeyError:
        raise ValueError(f"orientation must be 'landscape' or 'portrait', not {orientation!r}") from None
    if not _is_int(dpi) or dpi not in _PLAN_DPI_RANGE:
        raise ValueError(f"dpi must be {_PLAN_DPI_RANGE.start}-{_PLAN_DPI_RANGE.stop - 1}, not {dpi}")
    return {
        "fub_type": "1",
        "fub_page_count_x": "1",
        "fub_page_count_y": "1",
        "fub_name": name,
        "fub_comment": comment,
        "fub_paper": paper_id,
        "fub_orientation": orientation_id,
        "fub_resolution": str(dpi),
    }


def _is_int(value: Any) -> TypeIs[int]:
    """True for an int that is not a bool (range membership lets True and 2.0 through)."""
    return isinstance(value, int) and not isinstance(value, bool)


def _require_plan_collections(data: dict[str, Any], fub_id: int) -> None:
    """ComexioDataError unless a loadelements answer carries real elements and connections collections."""
    # normalize_plan_payload turns a missing collection into {} — fine to display, fatal to run.
    missing = [key for key in ("elements", "connections") if not isinstance(data.get(key), (dict, list))]
    if missing:
        raise ComexioDataError(f"Function plan {fub_id} payload has no {' / '.join(missing)} collection")


def _require(kind: str, accept: Callable[[Any], bool], values: dict[str, Any]) -> None:
    """TypeError naming every value accept refuses."""
    wrong = [f"{name}={value!r}" for name, value in values.items() if not accept(value)]
    if wrong:
        raise TypeError(f"Expected {kind}: {', '.join(wrong)}")


def _require_ints(**values: Any) -> None:
    """TypeError unless every value is an int (not a bool).

    Ids and pins go out as strings: 4.7 or True would reach Comexio as "4.7" / "True" instead of
    failing here, and a string id is no id either.
    """
    _require("integers", _is_int, values)


def _require_numbers(**values: Any) -> None:
    """TypeError unless every value is a finite int or float (not a bool) — canvas coordinates."""
    _require(
        "finite numbers", lambda value: _is_int(value) or (isinstance(value, float) and math.isfinite(value)), values
    )


def _require_strs(**values: Any) -> None:
    """TypeError unless every value is a str — names, comments and texts go out as they are."""
    _require("strings", lambda value: isinstance(value, str), values)


def _require_bools(**values: Any) -> None:
    """TypeError unless every value is a bool — Comexio's inverted flags."""
    _require("booleans", lambda value: isinstance(value, bool), values)


def _check_sink(sink: Any) -> None:
    """TypeError unless sink is an (element id, input pin, inverted) tuple."""
    if not isinstance(sink, tuple) or len(sink) != 3:
        raise TypeError(f"A sink must be an (element id, pin, inverted) tuple, not {sink!r}")
    element, pin, inverted = sink
    _require_ints(sink_element=element, sink_pin=pin)
    _require_bools(sink_inverted=inverted)


def _check_position(position: Any) -> None:
    """TypeError unless position is an (element id, x, y) tuple."""
    if not isinstance(position, tuple) or len(position) != 3:
        raise TypeError(f"A position must be an (element id, x, y) tuple, not {position!r}")
    element_id, x, y = position
    _require_ints(element_id=element_id)
    _require_numbers(x=x, y=y)


def _plan_id_by_name(fubs: Any, name: str, *, what: str) -> int:
    """Id of the one plan in $Fubs named name; ComexioDataError for none or several."""
    found = [plan_id for plan_id, plan in iter_group(fubs) if isinstance(plan, Mapping) and plan.get("Name") == name]
    if len(found) != 1:
        raise ComexioDataError(f"{what}: Comexio confirmed it, but $Fubs lists {len(found)} plans with that name")
    try:
        return int(found[0])
    except ValueError as err:
        raise ComexioDataError(f"{what}: the new plan has a non-numeric id {found[0]!r}") from err


def _answer_id(result: dict[str, Any], what: str) -> int:
    """The "id" of an add_element / saveconnection answer; an "error" answer is a refusal."""
    if "error" in result:
        raise ComexioRequestRejectedError(f"{what} was refused: {_excerpt(result)}")
    value = result.get(_ID_KEY)
    # int() would turn true into 1 and 1.9 into 1: only a positive integer, as number or digit string, is an id.
    if isinstance(value, str) and value.isascii() and value.isdigit():
        value = int(value)
    if _is_int(value) and value > 0:
        return value
    raise ComexioDataError(f"{what}: answer carries no id: {_excerpt(result)}")


def _marker_type(binary: bool) -> str:
    return _MARKER_TYPE_BINARY if binary else _MARKER_TYPE_ANALOG


def _js_timestamp() -> str:
    """Current UTC time the way JavaScript's Date.toISOString() writes it (millisecond precision)."""
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _command_ref(command_id: str | int | None) -> str:
    """Base64 command reference of the Web-IO command form: {"src":"command","id":<id or null>}."""
    ref = {"src": "command", "id": None if command_id is None else int(command_id)}
    return base64.b64encode(json.dumps(ref, separators=(",", ":")).encode()).decode()


def _command_range(html: str, command_id: str | int) -> tuple[float | None, float | None]:
    """(Min, Max) from a Web-IO command edit form; see ComexioClient.get_webio_command_range."""
    values: dict[str, float | None] = {"min": None, "max": None}
    matched = False
    for tag_match in _WEBIO_CMD_INPUT_RE.finditer(html):
        matched = True
        field = tag_match[1].lower()
        if not (value_match := _WEBIO_CMD_VALUE_RE.search(tag_match[0])):
            continue
        try:
            values[field] = float(value_match[1])
        except ValueError:
            _LOGGER.warning("Web-IO command %s has a non-numeric %s value: %r", command_id, field, value_match[1])
    if not matched:
        raise _missing_data_error(html, f"Web-IO command {command_id} edit form has no min/max fields")
    return values["min"], values["max"]


def _connection_values(raw: Any, fub_id: int) -> dict[str, list[Any]]:
    """Decode fupValueData's connection payload into {element id: [values]}."""
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError as err:
        raise ComexioDataError(f"Connection values of plan {fub_id} are not JSON: {_excerpt(raw)}") from err
    # Same PHP array/object ambiguity as the plan payload: a list's position IS the element id.
    if isinstance(parsed, list):
        parsed = {str(i): values for i, values in enumerate(parsed)}
    if not isinstance(parsed, dict):
        raise ComexioDataError(f"Connection values of plan {fub_id} are not an object: {_excerpt(parsed)}")
    return {str(elem_id): values if isinstance(values, list) else [values] for elem_id, values in parsed.items()}


def _bulk_plan_entry(
    fid_str: Any, data: Any, wanted: set[int] | None, seen: set[int], *, strict: bool
) -> tuple[int, dict[str, Any]] | None:
    """(fub_id, normalized plan) for one loadallelements entry, or None if skipped.

    Raises ComexioDataError if the id was already seen (e.g. "1" and "01"): there is no way to
    tell which payload is the real plan, even if one of them would be skipped as malformed.
    """
    try:
        fub_id = int(fid_str)
    except (TypeError, ValueError):
        _LOGGER.warning("Skipping bulk function plan entry with non-numeric id %r", fid_str)
        return None
    if fub_id in seen:
        raise ComexioDataError(f"Bulk function plan payload lists plan {fub_id} twice")
    seen.add(fub_id)
    if wanted is not None and fub_id not in wanted:
        return None
    if not isinstance(data, dict):
        _LOGGER.warning("Skipping bulk function plan %s: payload is not an object", fub_id)
        return None
    if strict and not plan_payload_has_elements(data):
        _LOGGER.warning("Skipping bulk function plan %s: payload has no elements collection", fub_id)
        return None
    try:
        return fub_id, normalize_plan_payload(data)
    except ComexioDataError as err:
        _LOGGER.warning("Skipping bulk function plan %s: %s", fub_id, err)
        return None


def _excerpt(value: Any, limit: int = 200) -> str:
    """Short repr of an unexpected payload for an error message."""
    text = repr(value)
    return text if len(text) <= limit else f"{text[:limit]}..."
