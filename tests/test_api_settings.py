"""API settings page: switch, login and access per data type of /api/ (get_api_settings)."""

import dataclasses
import logging
import re

import aiohttp
import pytest
from aiohttp import web

from aiocomexio import (
    ACCESS_READ,
    ACCESS_READ_WRITE,
    ACCESS_SELECTION,
    WRITE_TARGET_IO,
    WRITE_TARGET_KNX,
    WRITE_TARGET_MARKER,
    ApiSettings,
    ComexioAuthenticationError,
    ComexioClient,
    ComexioDataError,
    ComexioResponseError,
    session_kwargs,
)
from aiocomexio.api_settings import ApiPageFields
from aiocomexio.client import _ApiFormParser, parse_api_settings
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


@pytest.mark.parametrize("port", ["", "0", "65536", "-1", "15 74", "abc", "1574.0", "٣٠٠٠", "9" * 40])
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
    with pytest.raises(ValueError, match="unknown write target"):
        _settings().write_blockers("Marker")


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


@pytest.mark.parametrize(("port", "expected"), [("1", 1), ("65535", 65535), ("01574", 1574)])
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
    html = _select(FIXTURE, select_id, "4")

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

    with pytest.raises(ComexioDataError) as info:
        _settings(FIXTURE.replace(old, new))

    assert str(info.value) == message


def test_a_select_without_a_selected_option_is_a_changed_page() -> None:
    html = FIXTURE.replace(' selected="selected" id="in_output_3"', ' id="in_output_3"')

    with pytest.raises(ComexioDataError):
        _settings(html)


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
