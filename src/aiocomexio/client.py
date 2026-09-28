"""Async client for the Comexio IO-Server: admin login and read access.

The client works on an aiohttp.ClientSession the caller creates and owns (see
session.session_kwargs for the settings it needs). It never closes that session. Every call
either returns real data or raises a ComexioError subclass.
"""

import base64
import json
import logging
import secrets
import time
from collections.abc import Iterable
from dataclasses import dataclass
from http import HTTPStatus
from typing import Any

import aiohttp
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from .config import iter_group
from .exceptions import (
    ComexioAuthenticationError,
    ComexioConnectionError,
    ComexioDataError,
    ComexioError,
    ComexioResponseError,
)
from .function_plan.payload import normalize_plan_payload, plan_payload_has_elements
from .scrape import parse_comexio_version, parse_io_input_types, parse_io_types, scrape_js_vars
from .session import is_local_address

__all__ = ["ComexioClient", "LiveStates", "RawConfig"]

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

# The admin page still shows the login form ("Anmeldung") when the session is not logged in.
_LOGIN_PAGE_MARKER = "Anmeldung"
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

# Plain-text answer of fupValueData for a plan that is not running (instead of a JSON value dict).
_JSON_START_CHARS = ("{", "[")


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
    Call login() before any other method.
    """

    def __init__(self, host: str, username: str, password: str, *, session: aiohttp.ClientSession) -> None:
        self._host = host
        self._username = username
        self._password = password
        self._session = session
        # The IO-Server has no HTTPS endpoint; _warn_plain_http flags non-local hosts at login.
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
        refresh.update(
            {
                f"{_KNX_LIVE_KEY_PREFIX}{i}": {"action": "get", "KnxIo": f"K{i}", "Unit": "any"}
                for i in range(1, knx_max_id + 1)
            }
        )
        refresh[_MESSAGES_KEY] = {"action": "messages"}

        data = await self._dashboard_refresh(refresh, what="Live states")
        result = data.get("result") if isinstance(data, dict) else None
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
        result = data.get("result") if isinstance(data, dict) else None
        if not isinstance(result, dict) or "connection" not in result:
            raise ComexioDataError(f"Connection values of plan {fub_id}: no result.connection in {_excerpt(data)}")
        raw = result["connection"]
        if isinstance(raw, str) and raw and not raw.lstrip().startswith(_JSON_START_CHARS):
            _LOGGER.debug("No connection values for plan %s (not running: %s)", fub_id, raw)
            return {}
        if not raw:
            return {}
        return _connection_values(raw, fub_id)

    async def load_function_plan(self, fub_id: int, *, strict: bool = False) -> dict[str, Any]:
        """Elements and connections of one plan, both normalized to id-keyed dicts.

        strict=True raises ComexioDataError for a payload without a real elements collection
        instead of returning it as an empty plan — for callers whose next step is irreversible.
        """
        data = await self._request_json(
            "GET",
            _LOAD_ELEMENTS_PATH,
            what=f"Loading function plan {fub_id}",
            params={"fubid": fub_id},
            headers=self._xhr_headers(_FUNCTION_MODULE_PATH),
        )
        if not isinstance(data, dict):
            raise ComexioDataError(f"Function plan {fub_id} payload is not an object: {_excerpt(data)}")
        if strict and not plan_payload_has_elements(data):
            raise ComexioDataError(f"Function plan {fub_id} payload has no elements collection")
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
        elements collection (see load_function_plan). A requested plan the server leaves out is
        absent from the result.
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
        for fid_str, data in raw.items():
            entry = _bulk_plan_entry(fid_str, data, wanted, strict=strict)
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

    def _warn_plain_http(self) -> None:
        """Warn once that credentials go over plain HTTP to a non-local address."""
        if self._plain_http_warned or is_local_address(self._host):
            return
        _LOGGER.warning(
            "Logging into Comexio over plain HTTP on a non-local address (%s). "
            "Credentials may be transmitted in clear text.",
            self._host,
        )
        self._plain_http_warned = True

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
    fid_str: Any, data: Any, wanted: set[int] | None, *, strict: bool
) -> tuple[int, dict[str, Any]] | None:
    """(fub_id, normalized plan) for one loadallelements entry, or None if skipped."""
    try:
        fub_id = int(fid_str)
    except (TypeError, ValueError):
        _LOGGER.warning("Skipping bulk function plan entry with non-numeric id %r", fid_str)
        return None
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
