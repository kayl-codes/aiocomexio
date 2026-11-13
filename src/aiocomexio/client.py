"""Async client for the Comexio IO-Server: admin login, reads, value writes, Web-IO and function plans.

The client works on an aiohttp.ClientSession the caller creates and owns (see
session.session_kwargs for the settings it needs). It never closes that session. With a pinned
certificate fingerprint (see tls) it talks HTTPS, otherwise plain HTTP. Every call
either returns real data or raises a ComexioError subclass. The one exception is an invalid
argument (a non-finite value for a set_*_value call, a non-numeric command_id, ...): that
raises ValueError or TypeError before anything is sent, as the method's docstring states.
"""

import asyncio
import base64
import codecs
import inspect
import json
import logging
import math
import re
import secrets
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping, Sequence
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from html import unescape
from html.parser import HTMLParser
from http import HTTPStatus
from io import StringIO
from itertools import repeat
from operator import attrgetter, is_
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, NamedTuple, NoReturn, Self, TypeIs
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlsplit

import aiohttp
from aiohttp.client_reqrep import ConnectionKey
from aiohttp.http_exceptions import HttpProcessingError
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from .api_settings import (
    ACTIVE_RADIO,
    API_FORM_ID,
    API_INPUT_IDS,
    API_SELECT_IDS,
    ApiPageFields,
    ApiSettings,
    api_settings_from_fields,
)
from .config import iter_group, log_excerpt
from .doorstation import (
    CAMERA_ID_ATTRIBUTE,
    DOORSTATION_ATTRIBUTES,
    JPEG_EOI,
    Doorstation,
    JpegFrameReader,
    doorstation_stream_path,
    doorstations_from_attributes,
)
from .exceptions import (
    ComexioAuthenticationError,
    ComexioCertificateMismatchError,
    ComexioConnectionError,
    ComexioCreatedWithoutIdError,
    ComexioDataError,
    ComexioError,
    ComexioInsecureTransportError,
    ComexioRequestRejectedError,
    ComexioResponseError,
)
from .extension import EXTENSION_INPUTS, ExtensionInfo, extension_from_inputs, require_ext_id
from .function_plan.payload import build_run_payload, normalize_plan_payload, plan_payload_missing_collections
from .onewire import (
    _ANY_TYPE_COUNTER,
    OneWireSensor,
    OneWireStatus,
    parse_onewire_config,
    parse_onewire_device_count,
    parse_onewire_status,
)
from .scrape import (
    TimeServerSettings,
    parse_comexio_version,
    parse_free_storage_percent,
    parse_io_input_types,
    parse_io_types,
    parse_run_duration,
    parse_time_server,
    parse_webio_add_page,
    scrape_js_vars,
)
from .session import CONNECTION_KEY, is_local_address
from .tls import _HTTPS_DEFAULT_PORT, format_fingerprint, https_address, normalize_fingerprint
from .webio import CONTENT_TYPE_JSON, WEBIO_PROTOCOL_HTTP

if TYPE_CHECKING:
    from yarl import URL  # aiohttp's URL type; only named in annotations, so no dependency of its own

__all__ = [
    "ComexioClient",
    "CreatedFunctionPlan",
    "IoAddress",
    "LiveStates",
    "PlacedModuleBlock",
    "RawConfig",
    "SystemInfo",
    "WebioBaseInfo",
    "WebioCommandForm",
    "io_written_over_admin_session",
]

_LOGGER = logging.getLogger(__name__)

_ADMIN_PATH = "/admin/"
_LOGIN_PATH = "/board/home/login/"
_FUNCTION_MODULE_PATH = "/admin/function_function_module/home"
_KNX_CATALOG_PATH = "/admin/knx_one_wire/knx/"
_DASHBOARD_REFRESH_PATH = "/board/dashboard/refresh/"
_LOAD_ELEMENTS_PATH = "/admin/function_function_module/loadelements/"
_LOAD_ALL_ELEMENTS_PATH = "/admin/function_function_module/loadallelements"
_BUS_WORKLOAD_PATH = "/admin/in_output/inoutputinfo"
_ONEWIRE_STATUS_PATH = "/admin/knx_one_wire/read1WireStati/"
_ONEWIRE_MONITOR_PATH = "/admin/knx_one_wire/one_wire/"
_BUS_WORKLOAD_REFERER_PATH = "/admin/in_output/home"
_IO_HOME_PAGE = "IO home page"
# How long get_doorstation_snapshot waits for the first frame, and how much of the stream it reads at most: a
# frame is about 90 KB at 1280x720, so a stream without a frame in its first MBs is no camera stream.
_SNAPSHOT_TIMEOUT_SEC = 10.0
_MAX_SNAPSHOT_BYTES = 4 * 1024 * 1024
# Every request asks for its answer unencoded and does not let aiohttp decompress it (_unencoded): decompressed
# chunks would be allocated before a byte cap could apply, so a compressed answer could exhaust memory. A cap thus
# counts bytes of the page itself, and a compressed answer is refused (_content_encoding).
_ACCEPT_ENCODING = "Accept-Encoding"
_IDENTITY_ENCODING = "identity"
# Every extension block of the IO home page carries a device counter: a <div> with an id number_of_<ext> and a
# class number_of_type_<n>, the markup parse_onewire_device_count reads. The Quickbus and KNX counters carry the
# suffixes _30_2 / _30_3, so the type is a number with optional numeric suffixes.
_EXTENSION_COUNTER_ID_PREFIX = "number_of_"
_EXTENSION_COUNTER_CLASS_RE = re.compile(rf"{_ANY_TYPE_COUNTER}\d+(?:_\d+)*", re.ASCII)
_EXTENSION_FIRMWARE_PATH = "/admin/extension/checkextension_fwupdate/"
_EXTENSION_EDIT_PATH = "/admin/extension/edit/"
_API_SETTINGS_PATH = "/admin/api/home"
_SYSTEM_DASHBOARD_PATH = "/admin/admin_dashboard/home/"
_RUN_DURATION_PATH = "/admin/admin_dashboard/run_duration"
_API_PATH = "/api/"
# A request that comes after at least this long without any answer gets a debug line even if it succeeds.
_IDLE_LOG_MIN_SEC = 5.0
# The error text of the debug line is cut here: a transport error can carry a long message.
_DIAGNOSTIC_ERROR_MAX_CHARS = 300
# The ClientResponseError classes whose message is the reason phrase the server (or a proxy) sent. Matched by the
# exact class: aiohttp's other subclasses (ContentTypeError, WSServerHandshakeError, TooManyRedirects) carry its own
# message.
_REASON_PHRASE_ERRORS = (aiohttp.ClientResponseError, aiohttp.ClientHttpProxyError)
# What the debug line of a request says when the caller's own deadline cancelled it.
_CALLER_DEADLINE_EXPIRED = "the caller's deadline expired"
# The "user:password@" of a URL yarl could not parse (an InvalidURL), with or without a scheme, up to the last "@"
# before the path (a password may hold one); applied to the URL already cut at "?" and "#".
_URL_USER_INFO_RE = re.compile(r"^([^/]*//)?[^/]*@")
# Where _follow_redirects leaves the moment its request got a slot at the login gate, and the time of the last
# answer as it stood then (next to CONNECTION_KEY): what the request itself waited for is not "idle".
_STARTED_KEY = "started"
_PREVIOUS_RESPONSE_KEY = "previous_response_at"
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
_PLAN_SAVE_BLOCK_SETTINGS_PATH = "/admin/function_function_module/savefubbaseconfig/"
_PLAN_SAVE_SCENE_PATH = "/admin/function_function_module/save_scene/"
_PLAN_SAVE_QUANTUM_SETTINGS_PATH = "/admin/function_function_module/savecombinedmoduleconfig/"

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
# How long login() waits for the requests in flight before it renews the session anyway. A request
# that stalls longer (a session without a total timeout) must not hold up the login, and with it every
# request queued behind the login; such a request may then fail with ComexioAuthenticationError.
_LOGIN_WAIT_FOR_REQUESTS_SEC = 60.0
# How long the login itself may take. Every request waits while it runs, so a login stalled on a
# session without a total timeout must not hold them up forever; a real login takes seconds.
_LOGIN_TIMEOUT_SEC = 300.0
# PKCS#1 v1.5 padding takes 11 bytes of every RSA block.
_PKCS1_PADDING_BYTES = 11

# dashboard/refresh request/response key prefix for KNX objects ($FubModules type "11"). KNX
# objects and markers share one plain numeric id space, so the prefix is what splits the
# response back apart.
_KNX_LIVE_KEY_PREFIX = "knxIo_11_"
# Same for physical IOs: an IO id is globally unique across extensions, but shares the numeric id
# space with markers, so IO keys get their own prefix. They go out in a request of their own.
_IO_LIVE_KEY_PREFIX = "io_"
_ACTION_KEY = "action"
_ACTION_GET = "get"
_MESSAGES_KEY = "messages"
_RESULT_KEY = "result"

# $FubModules type of markers; delete_element needs it to know which object list the id is in.
_MARKER_FUB_MODULE_TYPE = "2"
# Marker type in the flag forms: 1 = digital, 2 = analog.
_MARKER_TYPE_BINARY = "1"
_MARKER_TYPE_ANALOG = "2"
# The Web-IO delete answer carries a jQuery UI error box when the device is still used in a plan.
_WEBIO_IN_USE_MARKER = "ui-state-error"
# The ids of the min/max <input> tags on a Web-IO command's edit form and the field each fills, e.g.
# <input type="text" id="min_cmd_io_0" name="min_cmd_io_0" value="-500000">.
_WEBIO_RANGE_INPUT_IDS = {"min_cmd_io_0": "min", "max_cmd_io_0": "max"}
# The ids of the <textarea> tags that hold a Web-IO command's request path ("Parameter") and its Lua body ("Daten").
_WEBIO_PARAMETER_FIELD = "parameter"
_WEBIO_DATA_FIELD = "data"
_WEBIO_TEXT_FIELD_IDS = frozenset({_WEBIO_PARAMETER_FIELD, _WEBIO_DATA_FIELD})
_LINE_BREAK_RE = re.compile(r"\r\n|\r|\n")
_LEADING_LINE_BREAK_RE = re.compile(r"\A(?:\r\n|\r|\n)")
_TEXTAREA = "textarea"
# The end tag of a <textarea> (RCDATA): "</textarea" followed by an HTML space character (tab, LF, FF, CR, space),
# "/" or ">" - not by a vertical tab or a non-ASCII space, which a browser keeps in the text.
_TEXTAREA_END_RE = re.compile(r"</textarea(?=[\t\n\f\r />])", re.IGNORECASE)
# Elements whose content a browser reads as text (RCDATA and raw text), so markup inside them is no form field; a
# <noscript> is raw text too, as a browser runs scripts.
_TEXT_ONLY_ELEMENTS = frozenset({_TEXTAREA, "title", "xmp", "iframe", "noembed", "noframes", "noscript", "plaintext"})
# html.parser releases that know a <noscript> is raw text when scripts run read it so when asked to.
_HTML_PARSER_SCRIPTING = {"scripting": True} if "scripting" in inspect.signature(HTMLParser.__init__).parameters else {}
# A text-only element that no end tag ends: the rest of the page is its text.
_ENDLESS_TEXT_ELEMENT = "plaintext"
# The ASCII whitespace a browser strips from and collapses in an option text it uses as the value.
_HTML_WHITESPACE = "\t\n\f\r "
_HTML_WHITESPACE_RE = re.compile(f"[{_HTML_WHITESPACE}]+")
# Tags that end an open <option>, so text after them is no longer its text.
_OPTION_ENDING_TAGS = frozenset({"option", "optgroup", "hr"})
# A CDATA section starts with it in foreign content; elsewhere a browser reads it as a bogus comment.
_CDATA_START = "<![CDATA["
# The most elements a form page may keep open (and list as active formatting ones): each tag searches these lists,
# so without a bound a deeply nested page takes quadratic time. A Comexio page nests a few dozen.
_MAX_OPEN_ELEMENTS = 256
_INNER_HTML = attrgetter("html")  # the HTML elements open inside a _ForeignElement
# Tag names the element sets below share.
_CAPTION = "caption"
_TABLE = "table"
_TBODY = "tbody"
_TEMPLATE = "template"
# Elements whose content is no option text (script-supporting elements); a <script/> stays open, as in a browser.
_HIDDEN_ELEMENTS = frozenset({"script", _TEMPLATE})
# Start tags that end an open <select> before a browser reads them.
_SELECT_ENDING_TAGS = frozenset({"input", "keygen", _TEXTAREA})
# The elements that start SVG and MathML (foreign) content, whose elements are no HTML form fields.
_FOREIGN_ROOTS = frozenset({"svg", "math"})
# Start tags a browser ignores inside an open <select>, so what follows them is still markup, not their text.
_IGNORED_IN_SELECT = (_TEXT_ONLY_ELEMENTS - _SELECT_ENDING_TAGS) | {"style"} | _FOREIGN_ROOTS
# Foreign elements whose content a browser reads as HTML again (html.parser reports tag names in lower case).
_SVG_HTML_INTEGRATION_POINTS = frozenset({"foreignobject", "desc", "title"})
_MATHML_TEXT_INTEGRATION_POINTS = frozenset({"mi", "mo", "mn", "ms", "mtext"})
_MATHML_GLYPH_TAGS = frozenset({"mglyph", "malignmark"})  # stay MathML inside a MathML text integration point
_HTML_ANNOTATION_ENCODINGS = frozenset({"text/html", "application/xhtml+xml"})
# HTML start tags that end foreign content (WHATWG "in foreign content"); a <font> only with one of the attributes.
_FOREIGN_BREAKOUT_TAGS = frozenset(
    {
        *("b", "big", "blockquote", "body", "br", "center", "code", "dd", "div", "dl", "dt", "em", "embed"),
        *("h1", "h2", "h3", "h4", "h5", "h6", "head", "hr", "i", "img", "li", "listing", "menu", "meta"),
        *("nobr", "ol", "p", "pre", "ruby", "s", "small", "span", "strong", "strike", "sub", "sup", _TABLE),
        *("tt", "u", "ul", "var"),
    }
)
_FONT_BREAKOUT_ATTRS = frozenset({"color", "face", "size"})
# HTML end tags that end foreign content as well.
_FOREIGN_BREAKOUT_END_TAGS = frozenset({"p", "br"})
# HTML elements without content or end tag: they are not left open.
_VOID_ELEMENTS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "keygen",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)
# The special HTML elements of the HTML standard: the end tag of another element does not reach past one.
_SPECIAL_ELEMENTS = frozenset(
    {
        *("address", "applet", "area", "article", "aside", "base", "basefont", "bgsound", "blockquote", "body", "br"),
        *("button", _CAPTION, "center", "col", "colgroup", "dd", "details", "dir", "div", "dl", "dt", "embed"),
        *("fieldset", "figcaption", "figure", "footer", "form", "frame", "frameset", "h1", "h2", "h3", "h4", "h5"),
        *("h6", "head", "header", "hgroup", "hr", "html", "iframe", "img", "input", "keygen", "li", "link"),
        *("listing", "main", "marquee", "menu", "meta", "nav", "noembed", "noframes", "noscript", "object", "ol"),
        *("p", "param", "plaintext", "pre", "script", "search", "section", "select", "source", "style", "summary"),
        *(_TABLE, _TBODY, "td", _TEMPLATE, _TEXTAREA, "tfoot", "th", "thead", "title", "tr", "track", "ul"),
        *("wbr", "xmp"),
    }
)
# Elements whose open element an end tag looks for in scope: it closes the elements above it, special or not, up to
# a scope boundary.
_SCOPE_BOUNDARIES = frozenset(
    {"applet", _CAPTION, "html", _TABLE, "td", "th", "marquee", "object", "select", _TEMPLATE}
)
_SCOPED_END_TAGS = frozenset(
    {
        *("address", "applet", "article", "aside", "blockquote", "button", "center", "details", "dialog", "dir"),
        *("div", "dl", "dd", "dt", "fieldset", "figcaption", "figure", "footer", "h1", "h2", "h3", "h4", "h5", "h6"),
        *("header", "hgroup", "listing", "main", "marquee", "menu", "nav", "object", "ol", "p", "pre", "search"),
        *("section", "select", "summary", "ul"),
    }
)
_LIST_ITEM_SCOPE_BOUNDARIES = _SCOPE_BOUNDARIES | {"ol", "ul"}
# Table parts: outside a <table> a browser ignores their start tag; their end tag looks for them in table scope.
_TABLE_PARTS = frozenset({_CAPTION, "col", "colgroup", _TBODY, "td", "tfoot", "th", "thead", "tr"})
_TABLE_SCOPE_BOUNDARIES = frozenset({"html", _TABLE, _TEMPLATE})
# Start tags a browser ignores in a page body (or only merges the attributes of): they open no element.
_IGNORED_IN_BODY = frozenset({"body", "frame", "frameset", "head", "html"})
# The root elements of a page: a repeated start tag of one makes no new element (see _ExtensionFormParser).
_ROOT_TAGS = frozenset({"html", "head", "body"})
# The only start tags a page can open with before its <head>: any other one implies the head (and later the body).
_BEFORE_HEAD_TAGS = frozenset({"html", "head"})
# The end tags that imply the head too when they come before it (the "anything else" case of the before-head mode).
_HEAD_IMPLYING_END_TAGS = frozenset({"html", "head", "body", "br"})
# The start tags that clear a browser's "frameset-ok" flag (an <input> too, unless it is hidden): after one of them a
# <frameset> is ignored. A page that has none of them yet may still be a frameset page; a <frame> counts only in one.
_FRAMESET_BLOCKERS = frozenset(
    {
        "applet", "area", "body", "br", "button", "dd", "dt", "embed", "hr", "iframe", "image", "img", "keygen",
        "li", "listing", "marquee", "object", "plaintext", "pre", "select", "table", "template", "textarea", "wbr",
        "xmp",
    }
)  # fmt: skip
# A <colgroup> holds only <col>s: anything else closes it, so its end tag never closes what follows it.
_OPENS_NO_ELEMENT = _IGNORED_IN_BODY | {"colgroup"}
_HEADINGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
_BUTTON_SCOPE_BOUNDARIES = _SCOPE_BOUNDARIES | {"button"}
# Formatting elements: their end tag runs the adoption agency, which looks for them in scope.
_FORMATTING_ELEMENTS = frozenset(
    {"a", "b", "big", "code", "em", "font", "i", "nobr", "s", "small", "strike", "strong", "tt", "u"}
)
# Start tags that close an open <p> first (unless a scope boundary or a <button> lies between); a <table> does so
# outside quirks mode, as on Comexio's pages, which declare <!DOCTYPE html>.
_CLOSES_A_P = frozenset(
    {
        *("address", "article", "aside", "blockquote", "center", "details", "dialog", "dir", "div", "dl"),
        *("fieldset", "figcaption", "figure", "footer", "header", "hgroup", "main", "menu", "nav", "ol", "p"),
        *("search", "section", "summary", "ul", "pre", "listing", "form", "li", "dd", "dt", "plaintext", "hr"),
        *("xmp", _TABLE, *_HEADINGS),
    }
)
# A new <li>, <dd> or <dt> closes an open one of its kind unless a special element other than these lies between.
_LIST_ITEM_TRANSPARENT = frozenset({"address", "div", "p"})
_DEFINITION_ITEMS = frozenset({"dd", "dt"})
# A new table cell, row or section closes the open one, with the parts inside it.
_TABLE_CELLS = frozenset({"td", "th"})
_TABLE_SECTIONS = frozenset({_TBODY, "thead", "tfoot"})
_TABLE_ROW_PARTS = {"tr": frozenset({"tr"}), **dict.fromkeys(_TABLE_SECTIONS, _TABLE_SECTIONS)}
# The open elements a table part start tag is placed in: it closes a <caption>, and a row or cell put straight into
# a table (or a cell into a section) gets the <tbody>/<tr> a browser inserts around it.
_TABLE_PART_HOSTS = frozenset({_TABLE, "tr", *_TABLE_SECTIONS})
_TABLE_PART_CLOSED = frozenset({_CAPTION, "td", "th"})  # a table part start tag inside one closes it first
_TABLE_CONTEXTS = _TABLE_PART_HOSTS | _TABLE_PART_CLOSED | {_TEMPLATE}
_IMPLIED_TABLE_PARENTS = {"tr": (_TBODY,), "td": (_TBODY, "tr"), "th": (_TBODY, "tr")}
# Elements that put a marker on the list of active formatting elements: formatting does not leak into them.
_FORMATTING_MARKERS = frozenset({"applet", _CAPTION, "marquee", "object", "td", _TEMPLATE, "th"})
# Start tags before which a browser does not reopen the formatting elements an end tag closed implicitly.
_NOT_RECONSTRUCTING = (
    _CLOSES_A_P
    | _TABLE_PARTS
    | _IGNORED_IN_BODY
    | _HIDDEN_ELEMENTS
    | _TEXT_ONLY_ELEMENTS
    | {"base", "basefont", "bgsound", "link", "meta", "style"}
) - {"xmp"}
# The start of a size attribute as a browser parses a non-negative integer (whitespace, an optional "+", digits).
_SIZE_RE = re.compile(rf"[{_HTML_WHITESPACE}]*\+?(\d+)", re.ASCII)
# The id of the form's protocol <select>, whose options read e.g. <option value="0" selected="selected">.
_WEBIO_PROTOCOL_SELECT_ID = "protocol"
# A protocol is a small number (leading zeros aside); a longer digit string is none, and int() would refuse one
# beyond 4300 digits, leading zeros included.
_PROTOCOL_VALUE_RE = re.compile(r"0*(\d{1,9})", re.ASCII)

# Plain-text answer of fupValueData for a plan that is not running (instead of a JSON value dict).
_JSON_START_CHARS = ("{", "[")
# dashboard/refresh answers under the request's keys; one fupValueData key per plan.
_RUN_STATE_KEY_PREFIX = "fup_"
# Each entry of the Web-IO add page's DeviceBaseList / DeviceList carries its key as "Id".
_WEBIO_LIST_ID_FIELD = "Id"
# fupValueData's answer for a stopped plan, observed live as "0:not_found".
_NOT_RUNNING_SENTINEL_RE = re.compile(r"\d+:not_found", re.ASCII)  # ASCII digits only, like Comexio's ids
# A plain decimal as dashboard/refresh sends a live value: sign, ASCII digits, optional fraction (comma or dot).
_PLAIN_DECIMAL_RE = re.compile(r"[+-]?\d+(?:[.,]\d+)?", re.ASCII)
# What /api/?action=set answers on success; anything else is logged at WARNING. "1" was observed
# live earlier, firmware 11.1.5 answers "0" to every write it stores (verified by reading the
# marker back, 2026-10-27). An unknown target answers "<n>:not_found" instead.
_API_SET_OK_ANSWERS = ("0", "1")
# What /api/?action=set answers (HTTP 200) for a target it does not let the API write: the 0-10 V
# analog outputs and the Quickbus IOs (verified live, firmware 11.1.5, 2026-11-04).
_API_SET_FORBIDDEN_ANSWER = "forbidden"  # compared case-insensitively
# The IOs sent to /api/: relay and dimmer outputs Q* and the Dali groups DG* (/api/ accepts DG1, so
# an API-only caller can still write them). It answers "Forbidden" for nearly every other output,
# so those go over the admin session's dashboard/refresh "set", as Comexio's own IO page does: the
# 0-10 V analog outputs AO* (2026-11-04) and the IOs Comexio writes itself, e.g. the
# Multiroom-Server's Next1, Alarm1, CurVol1 and the Touchpanel's Bright1, DisplayOn1, R (2026-11-06;
# verified live, firmware 11.1.5).
_API_WRITTEN_IO_RE = re.compile(r"(?:Q|DG)\d+", re.ASCII | re.IGNORECASE)
# The key of the one entry of a dashboard/refresh "set" request, and its answer for a stored value;
# an unknown extension or IO answers "0:io_not_found". A value beyond the IO's range is clamped.
_DASHBOARD_SET_KEY = "1"
_DASHBOARD_SET_OK_ANSWER = "1"

# save_fub form values: paper format ids and orientation ids as Comexio's plan dialog sends them.
_PLAN_PAPER_IDS = {"A3": "2", "A4": "3", "A5": "4"}
_PLAN_ORIENTATION_IDS = {"landscape": "0", "portrait": "1"}
_PLAN_DPI_RANGE = range(45, 121)
# save_fub / delete answer with a redirect whose query carries the verdict as one (key, value)
# parameter; the home page renders its message from exactly that parameter (verified live).
_REDIRECT_STATUSES = frozenset({HTTPStatus.MOVED_PERMANENTLY, HTTPStatus.FOUND, HTTPStatus.SEE_OTHER})
# Redirects the client follows itself (aiohttp would follow one anywhere: a pinned client to plain http://,
# without the pin, any client to another host). Like aiohttp, a POST turns into a GET on 301/302/303.
_FOLLOWED_REDIRECT_STATUSES = _REDIRECT_STATUSES | {HTTPStatus.TEMPORARY_REDIRECT, HTTPStatus.PERMANENT_REDIRECT}
_MAX_REDIRECTS = 10
_DEFAULT_PORTS = MappingProxyType({"http": 80, "https": _HTTPS_DEFAULT_PORT})
_PLAN_ADDED_CONFIRMATION = ("added", "1")
_PLAN_SAVED_CONFIRMATION = ("saved", "1")
_PLAN_DELETED_CONFIRMATION = ("delete", "ok")
# Element types and the catalog ref_id add_element needs for blocks that have no catalog entry.
_ELEMENT_TYPE_COMMENT = 14
_ELEMENT_TYPE_CONSTANT = 16
_ELEMENT_TYPE_SCENE = 13
_ELEMENT_TYPE_QUANTUM = 20
_ELEMENT_TYPE_QUANTUM_CENTRAL = 21
# add_element creates a new, empty scene for a scene block placed with this ref_id.
_NEW_SCENE_REF_ID = "-1"
_REF_ID_KEY = "ref_id"
_COMMENT_REF_ID = "3"
# Comexio normalizes a constant's ref_id to 0 once saved, but refuses ref_id 0 on create.
_CONSTANT_REF_ID = "1"
_COMMENT_WIDTHS = range(1, 6)
_CONNECTION_VALUE_TYPES = frozenset({"binary", "analog"})
_ID_KEY = "id"
# Cap of the error-page excerpt ComexioResponseError carries.
_ERROR_BODY_MAX_CHARS = 500
# Bytes of an error-status answer that are read at most: enough for the excerpt after collapsing whitespace, so a
# huge error page cannot fill memory.
_ERROR_BODY_MAX_BYTES = 64 * 1024
# Bytes of a successful answer that are read at most. A real answer is far smaller (the largest pages of an
# installation with 30 function plans are under 1 MB), so a device or proxy that sends more cannot fill memory:
# 16 MiB of payload, which reading and decoding hold about three times at once.
_RESPONSE_MAX_BYTES = 16 * 1024 * 1024
# Codecs that Python lists as text encodings but that transform a page instead of reading it (an escape sequence
# such as a backslash-n becomes a line feed); a declared charset naming one of them is treated as undeclared.
# The names are the CodecInfo.name values that codecs.lookup returns (hyphenated). idna is one of them: it reads
# ASCII text such as `xn--kche-0ra` as an internationalized label and returns another string.
_NOT_A_CHARSET = frozenset({"unicode-escape", "raw-unicode-escape", "punycode", "idna"})
_UTF8 = "utf-8"  # what a body without a usable charset is decoded as, like aiohttp's default fallback
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
class SystemInfo:
    """General system information from the system dashboard page.

    free_storage_percent is the free space of the Comexio's internal storage in percent (0-100),
    time_server the date & time form's time server settings; either is None if the page does not
    show it (logged at WARNING). A page that shows neither makes get_system_info raise
    ComexioDataError instead.
    """

    free_storage_percent: float | None
    time_server: TimeServerSettings | None


@dataclass(frozen=True, slots=True)
class CreatedFunctionPlan:
    """A plan create_function_plan created: its id and its $Fubs entry as the config lists it."""

    fub_id: int
    fubs_entry: dict[str, Any]


@dataclass(frozen=True, slots=True)
class PlacedModuleBlock:
    """A scene block or Quantum module placed on a plan, and the object Comexio created behind it.

    element_id is the plan element's id, ref_id the id of the new scene or Quantum module (the
    element's reference ref_id) — what save_function_plan_scene / save_function_plan_quantum_settings
    write to.
    """

    element_id: int
    ref_id: int


@dataclass(frozen=True, slots=True)
class WebioCommandForm:
    """The settings of one Web-IO command that only its edit form shows (see get_webio_command_form).

    Each is None when the form lacks the field (min, max and protocol also when the value is not numeric).
    parameter is the command's request path and data its Lua body, as a browser reads the two text fields: entities
    resolved, line breaks as "\\n", a first line break dropped.
    """

    min: float | None
    max: float | None
    protocol: int | None
    parameter: str | None = None
    data: str | None = None


@dataclass(frozen=True, slots=True)
class WebioBaseInfo:
    """A Web-IO class (Comexio's "device base") and whether Comexio offers to delete it.

    deletable is False while a device of the class still exists.
    """

    base_id: str
    deletable: bool


class IoAddress(NamedTuple):
    """One physical IO for get_io_live_states: its extension id and IO id, named so a swap stands out.

    ext_id is the key of the IO's extension group in $FubModules["1"] (parse_config's "ext_id"),
    io_id the IO's "Id" (parse_config's "id").
    """

    ext_id: int | str
    io_id: str


@dataclass(frozen=True, slots=True)
class LiveStates:
    """Live values from one dashboard refresh, keyed by plain numeric id per source.

    Markers and KNX objects share one numeric id space (marker 5 and KNX object 5 both exist),
    so they are kept apart instead of merged into one id-keyed dict.
    """

    markers: dict[str, Any]
    knx: dict[str, Any]


def io_written_over_admin_session(identifier: str) -> bool:
    """Whether ComexioClient.set_io_value writes this IO over the admin session instead of /api/.

    True for every IO but the relay and dimmer outputs Q1, Q2, ... and the Dali groups DG1, DG2, ...,
    which go to /api/: the 0-10 V analog outputs (AO1, ...) and the other IOs Comexio writes itself
    (Bright1, Alarm1, CurVol1, ...), which /api/ refuses.
    Such a write needs a logged-in admin session; a caller that keeps one can log in again on
    ComexioAuthenticationError.
    """
    return not _API_WRITTEN_IO_RE.fullmatch(identifier)


class _LoginGate:
    """Gives login() the session to itself, while every other request shares it.

    login() clears the cookie jar and starts a new session, so a request sent during a login would
    carry no session cookie and get the login form. A login therefore waits for the requests in
    flight (at most _LOGIN_WAIT_FOR_REQUESTS_SEC), and a request that comes up meanwhile waits until
    the login is done. A waiting login lets no new request start, so a steady stream of requests
    cannot starve it; the requests the login sends itself pass (they count as running, but a login never
    waits for them: it waits only for its _InFlight batch). A login queued behind another one
    holds nothing yet: a request can start before it, and it then waits for that request as well.
    A login that gave up waiting leaves the requests it waited for behind, so a later login does not
    wait for them again. A request must not start another one while it holds its slot: behind a
    waiting login, the inner one would wait for the outer one until the login gives up waiting.
    Likewise the login sends its requests from its own task: one sent from a task it starts would
    wait for the login to end. Cancellation never leaves the gate locked: a login or a request
    releases its hold in a finally, and a waiter has changed nothing yet.
    """

    def __init__(self) -> None:
        self._in_flight = _InFlight()
        self._running = 0  # every running request, the ones of a batch a login left behind too
        self._login_task: asyncio.Task[Any] | None = None
        self._no_login = asyncio.Event()
        self._no_login.set()

    @property
    def requests_in_flight(self) -> int:
        """How many requests are running: also those a login that gave up waiting left behind, and the login's own."""
        return self._running

    @asynccontextmanager
    async def request(self) -> AsyncIterator[None]:
        """Hold one request slot; waits while a login runs or waits."""
        if self._login_task is not None and self._login_task is asyncio.current_task():
            # One of the login's own requests: it waits for nothing and no login waits for it, but it is running.
            self._running += 1
            try:
                yield
            finally:
                self._running -= 1
            return
        while self._login_task is not None:
            await self._no_login.wait()
        in_flight = self._in_flight  # a request counts against the batch it started in
        in_flight.add()
        self._running += 1
        try:
            yield
        finally:
            self._running -= 1
            in_flight.remove()

    @asynccontextmanager
    async def login(self) -> AsyncIterator[None]:
        """Hold the session alone: waits for a running login, then for the requests in flight."""
        while self._login_task is not None:
            await self._no_login.wait()
        self._login_task = asyncio.current_task()
        self._no_login.clear()
        try:
            await self._wait_for_requests()
            yield
        finally:
            self._login_task = None
            self._no_login.set()

    async def _wait_for_requests(self) -> None:
        """Wait until no request is in flight, or _LOGIN_WAIT_FOR_REQUESTS_SEC has passed."""
        try:
            async with asyncio.timeout(_LOGIN_WAIT_FOR_REQUESTS_SEC):
                await self._in_flight.idle.wait()
        except TimeoutError:
            _LOGGER.warning(
                "Login goes ahead after waiting %s s; requests still in flight: %d, they may fail as not logged in",
                _LOGIN_WAIT_FOR_REQUESTS_SEC,
                self._in_flight.count,
            )
            self._in_flight = _InFlight()  # left behind: no later login waits for them again


class _InFlight:
    """The requests started in one batch: how many are still running, and an event set while none is."""

    def __init__(self) -> None:
        self.count = 0
        self.idle = asyncio.Event()
        self.idle.set()

    def add(self) -> None:
        self.count += 1
        self.idle.clear()

    def remove(self) -> None:
        self.count -= 1
        if not self.count:
            self.idle.set()


class ComexioClient:
    """Admin-session client for one Comexio IO-Server.

    host is "hostname", "ip" or either with ":port". With fingerprint (the SHA-256 fingerprint of
    the server's TLS certificate, see tls.fetch_certificate_fingerprint) every request goes over
    HTTPS and is refused with ComexioCertificateMismatchError unless the server presents exactly
    that certificate; without it the client uses plain HTTP. A pinned client follows a redirect only
    within its HTTPS origin. An invalid fingerprint string raises ValueError, a non-str TypeError.
    The port in host applies to whichever scheme is used. session must have its own cookie jar,
    since the admin login lives in it, and should have a total timeout: a login holds every other
    request back while it runs, and without one a stalled login request only gives up after
    _LOGIN_TIMEOUT_SEC. session.session_kwargs gives it both. The session timeout covers each
    request on the wire, not the time it waits for a login: per login up to
    _LOGIN_WAIT_FOR_REQUESTS_SEC plus _LOGIN_TIMEOUT_SEC; a caller that needs a tighter bound sets
    its own around the call. Call login() before any other admin method.
    api_username / api_password are the separate Comexio API user the set_*_value methods send as
    Basic Auth; without api_username they send no credentials at all.
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
        fingerprint: str | None = None,
    ) -> None:
        self._host = host
        self._username = username
        self._password = password
        self._api_username = api_username
        self._api_password = api_password
        self._session = session
        self._fingerprint = None if fingerprint is None else normalize_fingerprint(fingerprint)
        self._ssl: aiohttp.Fingerprint | bool = True
        if self._fingerprint is None:
            # Unpinned: _warn_plain_http flags non-local hosts before credentials go out.
            self._base_url = f"http://{host}"  # NOSONAR
        else:
            https_address(host)  # a pinned client must reach exactly the host the fingerprint was read from
            self._base_url = f"https://{host}"
            self._ssl = aiohttp.Fingerprint(bytes.fromhex(self._fingerprint))
        self._plain_http_warned = False
        self._gate = _LoginGate()
        # time.monotonic() of the last request that got an answer; the debug line about a request reads it.
        self._last_response_at: float | None = None

    @property
    def host(self) -> str:
        """The host this client talks to."""
        return self._host

    @property
    def fingerprint(self) -> str | None:
        """The pinned certificate fingerprint (64 lowercase hex digits), None when the client uses plain HTTP."""
        return self._fingerprint

    async def login(self) -> None:
        """RSA admin login (Comexio v11 scheme: PKCS#1 v1.5 blocks over salt + nonce + password).

        Raises ComexioAuthenticationError if the server rejects the credentials (or the password
        cannot be encoded for the login form), ComexioCertificateMismatchError if a pinned client
        meets a foreign certificate, ComexioConnectionError / ComexioResponseError /
        ComexioDataError if the login could not be carried out at all, ComexioConnectionError also
        if the login itself takes longer than _LOGIN_TIMEOUT_SEC; the time it waits for a running
        login or for the requests in flight does not count, a caller that needs a bound on the whole
        call sets its own. It starts a new session, so it waits for the requests in flight and a
        request made meanwhile waits until the login is done (/api/ writes included: their answer
        may set a cookie in the jar the login renews);
        neither fails because the cookie jar is renewed under it. The wait for the requests in flight
        ends after _LOGIN_WAIT_FOR_REQUESTS_SEC: one still running then may fail as not logged in.
        """
        self._warn_plain_http()
        async with self._gate.login():
            try:
                async with asyncio.timeout(_LOGIN_TIMEOUT_SEC):
                    await self._rsa_login()
            except TimeoutError as err:  # the requests map their own timeouts to ComexioConnectionError
                raise ComexioConnectionError(
                    f"Login to {self._host} took longer than {_LOGIN_TIMEOUT_SEC:g} s"
                ) from err

    async def _rsa_login(self) -> None:
        """The login itself; login() holds the session alone around it."""
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
        # The login POST's own status and body are not meaningful (they are not read, so even a compressed or
        # endless answer cannot fail the login); the admin page below is the verdict.
        await self._request_unread("POST", _LOGIN_PATH, what="Login", data=form, headers={"Referer": login_url})
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
        except (KeyError, TypeError, ValueError, OverflowError) as err:
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
        refresh: dict[str, Any] = {
            str(i): {_ACTION_KEY: _ACTION_GET, "MarkerName": f"M{i}"} for i in range(1, marker_count + 1)
        }
        refresh |= {
            f"{_KNX_LIVE_KEY_PREFIX}{i}": {_ACTION_KEY: _ACTION_GET, "KnxIo": f"K{i}", "Unit": "any"}
            for i in range(1, knx_max_id + 1)
        }
        refresh[_MESSAGES_KEY] = {_ACTION_KEY: "messages"}

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

    async def get_io_live_states(self, ios: Iterable[IoAddress]) -> dict[str, str]:
        """Live values of physical IOs, all in one dashboard/refresh request of their own.

        ios are IoAddress(ext_id, io_id) entries (a plain (ext_id, io_id) tuple works at runtime, but
        the named form keeps the two ids from being swapped): ext_id is the key of the IO's extension
        group in $FubModules["1"] (a non-negative int or digit string, like parse_config's "ext_id"),
        io_id its "Id". An integer string counts by value: "007" is extension 7. One IO id under
        two different extension ids is a ValueError.
        Returns {IO id: value as Comexio sent it, "," normalized to "."}
        (a numeric string such as "74" or "9.96078"). An IO the answer leaves out, or answers
        with anything that is not a number — "0:not_found" for an inactive IO, a PHP warning —
        is absent from the result, never 0: a caller keeps its last known value for it. Such an
        answer is logged at DEBUG if it is Comexio's "<n>:not_found" or the key is missing, otherwise
        (an explicit null included) at WARNING (one line per call, IO ids as repr, answer types only).
        If the answer holds none of the requested IOs at all (a format change or wrong ids), that is
        one WARNING as well, since DEBUG lines would hide it.
        """
        pairs = _io_ext_ids(ios)
        if not pairs:
            return {}
        payload = {
            f"{_IO_LIVE_KEY_PREFIX}{io_id}": {_ACTION_KEY: _ACTION_GET, "ExtId": ext_id, "IOId": io_id}
            for io_id, ext_id in pairs.items()
        }
        data = await self._dashboard_refresh(payload, what="IO live states")
        result = data.get(_RESULT_KEY) if isinstance(data, dict) else None
        if not isinstance(result, dict):
            raise ComexioDataError(f"IO live states response has no result object: {_excerpt(data)}")
        values, unexpected = _collect_io_values(result, pairs)
        if not any(f"{_IO_LIVE_KEY_PREFIX}{io_id}" in result for io_id in pairs):
            # Keys only: they show a format change or wrong ids, the values would put IO readings in the log.
            _LOGGER.warning(
                "dashboard/refresh answered none of the %d requested IOs (format change or wrong ids?), keys: %s",
                len(pairs),
                _excerpt(sorted(result)),
            )
        if unexpected:
            _LOGGER.warning(
                "Dropped %d unexpected IO live answers from dashboard/refresh (IO id: answer type): %s",
                len(unexpected),
                "; ".join(unexpected),
            )
        return values

    async def get_function_plan_connection_values(self, fub_id: int) -> dict[str, list[Any]]:
        """Live output values per source element of one running plan (Studio's fupValueData).

        Returns {source FubElementId: [value per output row]}; a block with several outputs
        reports one list for all of them, indexed by output IOPos. Returns {} if the plan is not
        running (Comexio then answers with a plain-text sentinel such as "0:not_found") or reports
        an empty value set. Raises ComexioDataError if the answer carries no connection entry.
        """
        payload = {"connection": {_ACTION_KEY: "fupValueData", "fupId": fub_id}}
        data = await self._dashboard_refresh(payload, what=f"Connection values of plan {fub_id}")
        result = data.get(_RESULT_KEY) if isinstance(data, dict) else None
        if not isinstance(result, dict) or "connection" not in result:
            raise ComexioDataError(f"Connection values of plan {fub_id}: no result.connection in {_excerpt(data)}")
        raw = result["connection"]
        if isinstance(raw, str) and raw and not raw.lstrip().startswith(_JSON_START_CHARS):
            _LOGGER.debug("No connection values for plan %s (not running: %s)", fub_id, raw)
            return {}
        return {} if raw in _EMPTY_CONNECTION_VALUES else _connection_values(raw, fub_id)

    async def get_function_plan_run_states(self, fub_ids: Iterable[int]) -> dict[int, bool]:
        """Whether each plan is running, for all of fub_ids in one dashboard/refresh request.

        Asks fupValueData for every plan under its own key: a running plan answers with its
        value set as JSON ("{}" when it has no value elements), a stopped one with a plain-text
        sentinel such as "0:not_found". A plan the answer leaves out, or answers with anything
        else (a PHP warning, an error text, broken JSON), is absent from the result, never
        guessed: a caller keeps its last known state for it.
        """
        ids = list(dict.fromkeys(fub_ids))
        _require_ints(**{f"fub_ids[{index}]": fub_id for index, fub_id in enumerate(ids)})
        if not ids:
            return {}
        payload = {f"{_RUN_STATE_KEY_PREFIX}{fub_id}": {_ACTION_KEY: "fupValueData", "fupId": fub_id} for fub_id in ids}
        data = await self._dashboard_refresh(payload, what="Function plan run states")
        result = data.get(_RESULT_KEY) if isinstance(data, dict) else None
        if not isinstance(result, dict):
            raise ComexioDataError(f"Function plan run states response has no result object: {_excerpt(data)}")
        states: dict[int, bool] = {}
        for fub_id in ids:
            raw = result.get(f"{_RUN_STATE_KEY_PREFIX}{fub_id}")
            if (running := _plan_running(raw)) is None:
                _LOGGER.debug("Run state of plan %s unknown, fupValueData answered %s", fub_id, _excerpt(raw))
            else:
                states[fub_id] = running
        return states

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
        elements or connections collection (an empty one is real), like load_function_plan(strict=True)
        does. A plan skipped by strict mode is absent from the result, just like a plan the server
        leaves out or one that does not exist, and is logged at WARNING.
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
            except (AttributeError, TypeError, ValueError, OverflowError):
                _LOGGER.warning("Skipping malformed marker entry %s", _excerpt(marker_id))
        return titles

    async def is_logged_in(self) -> bool:
        """Whether the admin session is (still) logged in, asked with a cheap read-only admin request.

        True for the JSON object the request returns, False for Comexio's login form; any other
        answer raises ComexioDataError, so a mere error page never passes for "logged out". A
        dropped connection — typically a pooled keep-alive connection Comexio had already
        closed — is asked once more; any other failure raises as the request would.
        """
        body = await self._session_check_body()
        if _LOGIN_FORM_SUBMIT_MARKER in body:
            return False
        try:
            answer = json.loads(body)
        except ValueError as err:
            raise ComexioDataError(f"Session check: response is not JSON: {_excerpt(body)}") from err
        if not isinstance(answer, dict):
            raise ComexioDataError(f"Session check: expected a JSON object, got {_excerpt(body)}")
        return True

    async def _session_check_body(self) -> str:
        """Body of the session check request, asked once more after a dropped connection."""
        what = "Session check"
        return await _once_more_if_dropped(
            what,
            lambda: self._request_text(
                "POST", _BUS_WORKLOAD_PATH, what=what, headers=self._xhr_headers(_BUS_WORKLOAD_REFERER_PATH)
            ),
        )

    async def get_onewire_device_count(self) -> int:
        """The number of 1-Wire devices the IO home page counts (0: none, so get_onewire_status can be skipped).

        One request for the whole IO home page (about 0.9 MB on a full installation).
        """
        html = await self._request_admin_text("GET", _BUS_WORKLOAD_REFERER_PATH, what=_IO_HOME_PAGE)
        return parse_onewire_device_count(html)

    async def get_doorstations(self) -> dict[int, Doorstation]:
        """The Doorstations of the IO-Server, keyed by extension id (empty: the page shows none).

        One request for the whole IO home page (see get_onewire_device_count); see parse_doorstations.
        """
        html = await self._request_admin_text("GET", _BUS_WORKLOAD_REFERER_PATH, what=_IO_HOME_PAGE)
        return parse_doorstations(html)

    async def get_extension(self, ext_id: int) -> ExtensionInfo:
        """The settings of one extension (name, serial, type, IP address), read from its edit form.

        One small request (about 2 KB) to /admin/extension/edit/ with json_return=true, which makes Comexio answer
        with the HTML of the form (not JSON); see parse_extension_form. For a Doorstation the IP address is the one
        its camera stream is served under (ExtensionInfo.ip). That is None for `local` but also for a host name, an
        IPv6 address or a malformed one; ExtensionInfo.location keeps the raw field to tell them apart. ext_id must
        be a positive int (not a bool): anything else raises TypeError or ValueError before a request. An id Comexio
        does not know, a form without an IP address field and a page that nests more than 256 elements raise
        ComexioDataError, the login form ComexioAuthenticationError.
        """
        ext_id = require_ext_id(ext_id)
        html = await self._request_admin_text(
            "GET",
            _EXTENSION_EDIT_PATH,
            what=f"Extension {ext_id!r} edit form",
            params={"id": str(ext_id), "json_return": "true"},
        )
        return parse_extension_form(ext_id, html)

    async def get_api_settings(self, *, require_https: bool = False) -> ApiSettings:
        """The API settings of the IO-Server (switch, login, access per data type), read from its API page.

        One request with the admin session to /admin/api/home; see api_settings.ApiSettings. The page holds the API
        password in clear text: it is compared with the API password this client was created with and not kept, so
        the result has only the verdict (ApiSettings.password_matches), and nothing here logs it. For a write that
        failed this tells whether the settings explain it (ApiSettings.write_blockers). A page without the settings
        this reads raises ComexioDataError, the login form ComexioAuthenticationError.

        The page comes over the transport this client uses, so over plain HTTP anyone listening on the way sees the
        password in it (a write over /api/ sends it as Basic Auth the same way). With require_https=True a client
        without a pinned certificate raises ComexioInsecureTransportError before any request; the default False
        reads the page over whatever the client uses.
        """
        if require_https and self._fingerprint is None:
            raise ComexioInsecureTransportError(
                "API settings page not read: the client talks plain HTTP and the page holds the API password in clear "
                "text (require_https=True; give the client a certificate fingerprint to use HTTPS)"
            )
        html = await self._request_admin_text("GET", _API_SETTINGS_PATH, what="API settings page")
        return parse_api_settings(html, api_username=self._api_username, api_password=self._api_password)

    def doorstation_stream_url(self, camera_id: int) -> str:
        """URL of the MJPEG stream of a Doorstation camera on the IO-Server (Doorstation.camera_id)."""
        return f"{self._base_url}{doorstation_stream_path(camera_id)}"

    async def get_doorstation_snapshot(self, camera_id: int) -> bytes:
        """The current image of a Doorstation camera: the first complete JPEG of its MJPEG stream.

        Reads the stream with the admin session until the first frame is complete, then closes it. A stream that
        brings no frame within _SNAPSHOT_TIMEOUT_SEC seconds (the wait for the login gate included) raises
        ComexioConnectionError, as does a stream that breaks off; one that ends or passes _MAX_SNAPSHOT_BYTES
        without a frame, or whose frame is malformed, raises ComexioDataError, as does a stream sent compressed
        (it is asked for unencoded, see _IDENTITY_ENCODING), the login form
        ComexioAuthenticationError. A malformed frame shows only once a JPEG end marker arrived: a stream without
        one runs into the byte cap or the timeout.
        """
        path = doorstation_stream_path(camera_id)
        what = f"Doorstation camera {camera_id} snapshot"
        deadline = asyncio.timeout(_SNAPSHOT_TIMEOUT_SEC)
        try:
            async with (
                deadline,
                self._response("GET", path, what=what, deadline=deadline) as resp,
            ):
                if resp.status != HTTPStatus.OK:
                    raise _status_error(what, resp.status, _collapse_error_body(await _error_page_text(resp, what)))
                if encoding := _content_encoding(resp):
                    raise ComexioDataError(f"{what}: the stream came {_excerpt(encoding)}-encoded, not unencoded")
                return await _read_first_frame(resp, what)
        except (aiohttp.ClientError, TimeoutError) as err:
            # Only the snapshot's own deadline means no frame came; aiohttp's timeouts (the session's total one,
            # connect, socket read) are transport errors like on every other request.
            if deadline.expired():
                _raise_from_stripped(
                    ComexioConnectionError(f"{what}: no complete frame within {_SNAPSHOT_TIMEOUT_SEC} s"), err
                )
            _raise_from_stripped(self._transport_error(what, err), err)

    async def get_onewire_sensors(self) -> dict[str, OneWireSensor]:
        """The active 1-Wire sensors of the configuration, keyed by address (OT1, OH3, ...).

        Scrapes the function module page (see get_raw_config), so it costs one config fetch.
        """
        raw = await self.get_raw_config()
        return parse_onewire_config(raw.variables["FubModules"])

    async def get_onewire_status(self) -> dict[str, OneWireStatus]:
        """The live state of every 1-Wire sensor (read1WireStati, the 1-Wire monitor), keyed by address.

        Needs the admin session (see login). A sensor's value, read error (online False) and
        not-found state (visible False) come from the same answer. The monitor is not an official
        interface: a changed answer raises ComexioDataError.
        """
        payload = await self._request_json(
            "POST",
            _ONEWIRE_STATUS_PATH,
            what="1-Wire status",
            data={"all": "all"},
            headers={"X-Requested-With": "XMLHttpRequest", "Referer": f"{self._base_url}{_ONEWIRE_MONITOR_PATH}"},
        )
        return parse_onewire_status(payload)

    async def get_bus_workload(self) -> dict[str, Any]:
        """Internal bus workload and SD card presence (the admin in/output info)."""
        data = await self._request_json(
            "POST", _BUS_WORKLOAD_PATH, what="Bus workload", headers=self._xhr_headers(_BUS_WORKLOAD_REFERER_PATH)
        )
        if not isinstance(data, dict):
            raise ComexioDataError(f"Bus workload payload is not an object: {_excerpt(data)}")
        return data

    async def get_system_info(self) -> SystemInfo:
        """General system information (free storage, time server settings) from the system dashboard page.

        The page is requested without any query parameter: the same URL with restart=1 reboots
        the system (system_emergency_reboot). Raises ComexioAuthenticationError if the session is
        no longer logged in and ComexioDataError if the page shows none of the information; a part
        that is missing alone is None and logged at WARNING.
        """
        html = await self._request_admin_text("GET", _SYSTEM_DASHBOARD_PATH, what="System info")
        info = SystemInfo(free_storage_percent=parse_free_storage_percent(html), time_server=parse_time_server(html))
        if info.free_storage_percent is None and info.time_server is None:
            raise ComexioDataError(f"System dashboard page ({len(html)} chars) shows no system information")
        for part, value in (("free_storage_percent", info.free_storage_percent), ("time_server", info.time_server)):
            if value is None:
                _LOGGER.warning("System dashboard page of %r does not show %s", self._host, part)
        return info

    async def get_uptime(self) -> timedelta:
        """How long the Comexio system has been running (the system dashboard's active time).

        Raises ComexioAuthenticationError if the session is no longer logged in and
        ComexioDataError if the answer carries no parseable duration.
        """
        # The login form check by its submit button, not by its (German) heading: any UI language.
        body = await self._request_admin_text(
            "POST", _RUN_DURATION_PATH, what="Uptime", headers=self._xhr_headers(_SYSTEM_DASHBOARD_PATH)
        )
        try:
            data = json.loads(body)
        except ValueError as err:
            raise ComexioDataError(f"Uptime answer is not JSON: {_excerpt(body)}") from err
        duration = data.get("duration") if isinstance(data, dict) else None
        uptime = parse_run_duration(duration) if isinstance(duration, str) else None
        if uptime is None:
            raise ComexioDataError(f"Uptime answer carries no parseable duration: {_excerpt(data)}")
        return uptime

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
        """Write a marker's value through the /api/ interface.

        Success is only confirmed by the answer "0" or "1". A "<n>:not_found" answer raises
        ComexioRequestRejectedError without a warning; any other unexpected answer is logged at WARNING.
        A non-finite float (nan, inf) raises ValueError.
        """

        await self._api_set({"marker": f"M{marker_id}"}, value, what=f"Writing marker M{marker_id}")

    async def set_knx_value(self, knx_id: int, value: float) -> None:
        """Write a KNX object's value through the /api/ interface (query parameter knx=K<id>).

        Success is only confirmed by the answer "0" or "1". A "<n>:not_found" answer raises
        ComexioRequestRejectedError without a warning; any other unexpected answer is logged at WARNING.
        A non-finite float (nan, inf) raises ValueError.
        """

        await self._api_set({"knx": f"K{knx_id}"}, value, what=f"Writing KNX object K{knx_id}")

    async def set_io_value(self, ext: str, io: str, value: float) -> None:
        """Write an IO's value; ext is the extension name, io its identifier.

        A relay or dimmer output Q* and a Dali group DG* go through the /api/ interface, without an admin
        login: success is only confirmed by the answer "0" or "1", a "<n>:not_found" or "Forbidden" answer
        raises ComexioRequestRejectedError, any other unexpected answer is logged at WARNING. Every other IO
        (io_written_over_admin_session) goes through the admin session instead, as /api/ refuses it: needs a
        logged-in session (else ComexioAuthenticationError), and any answer but "1" raises
        ComexioRequestRejectedError. "1" only says Comexio stored the value: one beyond the IO's range is
        clamped, and a momentary IO (parse_config) falls back to 0 right after. A Quickbus IO cannot be
        addressed by ext and identifier (they repeat across its devices) and must not be passed: the admin
        session could write another device's IO of that name.
        A non-finite float (nan, inf) raises ValueError.
        """
        what = f"Writing IO {io!r} of {ext!r}"
        if io_written_over_admin_session(io):
            await self._dashboard_set_io(ext, io, value, what=what)
            return
        await self._api_set({"ext": ext, "io": io}, value, what=what)

    async def _dashboard_set_io(self, ext: str, io: str, value: float, *, what: str) -> None:
        """One dashboard/refresh "set" of an IO over the admin session, as Comexio's IO page sends it."""
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError(f"{what}: {value!r} is not a finite number")
        payload = {_DASHBOARD_SET_KEY: {_ACTION_KEY: "set", "EName": ext, "IOIdentifier": io, "Value": value}}
        # Comexio closes an idle connection: the next request fails on it before Comexio handles it, so
        # asking once more sets the value once — also for a momentary IO, where a second set would be a
        # second key press. Without the retry, the first write after a quiet spell would fail.
        data = await _once_more_if_dropped(what, lambda: self._dashboard_refresh(payload, what=what))
        result = data.get(_RESULT_KEY) if isinstance(data, dict) else None
        answer = result.get(_DASHBOARD_SET_KEY) if isinstance(result, dict) else None
        if answer != _DASHBOARD_SET_OK_ANSWER:
            raise ComexioRequestRejectedError(f"{what} was not accepted: Comexio answered {_excerpt(data)}")
        _LOGGER.debug("%s: %s", what, answer)

    # --- Web-IO lifecycle (admin session) ---

    async def get_webio_base_info(self, class_name: str) -> WebioBaseInfo | None:
        """The Web-IO class named class_name, or None if the server has no such class.

        None is only returned for a page that was fetched and really lists no such class — a
        failed fetch raises, so a caller never uploads a duplicate class next to one that is
        still there. Raises ComexioAuthenticationError if the session is not logged in.
        """
        base_list, _ = await self._get_webio_lists()
        if (base_id := _find_by_label(base_list, "Identifier", class_name)) is None:
            return None
        window = await self._request_admin_text("GET", _WEBIO_BASE_WINDOW_PATH, what="Web-IO base window")
        delete_link = re.compile(rf"delete_web_device_base/\?id={re.escape(base_id)}(?!\d)")
        return WebioBaseInfo(base_id=base_id, deletable=delete_link.search(window) is not None)

    async def get_webio_device_id(self, device_name: str) -> str | None:
        """Id of the Web-IO device named device_name, or None if the server has no such device.

        Same contract as get_webio_base_info: None only for a fetched page without that device.
        """
        _, device_list = await self._get_webio_lists()
        return _find_by_label(device_list, "Name", device_name)

    async def _get_webio_lists(self) -> tuple[dict[str, Any], dict[str, Any]]:
        """(classes, devices) from the Web-IO add page; ComexioDataError unless it carries both lists.

        A page without the lists is not the add page, so it must not pass as one listing no
        such class or device: callers upload a class or recreate a device on that basis.
        """
        html = await self._request_admin_text("GET", _WEBIO_ADD_PATH, what="Web-IO add page")
        if (lists := parse_webio_add_page(html)) is None:
            raise ComexioDataError("The Web-IO add page carries no DeviceBaseList/DeviceList, the page layout changed")
        return lists

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
        Max, Protocol — HTTP when absent, an int otherwise (ValueError) —, optionally HeaderModifier /
        PostGet / Authentication — the KNX loopback command needs its own values for those three).
        base_id is the device's class, only used for a
        new command. command_id must be numeric (ValueError otherwise). Comexio's answer carries
        no verdict, so returning only means the request was accepted — the next config scrape
        shows whether the command is there.
        """
        protocol = command.get("Protocol", WEBIO_PROTOCOL_HTTP)
        if type(protocol) is not int:  # bool is an int subclass; None is an unread form value
            raise ValueError(f"Web-IO command protocol must be an int, got {protocol!r}")
        payload: dict[str, Any] = {
            "dlg_web_device_id": str(device_id),
            "protocol": protocol,
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
        """(Min, Max) of one Web-IO command, read from its edit form (see get_webio_command_form)."""
        form = await self._read_command_form(command_id, device_id, with_texts=False)
        return form.min, form.max

    async def get_webio_command_form(self, command_id: str | int, device_id: str | int) -> WebioCommandForm:
        """Min, Max, protocol, path ("parameter") and body ("data") of one Web-IO command, from its edit form.

        The config scrape never carries these fields of Web-IO commands, so the edit form is the
        only source. A field that is missing or not numeric comes back as None (so does a path or
        body textarea the form lacks); a form without any
        min/max field raises ComexioDataError (or ComexioAuthenticationError for the login form), as
        does a page that nests more than 256 elements.
        """
        return await self._read_command_form(command_id, device_id, with_texts=True)

    async def _read_command_form(
        self, command_id: str | int, device_id: str | int, *, with_texts: bool
    ) -> WebioCommandForm:
        """get_webio_command_form; with_texts=False skips _command_texts, which a range read does not need."""
        html = await self._request_admin_text(
            "GET",
            _WEBIO_EDIT_COMMAND_PATH,
            what=f"Web-IO command {command_id} edit form",
            params={"Id": str(command_id), "TestDevice": str(device_id)},
        )
        v_min, v_max = _command_range(html, command_id)
        texts = _command_texts(html) if with_texts else {}
        return WebioCommandForm(
            min=v_min,
            max=v_max,
            protocol=_command_protocol(html, command_id),
            parameter=texts.get(_WEBIO_PARAMETER_FIELD),
            data=texts.get(_WEBIO_DATA_FIELD),
        )

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
        except (KeyError, TypeError, ValueError, OverflowError) as err:
            raise ComexioDataError(f"{what}: answer carries no marker id: {_excerpt(result)}") from err
        _LOGGER.debug("Created marker M%s (binary=%s)", marker_id, binary)
        return marker_id

    async def rename_marker(
        self,
        marker_id: int,
        name: str,
        *,
        binary: bool,
        default: float = 0,
        store_memory: bool = False,
    ) -> None:
        """Set a marker's title (after Comexio's own uniqueness check).

        Comexio's marker save takes the whole form, so it also sets the marker's default value
        and "store in memory" flag. Leaving them out does not keep them: verified live 2026-10-10,
        an omitted default became 0 (the flag was kept). To rename a marker in use, pass its
        current values — the raw config's FubModules["2"] entry carries them as "DefaultValue"
        and "Store" ("Store" is a 0/1 number there: convert it to a bool yourself, a raw number
        is refused so an unexpected value is never guessed into on/off). Comexio has no
        conditional save, so a change made to either setting between that read and this call is
        overwritten — read them right before renaming. The defaults (0, off) suit a marker just
        made with create_marker. binary must match the marker's real type. The marker's live
        value is not touched (the form's value_<id> field did not change it, verified live
        2026-10-10).
        Raises TypeError/ValueError, before anything is sent, if default is not a finite int or
        float or store_memory is not a bool — a value sent anyway would silently overwrite the
        marker's setting. Raises ComexioRequestRejectedError if the name is taken or the save is
        not confirmed.
        """
        if not isinstance(store_memory, bool):
            raise TypeError(f"store_memory must be a bool, not {type(store_memory).__name__}")
        default_value = _form_number(default)
        what = f"Renaming marker M{marker_id}"
        await self._check_name_unique("memory", marker_id, name, what=what)
        marker_type = _marker_type(binary)
        payload = {
            "id": str(marker_id),
            "default_default": default_value,
            "default_type": marker_type,
            "name": name,
            "type": marker_type,
            f"value_{marker_id}": "0",
            "default": default_value,
            "store_memory": "1" if store_memory else "0",
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
        _require_ints(marker_id=marker_id)
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
    ) -> CreatedFunctionPlan:
        """Create an empty, inactive function plan; its id and $Fubs entry.

        paper_format is "A3", "A4" or "A5", orientation "landscape" or "portrait", dpi 45-120.
        Comexio's answer carries no id, so the new plan is looked up by name in a fresh config
        scrape. Raises ComexioRequestRejectedError if the name is taken or Comexio does not
        confirm the plan, ComexioCreatedWithoutIdError if it confirmed the plan but the scrape
        failed or $Fubs does not list it exactly once — the plan exists, do not create it again.
        A ComexioConnectionError from the create request itself leaves open whether the plan
        was created, and so does a ComexioDataError (an answer longer than _RESPONSE_MAX_BYTES or sent
        compressed); look it up by name before creating it again.
        """
        what = f"Creating function plan {name!r}"
        form = _plan_form(name, comment, paper_format, orientation, dpi)
        await self._check_name_unique("fub", None, name, what=what)
        form.update({"fub_position": "-1", "fub_active": "0", "fub_reset_on_close": "0", "fub_create": "Erzeugen"})
        await self._post_plan_form(form, _PLAN_ADDED_CONFIRMATION, what=what)
        try:
            config = await self.get_raw_config()
            return _plan_by_name(config.variables.get("Fubs"), name, what=what)
        except ComexioError as err:
            raise ComexioCreatedWithoutIdError(
                f"{what}: Comexio confirmed the plan, only reading back its id failed: {err}"
            ) from err

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
        Raises ComexioRequestRejectedError if Comexio does not confirm the save. A ComexioDataError (an answer
        longer than _RESPONSE_MAX_BYTES or sent compressed) leaves open whether the save took effect.
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

        Raises ComexioRequestRejectedError if Comexio does not confirm the deletion. A ComexioDataError (an answer
        longer than _RESPONSE_MAX_BYTES or sent compressed) leaves open whether the plan is gone.
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
        if not _redirect_confirms(location, _PLAN_DELETED_CONFIRMATION):
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
        _require(
            "a mapping or None", lambda value: value is None or isinstance(value, Mapping), {"connection": connection}
        )
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

    async def add_function_plan_scene(self, fub_id: int, *, x: float, y: float) -> PlacedModuleBlock:
        """Place a new scene block on a plan; returns its element id and the id of the scene Comexio created.

        The new scene has Comexio's default content (one scene "Alles Aus", no channels);
        save_function_plan_scene gives it its config, and with it its pins.
        """
        form = {"name": "", _REF_ID_KEY: _NEW_SCENE_REF_ID, "type": str(_ELEMENT_TYPE_SCENE), "id": "0"}
        return await self._add_module_block(fub_id, form, x, y, what=f"Placing a scene block on function plan {fub_id}")

    async def add_function_plan_quantum_module(
        self, fub_id: int, type_id: int, *, central: bool = False, x: float, y: float
    ) -> PlacedModuleBlock:
        """Place a new Quantum module of type_id on a plan; returns its element id and the new module's id.

        type_id is the module type ($CombinedModuleBaseSettings key, the TypeId of a $FubModules
        row), not the id of an existing module: Comexio always creates a new module, with that
        type's default settings and rules. central places the type's central module (element type
        21) instead of a single one (20).
        """
        _require_ints(type_id=type_id)
        if type_id <= 0:
            raise ValueError(f"type_id must be a positive Quantum module type, not {type_id}")
        if not isinstance(central, bool):
            raise TypeError(f"central must be a bool, not {type(central).__name__}")
        element_type = _ELEMENT_TYPE_QUANTUM_CENTRAL if central else _ELEMENT_TYPE_QUANTUM
        form = {"name": "", _REF_ID_KEY: str(type_id), "type": str(element_type), "id": "undefined"}
        what = f"Placing a Quantum module of type {type_id} (element type {element_type}) on function plan {fub_id}"
        return await self._add_module_block(fub_id, form, x, y, what=what)

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

    async def save_function_plan_block_settings(self, element_id: int, settings: Mapping[str, str]) -> None:
        """Save the settings of one logic block ($FubBaseConfig) the way Comexio's editor does.

        settings maps setting names to the string values Comexio stores (shutter travel times,
        the extended view "autohide", ...); only the named settings are written, others stay.
        Raises TypeError for settings that are not a mapping of str to str, ValueError for no
        settings or an empty setting name (nothing is sent), and ComexioRequestRejectedError if
        Comexio does not confirm the save.
        """
        _require_ints(element_id=element_id)
        if not isinstance(settings, Mapping):
            raise TypeError(f"settings must be a mapping of names to values, not {type(settings).__name__}")
        if not settings:
            raise ValueError("No block settings given")
        _require_strs(**{f"settings[{name!r}]": value for name, value in settings.items()})
        _require_strs(**{f"setting name {name!r}": name for name in settings})
        if "" in settings:
            raise ValueError("A block setting name must not be empty")
        what = f"Saving block settings {sorted(settings)} of function plan element {element_id}"
        form = {"id": str(element_id)} | {f"element_{name}": value for name, value in settings.items()}
        form["timestamp"] = _js_timestamp()
        result = await self._plan_json(_PLAN_SAVE_BLOCK_SETTINGS_PATH, form, what=what)
        saved_key, saved_value = _PLAN_SAVED_CONFIRMATION
        if str(result.get(saved_key)) != saved_value:
            raise ComexioRequestRejectedError(f"{what} was not confirmed: {_excerpt(result)}")

    async def save_function_plan_scene(self, scene_id: int, config: Mapping[str, Any]) -> None:
        """Save a scene block's config the way Comexio's scene dialog does.

        config has the shape of a $CurrentSceneConfig entry (name, category, allow_save,
        scene_toggle_off, common channels, scenes with their values); its "id" is set to scene_id.
        It replaces the whole config: channel, scene and value rows without an "id" are created,
        rows it leaves out are deleted, and Comexio derives the block's pins from it. Raises
        TypeError for a config that is not a JSON-serializable mapping with str keys, ValueError for
        a scene id that is not positive or an empty config (it would delete the whole scene; nothing
        is sent either way) and ComexioRequestRejectedError if Comexio does not confirm the save.
        """
        _require_positive_ids(scene_id=scene_id)
        if not isinstance(config, Mapping):
            raise TypeError(f"config must be a mapping, not {type(config).__name__}")
        if not config:
            raise ValueError("No scene config given")
        try:
            if _has_non_str_key(config):
                raise TypeError("config has a key that is not a str")
            payload = json.dumps({**config, _ID_KEY: str(scene_id)}, separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError, RecursionError) as err:
            raise TypeError(f"config is not JSON-serializable: {err}") from err
        what = f"Saving the config of scene {scene_id}"
        result = await self._plan_json(_PLAN_SAVE_SCENE_PATH, {"id": str(scene_id), "json": payload}, what=what)
        if "error" in result or str(result.get(_RESULT_KEY)) != "1":
            raise ComexioRequestRejectedError(f"{what} was not confirmed: {_excerpt(result)}")

    async def save_function_plan_quantum_settings(
        self,
        element_id: int,
        module_id: int,
        settings: Mapping[str, str],
        *,
        central: bool = False,
        name: str | None = None,
    ) -> None:
        """Save a Quantum module's settings the way Comexio's module dialog does.

        element_id is the module's plan element, module_id the module itself (the element's
        ref_id). settings REPLACES the module's whole setting set: a setting it leaves out is
        deleted, so pass every setting, not only the changed ones. The module's rule names follow
        its user_event_N settings; no other rule field is written. central saves a central module
        (element type 21); name renames a single one (a central module has no name of its own).
        Raises TypeError / ValueError for bad arguments (nothing is sent; no settings is refused,
        it would delete them all) and ComexioRequestRejectedError if Comexio does not confirm the save.
        """
        _require_positive_ids(element_id=element_id, module_id=module_id)
        if not isinstance(settings, Mapping):
            raise TypeError(f"settings must be a mapping of names to values, not {type(settings).__name__}")
        if not settings:
            raise ValueError("No Quantum module settings given")
        # Key types first: a key is rendered into the value check's message.
        if not all(isinstance(setting, str) for setting in settings):
            raise TypeError("Every Quantum module setting name must be a str")
        if "" in settings:
            raise ValueError("A Quantum module setting name must not be empty")
        _require_strs(**{f"settings[{setting!r}]": value for setting, value in settings.items()})
        if not isinstance(central, bool):
            raise TypeError(f"central must be a bool, not {type(central).__name__}")
        if name is not None:
            _require_strs(name=name)
            if not name:
                raise ValueError("A Quantum module name must not be empty")
            if central:
                raise ValueError("A central Quantum module has no name of its own")
        what = f"Saving {len(settings)} settings of Quantum module {module_id} (element {element_id})"
        form = {"id": str(element_id)}
        if name is not None:
            form["element_name"] = name
        form |= {f"{module_id}_{setting}": value for setting, value in settings.items()}
        if central:
            form["central_module"] = "true"
        form["timestamp"] = _js_timestamp()
        result = await self._plan_json(_PLAN_SAVE_QUANTUM_SETTINGS_PATH, form, what=what)
        saved_key, saved_value = _PLAN_SAVED_CONFIRMATION
        if "error" in result or str(result.get(saved_key)) != saved_value:
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
        if self._fingerprint is not None or self._plain_http_warned or is_local_address(self._host):
            return
        _LOGGER.warning(
            "Sending Comexio credentials over plain HTTP to a non-local address (%s). "
            "They may be transmitted in clear text.",
            self._host,
        )
        self._plain_http_warned = True

    async def _api_set(self, target: dict[str, str], value: float, *, what: str) -> None:
        """One /api/?action=set request; HTTP 401 means the API credentials were rejected."""
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError(f"{what}: {value!r} is not a finite number")
        headers: dict[str, str] = {}
        if self._api_username:
            self._warn_plain_http()
            headers["Authorization"] = _basic_auth_header(self._api_username, self._api_password)
        # Comexio's /api/ reads "+" literally, so "LED-Dimmer A3" must go out as "%20"; aiohttp's
        # params= would send "+". yarl keeps valid %XX escapes, so the path with the query is safe.
        query = urlencode({_ACTION_KEY: "set", "value": value, **target}, quote_via=quote)
        try:
            # Through the login gate as well: the answer may set a cookie in the jar the login renews.
            body = await self._request_text("GET", f"{_API_PATH}?{query}", what=what, headers=headers)
        except ComexioResponseError as err:
            if err.status != HTTPStatus.UNAUTHORIZED:
                raise
            if not self._api_username:
                raise ComexioAuthenticationError(
                    f"Comexio at {self._host} requires API credentials, none are configured (api_username)"
                ) from err
            raise ComexioAuthenticationError(f"Comexio at {self._host} rejected the API credentials") from err
        # A target Comexio does not know answers "0:not_found" with HTTP 200, one the API may not write
        # "Forbidden" — neither stored the value.
        if _NOT_RUNNING_SENTINEL_RE.fullmatch(body.strip()) or body.strip().casefold() == _API_SET_FORBIDDEN_ANSWER:
            raise ComexioRequestRejectedError(f"{what} was not accepted: Comexio answered {_excerpt(body)}")
        # The success answers are observations, not a documented contract, so any other answer is
        # only logged (an empty one included), never raised.
        if body.strip() not in _API_SET_OK_ANSWERS:
            _LOGGER.warning(
                "%s: Comexio answered %s instead of %s",
                what,
                _excerpt(body),
                " or ".join(map(repr, _API_SET_OK_ANSWERS)),
            )
            return
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

    async def _post_plan_form(self, form: dict[str, str], verdict: tuple[str, str], *, what: str) -> None:
        """POST the plan settings form; ComexioRequestRejectedError unless the redirect carries verdict."""
        location = await self._request_redirect(
            "POST", _PLAN_SAVE_PATH, what=what, data=form, headers=self._xhr_headers(_FUNCTION_MODULE_PATH)
        )
        if not _redirect_confirms(location, verdict):
            raise ComexioRequestRejectedError(f"{what} was not confirmed (redirect to {location!r})")
        _LOGGER.debug("%s confirmed (redirect to %r)", what, location)

    async def _plan_state_request(self, path: str, form: dict[str, str], *, what: str) -> None:
        """run_fup / stop_fup; both answer {"result": true, "state": ...} on success."""
        result = await self._plan_json(path, form, what=what)
        if result.get(_RESULT_KEY) is not True:
            raise ComexioRequestRejectedError(f"{what} was refused: {_excerpt(result)}")
        _LOGGER.debug("%s: state %s", what, result.get("state"))

    async def _add_plan_element(self, fub_id: int, form: dict[str, str], x: float, y: float, *, what: str) -> int:
        """add_element with the fields every element shares; returns the new element id."""
        return _answer_id(await self._add_plan_element_answer(fub_id, form, x, y, what=what), what)

    async def _add_module_block(
        self, fub_id: int, form: dict[str, str], x: float, y: float, *, what: str
    ) -> PlacedModuleBlock:
        """add_element of a scene block or Quantum module: its element id and the created object's id."""
        result = await self._add_plan_element_answer(fub_id, form, x, y, what=what)
        element_id = _answer_id(result, what)
        ref_id = _positive_id(result.get(_REF_ID_KEY))
        if ref_id is None:
            # The block is placed already: name it, so the caller can delete it.
            raise ComexioDataError(
                f"{what}: element {element_id} was placed, but the answer carries no ref_id: {_excerpt(result)}"
            )
        return PlacedModuleBlock(element_id, ref_id)

    async def _add_plan_element_answer(
        self, fub_id: int, form: dict[str, str], x: float, y: float, *, what: str
    ) -> dict[str, Any]:
        """The add_element answer, sent with the fields every element shares."""
        _require_ints(fub_id=fub_id)
        _require_numbers(x=x, y=y)
        form.update({"fubid": str(fub_id), "x": str(x), "y": str(y), "timestamp": _js_timestamp()})
        return await self._plan_json(_PLAN_ADD_ELEMENT_PATH, form, what=what)

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
        self,
        method: str,
        path: str,
        *,
        what: str,
        **kwargs: Any,
    ) -> str:
        """Body of one request as text, with transport errors and error statuses mapped to ComexioError."""
        try:
            async with self._response(method, path, what=what, **kwargs) as resp:
                if resp.status != HTTPStatus.OK:
                    raise _status_error(what, resp.status, _collapse_error_body(await _error_page_text(resp, what)))
                return await _response_text(resp, what)
        except (aiohttp.ClientError, TimeoutError) as err:
            _raise_from_stripped(self._transport_error(what, err), err)

    async def _request_unread(self, method: str, path: str, *, what: str, **kwargs: Any) -> None:
        """Send one request whose answer, whatever its status or body, is not looked at; only transport errors count."""
        try:
            async with self._response(method, path, what=what, **kwargs):
                return
        except (aiohttp.ClientError, TimeoutError) as err:
            _raise_from_stripped(self._transport_error(what, err), err)

    @asynccontextmanager
    async def _response(
        self, method: str, path: str, *, what: str, deadline: asyncio.Timeout | None = None, **kwargs: Any
    ) -> AsyncIterator[aiohttp.ClientResponse]:
        """The response to one request (see _follow_redirects), with a debug line when it fails or comes after a pause.

        The line says how long the client had been idle, on which connections the request was tried (a reused one,
        a new one; two entries mean aiohttp tried again itself) and how many other requests were in flight: what
        tells a kept-alive connection the server closed from a server that refuses a request (#289). Idle time and
        duration both count from the moment the request got its slot at the login gate. "failed" is any exception
        out of the request or the caller's block (a status or data error too; the line names its class, and the
        message only for a transport error). It costs nothing unless the aiocomexio logger is at DEBUG, and a failure
        of the line itself never replaces the request's own result. A request that bypasses this method
        (_request_redirect, the function plan forms) gets no line of its own, but its answer moves the time of the
        last one. `deadline` is the caller's asyncio.timeout around the request and its block: when it expires, the
        line names a timeout instead of the cancellation it raises in here.
        """
        notes: dict[str, Any] = {}
        failure: BaseException | None = None
        try:
            async with self._follow_redirects(method, path, what=what, connection=notes, **kwargs) as resp:
                yield resp
        # Only noted for the debug line, always re-raised.
        except BaseException as err:
            expired = isinstance(err, asyncio.CancelledError) and deadline is not None and deadline.expired()
            failure = TimeoutError(_CALLER_DEADLINE_EXPIRED) if expired else err
            raise
        finally:
            # First, and outside the guard below: the line reads the previous answer time from notes, not from here.
            self._note_status_answer(failure)
            # No slot at the login gate, nothing sent: a request cancelled in the queue has no line.
            if _STARTED_KEY in notes:
                self._log_request_safely(what, notes, failure)
            # The traceback of the error holds this frame: drop the reference so the two do not keep each other.
            failure = None

    def _note_status_answer(self, error: BaseException | None) -> None:
        """Move the time of the last answer for a ClientResponseError: a session with raise_for_status raises it while
        the request is entered (or later, then the time moves to the end), so the server answered although no response
        came back. A proxy's refusal is not an
        answer of the server."""
        if isinstance(error, aiohttp.ClientResponseError) and not isinstance(error, aiohttp.ClientHttpProxyError):
            self._last_response_at = time.monotonic()

    def _log_request_safely(self, what: str, notes: dict[str, Any], error: BaseException | None) -> None:
        try:
            # _follow_redirects sets both keys together: a missing one is a bug, reported below as a failed line.
            self._log_request(what, notes[_STARTED_KEY], notes[_PREVIOUS_RESPONSE_KEY], notes, error)
        # A diagnostic line must never replace the request's own exception.
        except Exception as line_error:
            # Raised while the request's error is in flight, which makes that error, not yet stripped, its context:
            # cut, or the traceback of this record would print it.
            line_error.__context__ = None
            # A logging handler that raises on the line may raise on this record too.
            with suppress(Exception):
                _LOGGER.debug("Request diagnostics for %s failed", _diagnostic_label(what), exc_info=line_error)

    def _log_request(
        self,
        what: str,
        started: float,
        previous_response_at: float | None,
        notes: dict[str, Any],
        error: BaseException | None,
    ) -> None:
        """Debug line for a request that failed, or that came after at least _IDLE_LOG_MIN_SEC without an answer."""
        if not _LOGGER.isEnabledFor(logging.DEBUG):
            return
        idle = None if previous_response_at is None else started - previous_response_at
        if error is None and (idle is None or idle < _IDLE_LOG_MIN_SEC):
            return
        tried = notes.get(CONNECTION_KEY, [])
        _LOGGER.debug(
            "%s: %s after %.3f s; idle %s before it, connection %s%s, %d other request(s) in flight%s",
            _diagnostic_label(what),
            "failed" if error else "answered",
            time.monotonic() - started,
            "n/a (first request)" if idle is None else f"{idle:.1f} s",
            " → ".join(tried) or ("unknown" if error else "unknown (no connection trace on the session?)"),
            " (aiohttp tried again)" if len(tried) > 1 else "",
            # This request has left the login gate by now, so the count is the others only.
            self._gate.requests_in_flight,
            "" if error is None else f": {_diagnostic_error_text(error)}",
        )

    @asynccontextmanager
    async def _follow_redirects(
        self, method: str, path: str, *, what: str, connection: dict[str, Any], **kwargs: Any
    ) -> AsyncIterator[aiohttp.ClientResponse]:
        """The response to one request; the client follows redirects itself, a pinned one only within its HTTPS origin.

        A redirect that leaves the pinned origin (plain http://, another host or port) raises
        ComexioResponseError before it is followed, so neither the session cookie nor credentials
        go anywhere the pin does not cover. An unpinned client follows a redirect only on its origin or on to
        HTTPS on port 443 of the same host (see _unpinned_redirect_target). Every request goes through the login gate.
        """
        redirect_target = _unpinned_redirect_target if self._fingerprint is None else _pinned_redirect_target
        kwargs = _unencoded(kwargs)
        async with self._gate.request():
            connection[_STARTED_KEY] = time.monotonic()
            connection[_PREVIOUS_RESPONSE_KEY] = self._last_response_at
            url = f"{self._base_url}{path}"
            for _ in range(_MAX_REDIRECTS + 1):
                # The connections of the hop being sent: a followed redirect is no second try of aiohttp.
                connection.pop(CONNECTION_KEY, None)
                async with self._session.request(
                    method, url, ssl=self._ssl, allow_redirects=False, trace_request_ctx=connection, **kwargs
                ) as resp:
                    # Headers are in: the server answered (a redirect hop too), whatever the body turns out to be.
                    self._last_response_at = time.monotonic()
                    if resp.status not in _FOLLOWED_REDIRECT_STATUSES:
                        yield resp
                        return
                    url = redirect_target(resp, what)
                    # The Location carries its own query; like aiohttp, a 303 (except on a HEAD) and a POST on
                    # 301/302 continue as a body-less GET.
                    kwargs.pop("params", None)
                    see_other = resp.status == HTTPStatus.SEE_OTHER and method != "HEAD"
                    if see_other or (resp.status in _REDIRECT_STATUSES and method == "POST"):
                        method = "GET"
                        kwargs.pop("data", None)
                    else:
                        _require_replayable_body(kwargs, what, resp.status)
            raise ComexioResponseError(f"{what} failed: more than {_MAX_REDIRECTS} redirects", status=resp.status)

    def _transport_error(self, what: str, err: aiohttp.ClientError | TimeoutError) -> ComexioError:
        """The ComexioError for a request that failed below HTTP: a foreign certificate or a connection problem.

        Raised by _raise_from_stripped, which gets it as an argument and only then strips err: the text here is taken
        from err as aiohttp raised it. An override gets err not yet stripped and must not log it.
        """
        if isinstance(err, aiohttp.ServerFingerprintMismatch):
            expected, actual = err.expected.hex(), err.got.hex()
            return ComexioCertificateMismatchError(
                f"{what} refused: Comexio at {self._host!r} presented the TLS certificate "
                f"{format_fingerprint(actual)} instead of the pinned {format_fingerprint(expected)}",
                expected=expected,
                actual=actual,
            )
        return ComexioConnectionError(f"{what} failed: {_transport_error_text(err)}")

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
        ComexioRequestRejectedError. A compressed 200 answer is no confirmation either and is not read; one
        longer than _RESPONSE_MAX_BYTES is read up to that size plus one byte, then refused. Both are a
        ComexioDataError.
        """
        try:
            async with (
                self._gate.request(),
                self._session.request(
                    method, f"{self._base_url}{path}", allow_redirects=False, ssl=self._ssl, **_unencoded(kwargs)
                ) as resp,
            ):
                self._last_response_at = time.monotonic()  # the server answered: the idle time of a later request
                if resp.status in _REDIRECT_STATUSES:
                    location = resp.headers.get("Location", "")
                    try:
                        location_path = urlsplit(location).path
                    except ValueError as err:
                        raise ComexioResponseError(
                            f"{what} failed: Comexio redirected to an unparsable location {location!r}",
                            status=resp.status,
                        ) from err
                    if "login" in location_path.lower():
                        raise ComexioAuthenticationError(
                            f"Comexio redirected to the login page, the session is not logged in ({what})"
                        )
                    return location
                status = resp.status
                body = await (
                    _response_text(resp, what, errors="replace")
                    if status == HTTPStatus.OK
                    else _error_page_text(resp, what)
                )
        except (aiohttp.ClientError, TimeoutError) as err:
            self._note_status_answer(err)
            _raise_from_stripped(self._transport_error(what, err), err)
        if _LOGIN_FORM_SUBMIT_MARKER in body:
            raise ComexioAuthenticationError(f"Comexio served the login form, the session is not logged in ({what})")
        if status != HTTPStatus.OK:
            raise _status_error(what, status, _collapse_error_body(body))
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
    """One PKCS#1 v1.5 block as zero-padded hex, the way Comexio's login form expects it.

    ComexioDataError if OpenSSL refuses the key, e.g. a modulus beyond 16384 bits.
    """
    try:
        encrypted = public_key.encrypt(message, padding.PKCS1v15())
    except ValueError as err:  # the message fits (the caller checks it): the key itself is refused
        raise ComexioDataError(f"Login key response carries an RSA key the crypto library refuses: {err}") from err
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
    """True for an int that is not a bool; a bare `in range(...)` check would also accept True and 2.0."""
    return isinstance(value, int) and not isinstance(value, bool)


def _require_plan_collections(data: dict[str, Any], fub_id: int) -> None:
    """ComexioDataError unless a loadelements answer carries real elements and connections collections."""
    # normalize_plan_payload turns a missing collection into {} — fine to display, fatal to run.
    missing = plan_payload_missing_collections(data)
    if missing:
        raise ComexioDataError(f"Function plan {fub_id} payload has no {' / '.join(missing)} collection")


async def _once_more_if_dropped[T](what: str, request: Callable[[], Awaitable[T]]) -> T:
    """Await a request, and once more if the server dropped the connection under it."""
    try:
        return await request()
    except ComexioConnectionError as err:
        if not isinstance(err.__cause__, aiohttp.ServerDisconnectedError):
            raise
        _LOGGER.debug("%s: %s — asking once more", what, err)
    return await request()


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


def _require_positive_ids(**values: Any) -> None:
    """Raise TypeError for a value that is not an int, ValueError for one that is not positive."""
    _require_ints(**values)
    for label, value in values.items():
        if value <= 0:
            raise ValueError(f"{label} must be a positive id, not {value}")


def _has_non_str_key(value: Any) -> bool:
    """Whether a JSON-bound value holds a mapping key that is not a str (json.dumps would coerce it)."""
    if isinstance(value, Mapping):
        return any(not isinstance(key, str) or _has_non_str_key(item) for key, item in value.items())
    if isinstance(value, list | tuple):
        return any(_has_non_str_key(item) for item in value)
    return False


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


def _plan_by_name(fubs: Any, name: str, *, what: str) -> CreatedFunctionPlan:
    """The one plan in $Fubs named name; ComexioDataError for none or several."""
    found = [(key, plan) for key, plan in iter_group(fubs) if isinstance(plan, Mapping) and plan.get("Name") == name]
    if len(found) != 1:
        raise ComexioDataError(f"{what}: $Fubs lists {len(found)} plans with that name")
    key, plan = found[0]
    plan_id = _positive_id(key)
    if plan_id is None:
        raise ComexioDataError(f"{what}: the new plan has no valid id, $Fubs lists it under {_excerpt(key)}")
    return CreatedFunctionPlan(plan_id, dict(plan))


def _redirect_confirms(location: str, verdict: tuple[str, str]) -> bool:
    """Whether the redirect's query carries exactly the verdict parameter, e.g. ("added", "1").

    A substring test would also take "?xadded=1" or "?added=10" for "added=1".
    """
    return verdict in parse_qsl(urlsplit(location).query, keep_blank_values=True)


def _find_by_label(entries: Mapping[str, Any], field: str, label: str) -> str | None:
    """Id of the Web-IO list entry whose field equals label, or None.

    An exact match wins; otherwise the first match ignoring case, as the lookup always matched
    the rendered labels case-insensitively. Raises ComexioDataError for an entry without a
    string field or whose Id differs from its key: None means "absent" to the caller, so a
    renamed field or a list keyed by position must not read as an empty list.
    """
    candidates: list[tuple[str, str]] = []
    for key, entry in iter_group(entries):
        value = entry.get(field) if isinstance(entry, Mapping) else None
        if not isinstance(value, str) or str(entry.get(_WEBIO_LIST_ID_FIELD, key)) != key:
            raise ComexioDataError(
                f"Web-IO list entry {_excerpt(key)} has no {field!r} or a different Id: {_excerpt(entry)}"
            )
        candidates.append((key, value))
    exact = [key for key, value in candidates if value == label]
    folded = [key for key, value in candidates if value.casefold() == label.casefold()]
    matches = exact or folded
    return matches[0] if matches else None


def _answer_id(result: dict[str, Any], what: str) -> int:
    """The "id" of an add_element / saveconnection answer; an "error" answer is a refusal."""
    if "error" in result:
        raise ComexioRequestRejectedError(f"{what} was refused: {_excerpt(result)}")
    value = _positive_id(result.get(_ID_KEY))
    if value is None:
        raise ComexioDataError(f"{what}: answer carries no id: {_excerpt(result)}")
    return value


def _positive_id(value: Any) -> int | None:
    """value as a Comexio id, or None: only a positive integer, as number or ASCII digit string."""
    # int() would turn true into 1, 1.9 into 1 and "-3" into -3.
    if isinstance(value, str) and value.isascii() and value.isdigit():
        try:
            value = int(value)
        except (ValueError, OverflowError):  # beyond Python's int-to-str digit limit
            return None
    return value if _is_int(value) and value > 0 else None


def _marker_type(binary: bool) -> str:
    return _MARKER_TYPE_BINARY if binary else _MARKER_TYPE_ANALOG


def _form_number(value: float) -> str:
    """A finite number the way Comexio's forms write it: no ".0", no exponent ("20", "2.5", "0.00001").

    Raises TypeError for a bool or a non-number and ValueError for NaN/infinity.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"expected an int or float, not {type(value).__name__}")
    if isinstance(value, int):
        return str(value)
    if not math.isfinite(value):
        raise ValueError(f"expected a finite number, not {value}")
    if value.is_integer():
        return str(int(value))
    return format(Decimal(repr(value)), "f")


def _js_timestamp() -> str:
    """Current UTC time the way JavaScript's Date.toISOString() writes it (millisecond precision)."""
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _command_ref(command_id: str | int | None) -> str:
    """Base64 command reference of the Web-IO command form: {"src":"command","id":<id or null>}."""
    ref = {"src": "command", "id": None if command_id is None else int(command_id)}
    return base64.b64encode(json.dumps(ref, separators=(",", ":")).encode()).decode()


def _command_range(html: str, command_id: str | int) -> tuple[float | None, float | None]:
    """(Min, Max) from a Web-IO command edit form; see ComexioClient.get_webio_command_range."""
    parser = _CommandRangeParser()
    parser.feed(html)
    parser.close()
    if not parser.fields:
        raise _missing_data_error(html, f"Web-IO command {command_id} edit form has no min/max fields")
    values: dict[str, float | None] = {"min": None, "max": None}
    for field, text in parser.fields.items():
        if text is None:
            continue
        try:
            number = float(text)
        except (ValueError, OverflowError):
            number = math.nan
        # float() reads "inf", "nan" and "1e999" without an error, but no Comexio range is one.
        if math.isfinite(number):
            values[field] = number
        else:
            _LOGGER.warning("Web-IO command %s has a non-numeric %s value: %s", command_id, field, _excerpt(text))
    return values["min"], values["max"]


def _first_attributes(attrs: list[tuple[str, str | None]]) -> dict[str, str]:
    """A tag's attributes as a browser keeps them: the first of a name wins and a bare one is ""."""
    attributes: dict[str, str] = {}
    for name, value in attrs:
        attributes.setdefault(name, value or "")
    return attributes


def _clears_frameset_ok(tag: str, attrs: list[tuple[str, str | None]]) -> bool:
    """Whether the start tag clears a browser's frameset-ok flag: an <input> does unless its type is hidden."""
    if tag == "input":
        return _first_attributes(attrs).get("type", "").lower() != "hidden"
    return tag in _FRAMESET_BLOCKERS


class _ForeignElement(NamedTuple):
    """An open SVG or MathML element.

    html_inside says start tags inside it are HTML again (an integration point); html lists the HTML elements open
    inside it, innermost last — while one is open, it and not this element is where the browser is.
    """

    tag: str
    namespace: str
    html_inside: bool
    html: list[str]


def _foreign_element(tag: str, namespace: str, attrs: list[tuple[str, str | None]]) -> _ForeignElement:
    """An SVG or MathML element, marked as an integration point where a browser reads its content as HTML."""
    if namespace == "svg":
        html_inside = tag in _SVG_HTML_INTEGRATION_POINTS
    elif tag == "annotation-xml":
        html_inside = _first_attributes(attrs).get("encoding", "").lower() in _HTML_ANNOTATION_ENCODINGS
    else:
        html_inside = tag in _MATHML_TEXT_INTEGRATION_POINTS
    return _ForeignElement(tag, namespace, html_inside, [])


class _OpenElement(str):
    """The tag name of one open HTML element: two open elements of a tag are equal names but not the same object.

    attributes are its start tag's, which a browser compares to tell equal formatting elements apart.
    """

    attributes: frozenset[tuple[str, str]]

    def __new__(cls, tag: str, attributes: frozenset[tuple[str, str]] = frozenset()) -> Self:
        element = super().__new__(cls, tag)
        element.attributes = attributes
        return element


def _bounds_scope(element: _ForeignElement) -> bool:
    """Whether a foreign element bounds an HTML end tag's scope (an integration point, a MathML <annotation-xml>)."""
    return element.html_inside or (element.namespace == "math" and element.tag == "annotation-xml")


def _end_tag_target(tag: str) -> tuple[frozenset[str], frozenset[str]]:
    """The elements an end tag closes the innermost open one of, and the open elements it does not reach past."""
    if tag in _HIDDEN_ELEMENTS:  # a </template> closes everything inside it
        return frozenset({tag}), frozenset()
    if tag in _HEADINGS:  # any heading end tag closes the innermost open heading
        return _HEADINGS, _SCOPE_BOUNDARIES
    if tag in _TABLE_PARTS or tag == _TABLE:
        return frozenset({tag}), _TABLE_SCOPE_BOUNDARIES
    if tag == "li":
        return frozenset({tag}), _LIST_ITEM_SCOPE_BOUNDARIES
    if tag == "p":
        return frozenset({tag}), _BUTTON_SCOPE_BOUNDARIES
    if tag in _SCOPED_END_TAGS or tag in _FORMATTING_ELEMENTS or tag == "form":
        return frozenset({tag}), _SCOPE_BOUNDARIES
    return frozenset({tag}), _SPECIAL_ELEMENTS


def _innermost_open_element(html: list[str], names: frozenset[str], barriers: frozenset[str]) -> int | None:
    """The index of the innermost element of one of the names in html; None if there is none or a barrier is first."""
    for index in range(len(html) - 1, -1, -1):
        if html[index] in names:
            return index
        if html[index] in barriers:
            return None
    return None


def _close_open_element(html: list[str], names: frozenset[str], barriers: frozenset[str]) -> None:
    """Close the innermost open element of one of the names, with the elements opened inside it, if it is in scope."""
    index = _innermost_open_element(html, names, barriers)
    if index is not None:
        del html[index:]


def _close_list_item(html: list[str], names: frozenset[str]) -> None:
    """Close an open <li> (or <dd>/<dt>) as a new one does: no special element other than a block may lie between."""
    for index in range(len(html) - 1, -1, -1):
        if html[index] in names:
            del html[index:]
            return
        if html[index] in _SPECIAL_ELEMENTS - _LIST_ITEM_TRANSPARENT:
            return


def _close_implied(tag: str, html: list[str]) -> None:
    """Close the open HTML elements a browser closes before it inserts the element of an HTML start tag."""
    if tag == "li":
        _close_list_item(html, frozenset({"li"}))
    elif tag in _DEFINITION_ITEMS:
        _close_list_item(html, _DEFINITION_ITEMS)
    elif tag in _TABLE_CELLS:
        _close_open_element(html, _TABLE_CELLS, _TABLE_SCOPE_BOUNDARIES)
    elif tag in _TABLE_ROW_PARTS:
        _close_open_element(html, _TABLE_ROW_PARTS[tag], _TABLE_SCOPE_BOUNDARIES)
    elif tag == "button":
        _close_open_element(html, frozenset({"button"}), _SCOPE_BOUNDARIES)
    if tag in _CLOSES_A_P:
        _close_open_element(html, frozenset({"p"}), _BUTTON_SCOPE_BOUNDARIES)
    if tag in _HEADINGS and html and html[-1] in _HEADINGS:
        html.pop()


def _open_table_parts(tag: str, html: list[str]) -> None:
    """Place a table part start tag as a browser does: close an open cell or <caption>, and open the <tbody>/<tr> a
    row or cell put straight into a table (or a cell into a section) gets, closing what else lies open in between."""
    if tag not in _TABLE_PARTS:
        return
    index = _innermost_open_element(html, _TABLE_CONTEXTS, frozenset())
    while index is not None and html[index] in _TABLE_PART_CLOSED:
        del html[index:]
        index = _innermost_open_element(html, _TABLE_CONTEXTS, frozenset())
    if index is None or html[index] not in _TABLE_PART_HOSTS:  # in a template the part stays where it is
        return
    parents = _IMPLIED_TABLE_PARENTS.get(tag)
    if parents is None:  # a section, caption or column group belongs straight into the table
        index = max((position for position in range(index + 1) if html[position] == _TABLE), default=index)
        parents = ()
    elif html[index] in _TABLE_SECTIONS:
        parents = parents[1:]
    elif html[index] == "tr":
        parents = ()
    html[index + 1 :] = [_OpenElement(parent) for parent in parents]


def _left_open_by_end_tag(tag: str, above: list[str]) -> list[str]:
    """The HTML elements opened inside a closed element that stay open: none, except after a formatting end tag.

    A browser's adoption agency moves the special elements (and the formatting elements before the last of them) out
    of the formatting element and closes the rest, so a </b> keeps a <div> inside it open but closes an <svg> above.
    """
    if tag not in _FORMATTING_ELEMENTS:
        return []
    last = max((index for index, name in enumerate(above) if name in _SPECIAL_ELEMENTS), default=-1)
    return [
        name
        for index, name in enumerate(above)
        if name in _SPECIAL_ELEMENTS or (index < last and name in _FORMATTING_ELEMENTS)
    ]


class _FormParser(HTMLParser):
    """An HTMLParser that reads form fields as a browser does, for subclasses that pick out the fields they need.

    The content of a <textarea>, <title> or raw-text element is text: html.parser reports markup inside most of them
    as tags (older 3.13 releases even inside <textarea> and <title>), so a field written as text into one would be
    read as a real one; a <plaintext> makes the rest of the page text. The content of a <script> or <template> is no
    field either, nor is an SVG or MathML element (an <input> inside an <svg> is no form field), except inside an
    integration point such as <foreignObject>, whose content is HTML again; a breakout tag such as <div> or </p> ends
    the foreign content, and while an HTML element is open inside an integration point a foreign end tag is ignored;
    the end tag of an open HTML element closes the SVG or MathML elements opened inside it, unless a special element
    or a scope boundary lies between (a </span> stops at a <div>, a </div> at an <object>; a formatting end tag such
    as </b> reaches past a <div> and leaves it open, any heading end tag closes the open heading). To know which HTML
    elements are open, it closes what a browser closes implicitly (a <p> before a <div> or, outside quirks mode, a
    <table>; an <li> before the next one, a cell before the next one or a <caption>, a <select> with the cell it is
    in), opens what a browser inserts (the <tbody> and <tr> around a cell put straight into a table; a formatting
    element such as <i> that an end tag closed implicitly, reopened before most start tags, outside the cell it was
    opened in, no more than three equal ones) and ignores what a browser ignores (a table part outside a <table>, a
    <form> inside a form).
    A <![CDATA[ section exists only in foreign content; elsewhere it is a bogus comment up to the next ">", on every
    html.parser release. Inside an open <select> a browser ignores the start tag of another text-only
    element, a <style>, <svg> or <math>, and a <select>, <input>, <keygen> or <textarea> start tag ends the select.
    A page that keeps more than _MAX_OPEN_ELEMENTS elements open raises ComexioDataError.
    Subclasses get the remaining tags through handle_form_tag and handle_form_endtag, with the attributes from
    _first_attributes, and the end of a select through handle_select_end.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True, **_HTML_PARSER_SCRIPTING)
        self._text_only: str | None = None
        self._html: list[str] = []  # HTML elements open outside foreign content, innermost last
        # Open <script>/<template> elements, each with the depth of the foreign stack it was opened at: their
        # content is no field or text of one.
        self._hidden: list[tuple[str, int]] = []
        self._select_open = False  # any select is open; a <select> inside it only ends it
        self._form_open = False  # a form is open (the browser's form element pointer): another <form> is ignored
        self._start_ignored = False  # the start tag handle_form_tag now reads is one a browser ignores: no element
        self._frameset_ok = True  # no start tag has cleared the browser's "frameset-ok" flag yet
        self._frameset_depth = 0  # the <frameset> elements a browser keeps open (a <frame> belongs in one of them)
        # The browser's list of active formatting elements: open (or implicitly closed) formatting elements, and the
        # cells, captions, templates and objects they do not leak into (markers).
        self._formatting: list[_OpenElement] = []
        self._foreign: list[_ForeignElement] = []  # open SVG/MathML elements, outermost first
        self._foreign_start = False  # the last start tag was a foreign element: html.parser keeps no text mode for it
        self._text_start = False  # the last start tag was text of a text-only element (older html.parser reports it)
        self._closing = False  # close() is reading the rest of the page: no more of it will come
        self._sync_cdata_support()

    def close(self) -> None:
        self._closing = True
        super().close()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._start(tag, attrs, self_closing=False)
        self._sync_cdata_support()

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # A browser ignores the slash of an HTML start tag: <select/> opens a select and what follows <textarea/>
        # is still its text; a void element such as <input/> has no end tag either way. The slash only closes a
        # foreign element, such as <svg/>.
        self._start(tag, attrs, self_closing=True)
        if tag in self.CDATA_CONTENT_ELEMENTS:
            # html.parser switches to raw text only after handle_starttag; after <style/> a browser reads it too.
            self.set_cdata_mode(tag)
        self._sync_cdata_support()

    def set_cdata_mode(self, elem: str, *args: Any, **kwargs: Any) -> None:
        # html.parser switches to text mode after handle_starttag; a foreign element (an SVG <style>) has none, a tag
        # that is text of a text-only element is none, and inside an open select a browser ignores the tag.
        if self._foreign_start or self._text_start or (self._reads_select_markup() and elem in _IGNORED_IN_SELECT):
            return
        super().set_cdata_mode(elem, *args, **kwargs)

    def handle_endtag(self, tag: str) -> None:
        if self._text_only is not None:
            if tag == self._text_only != _ENDLESS_TEXT_ELEMENT:
                self._text_only = None
        elif not (self._foreign and self._end_foreign(tag)):
            self._end_html(tag)
        self._sync_cdata_support()

    def parse_html_declaration(self, i: int) -> int:
        # Older html.parser releases read every <![ as a marked section up to "]]>", which would hide the fields
        # after it; a browser reads a <![CDATA[ section only in foreign content and any other <![ as a bogus comment.
        rawdata = self.rawdata
        if not rawdata.startswith("<![", i):
            return super().parse_html_declaration(i)
        if not (self._reads_cdata() and rawdata.startswith(_CDATA_START, i)):
            return self.parse_bogus_comment(i)
        end = rawdata.find("]]>", i + len(_CDATA_START))
        if end < 0:
            if not self._closing:
                return -1  # wait for more of the page
            # A section the page ends in runs to its end; older releases would read it as text up to the next ">".
            end = len(rawdata)
            self.unknown_decl(rawdata[i + 3 : end])
            return end
        self.unknown_decl(rawdata[i + 3 : end])
        return end + 3

    def _reads_cdata(self) -> bool:
        """Whether a <![CDATA[ now starts a CDATA section: in foreign content, with no HTML element open inside it."""
        return bool(self._foreign) and not self._foreign[-1].html

    def _sync_cdata_support(self) -> None:
        # html.parser releases that can tell a CDATA section from a bogus comment let the caller say which applies
        # (for one the page ends in, which parse_html_declaration never sees).
        set_support_cdata = getattr(self, "_set_support_cdata", None)
        if set_support_cdata is not None:
            set_support_cdata(self._reads_cdata())

    def _hidden_floor(self) -> int:
        """The depth of the foreign stack below the innermost hidden element, which no foreign tag reaches past."""
        return self._hidden[-1][1] if self._hidden else 0

    def _start(self, tag: str, attrs: list[tuple[str, str | None]], *, self_closing: bool) -> None:
        self._foreign_start = False
        self._start_ignored = False
        self._text_start = self._text_only is not None
        if self._text_start:
            return
        if self._foreign and self._is_foreign(tag, attrs):
            self._start_foreign(tag, attrs, self_closing=self_closing)
            self._check_depth()
            return
        if self._select_ignores(tag):
            return
        html = self._foreign[-1].html if self._foreign else self._html
        self._prepare_start(tag, html)
        self._start_ignored = self._is_ignored(tag, html)
        self._track_frameset(tag, attrs)
        if self._start_html(tag, attrs, self_closing=self_closing) and self._opens(tag, html):
            self._insert(tag, attrs, html)
        self._check_depth()

    def _check_depth(self) -> None:
        """Raise ComexioDataError once the page keeps more than _MAX_OPEN_ELEMENTS elements open or listed.

        Only a start tag adds to these lists, so _start checks after each one.
        """
        open_elements = len(self._html)  # a hidden element is one of the open HTML elements too
        if self._foreign:
            open_elements += len(self._foreign) + sum(map(len, map(_INNER_HTML, self._foreign)))
        if max(open_elements, len(self._formatting)) > _MAX_OPEN_ELEMENTS:
            raise ComexioDataError(f"form page nests more than {_MAX_OPEN_ELEMENTS} elements")

    def _report_other(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Hand a start tag that is no form field (but an element of the page) to handle_other_tag, unless hidden."""
        if not self._hidden:
            self.handle_other_tag(tag, _first_attributes(attrs))

    def _start_foreign(self, tag: str, attrs: list[tuple[str, str | None]], *, self_closing: bool) -> None:
        self._foreign_start = True
        self._report_other(tag, attrs)
        if not self_closing:
            current = self._foreign[-1]
            # An <svg> inside a MathML <annotation-xml> is SVG; any other element keeps the namespace around it.
            svg = tag == "svg" and current.tag == "annotation-xml"
            self._foreign.append(_foreign_element(tag, "svg" if svg else current.namespace, attrs))

    def _insert(self, tag: str, attrs: list[tuple[str, str | None]], html: list[str]) -> None:
        """Open an HTML element, listing a formatting element or marker element among the active formatting ones."""
        element = _OpenElement(tag, frozenset(_first_attributes(attrs).items()))
        html.append(element)
        if tag in _FORMATTING_ELEMENTS:
            self._forget_fourth_equal_formatting(element)
        if tag in _FORMATTING_ELEMENTS or tag in _FORMATTING_MARKERS:
            self._formatting.append(element)

    def _forget_fourth_equal_formatting(self, element: _OpenElement) -> None:
        """Drop the earliest of three active formatting elements equal to a new one (same tag and attributes) past the
        last marker, as a browser does (the "Noah's Ark" clause): <p><b></p> repeated reopens no more than three <b>."""
        equal = 0
        for index in range(len(self._formatting) - 1, -1, -1):
            entry = self._formatting[index]
            if entry in _FORMATTING_MARKERS:
                return
            if entry == element and entry.attributes == element.attributes:
                equal += 1
                if equal == 3:
                    del self._formatting[index]
                    return

    def _prepare_start(self, tag: str, html: list[str]) -> None:
        """Close and reopen the HTML elements a browser closes or reopens before it inserts a start tag's element."""
        _close_implied(tag, html)
        _open_table_parts(tag, html)
        self._end_closed_select()
        self._forget_closed_markers()  # before a new marker joins the list behind a closed one
        if tag in ("a", "nobr") and self._listed_formatting(tag) is not None:
            self._close_html(tag)  # an open <a> or <nobr> ends before the next one
        if tag not in _NOT_RECONSTRUCTING and not self._select_open:
            self._reconstruct_formatting(html)

    def _open_html_lists(self) -> list[list[str]]:
        return [self._html, *map(_INNER_HTML, self._foreign)]

    def _is_open(self, element: str) -> bool:
        # Innermost first: the element asked about is almost always one of the last opened. An equal name in a list
        # comes before the search for the element itself.
        return any(
            element in html and any(map(is_, repeat(element), reversed(html)))
            for html in reversed(self._open_html_lists())
        )

    def _forget_closed_markers(self) -> None:
        """Drop a closed marker element from the active formatting elements, with the entries it bounded."""
        if _FORMATTING_MARKERS.isdisjoint(self._formatting):
            return
        for index, entry in enumerate(self._formatting):
            if entry in _FORMATTING_MARKERS and not self._is_open(entry):
                del self._formatting[index:]
                return

    def _listed_formatting(self, tag: str) -> int | None:
        """The index of the innermost active formatting element of the tag past the last marker, if there is one."""
        self._forget_closed_markers()
        for index in range(len(self._formatting) - 1, -1, -1):
            entry = self._formatting[index]
            if entry == tag:
                return index
            if entry in _FORMATTING_MARKERS:
                return None
        return None

    def _forget_formatting(self, closed: str) -> None:
        """Remove a formatting element its end tag closed from the active formatting elements.

        A closed marker element stays listed until _forget_closed_markers drops it with the entries it bounded.
        """
        if closed in _FORMATTING_ELEMENTS:
            self._formatting = [entry for entry in self._formatting if entry is not closed]

    def _forgets_closed_formatting(self, tag: str) -> bool:
        """Whether a formatting end tag only removes its active formatting element, closed implicitly before."""
        if tag not in _FORMATTING_ELEMENTS:
            return False
        index = self._listed_formatting(tag)
        if index is None or self._is_open(self._formatting[index]):
            return False
        del self._formatting[index]
        return True

    def _reconstruct_formatting(self, html: list[str]) -> None:
        """Reopen the active formatting elements past the last marker that an end tag closed implicitly, as a browser
        does before most start tags (a <b><i></b><svg> puts the svg into a new <i>)."""
        self._forget_closed_markers()
        entries = self._formatting
        start = len(entries)
        while start and entries[start - 1] not in _FORMATTING_MARKERS and not self._is_open(entries[start - 1]):
            start -= 1
        for index in range(start, len(entries)):
            entries[index] = _OpenElement(entries[index], entries[index].attributes)
            html.append(entries[index])

    def _is_ignored(self, tag: str, html: list[str]) -> bool:
        """Whether a browser ignores the start tag altogether, so that it makes no element of the page.

        That is a table part outside a <table>, a <frameset> once the page cannot be a frameset page any more (or
        after its frameset has ended), a <frame> outside a frameset, and a <form> while another one is open
        (outside a <template>). <html>, <body> and <head> are not listed: a browser merges their attributes, and
        the first of each is a real element. A <colgroup> inside a table does make an element (_opens only keeps no
        stack entry for it).
        """
        if tag in _TABLE_PARTS:
            return _TABLE not in html
        if tag == "frameset":
            return not (self._frameset_ok or self._frameset_depth)
        if tag == "frame":
            return not self._frameset_depth
        return tag == "form" and self._form_open and not self._hidden

    def _track_frameset(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Follow the browser's frameset-ok flag and the frameset elements it keeps open (see _is_ignored)."""
        if tag == "frameset" and not self._start_ignored and not self._hidden:  # a hidden one is ignored too
            self._frameset_depth += 1
            self._frameset_ok = False  # the first frameset replaces the body; a later one only nests or is ignored
        elif _clears_frameset_ok(tag, attrs):
            self._frameset_ok = False

    def _opens(self, tag: str, html: list[str]) -> bool:
        """Whether a browser opens an element for the start tag, given the HTML elements open around it.

        It ignores a table part outside a <table>, a <head> or <body> in the body, and a <form> while another one
        is open (outside a <template>).
        """
        if tag in _OPENS_NO_ELEMENT or (tag in _TABLE_PARTS and _TABLE not in html):
            return False
        if tag == "form" and not self._hidden:
            if self._form_open:
                return False
            self._form_open = True
        return True

    def _is_foreign(self, tag: str, attrs: list[tuple[str, str | None]]) -> bool:
        """Whether a start tag inside SVG/MathML content is a foreign element; a breakout tag ends that content."""
        current = self._foreign[-1]
        if current.html:  # an HTML element inside the integration point is where the browser is
            return False
        if current.html_inside and not (current.tag in _MATHML_TEXT_INTEGRATION_POINTS and tag in _MATHML_GLYPH_TAGS):
            return False
        if tag in _FOREIGN_BREAKOUT_TAGS or (tag == "font" and any(name in _FONT_BREAKOUT_ATTRS for name, _ in attrs)):
            self._break_out()
            return False
        return True

    def _break_out(self) -> None:
        """Close the foreign elements up to the innermost integration point, as a breakout tag does."""
        floor = self._hidden_floor()
        while len(self._foreign) > floor and not self._foreign[-1].html_inside:
            self._foreign.pop()

    def _start_html(self, tag: str, attrs: list[tuple[str, str | None]], *, self_closing: bool) -> bool:
        """Read an HTML start tag; True if it opened an element that stays open until an end tag closes it."""
        if tag in _TEXT_ONLY_ELEMENTS:
            if not self._hidden:
                self.handle_text_only_tag(tag, _first_attributes(attrs))
            self._text_only = tag
            return False
        if tag in _FOREIGN_ROOTS:
            self._foreign_start = True
            self._report_other(tag, attrs)
            if not self_closing:
                self._foreign.append(_ForeignElement(tag, tag, False, []))
            return False
        if tag in _HIDDEN_ELEMENTS:
            self._report_other(tag, attrs)
            self._hidden.append((tag, len(self._foreign)))
            return True
        if self._hidden:  # a tag inside a hidden element is no field
            return tag not in _VOID_ELEMENTS
        if tag == "select" and self._select_open:
            self._end_select()
            return False
        self._select_open = self._select_open or tag == "select"
        self.handle_form_tag(tag, _first_attributes(attrs))
        return tag not in _VOID_ELEMENTS

    def _select_ignores(self, tag: str) -> bool:
        """Whether an open select ignores the start tag; one that ends the select ends it here."""
        if not self._reads_select_markup():
            return False
        if tag in _IGNORED_IN_SELECT:
            return True
        if tag in _SELECT_ENDING_TAGS:
            self._end_select()
        return False

    def _end_foreign(self, tag: str) -> bool:
        """Read an end tag in foreign content; False if a browser reads it as an HTML end tag instead."""
        if self._foreign[-1].html:  # an HTML element inside the integration point is where the browser is
            return False
        if tag in _FOREIGN_BREAKOUT_END_TAGS:
            self._break_out()
            return False
        for index in range(len(self._foreign) - 1, self._hidden_floor() - 1, -1):
            element = self._foreign[index]
            if index < len(self._foreign) - 1 and element.html:  # the HTML elements open inside it come first
                return False
            if element.tag == tag:
                del self._foreign[index:]
                return True
        return False

    def _end_html(self, tag: str) -> None:
        if tag == "frameset" and self._frameset_depth and not self._hidden:
            self._frameset_depth -= 1
        if self._hidden:  # only the end tag of the innermost hidden element ends it
            name, depth = self._hidden[-1]
            if tag == name:
                self._hidden.pop()
                del self._foreign[depth:]
                self._close_html(tag)
            return
        self._form_open = self._form_open and tag != "form"  # a </form> forgets the open form even out of scope
        self._close_html(tag)
        if tag == "select":
            if self._select_open:
                self._end_select()
        else:
            self.handle_form_endtag(tag)

    def _close_html(self, tag: str) -> None:
        """Close the innermost open HTML element of the tag, with the elements opened inside it, as a browser does.

        The end tag reaches no element past an open element that bounds it (_end_tag_target) or, unless it looks in
        table scope, past an SVG or MathML integration point; a </form> removes the form alone, and a formatting end
        tag leaves the special elements inside it open (_left_open_by_end_tag) and the active formatting element
        closed, while it only forgets one an earlier end tag closed implicitly.
        """
        if self._forgets_closed_formatting(tag):
            return
        names, barriers = _end_tag_target(tag)
        for depth in range(len(self._foreign), self._hidden_floor() - 1, -1):
            html = self._foreign[depth - 1].html if depth else self._html
            index = _innermost_open_element(html, names, barriers)
            if index is not None:
                self._close_open_html(tag, depth, index)
                return
            # Only an integration point holds HTML elements, and it bounds the scope; the page itself is the last.
            if depth and barriers is not _TABLE_SCOPE_BOUNDARIES and _bounds_scope(self._foreign[depth - 1]):
                return

    def _close_open_html(self, tag: str, depth: int, index: int) -> None:
        """Close the open HTML element at index of the HTML elements open at that depth of the foreign stack."""
        html = self._foreign[depth - 1].html if depth else self._html
        closed = html[index]
        if tag == "form":
            del html[index]
        else:
            del self._foreign[depth:]
            html[index:] = _left_open_by_end_tag(tag, html[index + 1 :])
        self._forget_formatting(closed)
        self._end_closed_select()

    def _end_closed_select(self) -> None:
        """End the open select if an end tag or start tag closed it with the element it was in (a </td>)."""
        if self._select_open and not any("select" in html for html in self._open_html_lists()):
            self._end_select()

    def _reads_select_markup(self) -> bool:
        """Whether a start tag now is markup of an open select (not text, not inside a <script> or <template>)."""
        return self._select_open and self._text_only is None and not self._hidden

    def _end_select(self) -> None:
        self._select_open = False
        self._close_html("select")
        self.handle_select_end()

    def handle_data(self, data: str) -> None:
        # Text of the page itself (not of a text-only element, a <script> or <style>) that is more than whitespace
        # clears the frameset-ok flag: a <frameset> after it is ignored. A NUL is dropped, so it does not count.
        if (
            self._text_only is None
            and not self._hidden
            and self.cdata_elem is None
            and data.strip(_HTML_WHITESPACE + "\0")
        ):
            self._frameset_ok = False

    def handle_form_tag(self, tag: str, attributes: dict[str, str]) -> None:
        """A start tag of an HTML element outside a text-only or hidden element; a <select> only when none is open."""

    def handle_text_only_tag(self, tag: str, attributes: dict[str, str]) -> None:
        """A start tag of a text-only element (a <textarea>, <title>, ...) outside a hidden one; its text follows."""

    def handle_other_tag(self, tag: str, attributes: dict[str, str]) -> None:
        """A start tag of a <script>, a <template> or an SVG or MathML element outside a hidden element.

        They are elements of the page, not form fields: a subclass that needs every element of an id (getElementById
        finds them all) reads them here, the others ignore them.
        """

    def handle_form_endtag(self, tag: str) -> None:
        """An end tag other than </select> outside a text-only or hidden element or an open foreign element."""

    def handle_select_end(self) -> None:
        """The open select ended (its </select>, or a start tag that ends it)."""


class _CommandRangeParser(_FormParser):
    """The value of each min/max <input> on a Web-IO command edit form, read as a browser does.

    Attribute values come unquoted with entities resolved and a ">" inside a quoted value does not end its tag.
    The first input of an id counts and the id is compared exactly (case included), as getElementById finds it.
    fields maps "min"/"max" to the value, or None if the input has no value attribute.
    """

    def __init__(self) -> None:
        super().__init__()
        self.fields: dict[str, str | None] = {}

    def handle_form_tag(self, tag: str, attributes: dict[str, str]) -> None:
        if tag == "input" and (field := _WEBIO_RANGE_INPUT_IDS.get(attributes.get("id", ""))) is not None:
            self.fields.setdefault(field, attributes.get("value"))


class _DoorstationParser(_FormParser):
    """The Doorstation attributes of every element carrying data-camera_id, read as a browser reads them.

    Only the distinct attribute sets are kept, in the order they first appear: a Doorstation tab repeats them on
    many elements, so the parser's state grows with the Doorstations, not with the page.
    """

    def __init__(self) -> None:
        super().__init__()
        self.elements: dict[tuple[tuple[str, str], ...], None] = {}
        self.saw_extension_counter = False

    def handle_form_tag(self, tag: str, attributes: dict[str, str]) -> None:
        if _is_extension_counter(tag, attributes):
            self.saw_extension_counter = True
        if CAMERA_ID_ATTRIBUTE in attributes:
            kept = tuple((name, attributes[name]) for name in DOORSTATION_ATTRIBUTES if name in attributes)
            self.elements.setdefault(kept, None)


def _is_extension_counter(tag: str, attributes: dict[str, str]) -> bool:
    """Whether a tag is the device counter of an extension block (see _EXTENSION_COUNTER_ID_PREFIX)."""
    return (
        tag == "div"
        and attributes.get("id", "").startswith(_EXTENSION_COUNTER_ID_PREFIX)
        and any(_EXTENSION_COUNTER_CLASS_RE.fullmatch(name) for name in attributes.get("class", "").split())
    )


def parse_doorstations(io_home_html: str) -> dict[int, Doorstation]:
    """The Doorstations of the IO home page, keyed by extension id (see doorstation.doorstations_from_attributes).

    Every extension block of the page, BASE included, carries a device counter (see _EXTENSION_COUNTER_ID_PREFIX).
    A page with such counters but no element with data-camera_id has no Doorstation and gives an empty result. A
    page with neither counters nor an element with a non-empty data-camera_id is a changed page, not an installation
    without Doorstations: that raises ComexioDataError, so a caller never drops its cameras on a silent empty result;
    other Doorstation attributes alone (data-extid, an empty data-camera_id) do not count, a page that kept them but
    lost its camera ids has changed. Limit: a firmware that renames data-camera_id but keeps the counters looks like
    an installation without Doorstations.
    """
    parser = _DoorstationParser()
    parser.feed(io_home_html)
    parser.close()
    names_a_camera = any(dict(element).get(CAMERA_ID_ATTRIBUTE) for element in parser.elements)
    if not parser.saw_extension_counter and not names_a_camera:
        raise ComexioDataError("IO home page has no extension counters, the Doorstations are unknown")
    return doorstations_from_attributes(dict(element) for element in parser.elements)


class _ExtensionFormParser(_FormParser):
    """The value of each input of the extension edit form the client reads (EXTENSION_INPUTS), read as a browser does.

    The first element of an id counts, whatever its tag (a <script>, <template>, <textarea> or SVG/MathML element
    too), and the id is compared exactly, as getElementById finds it: if that element is no input, the id has no input
    at all and a later input of it is not read. What lies inside a <script>, <template> or <textarea> is no element of
    the page and does not count. inputs maps the id to the value, or None if the input has no value attribute; an
    input the form lacks has no key. The login rows of the form are not among the ids, so no password field is ever
    kept.
    """

    def __init__(self) -> None:
        super().__init__()
        self.inputs: dict[str, str | None] = {}
        self._seen: set[str] = set()
        self._root_has_id: dict[str, bool] = {}  # each of html/head/body seen so far: does its element carry an id
        self._head_implied = False  # a start tag other than html/head came: the head exists, a <head> tag is ignored

    def handle_form_tag(self, tag: str, attributes: dict[str, str]) -> None:
        element_id = attributes.get("id", "")
        counts = tag not in _ROOT_TAGS or self._root_adds_attributes(tag, "id" in attributes)
        self._head_implied = self._head_implied or tag not in _BEFORE_HEAD_TAGS
        if not counts or self._start_ignored or element_id not in EXTENSION_INPUTS or element_id in self._seen:
            return
        self._seen.add(element_id)
        if tag == "input":
            self.inputs[element_id] = attributes.get("value")

    def handle_form_endtag(self, tag: str) -> None:
        self._head_implied = self._head_implied or tag in _HEAD_IMPLYING_END_TAGS

    def handle_data(self, data: str) -> None:
        super().handle_data(data)
        # Text that is more than whitespace makes the browser insert the head before the first <head>: it is ignored.
        self._head_implied = self._head_implied or bool(data.strip(_HTML_WHITESPACE))

    def handle_text_only_tag(self, tag: str, attributes: dict[str, str]) -> None:
        self._note_other_element(attributes)

    def handle_other_tag(self, tag: str, attributes: dict[str, str]) -> None:
        self._note_other_element(attributes)

    def _root_adds_attributes(self, tag: str, has_id: bool) -> bool:
        """Whether a start tag of html, head or body can give its element an id, as a browser does.

        The first one makes the element. A repeated <html> or <body> only adds the attributes its element lacks, so
        its id counts only while the element has none; a repeated <head> is ignored altogether, and so is the first
        <head> once another start tag has implied the head (a <head> in the body, or after a <meta>).
        """
        first = tag not in self._root_has_id
        had_id = self._root_has_id.get(tag, False)
        self._root_has_id[tag] = had_id or has_id
        if tag == "head":
            return first and not self._head_implied
        return first or not had_id

    def _note_other_element(self, attributes: dict[str, str]) -> None:
        """An element that is no input: it is the first of its id unless an element of that id came before."""
        self._head_implied = True  # a script, template, textarea or SVG/MathML element: no <html>/<head> tag
        element_id = attributes.get("id", "")
        if element_id in EXTENSION_INPUTS:
            self._seen.add(element_id)


def parse_extension_form(ext_id: int, html: str) -> ExtensionInfo:
    """The ExtensionInfo of the edit form html of extension ext_id (see extension.extension_from_inputs)."""
    parser = _ExtensionFormParser()
    parser.feed(html)
    parser.close()
    return extension_from_inputs(ext_id, parser.inputs)


# The leading digits of a size, as a browser reads them (ASCII digits only: \d alone would take any Unicode digit).
_SELECT_SIZE = re.compile(r"[ \t\n\f\r]*\+?(\d+)", re.ASCII)


def _is_list_box(attributes: dict[str, str]) -> bool:
    """Whether the select is shown as a list (a size of 2 or more), not as a drop-down that shows its first option."""
    found = _SELECT_SIZE.match(attributes.get("size", ""))
    return found is not None and found.group(1).lstrip("0") not in ("", "1")


class _ApiFormParser(_FormParser):
    """The fields of the API settings page the client reads (see api_settings), read as a browser does.

    Only the fields of the first form apiform count (a page without it has none, and the settings are not read; a
    second form of the id is ignored, as a browser's getElementById ignores it). The
    checked api_active radio counts (the last one, as a browser keeps one checked per group; an input of another type
    is no switch), the first input of an id
    (compared exactly), the first select of an id and its selected option (the last one, a browser shows one).
    An option of a disabled <optgroup> is disabled, a select that allows several selected options has no single
    value (it does not count), and a select shown as a list (size 2 or more) shows no option unless one is selected.
    Everything else on the page is ignored.
    """

    def __init__(self) -> None:
        super().__init__()
        self.fields = ApiPageFields()
        self._in_api_form = False
        self._api_form_seen = False  # the first form apiform was met; a later one does not count
        self._select_id: str | None = None
        self._selects_seen: set[str] = set()
        # The open select's last selected option, and its first enabled one (what a browser shows if none is selected).
        self._chosen: str | None = None
        self._first_enabled: str | None = None
        self._multiple = False  # the open select allows several selected options
        self._list_box = False  # the open select is shown as a list (size 2 or more)
        self._group_disabled = False  # the open <optgroup> is disabled

    def handle_form_tag(self, tag: str, attributes: dict[str, str]) -> None:
        if tag == "form":
            # A form inside a form is ignored by a browser: the outer one stays open.
            if not self._start_ignored:
                self._in_api_form = attributes.get("id") == API_FORM_ID and not self._api_form_seen
                self._api_form_seen = self._api_form_seen or self._in_api_form
        elif not self._in_api_form:
            return
        elif tag == "input":
            self._input(attributes)
        elif tag == "select":
            self._select_start(attributes)
        elif tag == "option" and self._select_id is not None:
            self._option(attributes)
        elif tag == "optgroup" and self._select_id is not None:
            self._group_disabled = "disabled" in attributes

    def _select_start(self, attributes: dict[str, str]) -> None:
        # The first select of an id counts, like the first input.
        select_id = attributes.get("id")
        if select_id in API_SELECT_IDS and select_id not in self._selects_seen:
            self._selects_seen.add(select_id)
            self._select_id = select_id
            self._multiple = "multiple" in attributes
            self._list_box = _is_list_box(attributes)
        else:
            self._select_id = None

    def _option(self, attributes: dict[str, str]) -> None:
        value = attributes.get("value", "")
        if "selected" in attributes:
            self._chosen = value
        if self._first_enabled is None and "disabled" not in attributes and not self._group_disabled:
            self._first_enabled = value

    def handle_form_endtag(self, tag: str) -> None:
        if tag == "form":
            self._in_api_form = False
        elif tag == "optgroup":
            self._group_disabled = False

    def _input(self, attributes: dict[str, str]) -> None:
        if (
            attributes.get("name") == ACTIVE_RADIO
            and attributes.get("type", "").lower() == "radio"
            and "checked" in attributes
        ):
            self.fields.active_choice = attributes.get("value", "")
        if (input_id := attributes.get("id", "")) in API_INPUT_IDS:
            self.fields.inputs.setdefault(input_id, attributes)

    def _shown_option(self) -> str | None:
        """The value a browser shows: the selected option, else the first enabled one (a list box shows none)."""
        if self._chosen is not None:
            return self._chosen
        return None if self._list_box else self._first_enabled

    def handle_select_end(self) -> None:
        # Only a select that ended counts: a page cut off inside it says nothing about its value, and a select that
        # allows several selected options has none.
        if self._select_id is not None and not self._multiple and (choice := self._shown_option()) is not None:
            self.fields.selected[self._select_id] = choice
        self._select_id = self._chosen = self._first_enabled = None
        self._multiple = self._list_box = self._group_disabled = False


def parse_api_settings(html: str, *, api_username: str, api_password: str) -> ApiSettings:
    """The ApiSettings of the API page html, with the login compared to api_username and api_password.

    See api_settings.api_settings_from_fields; the password is compared and not kept.
    """
    parser = _ApiFormParser()
    parser.feed(html)
    parser.close()
    return api_settings_from_fields(parser.fields, api_username=api_username, api_password=api_password)


async def _read_first_frame(resp: aiohttp.ClientResponse, what: str) -> bytes:
    """The first complete JPEG of a stream response; see ComexioClient.get_doorstation_snapshot."""
    received = bytearray()
    reader = JpegFrameReader()
    # Each read asks for at most the bytes left under the cap, so no byte past it is ever taken from the stream (a
    # frame that ends beyond the cap is too large, like no frame at all). With the cap reached no further byte can
    # complete a frame, so the loop ends there and does not wait for more (a stream that stalls at exactly the cap
    # would end in a timeout). An empty read is the end of the stream.
    while (left := _MAX_SNAPSHOT_BYTES - len(received)) > 0 and (chunk := await resp.content.read(left)):
        received += chunk
        # A frame is complete only once its EOI arrived; looking for it only in the new bytes (and the byte before
        # them, a marker may span two chunks) keeps the stream from being rescanned for every chunk.
        new_bytes = received[-len(chunk) - 1 :]
        if JPEG_EOI in new_bytes and (frame := _complete_frame(reader, received, what)) is not None:
            return frame
    # A login form in the kept bytes is an expired session, also when the stream ran past the cap.
    if _LOGIN_FORM_SUBMIT_MARKER.encode() in received:
        raise ComexioAuthenticationError(f"Comexio served the login form, the session is not logged in ({what})")
    if len(received) >= _MAX_SNAPSHOT_BYTES:
        raise ComexioDataError(f"{what}: no complete JPEG in the first {_MAX_SNAPSHOT_BYTES} bytes of the stream")
    raise ComexioDataError(
        f"{what}: the stream ({resp.content_type!r}) ended after {len(received)} bytes without a complete JPEG"
    )


def _complete_frame(reader: JpegFrameReader, received: bytearray, what: str) -> bytes | None:
    """The first complete JPEG of the stream so far (reader goes on where it stopped), its error naming the camera."""
    try:
        return reader.frame(received)
    except ComexioDataError as err:
        raise ComexioDataError(f"{what}: {err}") from err


class _CommandTextParser(_FormParser):
    """Where the text of the first <textarea id="parameter"> and <textarea id="data"> of an edit form lies.

    The parser only finds the fields (a field in a <template> or <script> or inside another text-only element does
    not count, the first of an id wins, the id is compared exactly); _command_texts cuts the text out of the page
    itself, so it does not depend on whether this html.parser release reports markup inside a textarea as text.
    spans maps "parameter"/"data" to the (start, end) indexes of its text in the page. A textarea tag that lies in
    the text of an earlier textarea is no field, even where this html.parser release ends that text sooner than a
    browser does (at "</textarea" plus a vertical tab, say).
    """

    def __init__(self, page: str) -> None:
        super().__init__()
        self.spans: dict[str, tuple[int, int]] = {}
        self._page = page
        self._text_end = 0
        self._last_line = (1, 0)

    def handle_text_only_tag(self, tag: str, attributes: dict[str, str]) -> None:
        if tag != _TEXTAREA:
            return
        line, column = self.getpos()
        start = self._line_offset(line) + column + len(self.get_starttag_text() or "")
        if start < self._text_end:
            return
        end = _TEXTAREA_END_RE.search(self._page, start)
        self._text_end = end.start() if end else len(self._page)
        field = attributes.get("id", "")
        if field in _WEBIO_TEXT_FIELD_IDS and field not in self.spans:
            self.spans[field] = (start, self._text_end)

    def _line_offset(self, line: int) -> int:
        """The index in the page where getpos()'s 1-based line starts (html.parser counts lines on LF only)."""
        # getpos() only moves forward, so each call goes on from the last line it was asked for.
        current, offset = self._last_line if line >= self._last_line[0] else (1, 0)
        for _ in range(line - current):
            offset = self._page.index("\n", offset) + 1
        self._last_line = (line, offset)
        return offset


def _command_texts(html: str) -> dict[str, str]:
    """{"parameter": ..., "data": ...} of a Web-IO command edit form; a text field the form lacks is left out.

    A textarea holds text up to its own end tag (or the end of the page): a NUL becomes U+FFFD, entities are
    resolved, every line break (a decoded one too) is a line feed, and the first line feed straight after the start
    tag, written literally or as a reference such as "&#10;", is no part of it.
    """
    parser = _CommandTextParser(html)
    parser.feed(html)
    parser.close()
    texts: dict[str, str] = {}
    for field, (start, end) in parser.spans.items():
        raw, dropped = _LEADING_LINE_BREAK_RE.subn("", html[start:end], count=1)
        text = unescape(raw.replace("\0", "�"))
        if not dropped:
            # The first character the browser reads after the start tag is a line feed, written as a reference.
            text = text.removeprefix("\n")
        texts[field] = _LINE_BREAK_RE.sub("\n", text)
    return texts


class _Option(NamedTuple):
    attributes: dict[str, str]
    # Its text, written as it comes: a list of the chunks a comment splits it into would hold an object per chunk.
    buffer: StringIO

    @property
    def text(self) -> str:
        return self.buffer.getvalue()


class _ProtocolSelectParser(_FormParser):
    """The <option>s of a page's first <select id="protocol">, read as a browser does.

    Attribute values come unquoted with entities resolved, the first of a name wins and a bare one is "";
    a ">" inside a quoted value does not end its tag. options counts the options and selections the selected ones;
    selected is the last selected option and first_enabled the first one that neither it nor its <optgroup> disables.
    Only those two keep their attributes and text, so a select with many options holds no more than two of them.
    closed says the select ended (see _FormParser), so its options are complete (a cut-off answer drops a half-read
    option tag); multiple says it allows several selected options and listbox that its size shows more than one row,
    so it selects no option by default.
    """

    def __init__(self) -> None:
        super().__init__()
        self.found = False
        self.closed = False
        self.multiple = False
        self.listbox = False
        self.options = 0
        self.selections = 0
        self.selected: _Option | None = None
        self.first_enabled: _Option | None = None
        self._inside = False  # the open select is the protocol select
        self._text: StringIO | None = None  # the text of the open option, if it is kept
        self._group: bool | None = None  # whether the open <optgroup> is disabled; None: no optgroup is open

    def handle_form_tag(self, tag: str, attributes: dict[str, str]) -> None:
        if tag == "select":
            self._start_select(attributes)
        elif tag in _OPTION_ENDING_TAGS:
            self._start_option_ending_tag(tag, attributes)

    def _start_select(self, attributes: dict[str, str]) -> None:
        self._inside = not self.found and attributes.get("id") == _WEBIO_PROTOCOL_SELECT_ID
        if self._inside:
            self.found = True
            self.multiple = "multiple" in attributes
            self.listbox = _shows_several_rows(attributes.get("size", ""))

    def _start_option_ending_tag(self, tag: str, attributes: dict[str, str]) -> None:
        if tag != "option":  # an <optgroup> outside the protocol select does not group its options
            self._group = "disabled" in attributes if tag == "optgroup" and self._inside else None
        self._text = None
        if tag == "option" and self._inside:
            self._start_option(attributes)

    def _start_option(self, attributes: dict[str, str]) -> None:
        self.options += 1
        selected = "selected" in attributes
        first_enabled = self.first_enabled is None and not ("disabled" in attributes or self._group)
        if not (selected or first_enabled):
            return  # neither selected nor the first enabled option: it is never the chosen one
        option = _Option(attributes, StringIO())
        self._text = option.buffer
        if selected:
            self.selections += 1
            self.selected = option
        if first_enabled:
            self.first_enabled = option

    def handle_form_endtag(self, tag: str) -> None:
        # A browser ignores </hr>, and </optgroup> without an open optgroup; </option> and </optgroup> end the option.
        if tag == "option":
            self._text = None
        elif tag == "optgroup" and self._group is not None:
            self._text = None
            self._group = None

    def handle_data(self, data: str) -> None:
        super().handle_data(data)
        # The text of a <title> in an option is part of its text; the content of a <script> or <template> is not.
        if self._text is not None and not self._hidden:
            self._text.write(data)

    def handle_select_end(self) -> None:
        self.closed = self.closed or self._inside
        self._inside = False
        self._text = None
        self._group = None


def _shows_several_rows(size: str) -> bool:
    """Whether a select's size attribute, parsed as a browser parses it, is above 1 (a missing or bad one is 1)."""
    if (match := _SIZE_RE.match(size)) is None:
        return False
    digits = match.group(1).lstrip("0")  # compared as text: int() refuses more than 4300 digits
    return len(digits) > 1 or digits > "1"


def _chosen_option(parser: _ProtocolSelectParser, command_id: str | int) -> _Option | None:
    """The option a single select shows: its last selected one, else its first enabled one unless it is a list box."""
    if parser.selections > 1:
        _LOGGER.debug(
            "Web-IO command %r protocol select has %d selected options, the last counts", command_id, parser.selections
        )
    if parser.selected is not None:
        return parser.selected
    if parser.listbox:
        _LOGGER.debug("Web-IO command %r protocol select is a list box without a selected option", command_id)
        return None
    if parser.first_enabled is None:
        _LOGGER.debug("Web-IO command %r protocol select has no enabled option", command_id)
        return None
    return parser.first_enabled


def _option_value(attributes: dict[str, str], text: str) -> str:
    """An option's value as a browser submits it: its value attribute, else its text with whitespace collapsed."""
    if "value" in attributes:
        return attributes["value"]
    return _HTML_WHITESPACE_RE.sub(" ", text).strip(_HTML_WHITESPACE)


def _command_protocol(html: str, command_id: str | int) -> int | None:
    """The protocol a Web-IO command edit form has chosen, as a browser shows it.

    That is its last selected option (a single select keeps only the last), else its first enabled option.
    None means unknown — the form has no protocol select, no option, a multiple select (none or several
    protocols), a list box or only disabled options without a selected one, or no numeric value — never HTTP.
    """
    parser = _ProtocolSelectParser()
    parser.feed(html)
    parser.close()
    if not parser.found:
        _LOGGER.debug("Web-IO command %r edit form has no protocol select", command_id)
        return None
    if not parser.closed:
        _LOGGER.debug("Web-IO command %r protocol select is not closed", command_id)
        return None
    if parser.multiple:
        _LOGGER.debug("Web-IO command %r protocol select allows several options", command_id)
        return None
    if not parser.options:
        _LOGGER.debug("Web-IO command %r protocol select has no option", command_id)
        return None
    if (chosen := _chosen_option(parser, command_id)) is None:
        return None
    value = _option_value(chosen.attributes, chosen.text)
    if (match := _PROTOCOL_VALUE_RE.fullmatch(value)) is None:
        _LOGGER.warning("Web-IO command %r has an unknown protocol value: %s", command_id, _excerpt(value))
        return None
    return int(match.group(1))


def _collect_io_values(result: dict[str, Any], io_ids: Iterable[str]) -> tuple[dict[str, str], list[str]]:
    """({IO id: numeric text}, ["IO id: answer type", ...] for answers that are neither a number nor not_found).

    The type, not the answer: an unexpected format may still wrap an IO reading, which stays at DEBUG.
    """
    values: dict[str, str] = {}
    unexpected: list[str] = []
    for io_id in io_ids:
        key = f"{_IO_LIVE_KEY_PREFIX}{io_id}"
        raw = result.get(key)
        if (value := _numeric_text(raw)) is not None:
            values[io_id] = value
            continue
        # %r / repr: an IO id is caller input and must not put raw line breaks into a log line.
        _LOGGER.debug("No live value for IO %r, dashboard/refresh answered %s", io_id, _excerpt(raw))
        # An absent key and Comexio's not_found sentinel are expected; a key that is present with
        # JSON null (or anything else that is no number) is not.
        if key in result and not (isinstance(raw, str) and _NOT_RUNNING_SENTINEL_RE.fullmatch(raw.strip())):
            unexpected.append(f"{io_id!r}: {type(raw).__name__}")
    return values, unexpected


def _io_ext_ids(ios: Iterable[tuple[int | str, str]]) -> dict[str, int]:
    """{IO id: extension id as int} from (extension id, IO id) pairs; identical duplicates collapse.

    TypeError for an entry that is no 2-tuple, a bool or non-int/str extension id and a non-str IO id,
    ValueError for a negative extension id, one that is not a digit string and for one IO id given
    with two different extension ids.
    """
    pairs: dict[str, int] = {}
    for entry in ios:
        # A str entry would unpack too ("12" -> ext "1", IO "2"), so the shape is checked first.
        if not (isinstance(entry, tuple) and len(entry) == 2):
            raise TypeError(f"ios entries must be IoAddress(ext_id, io_id) pairs, got {entry!r}")
        ext_id, io_id = entry
        _require_strs(**{f"io_id[{io_id!r}]": io_id})
        ext = _ext_id_int(ext_id, io_id)
        if pairs.setdefault(io_id, ext) != ext:
            raise ValueError(f"IO {io_id!r} is given with two different extension ids ({pairs[io_id]} and {ext})")
    return pairs


def _ext_id_int(ext_id: Any, io_id: str) -> int:
    """The extension id as int: a non-negative int or digit string is accepted, a bool is not."""
    if _is_int(ext_id):
        if ext_id < 0:
            raise ValueError(f"ext_id[{io_id!r}] must not be negative, got {ext_id!r}")
        return ext_id
    if not isinstance(ext_id, str):
        raise TypeError(f"ext_id[{io_id!r}] must be integers or integer strings, got {ext_id!r}")
    if not (ext_id.isascii() and ext_id.isdigit()):
        raise ValueError(f"ext_id[{io_id!r}] is not an integer string: {ext_id!r}")
    return int(ext_id)


def _numeric_text(raw: Any) -> str | None:
    """raw as a normalized decimal string ("21,5" -> "21.5"), or None if it is no plain decimal number.

    dashboard/refresh answers a value as a string; an int or float is accepted as well, a bool
    is not. Only an optional sign, digits and an optional fraction count: "0:not_found" (inactive
    IO), "", an error text, "1_0", exponents, "inf" and "nan" yield None.
    """
    if isinstance(raw, bool) or not isinstance(raw, (str, int, float)):
        return None
    if isinstance(raw, float):
        if not math.isfinite(raw):
            return None
        text = format(Decimal(repr(raw)), "f")  # repr never switches to an exponent in "f"
    else:
        text = str(raw).strip()
    return text.replace(",", ".") if _PLAIN_DECIMAL_RE.fullmatch(text) else None


def _plan_running(raw: Any) -> bool | None:
    """Run state from one fupValueData answer: True for a JSON value set, False for the not-running sentinel.

    None for anything else — only these two shapes were observed, so a PHP warning or an error
    text must not pass as "stopped", nor a truncated "{..." as "running".
    """
    if isinstance(raw, (dict, list)):
        return True
    if not isinstance(raw, str):
        return None
    if _NOT_RUNNING_SENTINEL_RE.fullmatch(text := raw.strip()):
        return False
    try:
        return True if isinstance(json.loads(text), (dict, list)) else None
    except ValueError:
        return None


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
    except (TypeError, ValueError, OverflowError):
        _LOGGER.warning("Skipping bulk function plan entry with non-numeric id %s", _excerpt(fid_str))
        return None
    if fub_id in seen:
        raise ComexioDataError(f"Bulk function plan payload lists plan {fub_id} twice")
    seen.add(fub_id)
    if wanted is not None and fub_id not in wanted:
        return None
    if not isinstance(data, dict):
        _LOGGER.warning("Skipping bulk function plan %s: payload is not an object", fub_id)
        return None
    missing = plan_payload_missing_collections(data) if strict else []
    if missing:
        _LOGGER.warning(
            "Skipping bulk function plan %s: payload has no %s collection", fub_id, " and no ".join(missing)
        )
        return None
    try:
        return fub_id, normalize_plan_payload(data)
    except ComexioDataError as err:
        _LOGGER.warning("Skipping bulk function plan %s: %s", fub_id, err)
        return None


def _pinned_redirect_target(resp: aiohttp.ClientResponse, what: str) -> str:
    """Where a redirect of a pinned client leads; ComexioResponseError unless it stays on the same HTTPS origin."""
    return _redirect_target(resp, what, _same_origin, "outside the pinned HTTPS connection")


def _unpinned_redirect_target(resp: aiohttp.ClientResponse, what: str) -> str:
    """Where a redirect of an unpinned client leads; ComexioResponseError unless it stays on the Comexio server.

    It may stay on the origin or move on to HTTPS on port 443 of the same host (plain HTTP sent on to HTTPS). Any
    other port may be another service of that host, and the session cookie is not bound to a port; any other host
    would let a peer make the caller request whatever its network reaches.
    """
    return _redirect_target(resp, what, _same_origin_or_https, "away from the Comexio server")


def _same_origin(current: str, target: str) -> bool:
    """Whether target has the scheme, host and port of current; ValueError for a malformed port."""
    return _origin(target) == _origin(current)


def _same_origin_or_https(current: str, target: str) -> bool:
    """Whether target is on current's origin or HTTPS on its host's default port; ValueError for a malformed port."""
    here = _origin(current)
    return _origin(target) in {here, ("https", here[1], _HTTPS_DEFAULT_PORT)}


def _require_replayable_body(kwargs: dict[str, Any], what: str, status: int) -> None:
    """ComexioResponseError if a redirect that keeps the method would send a multipart body a second time.

    aiohttp.FormData can be sent once; a second use raises a RuntimeError of aiohttp's own, which no caller expects.
    """
    if isinstance(kwargs.get("data"), aiohttp.FormData):
        raise ComexioResponseError(
            f"{what} failed: Comexio answered {status} to a multipart upload, which cannot be sent again", status=status
        )


def _redirect_target(resp: aiohttp.ClientResponse, what: str, stays: Callable[[str, str], bool], outside: str) -> str:
    """Where a redirect leads; ComexioResponseError unless stays(current URL, target) holds."""
    location = resp.headers.get("Location", "")
    if not location:
        raise ComexioResponseError(
            f"{what} failed: Comexio answered {resp.status} without a Location header", status=resp.status
        )
    current = str(resp.url)
    try:
        target = urljoin(current, location)
        same_place = stays(current, target)
    except ValueError as err:
        raise ComexioResponseError(
            f"{what} failed: Comexio redirected to an unparsable location {_excerpt(location)}", status=resp.status
        ) from err
    if not same_place:
        raise ComexioResponseError(
            f"{what} refused: Comexio redirected to {_excerpt(location)}, {outside}", status=resp.status
        )
    return target


def _origin(url: str) -> tuple[str, str | None, int | None]:
    """Scheme, host and port of url, the port filled in for http and https; ValueError for a malformed port."""
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    port = parts.port  # parses the port, so a malformed one raises here and not inside aiohttp
    return scheme, parts.hostname, _DEFAULT_PORTS.get(scheme) if port is None else port


def _unencoded(kwargs: dict[str, Any]) -> dict[str, Any]:
    """The request arguments with an Accept-Encoding: identity header, and aiohttp not decompressing the answer.

    aiohttp decompresses an answer before the reader sees it, so a byte cap on what is read (_RESPONSE_MAX_BYTES,
    _ERROR_BODY_MAX_BYTES) would limit the plain bytes only when nothing was compressed. A device that ignores the
    header and sends a compressed answer anyway is caught by _content_encoding, since the bytes are not decompressed.
    A header the caller sets itself wins, whatever the case of its name (HTTP header names are case-insensitive);
    a caller that asks for a coding gets every answer the device really sends in a coding refused (one sent
    unencoded is still read). auto_decompress is always False.
    """
    headers = dict(kwargs.get("headers") or {})
    if all(name.lower() != _ACCEPT_ENCODING.lower() for name in headers):
        headers[_ACCEPT_ENCODING] = _IDENTITY_ENCODING
    return {**kwargs, "headers": headers, "auto_decompress": False}


def _content_encoding(resp: aiohttp.ClientResponse) -> str:
    """The first coding other than identity that resp's Content-Encoding lines declare, "" if there is none.

    An empty result means the bytes are the plain page. Every header line counts and each may list codings
    (`identity, gzip`).
    """
    codings = (token.strip() for line in resp.headers.getall("Content-Encoding", []) for token in line.split(","))
    return next((coding for coding in codings if coding and coding.lower() != _IDENTITY_ENCODING), "")


def _text_encoding(resp: aiohttp.ClientResponse) -> str:
    """The codec resp's body is decoded with: its declared charset if it is a text charset Python knows, else UTF-8.

    UTF-8 is also the fallback of aiohttp's default; a fallback_charset_resolver of the caller's session does not
    apply, since the body is read here and not through ClientResponse.text().
    """
    if resp.charset:
        with suppress(LookupError, ValueError):
            name = codecs.lookup(resp.charset).name
            "".encode(name)  # raises for a codec that is no text encoding (hex, zlib, ...) and for "undefined"
            if name not in _NOT_A_CHARSET:
                return name
    return _UTF8


def _diagnostic_error_text(error: BaseException) -> str:
    """A transport error as _transport_error_text, cut to _DIAGNOSTIC_ERROR_MAX_CHARS; any other error by its class.

    A status or data error carries a page excerpt: it is named by class only.
    """
    if isinstance(error, (aiohttp.ClientError, TimeoutError)):
        return _bounded(_transport_error_text(error))
    return type(error).__name__


def _diagnostic_label(what: str) -> str:
    """The label of a request for the debug line: cut, control characters escaped.

    Mostly built from constants and numbers, but a device id is the string Comexio's own list gave (or the caller
    passed), so a line break in it would otherwise start a second log line.
    """
    return log_excerpt([what])


def _bounded(text: str) -> str:
    """text, cut to _DIAGNOSTIC_ERROR_MAX_CHARS."""
    return text if len(text) <= _DIAGNOSTIC_ERROR_MAX_CHARS else f"{text[:_DIAGNOSTIC_ERROR_MAX_CHARS]}…"


def _transport_error_text(error: BaseException) -> str:
    """The repr of a transport error, without what it may carry of the request or the answer.

    A ClientResponseError (a session with raise_for_status) carries the request, its URL (whose query holds the value
    of an API write) and headers (Authorization); an InvalidURL is the URL: named by class (and status) only. A
    ServerDisconnectedError for a connection dropped in the middle of an answer carries the part aiohttp parsed, its
    headers (Set-Cookie) included: named by class and status only. A connection error carries the connection key,
    which holds the proxy and its credentials when the request went through one: shown without them.
    """
    if isinstance(error, aiohttp.ClientResponseError):
        return f"{type(error).__name__} (status {error.status})"
    if isinstance(error, aiohttp.InvalidURL):
        return type(error).__name__
    if _carries_an_answer(error):
        return f"{type(error).__name__} (in the middle of an answer, status {getattr(error.message, 'code', '?')})"
    if isinstance(error, aiohttp.ClientConnectorError):
        # Its repr, with the key in it replaced.
        return f"{type(error).__name__}({', '.join(repr(_without_connection_key(arg)) for arg in error.args)})"
    return repr(error)


def _raise_from_stripped(error: ComexioError, cause: BaseException) -> NoReturn:
    """Raise error from cause, once cause is stripped of what _transport_error_text leaves out of its text.

    Called in the except block that caught cause, with error built from cause before (the arguments are evaluated
    first, so its text comes from cause as aiohttp raised it). cause stays the cause: a traceback prints its message,
    and a caller may re-raise and log it itself. Only its content changes, its type stays, so _once_more_if_dropped
    and such a caller still recognise it. Should stripping fail, error goes out with neither a cause nor a context:
    cause would still show what was to go. Its type is then gone with it, so a dropped connection is not tried again.
    """
    try:
        _strip_request_and_answer(cause, _transport_error_text(cause))
    # Stripping must never replace the request's own error. The classes alone: a message could hold what was to go.
    except Exception as strip_error:
        # A logging filter or handler that raises on the record must not replace error either: what it raised would
        # carry the unstripped cause as its context.
        with suppress(Exception):
            _LOGGER.warning(
                "Could not strip the request from %s (%s): raised without it as the cause or context",
                type(cause).__name__,
                type(strip_error).__name__,
            )
    else:
        raise error from cause
    try:
        raise error from None
    # The caller's except block makes cause the context of any raise in it, `from None` only hides it: cleared, and
    # re-raised bare, which does not set it again.
    except ComexioError:
        error.__context__ = None
        raise


def _strip_request_and_answer(error: BaseException, text: str) -> None:
    """Remove from a transport error what _transport_error_text left out of `text`."""
    if isinstance(error, aiohttp.ClientResponseError):
        _strip_response_error(error)
    elif isinstance(error, aiohttp.InvalidURL):
        _strip_invalid_url(error)
    elif isinstance(error, aiohttp.ClientConnectorError):
        _strip_connection_key(error)
    elif _carries_an_answer(error):
        error.args = (text,)
        error.message = text


def _strip_response_error(error: aiohttp.ClientResponseError) -> None:
    """Request URL query and headers, the answer's headers, raw bytes and redirects go; status, type and message stay.

    What cannot fail goes first; the request is rebuilt last.
    """
    # A parse error of the answer (aiohttp raises it from an HttpProcessingError) carries the raw bytes as its
    # message: the parser error's class instead, which tells what was wrong. raise_for_status and a proxy's refusal
    # carry the reason phrase as sent, which could hold anything: the standard phrase of the status instead. Any other
    # message is aiohttp's own and stays, cut like the text of the debug line.
    if isinstance(error.__cause__, HttpProcessingError):
        error.message = type(error.__cause__).__name__
    elif type(error) in _REASON_PHRASE_ERRORS:
        error.message = _standard_reason(error.status)
    elif isinstance(error.message, str):
        error.message = _bounded(error.message)
    error.headers = None  # the answer's headers (Set-Cookie)
    error.history = ()  # the redirects before it, each with its URL
    _cut_cause(error)
    info = error.request_info
    # aiohttp always passes a RequestInfo; anything else (a test double) has nothing of the request to strip.
    if isinstance(info, aiohttp.RequestInfo):
        headers = info.headers.copy()
        headers.clear()  # an empty mapping of the type aiohttp uses (Authorization was in it)
        # Built anew, not by _replace: older aiohttp versions have no NamedTuple here.
        info = aiohttp.RequestInfo(
            _bare_url(info.url), info.method, type(info.headers)(headers), _bare_url(info.real_url)
        )
    error.request_info = info
    error.args = (info, ())


def _standard_reason(status: int) -> str:
    """The standard reason phrase of an HTTP status, empty for a status without one."""
    try:
        return HTTPStatus(status).phrase
    except ValueError:
        return ""


def _strip_invalid_url(error: aiohttp.InvalidURL) -> None:
    """Its message is the URL: query, fragment and user info go, the rest stays to show what is wrong with it."""
    url = error.url
    if isinstance(url, str):
        bare: str | URL = _URL_USER_INFO_RE.sub(r"\1", re.split(r"[?#]", url, maxsplit=1)[0])
    else:
        bare = _bare_url(url)
    error._url = bare  # what InvalidURL's url and str() read
    error.args = (bare, *error.args[1:])
    _cut_cause(error)


def _strip_connection_key(error: aiohttp.ClientConnectorError) -> None:
    """The connection key loses the proxy's credentials; host, port and the OS error stay to show what failed."""
    key = error.args[0]
    if isinstance(key, ConnectionKey):
        error._conn_key = key = _bare_connection_key(key)  # what the error's host, port and str() read
        error.args = (key, *error.args[1:])


def _without_connection_key(arg: object) -> object:
    """arg for the text of a connection error: a connection key bare (see _bare_connection_key), anything else as is."""
    return _bare_connection_key(arg) if isinstance(arg, ConnectionKey) else arg


def _bare_connection_key(key: ConnectionKey) -> ConnectionKey:
    """The connection key without the proxy's credentials (proxy_auth, user info in its URL)."""
    return key._replace(proxy=None if key.proxy is None else _bare_url(key.proxy), proxy_auth=None)


def _bare_url(url: "URL") -> "URL":
    """The URL without query (the value of an API write), fragment and user info."""
    bare = url.with_query(None).with_fragment(None)
    return bare.with_user(None) if bare.absolute else bare  # a relative URL has no user info, and yarl refuses it


def _cut_cause(error: BaseException) -> None:
    """aiohttp raises these from the parser's or yarl's error, whose message holds the raw answer or the URL."""
    error.__cause__ = None
    error.__context__ = None
    error.__suppress_context__ = True


def _carries_an_answer(error: BaseException) -> TypeIs[aiohttp.ServerDisconnectedError]:
    """True for a ServerDisconnectedError that holds the parsed part of an answer (headers included), not a text."""
    return isinstance(error, aiohttp.ServerDisconnectedError) and not isinstance(error.message, str)


async def _response_text(resp: aiohttp.ClientResponse, what: str, *, errors: str = "strict") -> str:
    """The body of a successful answer as text; ComexioDataError if it passes _RESPONSE_MAX_BYTES or cannot be decoded.

    Reads at most one byte more than the cap, so an answer of any size costs at most the cap in memory, and
    refuses the answer instead of cutting it: a cut page or plan would read as complete. A compressed answer
    (see _unencoded) is refused the same way, before its text could show the login form: a device that ignores
    the identity request fails with ComexioDataError, not ComexioAuthenticationError.
    """
    if encoding := _content_encoding(resp):
        raise ComexioDataError(f"{what}: the answer came {_excerpt(encoding)}-encoded, not unencoded")
    body = bytearray()
    while (left := _RESPONSE_MAX_BYTES + 1 - len(body)) > 0 and (chunk := await resp.content.read(left)):
        body += chunk
    if len(body) > _RESPONSE_MAX_BYTES:
        raise ComexioDataError(f"{what}: response is longer than {_RESPONSE_MAX_BYTES} bytes")
    encoding = _text_encoding(resp)
    try:
        return body.decode(encoding, errors=errors)
    except UnicodeError as err:  # not only UnicodeDecodeError: a codec may raise a plain UnicodeError
        raise ComexioDataError(f"{what}: response is not valid {encoding}") from err


async def _error_page_text(resp: aiohttp.ClientResponse, what: str) -> str:
    """The first _ERROR_BODY_MAX_BYTES of the body of an error-status answer as text; "" if it breaks off mid-read.

    The rest is not read, and the status must be reported even when the body cannot be. Undecodable bytes are
    replaced; a compressed page (see _unencoded) is not read, its text names the coding so the status is not
    reported without a cause.
    """
    if encoding := _content_encoding(resp):
        return f"({_excerpt(encoding)}-encoded body not read)"
    body = bytearray()
    try:
        while (left := _ERROR_BODY_MAX_BYTES - len(body)) > 0 and (chunk := await resp.content.read(left)):
            body += chunk
    except (aiohttp.ClientError, TimeoutError) as err:
        _LOGGER.debug("%s: body of the HTTP %s answer could not be read: %r", what, resp.status, err)
        return ""
    try:
        return body.decode(_text_encoding(resp), errors="replace")
    except UnicodeError:  # a codec that fails whatever errors says: the status is reported without its text
        return body.decode(_UTF8, errors="replace")


def _collapse_error_body(text: str) -> str | None:
    """Error page text on one line, at most _ERROR_BODY_MAX_CHARS long including the "..." mark; None if blank."""
    collapsed = " ".join(text.split())
    if not collapsed:
        return None
    return collapsed if len(collapsed) <= _ERROR_BODY_MAX_CHARS else f"{collapsed[: _ERROR_BODY_MAX_CHARS - 3]}..."


def _status_error(what: str, status: int, body: str | None) -> ComexioResponseError:
    """ComexioResponseError for an HTTP error status, the body excerpt appended to the message."""
    message = f"{what} failed: HTTP {status}"
    return ComexioResponseError(f"{message}: {body}" if body else message, status=status, body=body)


def _excerpt(value: Any, limit: int = 200) -> str:
    """Short repr of an unexpected payload for an error message."""
    text = repr(value)
    return text if len(text) <= limit else f"{text[:limit]}..."
