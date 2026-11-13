"""API settings page: switch, login and access per data type of /api/ (get_api_settings)."""

import asyncio
import dataclasses
import logging
import re
import traceback

import aiohttp
import pytest
from aiohttp import web

from aiocomexio import (
    ACCESS_FORBIDDEN,
    ACCESS_READ,
    ACCESS_READ_WRITE,
    ACCESS_SELECTION,
    WRITE_TARGET_IO,
    WRITE_TARGET_KNX,
    WRITE_TARGET_MARKER,
    ApiSettings,
    ComexioAuthenticationError,
    ComexioClient,
    ComexioConnectionError,
    ComexioDataError,
    ComexioError,
    ComexioInsecureTransportError,
    ComexioResponseError,
    session_kwargs,
)
from aiocomexio.api_settings import ApiPageFields
from aiocomexio.client import _ApiFormParser, _clear_frames, parse_api_settings
from tests.common import load_fixture
from tests.fake_comexio import LOGIN_PAGE, PASSWORD, SESSION_COOKIE, SESSION_VALUE, USERNAME, FakeComexio, Handler

API_PATH = "/admin/api/home"
FIXTURE = load_fixture("api_settings_page.html")
# The user and the password the fixture carries in its login rows: synthetic, like everything in the fixture.
PAGE_USER = "api-user"
PAGE_PASSWORD = "s3cret-api-pw"  # nosec B105
SELECTION = "access to markers is single selection"
MARKER_LIST = "{'1':0,'2':0,'3':0,'4':0,'5':0,'6':null,'7':0,'8':null}"


def _select(html: str, select_id: str, value: str) -> str:
    """html with the option of the select select_id that has this value selected, the others not."""
    match = re.search(rf'<select id="{select_id}".*?</select>', html, re.DOTALL)
    assert match is not None
    block = match[0].replace(' selected="selected"', "")
    block = block.replace(f'<option value="{value}"', f'<option value="{value}" selected="selected"')
    return html.replace(match[0], block)


def _inactive(html: str) -> str:
    html = html.replace('name="api_active" checked="checked" value="active"', 'name="api_active" value="active"')
    return html.replace('name="api_active" value="inactive"', 'name="api_active" checked="checked" value="inactive"')


def _markers(html: str, marker_list: str) -> str:
    assert MARKER_LIST in html
    return html.replace(MARKER_LIST, marker_list)


def _settings(html: str = FIXTURE, user: str = PAGE_USER, password: str = PAGE_PASSWORD) -> ApiSettings:
    return parse_api_settings(html, api_username=user, api_password=password)


# --- the page ------------------------------------------------------------------------------------------------------


def test_the_page_of_an_active_api_is_read() -> None:
    settings = _settings()

    assert settings == ApiSettings(
        active=True,
        tcp_enabled=True,
        tcp_port=1574,
        login_required=True,
        username=PAGE_USER,
        username_matches=True,
        password_matches=True,
        in_output=ACCESS_READ_WRITE,
        flag=ACCESS_READ_WRITE,
        one_wire=ACCESS_READ,
        marker_rights={1: 0, 2: 0, 3: 0, 4: 0, 5: 0, 7: 0},
    )


def test_the_password_of_the_page_is_not_kept() -> None:
    settings = _settings()

    assert PAGE_PASSWORD in FIXTURE  # the page does carry it in clear text
    assert PAGE_PASSWORD not in repr(settings)
    assert PAGE_PASSWORD not in repr(dataclasses.asdict(settings))
    assert PAGE_PASSWORD not in settings.summary()


def test_the_password_the_client_was_not_created_with_does_not_match() -> None:
    settings = _settings(password="another-password")  # nosec B106

    assert settings.password_matches is False
    assert settings.username_matches is True
    assert settings.write_blockers(WRITE_TARGET_MARKER, 1) == (
        "the API requires a login and its password differs from the configured API password",
    )


def test_a_user_that_differs_is_named_before_the_password() -> None:
    settings = _settings(user="someone", password="another-password")  # nosec B106

    assert settings.username_matches is False
    assert settings.write_blockers(WRITE_TARGET_IO) == (
        "the API requires a login and its user differs from the configured API user",
    )


def test_no_configured_credentials_do_not_match_a_login() -> None:
    settings = _settings(user="", password="")  # nosec B106

    assert (settings.username_matches, settings.password_matches) == (False, False)


def test_a_comparison_is_exact() -> None:
    assert _settings(user=PAGE_USER.upper()).username_matches is False
    assert _settings(password=f"{PAGE_PASSWORD} ").password_matches is False


def test_without_a_login_the_credentials_have_nothing_to_differ_from() -> None:
    html = FIXTURE.replace(' id="use_login" name="use_login" checked="checked"', ' id="use_login" name="use_login"')

    settings = _settings(html, user="", password="")  # nosec B106

    assert settings.login_required is False
    assert (settings.username_matches, settings.password_matches) == (True, True)
    assert settings.write_blockers(WRITE_TARGET_MARKER, 1) == ()


def test_an_inactive_api_is_read_as_inactive_and_blocks_every_write() -> None:
    settings = _settings(_inactive(FIXTURE))

    assert settings.active is False
    for target in (WRITE_TARGET_MARKER, WRITE_TARGET_IO, WRITE_TARGET_KNX):
        assert settings.write_blockers(target, 251) == ("the API is switched off (access: inactive)",)


def test_the_tcp_interface_is_read_whatever_the_api_says() -> None:
    html = FIXTURE.replace(' id="use_tcp" name="use_tcp" checked="checked"', ' id="use_tcp" name="use_tcp"')
    html = html.replace('id="tcp_port" name="tcp_port" value="1574"', 'id="tcp_port" name="tcp_port" value="2000"')

    settings = _settings(html)

    assert (settings.tcp_enabled, settings.tcp_port) == (False, 2000)
    assert settings.write_blockers(WRITE_TARGET_MARKER, 1) == ()


@pytest.mark.parametrize(
    "port",
    [
        "",
        "0",
        "65536",
        "-1",
        "15 74",
        "abc",
        "1574.0",
        "٣٠٠٠",
        "9" * 40,
        "0" * 40,
        "065536",
        "0065536",
        " 1574",
        "0" * 5000 + "0",
    ],
)
def test_a_port_that_is_no_port_is_none(port: str) -> None:
    html = FIXTURE.replace('name="tcp_port" value="1574"', f'name="tcp_port" value="{port}"')

    assert _settings(html).tcp_port is None


def test_a_missing_port_field_is_none() -> None:
    html = FIXTURE.replace('id="tcp_port"', 'id="tcp_port_other"')

    assert _settings(html).tcp_port is None


# --- what stops a write --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("select_id", "target", "value", "blockers"),
    [
        ("flag", WRITE_TARGET_MARKER, "3", ()),
        ("flag", WRITE_TARGET_MARKER, "1", ("access to markers is read only",)),
        ("flag", WRITE_TARGET_MARKER, "0", ("access to markers is forbidden",)),
        ("in_output", WRITE_TARGET_IO, "3", ()),
        ("in_output", WRITE_TARGET_IO, "1", ("access to IOs is read only",)),
        ("in_output", WRITE_TARGET_IO, "0", ("access to IOs is forbidden",)),
        # The other data types do not matter for a write to this one.
        ("in_output", WRITE_TARGET_MARKER, "0", ()),
        ("flag", WRITE_TARGET_IO, "0", ()),
        ("one_wire", WRITE_TARGET_MARKER, "0", ()),
        ("one_wire", WRITE_TARGET_IO, "0", ()),
        # The page has no access setting for KNX objects.
        ("flag", WRITE_TARGET_KNX, "0", ()),
        ("in_output", WRITE_TARGET_KNX, "0", ()),
    ],
)
def test_the_access_of_the_data_type_decides(
    select_id: str, target: str, value: str, blockers: tuple[str, ...]
) -> None:
    settings = _settings(_select(FIXTURE, select_id, value))

    assert settings.write_blockers(target, 251) == blockers


def test_an_inactive_api_and_a_read_only_data_type_are_both_named() -> None:
    settings = _settings(_inactive(_select(FIXTURE, "flag", "1")))

    assert settings.write_blockers(WRITE_TARGET_MARKER, 251) == (
        "the API is switched off (access: inactive)",
        "access to markers is read only",
    )


@pytest.mark.parametrize(
    ("marker_list", "marker_id", "blockers"),
    [
        ("{'251':3}", 251, ()),
        ("{'251':2}", 251, ()),
        ("{'251':1}", 251, (f"{SELECTION} and M251 is not released for writing",)),
        ("{'251':0}", 251, (f"{SELECTION} and M251 is not released for writing",)),
        ("{'250':3}", 251, (f"{SELECTION} and M251 is not released for writing",)),
        ("{'251':null}", 251, (f"{SELECTION} and M251 is not released for writing",)),
        ("{'251':3}", None, (f"{SELECTION} and the marker is not released for writing",)),
        (
            "not a list",
            251,
            ("access to markers is single selection and its list could not be read (M251 is unknown)",),
        ),
    ],
)
def test_a_single_selection_of_markers_asks_the_list(
    marker_list: str, marker_id: int | None, blockers: tuple[str, ...]
) -> None:
    html = _markers(_select(FIXTURE, "flag", "4"), marker_list)

    assert _settings(html).write_blockers(WRITE_TARGET_MARKER, marker_id) == blockers


def test_the_single_selection_list_is_not_asked_for_an_io() -> None:
    html = _markers(_select(FIXTURE, "flag", "4"), "{'251':0}")

    assert _settings(html).write_blockers(WRITE_TARGET_IO, 251) == ()


def test_a_page_whose_list_is_missing_has_no_marker_rights() -> None:
    html = FIXTURE.replace('id="api_special_2"', 'id="api_special_3"')

    assert _settings(html).marker_rights is None


# --- the marker list -----------------------------------------------------------------------------------------------


def test_the_marker_list_is_read_as_the_script_of_the_page_reads_it() -> None:
    html = _markers(FIXTURE, "{'1':3,'2':1,'3':2,'4':0,'5':null,'251':3}")

    assert _settings(html).marker_rights == {1: 3, 2: 1, 3: 2, 4: 0, 251: 3}


@pytest.mark.parametrize(
    "marker_list",
    [
        "",
        "[1,2,3]",
        "{'a':1}",
        "{'-1':1}",
        "{'1':4}",
        "{'1':-1}",
        "{'1':true}",
        "{'1':'3'}",
        "{'1':1.5}",
        "{'1':[]}",
        "{'" + "9" * 12 + "':1}",
        "{'1':" + "9" * 5000 + "}",
        "{'1':3,",
        "{'٣':1}",
        "{'1':3,'a':1}",
        "{'1':3,'2':4}",
        "{'1':3,'" + "9" * 12 + "':1}",
        "{'7':0,'07':2}",
        "{'01':3}",
        pytest.param("[" * 100000, id="nested-too-deep"),
    ],
)
@pytest.mark.usefixtures("garbage_collected")
def test_a_malformed_marker_list_has_no_rights(marker_list: str, caplog: pytest.LogCaptureFixture) -> None:
    html = _markers(FIXTURE, marker_list.replace('"', "&quot;"))

    with caplog.at_level(logging.DEBUG, logger="aiocomexio.api_settings"):
        assert _settings(html).marker_rights is None
    # One bad entry makes the whole list untrustworthy, and says so; the entry itself is not quoted.
    assert any("marker list" in record.message for record in caplog.records)
    assert all("9" * 12 not in record.message and "07" not in record.message for record in caplog.records)


def test_neither_the_password_nor_the_user_of_the_page_reaches_the_debug_log(caplog: pytest.LogCaptureFixture) -> None:
    html = _markers(FIXTURE, "{'1':3,'a':1}")

    with caplog.at_level(logging.DEBUG):
        settings = _settings(html, password="other-pw")
        repr_text = repr(settings)

    logged = caplog.text + repr_text + settings.summary()
    for secret in (PAGE_PASSWORD, "other-pw", PAGE_USER):
        assert secret not in logged


def test_an_empty_marker_list_releases_no_marker() -> None:
    html = _select(_markers(FIXTURE, "{}"), "flag", "4")

    settings = _settings(html)

    assert settings.marker_rights == {}
    assert settings.write_blockers(WRITE_TARGET_MARKER, 251) == (
        "access to markers is single selection and M251 is not released for writing",
    )


def test_an_unknown_write_target_is_an_error_not_a_free_pass() -> None:
    settings = _settings()

    with pytest.raises(ValueError, match="unknown write target"):
        settings.write_blockers("Marker")


def test_a_knx_object_is_stopped_by_the_login_only() -> None:
    settings = _settings(password="another-password")  # nosec B106

    assert settings.write_blockers(WRITE_TARGET_KNX) == (
        "the API requires a login and its password differs from the configured API password",
    )


def test_an_inactive_api_and_a_wrong_password_are_named_in_this_order() -> None:
    settings = _settings(_inactive(FIXTURE), password="another-password")  # nosec B106

    assert settings.write_blockers(WRITE_TARGET_MARKER, 1) == (
        "the API is switched off (access: inactive)",
        "the API requires a login and its password differs from the configured API password",
    )


@pytest.mark.parametrize(
    ("port", "expected"), [("1", 1), ("65535", 65535), ("01574", 1574), ("00001574", 1574), ("0" * 5000 + "1574", 1574)]
)
def test_a_port_at_the_edge_of_the_range_is_read(port: str, expected: int) -> None:
    html = FIXTURE.replace('name="tcp_port" value="1574"', f'name="tcp_port" value="{port}"')

    assert _settings(html).tcp_port == expected


def test_the_summary_says_when_the_port_is_unreadable() -> None:
    html = FIXTURE.replace('name="tcp_port" value="1574"', 'name="tcp_port" value=""')

    assert "TCP interface on, port unreadable" in _settings(html).summary()


def test_the_first_select_of_an_id_counts() -> None:
    html = FIXTURE.replace(
        "</form>", '<select id="flag"><option value="0" selected="selected">x</option></select></form>'
    )

    assert _settings(html).flag == ACCESS_READ_WRITE


def test_a_form_nested_in_the_api_form_does_not_end_it() -> None:
    # A browser ignores the start tag of a form inside a form, so the fields after it still belong to the API form.
    html = FIXTURE.replace('<form id="apiform"', '<form id="apiform"', 1)
    start = html.index('<form id="apiform"')
    end = html.index(">", start) + 1
    html = f'{html[:end]}<form id="inner">{html[end:]}'

    assert _settings(html) == _settings()


def test_a_field_outside_the_api_form_is_ignored() -> None:
    decoy = '<input type="checkbox" id="use_login"><input id="username" value="decoy"><input id="password" value="x">'
    html = FIXTURE.replace('<form id="apiform"', f'{decoy}</form><form id="apiform"', 1)

    settings = _settings(html)

    assert settings.username == PAGE_USER
    assert settings.login_required is True


def test_a_field_after_the_api_form_is_ignored() -> None:
    html = FIXTURE.replace(
        "</form>", '</form><select id="flag"><option value="0" selected="selected">x</option></select>'
    )

    assert _settings(html).flag == ACCESS_READ_WRITE


def test_a_page_with_another_form_is_not_the_api_page() -> None:
    html = FIXTURE.replace('<form id="apiform"', '<form id="other"')

    with pytest.raises(ComexioDataError):
        _settings(html)


@pytest.mark.parametrize("input_id", ["use_login", "use_tcp"])
def test_a_switch_that_is_no_checkbox_is_a_changed_page(input_id: str) -> None:
    html = FIXTURE.replace(f'<input type="checkbox" id="{input_id}"', f'<input type="hidden" id="{input_id}"')

    with pytest.raises(ComexioDataError, match=input_id):
        _settings(html)


@pytest.mark.parametrize("select_id", ["in_output", "one_wire"])
def test_the_single_selection_exists_for_the_markers_only(select_id: str) -> None:
    # The fixture offers the option 4 for the markers only: add it to the select under test.
    html = re.sub(
        rf'(<select id="{select_id}".*?)</select>', r'\1<option value="4">x</option></select>', FIXTURE, count=1
    )
    html = _select(html, select_id, "4")

    with pytest.raises(ComexioDataError, match=select_id):
        _settings(html)


def test_bare_checked_and_selected_attributes_read_as_the_page_does() -> None:
    html = FIXTURE.replace('checked="checked"', "checked").replace('selected="selected"', "selected")

    assert _settings(html) == _settings()


def test_two_checked_choices_of_the_switch_read_as_a_browser_shows_them() -> None:
    html = FIXTURE.replace('name="api_active" value="inactive"', 'name="api_active" checked="checked" value="inactive"')

    assert _settings(html).active is False


@pytest.mark.parametrize(
    ("page", "configured", "matches"),
    [
        ("pässwörd", "pässwörd", True),
        ("pässwörd", "passwörd", False),
        ("p&amp;ss&quot;w", 'p&ss"w', True),
        (PAGE_PASSWORD, chr(0xD800), False),
    ],
)
def test_the_password_is_compared_whatever_its_characters(page: str, configured: str, matches: bool) -> None:
    html = FIXTURE.replace(f'value="{PAGE_PASSWORD}"', f'value="{page}"')

    assert _settings(html, password=configured).password_matches is matches  # nosec B106


def test_neither_repr_of_a_result_holds_the_password_or_the_user() -> None:
    settings = _settings(password="another-password")  # nosec B106
    fields = ApiPageFields(inputs={"password": {"value": PAGE_PASSWORD}})

    assert PAGE_USER not in repr(settings)
    assert PAGE_PASSWORD not in repr(fields)
    assert "another-password" not in repr(settings) + settings.summary() + "".join(settings.write_blockers("io"))


def test_a_page_without_use_login_raises_without_the_password_in_the_message() -> None:
    html = FIXTURE.replace('id="use_login"', 'id="use_login_gone"')

    with pytest.raises(ComexioDataError) as info:
        _settings(html)

    assert PAGE_PASSWORD not in str(info.value)


# --- a page that is none ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("html", ["", "<html></html>", "<form></form>", LOGIN_PAGE])
def test_a_page_without_the_form_raises_data_error(html: str) -> None:
    with pytest.raises(ComexioDataError):
        _settings(html)


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        (
            'name="api_active" checked="checked" value="active"',
            'name="api_active" value="active"',
            "API page: the api_active choice is None, expected active or inactive",
        ),
        (
            'name="api_active" checked="checked" value="active"',
            'name="api_active" checked="checked" value="maybe"',
            "API page: the api_active choice is 'maybe', expected active or inactive",
        ),
        (
            'name="api_active" checked="checked" value="active"',
            'name="api_active" checked="checked" value=""',
            "API page: the api_active choice is '', expected active or inactive",
        ),
        (
            'type="radio" name="api_active" checked="checked" value="active"',
            'type="checkbox" name="api_active" checked="checked" value="active"',
            "API page: the api_active choice is None, expected active or inactive",
        ),
        ('id="use_login"', 'id="use_login_x"', "API page has no use_login input: changed page"),
        ('id="use_tcp"', 'id="use_tcp_x"', "API page has no use_tcp input: changed page"),
        ('id="username"', 'id="username_x"', "API page has no username input: changed page"),
        ('id="password"', 'id="password_x"', "API page has no password input: changed page"),
        ('<select id="flag"', '<select id="flag_x"', "API page has no selected option for flag: changed page"),
        (
            '<option value="3" selected="selected" id="in_output_3">',
            '<option value="9" selected="selected" id="in_output_3">',
            "API page: in_output is set to an unknown access '9'",
        ),
    ],
)
def test_a_changed_page_raises_data_error(old: str, new: str, message: str) -> None:
    assert old in FIXTURE
    html = FIXTURE.replace(old, new)

    with pytest.raises(ComexioDataError) as info:
        _settings(html)

    assert str(info.value) == message


def test_an_unknown_api_switch_value_is_capped_in_the_error() -> None:
    long_value = "x" * 1000
    assert 'name="api_active" checked="checked" value="active"' in FIXTURE
    html = FIXTURE.replace(
        'name="api_active" checked="checked" value="active"',
        f'name="api_active" checked="checked" value="{long_value}"',
    )

    with pytest.raises(ComexioDataError) as info:
        _settings(html)

    assert str(info.value) == f"API page: the api_active choice is {long_value[:20]!r}, expected active or inactive"


def test_a_select_without_a_selected_option_shows_its_first_enabled_one() -> None:
    # As a browser shows it: in_output starts with "Verbieten" (0).
    html = FIXTURE.replace(' selected="selected" id="in_output_3"', ' id="in_output_3"')

    assert _settings(html).in_output == ACCESS_FORBIDDEN


def test_a_disabled_first_option_is_skipped_when_none_is_selected() -> None:
    html = FIXTURE.replace(' selected="selected" id="in_output_3"', ' id="in_output_3"')
    html = html.replace('<option value="0" id="in_output_1">', '<option value="0" disabled id="in_output_1">')

    assert _settings(html).in_output == ACCESS_READ


def test_a_selected_disabled_option_still_counts() -> None:
    html = _select(FIXTURE, "in_output", "1").replace(
        '<option value="1" selected="selected"', '<option value="1" disabled selected="selected"'
    )

    assert _settings(html).in_output == ACCESS_READ


def test_an_option_of_a_disabled_optgroup_is_skipped_when_none_is_selected() -> None:
    html = FIXTURE.replace(' selected="selected" id="in_output_3"', ' id="in_output_3"')
    html = html.replace(
        '<option value="0" id="in_output_1">Verbieten</option>',
        '<optgroup disabled><option value="0" id="in_output_1">Verbieten</option></optgroup>',
    )

    assert _settings(html).in_output == ACCESS_READ


def test_the_disabled_optgroup_ends_with_its_end_tag() -> None:
    html = FIXTURE.replace(' selected="selected" id="in_output_3"', ' id="in_output_3"')
    html = html.replace(
        '<option value="0" id="in_output_1">Verbieten</option>',
        '<optgroup disabled><option value="0" id="in_output_1">Verbieten</option>',
    )
    # Without the reset at </optgroup>, the options after it would still be disabled and the page unreadable.
    html = html.replace('<option value="1" id="in_output_2">', '</optgroup><option value="1" id="in_output_2">')

    assert _settings(html).in_output == ACCESS_READ


def test_a_selected_option_of_a_disabled_optgroup_still_counts() -> None:
    html = FIXTURE.replace(
        '<option value="3" selected="selected" id="in_output_3">',
        '<optgroup disabled><option value="3" selected="selected" id="in_output_3">',
    )

    assert _settings(html).in_output == ACCESS_READ_WRITE


def test_a_disabled_optgroup_does_not_reach_the_next_select() -> None:
    # An optgroup the page never closes must not disable the first option of the select after it.
    html = FIXTURE.replace(
        '<option value="0" id="in_output_1">Verbieten</option>',
        '<optgroup disabled><option value="0" id="in_output_1">Verbieten</option>',
    )
    html = html.replace(' selected="selected" id="flag_3"', ' id="flag_3"')

    assert _settings(html).flag == ACCESS_FORBIDDEN


def test_the_type_of_the_api_switch_is_read_without_regard_to_case() -> None:
    html = FIXTURE.replace('type="radio" name="api_active"', 'type="RADIO" name="api_active"')

    assert _settings(html).active is True


def test_a_checked_input_of_another_type_does_not_replace_the_api_switch() -> None:
    html = FIXTURE.replace(
        '<input type="radio" name="api_active" value="inactive"',
        '<input type="checkbox" name="api_active" checked="checked" value="inactive"',
    )

    assert _settings(html).active is True


def test_a_list_box_without_a_selected_option_shows_none() -> None:
    # A drop-down shows its first enabled option, a list box (size 2 or more) shows none.
    html = FIXTURE.replace(' selected="selected" id="in_output_3"', ' id="in_output_3"')
    html = html.replace('<select id="in_output" name="in_output"', '<select size="3" id="in_output" name="in_output"')

    with pytest.raises(ComexioDataError, match="no selected option for in_output"):
        _settings(html)


def test_a_list_box_with_a_selected_option_still_counts() -> None:
    html = FIXTURE.replace(
        '<select id="in_output" name="in_output"', '<select size="3" id="in_output" name="in_output"'
    )

    assert _settings(html).in_output == ACCESS_READ_WRITE


@pytest.mark.parametrize("size", ["1", "01", "0", "", "abc", "-2", " 1 ", "٣", "1px", "-3", "\u00a03", "\u20033"])
def test_a_select_that_is_no_list_box_shows_its_first_enabled_option(size: str) -> None:
    html = FIXTURE.replace(' selected="selected" id="in_output_3"', ' id="in_output_3"')
    html = html.replace(
        '<select id="in_output" name="in_output"', f'<select size="{size}" id="in_output" name="in_output"'
    )

    assert _settings(html).in_output == ACCESS_FORBIDDEN


@pytest.mark.parametrize("size", [" 3 ", "+3", "3px", "2abc", "\t3", "0003", "99999999999999999999"])
def test_a_select_with_a_size_of_two_or_more_is_a_list_box(size: str) -> None:
    # A browser reads the leading digits of the size: "3px" is 3, "+3" is 3.
    html = FIXTURE.replace(' selected="selected" id="in_output_3"', ' id="in_output_3"')
    html = html.replace(
        '<select id="in_output" name="in_output"', f'<select size="{size}" id="in_output" name="in_output"'
    )

    with pytest.raises(ComexioDataError, match="no selected option for in_output"):
        _settings(html)


def test_the_size_attribute_is_read_without_regard_to_the_case_of_its_name() -> None:
    html = FIXTURE.replace(' selected="selected" id="in_output_3"', ' id="in_output_3"')
    html = html.replace('<select id="in_output" name="in_output"', '<select SIZE="3" id="in_output" name="in_output"')

    with pytest.raises(ComexioDataError, match="no selected option for in_output"):
        _settings(html)


def test_a_selected_disabled_option_of_a_list_box_still_counts() -> None:
    html = _select(FIXTURE, "in_output", "1").replace(
        '<option value="1" selected="selected"', '<option value="1" disabled selected="selected"'
    )
    html = html.replace('<select id="in_output" name="in_output"', '<select size="3" id="in_output" name="in_output"')

    assert _settings(html).in_output == ACCESS_READ


def test_a_checked_input_without_a_type_is_no_switch() -> None:
    # An input without a type is a text field for a browser.
    html = FIXTURE.replace(
        'type="radio" name="api_active" checked="checked" value="active"',
        'name="api_active" checked="checked" value="active"',
    )

    with pytest.raises(ComexioDataError, match="the api_active choice is None"):
        _settings(html)


def test_the_type_attribute_of_the_api_switch_is_read_without_regard_to_the_case_of_its_name() -> None:
    html = FIXTURE.replace('type="radio" name="api_active"', 'TYPE="radio" name="api_active"')

    assert _settings(html).active is True


def test_a_multiple_select_has_no_single_value() -> None:
    html = FIXTURE.replace(
        '<select id="in_output" name="in_output"', '<select multiple id="in_output" name="in_output"'
    )

    with pytest.raises(ComexioDataError, match="no selected option for in_output"):
        _settings(html)


def test_a_multiple_select_without_a_selected_option_has_no_single_value() -> None:
    html = FIXTURE.replace(' selected="selected" id="in_output_3"', ' id="in_output_3"')
    html = html.replace('<select id="in_output" name="in_output"', '<select multiple id="in_output" name="in_output"')

    with pytest.raises(ComexioDataError, match="no selected option for in_output"):
        _settings(html)


def test_a_select_without_any_enabled_option_is_a_changed_page() -> None:
    html = FIXTURE.replace(' selected="selected" id="in_output_3"', ' disabled id="in_output_3"')
    html = html.replace('<option value="0" id="in_output_1">', '<option value="0" disabled id="in_output_1">')
    html = html.replace('<option value="1" id="in_output_2">', '<option value="1" disabled id="in_output_2">')

    with pytest.raises(ComexioDataError, match="in_output"):
        _settings(html)


def test_a_page_cut_off_inside_a_select_does_not_count_its_value() -> None:
    # The select of the markers never closes: what it showed so far says nothing about its value.
    cut = FIXTURE.index('<option value="3" selected="selected" id="flag_3">') + len(
        '<option value="3" selected="selected" id="flag_3">'
    )

    with pytest.raises(ComexioDataError, match="flag"):
        _settings(FIXTURE[:cut])


def test_the_marker_right_bits_are_exported_from_the_package_root() -> None:
    import aiocomexio

    assert (aiocomexio.RIGHT_READ, aiocomexio.RIGHT_WRITE) == (1, 2)
    assert {"RIGHT_READ", "RIGHT_WRITE"} <= set(aiocomexio.__all__)


def test_the_marker_rights_cannot_be_changed_through_the_settings() -> None:
    rights = _settings().marker_rights

    assert rights is not None
    with pytest.raises(TypeError):
        rights[1] = 3  # type: ignore[index]
    assert dataclasses.asdict(_settings())["marker_rights"] == dict(rights)
    assert repr(dict(rights)) in repr(rights)


@pytest.mark.parametrize(
    ("user", "password"),
    [
        pytest.param("", "pw", id="empty-user"),
        pytest.param("a:b", "pw", id="colon-in-user"),
        pytest.param("us\u20acr", "pw", id="user-beyond-latin1"),
        pytest.param("user", "p\u20acw", id="password-beyond-latin1"),
    ],
)
def test_configured_credentials_the_client_cannot_send_are_a_login_blocker(user: str, password: str) -> None:
    # The page asks for the same credentials, but the client sends no Authorization header for them.
    html = FIXTURE.replace('value="api-user"', f'value="{user}"').replace(PAGE_PASSWORD, password)

    settings = _settings(html, user=user, password=password)

    assert settings.username_matches is True
    assert settings.password_matches is True
    assert settings.credentials_usable is False
    assert settings.write_blockers(WRITE_TARGET_MARKER, 1) == (
        "the API requires a login and the configured API credentials cannot be sent "
        "(empty user, ':' in the user, or characters outside ISO-8859-1)",
    )
    assert "configured credentials cannot be sent" in settings.summary()
    assert not user or user not in settings.summary()


def test_credentials_the_client_cannot_send_are_no_blocker_without_a_login() -> None:
    html = FIXTURE.replace(' id="use_login" name="use_login" checked="checked"', ' id="use_login" name="use_login"')

    settings = _settings(html, user="", password="")

    assert settings.credentials_usable is False
    assert settings.write_blockers(WRITE_TARGET_MARKER, 1) == ()


@pytest.mark.parametrize(
    ("user", "password"),
    [
        pytest.param("sentinel:user", "pw", id="colon-in-user"),
        pytest.param("sentinel-us\u20acr", "pw", id="user-beyond-latin1"),
        pytest.param("sentinel-user", "p\u20acw", id="password-beyond-latin1"),
    ],
)
def test_credentials_the_client_refuses_to_send_are_a_blocker_without_a_login_too(user: str, password: str) -> None:
    # _api_set builds the header for every non-empty user, so such a write fails before the request, login or not.
    html = FIXTURE.replace(' id="use_login" name="use_login" checked="checked"', ' id="use_login" name="use_login"')

    settings = _settings(html, user=user, password=password)

    assert settings.login_required is False
    assert settings.credentials_refused is True
    assert settings.write_blockers(WRITE_TARGET_MARKER, 1) == (
        "the configured API credentials cannot be sent (':' in the user, or characters outside ISO-8859-1), "
        "so the client refuses every /api/ write",
    )
    assert "no login, configured credentials cannot be sent" in settings.summary()
    assert user not in settings.summary()


@pytest.mark.parametrize(
    ("user", "password"),
    [
        pytest.param("api-user", PAGE_PASSWORD, id="plain"),
        pytest.param("api-user", "pa:ss", id="colon-in-password"),
        pytest.param("\u00fc-user", "pw", id="latin1-user"),
    ],
)
def test_sendable_credentials_are_not_refused(user: str, password: str) -> None:
    html = FIXTURE.replace(' id="use_login" name="use_login" checked="checked"', ' id="use_login" name="use_login"')

    settings = _settings(html, user=user, password=password)

    assert settings.credentials_refused is False
    assert settings.write_blockers(WRITE_TARGET_MARKER, 1) == ()


def test_an_api_form_nested_in_another_form_does_not_hide_the_real_one() -> None:
    # A browser ignores a form start tag inside a form, so the decoy is no API form and must not use up "the first".
    decoy = (
        '<form id="outer"><form id="apiform"><input type="radio" name="api_active" checked="checked" value="inactive">'
        '<input type="text" id="username" value="decoy"></form>'
    )

    settings = _settings(f"{decoy}{FIXTURE}")

    assert settings == _settings()
    assert settings.active is True


def test_a_second_form_with_the_id_of_the_api_form_is_ignored() -> None:
    # The first form counts, a later one of the same id does not even with a differing switch (no mixed settings).
    assert 'name="api_active" checked="checked" value="active"' in FIXTURE
    second = (
        '<form id="apiform"><input type="radio" name="api_active" checked="checked" value="inactive">'
        '<input type="text" id="username" value="other"></form>'
    )

    settings = _settings(FIXTURE.replace("</form>", f"</form>{second}", 1))

    assert settings == _settings()
    assert settings.active is True


def test_the_first_input_of_an_id_counts() -> None:
    html = FIXTURE.replace(
        '<input type="text" id="username" name="username" value="api-user" />',
        '<input id="username" value="first"><input type="text" id="username" name="username" value="api-user" />',
    )

    assert _settings(html, user="first").username == "first"


def test_the_last_selected_option_of_a_select_counts() -> None:
    html = FIXTURE.replace('<option value="4" id="flag_4">', '<option value="4" selected="selected" id="flag_4">')

    assert _settings(html).flag == ACCESS_SELECTION


def _selected_choices(html: str) -> dict[str, str]:
    parser = _ApiFormParser()
    parser.feed(html)
    parser.close()
    return dict(parser.fields.selected)


def test_a_select_of_another_id_leaves_no_choice_behind() -> None:
    # Only the three selects the page is read for keep their selected option.
    html = FIXTURE.replace(
        "</form>", '<select id="other"><option value="0" selected="selected">x</option></select></form>'
    )

    assert _selected_choices(html) == {"in_output": "3", "flag": "3", "one_wire": "1"}


def test_a_select_left_open_does_not_hand_its_options_to_the_next_one() -> None:
    # The flag select is never closed; the select that follows is no API field, and its selected option is its own.
    html = FIXTURE.replace(
        '<option value="4" id="flag_4">Einzelauswahl</option></select>',
        '<option value="4" id="flag_4">Einzelauswahl</option>'
        '<select id="other"><option value="0" selected="selected">x</option></select>',
    )
    assert 'id="other"' in html

    assert _settings(html).flag == ACCESS_READ_WRITE
    assert "other" not in _selected_choices(html)


# --- the summary -----------------------------------------------------------------------------------------------------


def test_the_summary_names_the_state_without_user_or_password() -> None:
    summary = _settings().summary()

    assert summary == (
        "API active; TCP interface on port 1574; login required (user matches, password matches); "
        "IOs read and write, markers read and write, 1-Wire read only"
    )
    assert PAGE_USER not in summary


def test_the_summary_of_an_inactive_api_without_tcp_and_login() -> None:
    html = _inactive(_select(FIXTURE, "flag", "4"))
    html = html.replace(' id="use_tcp" name="use_tcp" checked="checked"', ' id="use_tcp" name="use_tcp"')
    html = html.replace(' id="use_login" name="use_login" checked="checked"', ' id="use_login" name="use_login"')

    assert _settings(html).summary() == (
        "API inactive; TCP interface off; no login; IOs read and write, markers single selection, 1-Wire read only"
    )


def test_the_summary_says_which_part_of_the_login_differs() -> None:
    summary = _settings(password="another-password").summary()  # nosec B106

    assert "login required (user matches, password differs)" in summary


# --- the client ------------------------------------------------------------------------------------------------------


def _admin_page(text: str) -> Handler:
    async def handler(request: web.Request) -> web.Response:
        logged_in = request.cookies.get(SESSION_COOKIE) == SESSION_VALUE
        return web.Response(text=text if logged_in else LOGIN_PAGE, content_type="text/html")

    return handler


async def _client(comexio: FakeComexio, session: aiohttp.ClientSession) -> ComexioClient:
    client = ComexioClient(
        comexio.host, USERNAME, PASSWORD, session=session, api_username=PAGE_USER, api_password=PAGE_PASSWORD
    )
    await client.login()
    return client


def _client_without_login(comexio: FakeComexio, session: aiohttp.ClientSession) -> ComexioClient:
    return ComexioClient(
        comexio.host, USERNAME, PASSWORD, session=session, api_username=PAGE_USER, api_password=PAGE_PASSWORD
    )


async def test_get_api_settings_reads_the_api_page_with_the_admin_session(comexio: FakeComexio) -> None:
    comexio.serve("GET", API_PATH, _admin_page(FIXTURE))
    async with aiohttp.ClientSession(**session_kwargs()) as session:
        client = await _client(comexio, session)

        settings = await client.get_api_settings()

    assert settings.active is True
    assert (settings.username_matches, settings.password_matches) == (True, True)
    assert len(comexio.received_at("GET", API_PATH)) == 1


async def test_get_api_settings_compares_with_the_api_credentials_of_the_client(comexio: FakeComexio) -> None:
    comexio.serve("GET", API_PATH, _admin_page(FIXTURE))
    async with aiohttp.ClientSession(**session_kwargs()) as session:
        client = ComexioClient(comexio.host, USERNAME, PASSWORD, session=session, api_username="other")
        await client.login()

        settings = await client.get_api_settings()

    assert (settings.username_matches, settings.password_matches) == (False, False)


async def test_get_api_settings_over_plain_http_reads_the_page_by_default_and_with_require_https_false(
    comexio: FakeComexio,
) -> None:
    comexio.serve("GET", API_PATH, _admin_page(FIXTURE))
    async with aiohttp.ClientSession(**session_kwargs()) as session:
        client = await _client(comexio, session)

        by_default = await client.get_api_settings()
        explicit = await client.get_api_settings(require_https=False)

    assert by_default == explicit
    assert by_default.active is True
    assert len(comexio.received_at("GET", API_PATH)) == 2


@pytest.mark.parametrize("logged_in", [True, False], ids=["logged-in", "no-login"])
async def test_get_api_settings_with_require_https_refuses_plain_http_before_any_request(
    comexio: FakeComexio, logged_in: bool
) -> None:
    comexio.serve("GET", API_PATH, _admin_page(FIXTURE))
    async with aiohttp.ClientSession(**session_kwargs()) as session:
        client = await _client(comexio, session) if logged_in else _client_without_login(comexio, session)
        received_before = list(comexio.received)

        with pytest.raises(ComexioInsecureTransportError) as info:
            await client.get_api_settings(require_https=True)

    # Not only the page: no login or other request either (the login of the logged-in case came before the snapshot).
    assert comexio.received == received_before
    assert str(info.value) == (
        "API settings page not read: the client talks plain HTTP and the page holds the API password in clear "
        "text (require_https=True; give the client a certificate fingerprint to use HTTPS)"
    )
    # A client-side refusal that retrying cannot help: neither a dropped connection nor a rejected login.
    assert isinstance(info.value, ComexioError)
    assert not isinstance(info.value, (ComexioConnectionError, ComexioAuthenticationError))


def _holds(value: object, needle: str, depth: int = 3) -> bool:
    """Whether value is needle's text or, up to depth levels down, a container or object that holds it."""
    if isinstance(value, str):
        return needle in value
    if isinstance(value, bytes | bytearray):
        return needle.encode() in value
    if depth == 0:
        return False
    if isinstance(value, dict):
        inner = [*value.keys(), *value.values()]
    elif isinstance(value, list | tuple | set | frozenset):
        inner = list(value)
    elif isinstance(value, BaseException):
        inner = list(value.args)
    else:
        slots = (name for cls in type(value).__mro__ for name in getattr(cls, "__slots__", ()))
        inner = [*getattr(value, "__dict__", {}).values(), *(getattr(value, name, None) for name in slots)]
    return any(_holds(item, needle, depth - 1) for item in inner)


def _frames_holding(
    error: BaseException, needle: str, *, module: str = "aiocomexio", ignore: tuple[object, ...] = ()
) -> tuple[list[str], list[str]]:
    """The functions of module in the traceback of error, and those among them whose locals hold needle.

    ignore: objects that hold needle by design (the client holds the credentials it was built with).
    """
    skipped = {id(thing) for thing in ignore}
    visited: list[str] = []
    holders: list[str] = []
    for frame, _ in traceback.walk_tb(error.__traceback__):
        if not str(frame.f_globals.get("__name__", "")).startswith(module):
            continue
        visited.append(frame.f_code.co_name)
        if any(id(value) not in skipped and _holds(value, needle) for value in frame.f_locals.values()):
            holders.append(frame.f_code.co_name)
    return visited, holders


async def _read_failing(comexio: FakeComexio, page: str) -> tuple[ComexioClient, ComexioDataError]:
    """The client and the error get_api_settings raises when the API page is page."""
    comexio.serve("GET", API_PATH, _admin_page(page))
    session = aiohttp.ClientSession(**session_kwargs())
    try:
        client = await _client(comexio, session)
        with pytest.raises(ComexioDataError) as info:
            await client.get_api_settings()
    finally:
        await session.close()
    return client, info.value


async def test_a_failed_read_leaves_no_page_in_the_locals_of_its_frames(comexio: FakeComexio) -> None:
    # A page without the switch fails in the parser, which holds the page and the password input.
    page = FIXTURE.replace('name="api_active"', 'name="other_switch"')
    assert PAGE_PASSWORD in page
    client, error = await _read_failing(comexio, page)

    assert str(error) == "API page: the api_active choice is None, expected active or inactive"
    visited, holders = _frames_holding(error, PAGE_PASSWORD, ignore=(client,))
    assert "parse_api_settings" in visited
    assert holders == []
    # ignore=(client,) hides the client from the check: it must hold the password only as its own credential.
    assert [name for name, value in vars(client).items() if _holds(value, PAGE_PASSWORD)] == ["_api_password"]


async def test_a_read_that_fails_after_the_password_leaves_no_page_in_the_locals_of_its_frames(
    comexio: FakeComexio,
) -> None:
    # The password input is read into a local of api_settings_from_fields before this select is checked.
    page = FIXTURE.replace('id="in_output"', 'id="in_output_other"')
    assert PAGE_PASSWORD in page
    client, error = await _read_failing(comexio, page)

    assert str(error) == "API page has no selected option for in_output: changed page"
    visited, holders = _frames_holding(error, PAGE_PASSWORD, ignore=(client,))
    assert "api_settings_from_fields" in visited
    assert holders == []


def _serve_admin_bytes(comexio: FakeComexio, body: bytes) -> None:
    async def handler(request: web.Request) -> web.Response:
        logged_in = request.cookies.get(SESSION_COOKIE) == SESSION_VALUE
        return web.Response(body=body if logged_in else LOGIN_PAGE.encode(), content_type="text/html")

    comexio.serve("GET", API_PATH, handler)


async def test_an_undecodable_answer_leaves_no_page_in_its_frames_or_in_the_error(comexio: FakeComexio) -> None:
    body = FIXTURE.encode() + b"\xff"
    _serve_admin_bytes(comexio, body)
    async with aiohttp.ClientSession(**session_kwargs()) as session:
        client = await _client(comexio, session)

        with pytest.raises(ComexioDataError) as info:
            await client.get_api_settings()

    error = info.value
    # The position and the reason are all that is kept of the UnicodeDecodeError.
    assert str(error) == f"API settings page: response is not valid utf-8 (byte {len(body) - 1}: invalid start byte)"
    # The UnicodeDecodeError holds the whole body in its object: it is not chained to the error.
    assert (error.__cause__, error.__context__) == (None, None)
    visited, holders = _frames_holding(error, PAGE_PASSWORD, ignore=(client,))
    assert "_response_text" in visited
    assert holders == []


async def test_a_read_cancelled_midway_leaves_no_page_in_the_locals_of_its_frames(
    comexio: FakeComexio, monkeypatch: pytest.MonkeyPatch
) -> None:
    comexio.serve("GET", API_PATH, _admin_page(FIXTURE))
    real_read = aiohttp.StreamReader.read
    chunks: list[bytes] = []

    async def read_then_cancel(self: aiohttp.StreamReader, n: int = -1) -> bytes:
        if chunks:
            raise asyncio.CancelledError
        chunks.append(await real_read(self, n))
        return chunks[0]

    async with aiohttp.ClientSession(**session_kwargs()) as session:
        client = await _client(comexio, session)
        monkeypatch.setattr(aiohttp.StreamReader, "read", read_then_cancel)

        with pytest.raises(asyncio.CancelledError) as info:
            await client.get_api_settings()

    assert PAGE_PASSWORD.encode() in chunks[0]  # what was read before the abort held the password
    visited, holders = _frames_holding(info.value, PAGE_PASSWORD, ignore=(client,))
    assert "_response_text" in visited
    assert holders == []


def _fail_holding(secret: str) -> None:
    held = f"{secret}!"
    raise ValueError(held)


def _caught_holding(secret: str) -> ValueError:
    """An error whose traceback has a finished frame holding secret."""
    try:
        _fail_holding(secret)
    except ValueError as err:
        return err
    raise AssertionError


def test_clear_frames_follows_causes_and_contexts_and_ends_on_a_cycle() -> None:
    first, second = _caught_holding("first-secret"), _caught_holding("second-secret")
    first.__context__ = second
    second.__cause__ = first  # a cycle: a walk that does not remember what it saw never ends
    assert "_fail_holding" in _frames_holding(second, "second-secret", module=__name__)[1]

    _clear_frames(first)

    assert _frames_holding(first, "first-secret", module=__name__)[1] == []
    assert _frames_holding(second, "second-secret", module=__name__)[1] == []


def test_clear_frames_reaches_the_frames_of_a_cause() -> None:
    cause = _caught_holding("cause-secret")
    error = ValueError("raised from the cause")
    error.__cause__ = cause

    _clear_frames(error)

    assert _frames_holding(cause, "cause-secret", module=__name__)[1] == []


def test_clear_frames_leaves_the_error_it_is_told_to_keep() -> None:
    kept, other, older = _caught_holding("kept-secret"), _caught_holding("other-secret"), _caught_holding("old-secret")
    other.__context__ = kept
    kept.__context__ = older

    _clear_frames(other, keep=kept)

    assert _frames_holding(other, "other-secret", module=__name__)[1] == []
    assert "_fail_holding" in _frames_holding(kept, "kept-secret", module=__name__)[1]
    assert "_fail_holding" in _frames_holding(older, "old-secret", module=__name__)[1]


async def test_a_read_inside_an_except_block_leaves_the_frames_of_the_error_being_handled(
    comexio: FakeComexio,
) -> None:
    page = FIXTURE.replace('name="api_active"', 'name="other_switch"')
    comexio.serve("GET", API_PATH, _admin_page(page))
    handled = _caught_holding("caller-secret")
    async with aiohttp.ClientSession(**session_kwargs()) as session:
        client = await _client(comexio, session)
        try:
            raise handled
        except ValueError:
            with pytest.raises(ComexioDataError) as info:
                await client.get_api_settings()

    assert info.value.__context__ is handled  # the read failed while handling it, so it is in the chain
    assert "_fail_holding" in _frames_holding(handled, "caller-secret", module=__name__)[1]
    assert _frames_holding(info.value, PAGE_PASSWORD, ignore=(client,))[1] == []


@pytest.mark.parametrize(
    ("body", "frame"),
    [
        pytest.param(
            FIXTURE.replace('name="api_active"', 'name="other_switch"').encode(), "parse_api_settings", id="parser"
        ),
        pytest.param(FIXTURE.encode() + b"\xff", "_response_text", id="decode"),
    ],
)
async def test_without_the_clearing_the_frames_check_finds_the_page(
    comexio: FakeComexio, monkeypatch: pytest.MonkeyPatch, body: bytes, frame: str
) -> None:
    # The positive control of the checks above: they must see the page in the frame once nothing empties it.
    monkeypatch.setattr("aiocomexio.client._clear_frames", lambda *args, **kwargs: None)
    _serve_admin_bytes(comexio, body)
    async with aiohttp.ClientSession(**session_kwargs()) as session:
        client = await _client(comexio, session)

        with pytest.raises(ComexioDataError) as info:
            await client.get_api_settings()

    assert frame in _frames_holding(info.value, PAGE_PASSWORD, ignore=(client,))[1]


async def test_a_clearing_that_fails_does_not_replace_the_error(
    comexio: FakeComexio, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing(_tb: object) -> None:
        raise ValueError("clearing failed")

    monkeypatch.setattr(traceback, "clear_frames", failing)
    page = FIXTURE.replace('name="api_active"', 'name="other_switch"')
    comexio.serve("GET", API_PATH, _admin_page(page))
    async with aiohttp.ClientSession(**session_kwargs()) as session:
        client = await _client(comexio, session)

        with pytest.raises(ComexioDataError) as info:
            await client.get_api_settings()

    assert str(info.value) == "API page: the api_active choice is None, expected active or inactive"


class _Slotted:
    __slots__ = ("held",)

    def __init__(self, held: object) -> None:
        self.held = held


@pytest.mark.parametrize(
    "holder",
    [
        pytest.param("a needle here", id="text"),
        pytest.param(b"a needle here", id="bytes"),
        pytest.param(bytearray(b"a needle here"), id="bytearray"),
        pytest.param(["x", ("y", "needle")], id="nested containers"),
        pytest.param({"key": ["needle"]}, id="dict value"),
        pytest.param({"needle": 1}, id="dict key"),
        pytest.param(ValueError("needle"), id="exception args"),
        pytest.param(_Slotted("needle"), id="slots"),
        pytest.param(_Slotted(ValueError("needle")), id="slots holding an exception"),
    ],
)
def test_holds_finds_the_needle_in_every_shape(holder: object) -> None:
    assert _holds(holder, "needle")


def test_holds_finds_nothing_that_is_not_there() -> None:
    assert not _holds(_Slotted(["other", ValueError("other")]), "needle")


async def test_get_api_settings_sends_no_api_credential_to_comexio(comexio: FakeComexio) -> None:
    comexio.serve("GET", API_PATH, _admin_page(FIXTURE))
    async with aiohttp.ClientSession(**session_kwargs()) as session:
        client = await _client(comexio, session)

        await client.get_api_settings()

    (request,) = comexio.received_at("GET", API_PATH)
    assert request.authorization is None


async def test_get_api_settings_needs_the_admin_session(comexio: FakeComexio) -> None:
    comexio.serve("GET", API_PATH, _admin_page(FIXTURE))
    async with aiohttp.ClientSession(**session_kwargs()) as session:
        client = ComexioClient(comexio.host, USERNAME, PASSWORD, session=session)

        with pytest.raises(ComexioAuthenticationError) as info:
            await client.get_api_settings()

    assert str(info.value) == "Comexio served the login form, the session is not logged in (API settings page)"


async def test_get_api_settings_of_a_page_that_is_none_raises_data_error(comexio: FakeComexio) -> None:
    comexio.serve("GET", API_PATH, _admin_page("<html><body>nothing here</body></html>"))
    async with aiohttp.ClientSession(**session_kwargs()) as session:
        client = await _client(comexio, session)

        with pytest.raises(ComexioDataError, match="api_active"):
            await client.get_api_settings()


async def test_get_api_settings_after_the_session_lapsed_raises_authentication_error(comexio: FakeComexio) -> None:
    comexio.serve_text("GET", API_PATH, LOGIN_PAGE)
    async with aiohttp.ClientSession(**session_kwargs()) as session:
        client = await _client(comexio, session)

        with pytest.raises(ComexioAuthenticationError):
            await client.get_api_settings()


@pytest.mark.parametrize("status", [404, 500])
async def test_get_api_settings_of_a_firmware_without_the_page_raises_response_error(
    comexio: FakeComexio, status: int
) -> None:
    comexio.serve_text("GET", API_PATH, "not found", status=status)
    async with aiohttp.ClientSession(**session_kwargs()) as session:
        client = await _client(comexio, session)

        with pytest.raises(ComexioResponseError) as info:
            await client.get_api_settings()

    assert info.value.status == status
