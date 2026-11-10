"""Extension edit form: name, serial, type and IP address of an extension (get_extension)."""

from typing import Any

import pytest
from aiohttp import web

from aiocomexio import ComexioAuthenticationError, ComexioClient, ComexioDataError, ExtensionInfo
from aiocomexio.client import _ExtensionFormParser, parse_extension_form
from aiocomexio.extension import DOORSTATION_TYPE_ID, extension_from_inputs, extension_ip
from tests.common import load_fixture
from tests.fake_comexio import LOGIN_PAGE, SESSION_COOKIE, SESSION_VALUE, FakeComexio, Handler

EDIT_PATH = "/admin/extension/edit/"
PASSWORD_IN_FIXTURE = "s3cret-pw"
NEW_EXTENSION_MESSAGE = "Extension 99 edit form names no extension type (found none): unknown id or changed page"
NO_ADDRESS_MESSAGE = "Extension 14 edit form has no IP address field: changed page"


def _form(location: str | None = "192.168.0.50", type_id: str | None = "16", extra: str = "") -> str:
    """A minimal edit form: the inputs the client reads, a location input only if location is not None."""
    type_input = "" if type_id is None else f'<input type="hidden" id="extension_type_id" value="{type_id}">'
    location_input = "" if location is None else f'<input id="extension_location" value="{location}">'
    return f'<form>{type_input}<input id="extension_name" value="Tür">{location_input}{extra}</form>'


def _admin_page(text: str) -> Handler:
    """A handler that serves text only with the admin session and the login form otherwise."""

    async def handler(request: web.Request) -> web.Response:
        logged_in = request.cookies.get(SESSION_COOKIE) == SESSION_VALUE
        return web.Response(text=text if logged_in else LOGIN_PAGE, content_type="text/html")

    return handler


# --- the form ------------------------------------------------------------------------------------------------------


def test_the_form_of_a_doorstation_with_an_address_is_read() -> None:
    info = parse_extension_form(14, load_fixture("extension_edit_doorstation.html"))

    assert info == ExtensionInfo(
        ext_id=14, type_id=DOORSTATION_TYPE_ID, name="Haustür", serial="XXXX-XXXX-XXXX", location="192.168.0.50"
    )
    assert info.ip == "192.168.0.50"
    assert info.is_doorstation is True


def test_no_credential_of_the_login_rows_reaches_the_result() -> None:
    html = load_fixture("extension_edit_doorstation.html")
    assert PASSWORD_IN_FIXTURE in html  # the fixture does carry one in its hidden login rows
    parser = _ExtensionFormParser()
    parser.feed(html)

    # The parser keeps exactly the four inputs the library reads; the old_* copies and the login rows are not kept.
    assert parser.inputs == {
        "extension_type_id": "16",
        "extension_name": "Haustür",
        "extension_serial": "XXXX-XXXX-XXXX",
        "extension_location": "192.168.0.50",
    }
    assert PASSWORD_IN_FIXTURE not in repr(parse_extension_form(14, html))


def test_a_doorstation_on_local_has_no_ip_and_no_serial() -> None:
    info = parse_extension_form(14, load_fixture("extension_edit_local.html"))

    assert info == ExtensionInfo(ext_id=14, type_id=DOORSTATION_TYPE_ID, name="vDS1", serial=None, location="local")
    assert info.ip is None
    assert info.is_doorstation is True


def test_an_extension_of_another_type_is_no_doorstation() -> None:
    info = parse_extension_form(3, _form(location="local", type_id="31"))

    assert info.type_id == 31
    assert info.is_doorstation is False


def test_the_form_for_a_new_extension_is_an_unknown_id() -> None:
    # Comexio answers an unknown id with 200 and the form that creates an extension: no type id, empty fields.
    html = load_fixture("extension_edit_new.html")

    with pytest.raises(ComexioDataError) as info:
        parse_extension_form(99, html)

    assert str(info.value) == NEW_EXTENSION_MESSAGE


@pytest.mark.parametrize("type_id", ["", "0", "-1", "16.0", "x", " 16", "١٦", "1" * 19], ids=repr)
def test_a_type_id_that_is_no_positive_int_is_no_extension_and_the_message_shows_it(type_id: str) -> None:
    html = _form(type_id=type_id)

    with pytest.raises(ComexioDataError) as info:
        parse_extension_form(99, html)

    assert str(info.value) == (
        f"Extension 99 edit form names no extension type (found {type_id!r}): unknown id or changed page"
    )


@pytest.mark.parametrize("length", [40, 41, 2_000_000])
def test_the_message_quotes_at_most_the_start_of_a_long_type_id(length: int) -> None:
    html = _form(type_id="x" * length)

    with pytest.raises(ComexioDataError) as info:
        parse_extension_form(99, html)

    shown = "'" + "x" * 40 + "'"
    suffix = "" if length == 40 else f"... ({length} characters)"
    assert str(info.value) == (
        f"Extension 99 edit form names no extension type (found {shown}{suffix}): unknown id or changed page"
    )


def test_a_form_without_a_type_id_input_is_an_unknown_id() -> None:
    html = _form(type_id=None)

    with pytest.raises(ComexioDataError) as info:
        parse_extension_form(99, html)

    assert str(info.value) == NEW_EXTENSION_MESSAGE


def test_the_type_id_input_is_found_by_its_exact_id_only() -> None:
    form = '<form><input type="hidden" id="Extension_Type_Id" value="16"><input id="extension_location"></form>'

    with pytest.raises(ComexioDataError) as info:
        parse_extension_form(99, form)

    assert str(info.value) == NEW_EXTENSION_MESSAGE


def test_a_form_without_the_address_field_is_a_changed_page() -> None:
    html = _form(location=None)

    with pytest.raises(ComexioDataError) as info:
        parse_extension_form(14, html)

    assert str(info.value) == NO_ADDRESS_MESSAGE


def test_a_form_without_type_and_address_reports_the_missing_type_first() -> None:
    html = _form(type_id=None, location=None)

    with pytest.raises(ComexioDataError) as info:
        parse_extension_form(99, html)

    assert str(info.value) == NEW_EXTENSION_MESSAGE


def test_a_page_that_is_no_form_is_an_unknown_id() -> None:
    with pytest.raises(ComexioDataError) as info:
        parse_extension_form(99, "")

    assert str(info.value) == NEW_EXTENSION_MESSAGE


def test_an_address_input_without_a_value_reads_as_unknown_not_as_empty() -> None:
    info = parse_extension_form(14, _form(location=None, extra='<input id="extension_location">'))

    assert info.location is None
    assert info.ip is None


def test_the_first_input_of_an_id_counts_and_the_id_is_exact() -> None:
    # The look-alikes come first: a parser that ignored case or took the last input would read 10.0.0.8 or 10.0.0.9.
    form = (
        '<form><input type="hidden" id="extension_type_id" value="16">'
        '<input id="Extension_Location" value="10.0.0.8"><input id="old_extension_location" value="10.0.0.7">'
        '<input id="extension_location" value="192.168.0.50"><input id="extension_location" value="10.0.0.9"></form>'
    )

    assert parse_extension_form(14, form).location == "192.168.0.50"


def test_a_first_input_without_a_value_is_not_replaced_by_a_later_one() -> None:
    extra = '<input id="extension_location"><input id="extension_location" value="10.0.0.9">'

    info = parse_extension_form(14, _form(location=None, extra=extra))

    assert info.location is None


@pytest.mark.parametrize("tag", ["div", "span", "label"])
def test_a_first_element_of_an_id_that_is_no_input_hides_a_later_input(tag: str) -> None:
    # getElementById finds the <tag>, so the input after it is not the element of that id: the field is missing.
    # The <tag> carries a value too: a parser that took the value of any first element would read 192.168.0.50 / 16.
    shadowed_address = f'<{tag} id="extension_location" value="192.168.0.50"></{tag}>' + _form(location="10.0.0.9")
    shadowed_type = f'<{tag} id="extension_type_id" value="16"></{tag}>' + _form(type_id="16")

    with pytest.raises(ComexioDataError) as no_address:
        parse_extension_form(14, shadowed_address)
    with pytest.raises(ComexioDataError) as no_type:
        parse_extension_form(99, shadowed_type)

    assert str(no_address.value) == NO_ADDRESS_MESSAGE
    assert str(no_type.value) == NEW_EXTENSION_MESSAGE


@pytest.mark.parametrize("field", ["name", "serial"])
def test_a_first_element_of_the_name_or_serial_id_that_is_no_input_hides_a_later_input(field: str) -> None:
    shadowed = f'<div id="extension_{field}" value="Alt"></div><input id="extension_{field}" value="Neu">'
    base = '<input type="hidden" id="extension_type_id" value="16"><input id="extension_location">'
    html = f"<form>{base}{shadowed}</form>"

    info = parse_extension_form(14, html)

    assert getattr(info, field) is None


def test_a_later_element_of_an_id_does_not_replace_the_input_before_it() -> None:
    form = _form(location="192.168.0.50", extra='<div id="extension_location" value="10.0.0.9"></div>')

    assert parse_extension_form(14, form).location == "192.168.0.50"


@pytest.mark.parametrize("wrapper", ["textarea", "template", "script"])
def test_an_input_inside_text_or_a_template_is_no_field(wrapper: str) -> None:
    hidden = f'<{wrapper}><input id="extension_location" value="10.0.0.9"></{wrapper}>'

    info = parse_extension_form(14, f"{hidden}{_form(location='192.168.0.50')}")

    assert info.location == "192.168.0.50"


@pytest.mark.parametrize(
    ("inputs", "name", "serial"),
    [
        ({"extension_name": "", "extension_serial": ""}, None, None),
        ({"extension_name": "  ", "extension_serial": "\t"}, None, None),
        ({"extension_name": None, "extension_serial": None}, None, None),
        ({}, None, None),
        ({"extension_serial": "S1"}, None, "S1"),
        ({"extension_name": " Tür ", "extension_serial": " S1 "}, "Tür", "S1"),
    ],
    ids=["empty", "blank", "no-value", "no-inputs", "serial-only", "padded"],
)
def test_a_name_or_serial_that_is_blank_or_missing_is_none(
    inputs: dict[str, str | None], name: str | None, serial: str | None
) -> None:
    form_inputs = {"extension_type_id": "16", "extension_location": "local", **inputs}

    assert extension_from_inputs(5, form_inputs) == ExtensionInfo(
        ext_id=5, type_id=16, name=name, serial=serial, location="local"
    )


def test_a_page_that_nests_too_deeply_is_refused() -> None:
    html = "<div>" * 300 + _form()

    with pytest.raises(ComexioDataError, match=r"^form page nests more than 256 elements$"):
        parse_extension_form(14, html)


# --- the address ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("location", "ip"),
    [
        ("192.168.0.50", "192.168.0.50"),
        ("  192.168.0.50\n", "192.168.0.50"),
        ("10.0.0.1", "10.0.0.1"),
        ("0.0.0.0", None),
        ("0.0.0.1", None),
        ("0.128.0.0", None),
        ("0.255.255.255", None),
        ("1.0.0.0", "1.0.0.0"),
        ("127.0.0.1", None),
        ("223.255.255.255", "223.255.255.255"),
        ("224.0.0.0", None),
        ("224.0.0.1", None),
        ("239.255.255.255", None),
        ("240.0.0.0", None),
        ("240.0.0.1", None),
        ("255.255.255.254", None),
        ("255.255.255.255", None),
        ("169.254.1.1", "169.254.1.1"),
        ("local", None),
        ("LOCAL", None),
        ("", None),
        ("   ", None),
        (None, None),
        ("192.168.0", None),
        ("192.168.0.256", None),
        ("192.168.0.050", None),
        ("192.168.0.50.1", None),
        ("192.168.0.50:80", None),
        ("http://192.168.0.50", None),
        ("doorstation.local", None),
        ("fe80::1", None),
        ("１９２.１６８.０.５０", None),
        ("192.168.0.50\x00", None),
    ],
    ids=repr,
)
def test_only_an_ipv4_address_is_an_ip(location: str | None, ip: str | None) -> None:
    assert extension_ip(location) == ip


@pytest.mark.parametrize("location", ["0.0.0.1", "239.255.255.255", "240.0.0.1", "local"])
def test_an_unusable_address_leaves_the_raw_location_on_the_extension(location: str) -> None:
    info = extension_from_inputs(7, {"extension_type_id": "16", "extension_location": location})

    assert info.ip is None
    assert info.location == location


# --- the client ----------------------------------------------------------------------------------------------------


async def test_get_extension_reads_the_edit_form_with_json_return(
    logged_in: ComexioClient, comexio: FakeComexio
) -> None:
    comexio.serve("GET", EDIT_PATH, _admin_page(load_fixture("extension_edit_doorstation.html")))

    info = await logged_in.get_extension(7)

    assert info == ExtensionInfo(
        ext_id=7, type_id=DOORSTATION_TYPE_ID, name="Haustür", serial="XXXX-XXXX-XXXX", location="192.168.0.50"
    )
    (request,) = comexio.received_at("GET", EDIT_PATH)
    assert request.query == {"id": "7", "json_return": "true"}


async def test_get_extension_sends_the_largest_id_the_library_accepts(
    logged_in: ComexioClient, comexio: FakeComexio
) -> None:
    comexio.serve("GET", EDIT_PATH, _admin_page(load_fixture("extension_edit_local.html")))

    info = await logged_in.get_extension(10**18 - 1)

    assert info.ext_id == 10**18 - 1
    (request,) = comexio.received_at("GET", EDIT_PATH)
    assert request.query == {"id": "999999999999999999", "json_return": "true"}


async def test_get_extension_of_an_unknown_id_raises_data_error(logged_in: ComexioClient, comexio: FakeComexio) -> None:
    comexio.serve("GET", EDIT_PATH, _admin_page(load_fixture("extension_edit_new.html")))

    with pytest.raises(ComexioDataError) as info:
        await logged_in.get_extension(99)

    assert str(info.value) == NEW_EXTENSION_MESSAGE


async def test_get_extension_needs_the_admin_session(client: ComexioClient, comexio: FakeComexio) -> None:
    comexio.serve("GET", EDIT_PATH, _admin_page(load_fixture("extension_edit_doorstation.html")))

    with pytest.raises(ComexioAuthenticationError) as info:
        await client.get_extension(14)

    assert str(info.value) == "Comexio served the login form, the session is not logged in (Extension 14 edit form)"


@pytest.mark.parametrize(
    ("ext_id", "error", "message"),
    [
        (0, ValueError, "ext_id must be a positive Comexio id"),
        (-1, ValueError, "ext_id must be a positive Comexio id"),
        (10**18, ValueError, "ext_id must be a positive Comexio id"),
        (10**30, ValueError, "ext_id must be a positive Comexio id"),
        (10**5000, ValueError, "ext_id must be a positive Comexio id"),
        (True, TypeError, "ext_id must be an int, not bool"),
        ("14", TypeError, "ext_id must be an int, not str"),
        (1.0, TypeError, "ext_id must be an int, not float"),
    ],
    ids=["zero", "negative", "limit", "huge", "beyond-str-limit", "bool", "str", "float"],
)
async def test_get_extension_refuses_an_invalid_id_before_any_request(
    logged_in: ComexioClient, comexio: FakeComexio, ext_id: Any, error: type[Exception], message: str
) -> None:
    requests_before = len(comexio.requests)

    with pytest.raises(error) as info:
        await logged_in.get_extension(ext_id)

    assert str(info.value) == message
    assert len(comexio.requests) == requests_before
