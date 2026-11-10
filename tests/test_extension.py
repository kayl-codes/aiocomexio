"""Extension edit form: name, serial, type and IP address of an extension (get_extension)."""

from typing import Any

import pytest
from aiohttp import web

from aiocomexio import ComexioAuthenticationError, ComexioClient, ComexioDataError, ExtensionInfo
from aiocomexio.client import _FRAMESET_BLOCKERS, _ExtensionFormParser, parse_extension_form
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


@pytest.mark.parametrize(
    "shadow",
    [
        "<script id='{}'></script>",
        "<template id='{}'></template>",
        "<textarea id='{}'></textarea>",
        "<title id='{}'></title>",
        "<svg id='{}'></svg>",
        "<svg id='{}'/>",
        "<svg><circle id='{}'/></svg>",
        "<svg><foreignObject><svg id='{}'></svg></foreignObject></svg>",
        "<math id='{}'></math>",
        "<math id='{}'/>",
        "<math><mi id='{}'>x</mi></math>",
    ],
)
def test_script_template_textarea_svg_and_math_elements_are_elements_of_their_id_too(shadow: str) -> None:
    # getElementById finds them like a <div>: a later input is not the element of the id.
    shadowed_address = shadow.format("extension_location") + _form(location="10.0.0.9")
    shadowed_type = shadow.format("extension_type_id") + _form(type_id="16")

    with pytest.raises(ComexioDataError) as no_address:
        parse_extension_form(14, shadowed_address)
    with pytest.raises(ComexioDataError) as no_type:
        parse_extension_form(99, shadowed_type)

    assert str(no_address.value) == NO_ADDRESS_MESSAGE
    assert str(no_type.value) == NEW_EXTENSION_MESSAGE


@pytest.mark.parametrize("wrapper", ["script", "template", "textarea"])
@pytest.mark.parametrize(
    "inner",
    [
        "<div id='extension_location'></div>",
        "<svg id='extension_location'></svg>",
        "<svg><circle id='extension_location'/></svg>",
        "<math id='extension_location'></math>",
        "<script id='extension_location'></script>",
    ],
)
def test_an_element_inside_script_template_or_textarea_is_no_element_of_the_page(wrapper: str, inner: str) -> None:
    hidden = f"<{wrapper}>{inner}</{wrapper}>"
    html = hidden + _form(location="192.168.0.50")

    assert parse_extension_form(14, html).location == "192.168.0.50"


@pytest.mark.parametrize(
    "ignored",
    [
        *(
            f"<{part} id='{{}}'>"
            for part in ("tr", "td", "th", "caption", "col", "colgroup", "tbody", "thead", "tfoot")
        ),
        "<frame id='{}'>",
        "<br><frameset id='{}'>",
        "<form><form id='{}'>",
    ],
)
def test_a_start_tag_a_browser_ignores_is_no_element_of_its_id(ignored: str) -> None:
    # A table part outside a table, a frame outside a frameset, a frameset in a page and a nested form make no
    # element: the input is the first of its id.
    html = ignored.format("extension_location") + _form(location="192.168.0.50")

    assert parse_extension_form(14, html).location == "192.168.0.50"


@pytest.mark.parametrize(
    "frames",
    [
        "<frameset id='extension_location'>",
        "<html><head></head><frameset id='extension_location'>",
        "<div><frameset id='extension_location'>",
        "<form><frameset id='extension_location'>",
        "<p><frameset id='extension_location'>",
        "<input type='HIDDEN'><frameset id='extension_location'>",
        "<frameset><frame id='extension_location'>",
        "<frameset><frameset id='extension_location'>",
        "<frameset><frameset></frameset><frame id='extension_location'>",
        "<svg><frameset id='extension_location'></frameset></svg>",
    ],
)
def test_a_frame_element_of_a_frameset_page_is_an_element_of_its_id(frames: str) -> None:
    # Until a start tag clears the frameset-ok flag a <frameset> makes the page a frameset page; a <frame> in one is
    # an element too, and so is any SVG element with the id.
    html = f"{frames}{_form(location='192.168.0.50')}"

    with pytest.raises(ComexioDataError) as no_address:
        parse_extension_form(14, html)

    assert str(no_address.value) == NO_ADDRESS_MESSAGE


@pytest.mark.parametrize(
    "frames",
    [
        "<input><frameset id='extension_location'>",
        "<body><frameset id='extension_location'>",
        "<table></table><frameset id='extension_location'>",
        "<frameset></frameset><frameset id='extension_location'>",
        "<frameset></frameset><frame id='extension_location'>",
        "<frameset><frameset></frameset></frameset><frame id='extension_location'>",
        "<input><frameset><frame id='extension_location'>",
        "</frameset><frame id='extension_location'>",
        "<template><frameset></frameset></template><frame id='extension_location'>",
        "<frameset><template><frameset></frameset></template></frameset><frame id='extension_location'>",
        "<template><frameset id='extension_location'></frameset></template>",
        "<template></template><frameset id='extension_location'>",
    ],
)
def test_a_frame_element_a_browser_ignores_is_no_element_of_its_id(frames: str) -> None:
    # An <input> (not a hidden one) or a <body> clears the flag, a frameset that has ended takes no more frames, and
    # an ignored frameset (or a stray end tag, or one in a <template>) opens none.
    html = f"{frames}{_form(location='192.168.0.50')}"

    assert parse_extension_form(14, html).location == "192.168.0.50"


# The start tags the HTML spec lists as clearing the frameset-ok flag ("in body", and <template> through "in head"),
# written out here, not derived from _FRAMESET_BLOCKERS, so a tag missing there fails the tests below. A visible
# <input> clears it too (see the ignored cases above).
_SPEC_FRAMESET_BLOCKERS = frozenset(
    {
        "applet",
        "area",
        "body",
        "br",
        "button",
        "dd",
        "dt",
        "embed",
        "hr",
        "iframe",
        "image",
        "img",
        "keygen",
        "li",
        "listing",
        "marquee",
        "object",
        "plaintext",
        "pre",
        "select",
        "table",
        "template",
        "textarea",
        "wbr",
        "xmp",
    }
)

# These open raw text or a hidden element that swallows the rest of the page, the form included (the closed
# <template></template> case has a test of its own above).
_RAW_TEXT_BLOCKERS = frozenset({"iframe", "plaintext", "template", "textarea", "xmp"})


def test_the_frameset_blockers_are_the_start_tags_of_the_spec() -> None:
    assert _FRAMESET_BLOCKERS == _SPEC_FRAMESET_BLOCKERS


@pytest.mark.parametrize("blocker", sorted(_SPEC_FRAMESET_BLOCKERS - _RAW_TEXT_BLOCKERS))
def test_every_start_tag_that_clears_the_frameset_ok_flag_ignores_a_later_frameset(blocker: str) -> None:
    html = f"<{blocker}><frameset id='extension_location'>{_form(location='192.168.0.50')}"

    assert parse_extension_form(14, html).location == "192.168.0.50"


@pytest.mark.parametrize(
    "text",
    [
        "text",
        "&nbsp;",
        " ",
        "\x0b",
        "<html><head></head>text",
        "<svg>text</svg>",
        "\n text \n",
    ],
)
def test_text_before_a_frameset_clears_the_frameset_ok_flag(text: str) -> None:
    # Text that is more than whitespace (a no-break space is none) puts the page in the body: the <frameset> is ignored.
    html = f"{text}<frameset id='extension_location'>{_form(location='192.168.0.50')}"

    assert parse_extension_form(14, html).location == "192.168.0.50"


@pytest.mark.parametrize(
    "lead",
    [
        " \t\r\n\f",
        "&#32;&#10;",
        "<!-- note -->",
        "<style>body { color: red }</style>",
        "<script>var text = 1;</script>",
        "<title>text</title>",
        "<noscript>text</noscript>",  # raw text, as in a browser that runs scripts
        "\x00",
        "\x00 \x00",
    ],
)
def test_whitespace_and_the_text_of_style_script_or_title_leave_a_frameset_an_element(lead: str) -> None:
    # A NUL is dropped by the parser, so a page of nothing but NULs and whitespace still has its frameset.
    html = f"{lead}<frameset id='extension_location'>{_form(location='192.168.0.50')}"

    with pytest.raises(ComexioDataError) as no_address:
        parse_extension_form(14, html)

    assert str(no_address.value) == NO_ADDRESS_MESSAGE


@pytest.mark.parametrize("part", ["tbody", "thead", "colgroup", "caption", "tr"])
def test_a_table_part_inside_a_table_is_an_element_of_its_id(part: str) -> None:
    html = f"<table><{part} id='extension_location'></{part}></table>" + _form(location="192.168.0.50")

    with pytest.raises(ComexioDataError) as no_address:
        parse_extension_form(14, html)

    assert str(no_address.value) == NO_ADDRESS_MESSAGE


def test_a_start_tag_a_browser_ignores_does_not_claim_the_type_id_either() -> None:
    html = f"<tr id='extension_type_id'/><frame id='extension_type_id'/>{_form(type_id='16')}"

    assert parse_extension_form(14, html).type_id == DOORSTATION_TYPE_ID


@pytest.mark.parametrize("part", ["tr", "td", "th", "caption", "col", "colgroup"])
def test_a_table_part_after_the_end_of_its_table_is_ignored_again(part: str) -> None:
    html = f"<table></table><{part} id='extension_location'>{_form(location='192.168.0.50')}"

    assert parse_extension_form(14, html).location == "192.168.0.50"


@pytest.mark.parametrize(
    "table",
    [
        "<table><tr><td id='extension_location'></td></tr></table>",
        "<table><tr><th id='extension_location'></th></tr></table>",
        "<table><colgroup><col id='extension_location'></colgroup></table>",
    ],
)
def test_a_table_part_nested_in_a_table_is_an_element_of_its_id(table: str) -> None:
    html = f"{table}{_form(location='192.168.0.50')}"

    with pytest.raises(ComexioDataError) as no_address:
        parse_extension_form(14, html)

    assert str(no_address.value) == NO_ADDRESS_MESSAGE


def test_a_form_after_the_end_of_another_one_is_an_element_of_its_id() -> None:
    shadowed = f"<form></form><form id='extension_location'>{_form(location='192.168.0.50')}"

    with pytest.raises(ComexioDataError) as no_address:
        parse_extension_form(14, shadowed)

    assert str(no_address.value) == NO_ADDRESS_MESSAGE


@pytest.mark.parametrize(
    "root",
    [
        "<html id='extension_location'>",
        "<body id='extension_location'>",
        "<head id='extension_location'>",
        "<html><html id='extension_location'>",
        "<body><body id='extension_location'>",
        "<html id=''><body id='extension_location'>",
        "<html id='root'><body id='extension_location'>",
        "<html id='root'><head id='extension_location'>",
        "<body id='root'><html id='extension_location'>",
    ],
)
def test_a_root_tag_that_gives_its_element_the_id_is_an_element_of_it(root: str) -> None:
    # The first html/head/body makes its element; a repeated html or body adds the id its element lacks.
    html = f"{root}{_form(location='192.168.0.50')}"

    with pytest.raises(ComexioDataError) as no_address:
        parse_extension_form(14, html)

    assert str(no_address.value) == NO_ADDRESS_MESSAGE


@pytest.mark.parametrize(
    "root",
    [
        "<html id='root'><html id='extension_location'>",
        "<body id='root'><body id='extension_location'>",
        "<head><head id='extension_location'>",
        "<head id='root'><head id='extension_location'>",
        "<html id=''><html id='extension_location'>",
        "<html><html id='a'><html id='extension_location'>",
        "<body><body id='a'><body id='extension_location'>",
    ],
)
def test_a_repeated_root_tag_that_adds_no_id_to_its_element_is_no_element_of_it(root: str) -> None:
    # The element already has an id (html, body) or the repeated tag is ignored altogether (head).
    html = f"{root}{_form(location='192.168.0.50')}"

    assert parse_extension_form(14, html).location == "192.168.0.50"


@pytest.mark.parametrize(
    "root",
    [
        "<body><head id='extension_location'>",
        "<body id='root'><head id='extension_location'>",
        "<html><body><head id='extension_location'>",
        "<div><head id='extension_location'>",
        "<meta><head id='extension_location'>",
        "<html><meta><head id='extension_location'>",
        "<title>t</title><head id='extension_location'>",
        "<script></script><head id='extension_location'>",
        "<svg></svg><head id='extension_location'>",
        "</head><head id='extension_location'>",
        "</body><head id='extension_location'>",
        "</html><head id='extension_location'>",
        "</br><head id='extension_location'>",
        "text<head id='extension_location'>",
        "&nbsp;<head id='extension_location'>",
        "<html>text<head id='extension_location'>",
    ],
)
def test_a_head_tag_after_another_start_tag_is_no_element_of_its_id(root: str) -> None:
    # A start tag but html/head, an end tag of html/head/body/br or text that is more than whitespace implies the
    # head: a later <head> is ignored.
    html = f"{root}{_form(location='192.168.0.50')}"

    assert parse_extension_form(14, html).location == "192.168.0.50"


@pytest.mark.parametrize(
    "root",
    [
        "<html><head id='extension_location'>",
        "<!-- note --><head id='extension_location'>",
        " <html> <head id='extension_location'>",
        "\t\r\n\f <head id='extension_location'>",
        "&#32;<head id='extension_location'>",
    ],
)
def test_a_head_tag_before_any_other_start_tag_is_an_element_of_its_id(root: str) -> None:
    html = f"{root}{_form(location='192.168.0.50')}"

    with pytest.raises(ComexioDataError) as no_address:
        parse_extension_form(14, html)

    assert str(no_address.value) == NO_ADDRESS_MESSAGE


def test_a_root_tag_with_the_type_id_hides_a_later_type_input_only() -> None:
    html = f"<html><html id='extension_type_id'>{_form(location='192.168.0.50', type_id='16')}"

    with pytest.raises(ComexioDataError) as no_type:
        parse_extension_form(99, html)

    assert str(no_type.value) == NEW_EXTENSION_MESSAGE


def test_only_the_ids_the_client_reads_are_remembered() -> None:
    # Any other id of a large page must not pile up in the parser (it keeps one entry per id it has to remember).
    unrelated = "".join(f"<div id='d{n}'></div><svg id='s{n}'></svg><textarea id='t{n}'></textarea>" for n in range(50))
    parser = _ExtensionFormParser()

    parser.feed(f"{unrelated}<script id='extension_name'></script>{_form()}")
    parser.close()

    assert parser._seen == {"extension_name", "extension_type_id", "extension_location"}


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
