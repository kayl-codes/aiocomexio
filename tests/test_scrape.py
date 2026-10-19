"""Scraping of Comexio's inline `var $Name = {...}` JS object literals out of admin pages."""

import json
import logging
from datetime import timedelta

import pytest

from aiocomexio.scrape import (
    TimeServerSettings,
    extract_js_object_literal,
    normalize_js_like_object,
    parse_comexio_version,
    parse_free_storage_percent,
    parse_io_input_types,
    parse_io_types,
    parse_run_duration,
    parse_time_server,
    parse_webio_add_page,
    scrape_js_vars,
)
from tests.common import load_fixture


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('{"a": 1,}', '{"a": 1}'),
        ('{"a": [1, 2, ], }', '{"a": [1, 2 ] }'),
        ('{"a": 1,\n  }', '{"a": 1\n  }'),
        ('{"a": 1}', '{"a": 1}'),
        ('{"a": "x,}", "b": "y,]",}', '{"a": "x,}", "b": "y,]"}'),
        ('{"a": "q\\",}",}', '{"a": "q\\",}"}'),
    ],
)
def test_normalize_js_like_object_strips_trailing_commas(raw: str, expected: str) -> None:
    assert normalize_js_like_object(raw) == expected


def test_extract_js_object_literal_returns_balanced_object_and_end_index() -> None:
    text = 'x = {"a": {"b": 1}} ; tail'
    start = text.index("{")

    literal, end = extract_js_object_literal(text, start)

    assert literal == '{"a": {"b": 1}}'
    assert text[end:] == " ; tail"


@pytest.mark.parametrize(
    "literal",
    [
        '{"name": "brace } inside"}',
        '{"name": "open { inside"}',
        "{'name': 'single } quoted'}",
        '{"name": "escaped \\" quote }"}',
    ],
)
def test_extract_js_object_literal_ignores_braces_inside_strings(literal: str) -> None:
    assert extract_js_object_literal(literal + " trailing", 0) == (literal, len(literal))


@pytest.mark.parametrize(
    ("text", "start"),
    [
        ('{"a": 1', 0),  # unterminated
        ('x = {"a": 1}', 0),  # start does not point at "{"
        ("{}", 5),  # start out of range
    ],
)
def test_extract_js_object_literal_returns_none_when_not_extractable(text: str, start: int) -> None:
    assert extract_js_object_literal(text, start) == (None, start)


def test_scrape_js_vars_parses_every_valid_object_literal() -> None:
    result = scrape_js_vars(load_fixture("function_module_page.html"), page_label="test")

    assert result == {
        "Paper": {"2": {"Id": 2, "Name": "A4", "MMX": 297, "MMY": 210}},
        "Fubs": {"1": {"Id": 1, "Name": 'Plan {mit} "Klammern"', "Paper": 2}},
        "FubModules": {"2": {"1": {"Id": 1, "Name": "Licht, Wohnen", "Type": 1}}, "10": []},
    }


def test_scrape_js_vars_skips_invalid_json_with_warning(caplog: pytest.LogCaptureFixture) -> None:
    result = scrape_js_vars(load_fixture("function_module_page.html"), page_label="test")

    assert "Broken" not in result
    assert "Failed to decode JSON for variable $Broken on test page" in caplog.text


# json.loads raises a plain ValueError, not JSONDecodeError, for an int beyond the int-to-str digit limit (4300).
_HUGE_INT = "1" + "0" * 5000


def _huge_int_error() -> str:
    with pytest.raises(ValueError) as exc_info:
        json.loads(_HUGE_INT)
    assert not isinstance(exc_info.value, json.JSONDecodeError)
    return str(exc_info.value)


@pytest.mark.usefixtures("int_max_str_digits")
def test_scrape_js_vars_skips_a_huge_int_with_warning(caplog: pytest.LogCaptureFixture) -> None:
    html = f'<script>var $Big = {{"1": {{"Id": {_HUGE_INT}}}}};\nvar $Fubs = {{"1": {{"Id": 1}}}};</script>'

    with caplog.at_level(logging.WARNING, logger="aiocomexio.scrape"):
        result = scrape_js_vars(html, page_label="test")

    assert result == {"Fubs": {"1": {"Id": 1}}}
    assert [r.getMessage() for r in caplog.records] == [
        f"Failed to decode JSON for variable $Big on test page: {_huge_int_error()}"
    ]


@pytest.mark.usefixtures("int_max_str_digits")
def test_parse_io_types_with_a_huge_int_returns_empty_with_warning(caplog: pytest.LogCaptureFixture) -> None:
    html = f'<script>var $ioTypes = {{"1": {{"binary": 1, "min": {_HUGE_INT}}}}};</script>'

    with caplog.at_level(logging.WARNING, logger="aiocomexio.scrape"):
        result = parse_io_types(html)

    assert result == {}
    assert [r.getMessage() for r in caplog.records] == [
        f"Failed to decode $ioTypes: {_huge_int_error()}",
        "No IO type data found ($ioTypes / $IOTypesBinary) — using identifier fallback",
    ]


@pytest.mark.usefixtures("int_max_str_digits")
def test_parse_io_input_types_with_a_huge_int_returns_empty_with_warning(caplog: pytest.LogCaptureFixture) -> None:
    html = f'<script>var $IOInputTypes = {{"1": {{"input": {_HUGE_INT}}}}};</script>'

    with caplog.at_level(logging.WARNING, logger="aiocomexio.scrape"):
        result = parse_io_input_types(html)

    assert result == {}
    assert [r.getMessage() for r in caplog.records] == [f"Failed to decode $IOInputTypes: {_huge_int_error()}"]


@pytest.mark.usefixtures("int_max_str_digits")
@pytest.mark.parametrize("broken", ["DeviceBaseList", "DeviceList"])
def test_parse_webio_add_page_with_a_huge_int_is_no_add_page(caplog: pytest.LogCaptureFixture, broken: str) -> None:
    lists = dict.fromkeys(("DeviceBaseList", "DeviceList"), '{"1": {"Id": 1}}')
    lists[broken] = f'{{"1": {{"Id": {_HUGE_INT}}}}}'
    html = f"<script>DeviceBaseList={lists['DeviceBaseList']}; DeviceList={lists['DeviceList']};</script>"

    with caplog.at_level(logging.WARNING, logger="aiocomexio.scrape"):
        result = parse_webio_add_page(html)

    assert result is None
    assert [r.getMessage() for r in caplog.records] == [f"Failed to decode {broken}: {_huge_int_error()}"]


def test_scrape_js_vars_empty_array_does_not_steal_next_object() -> None:
    """Regression: `var $Fubs = [];` (PHP json_encode of an empty array, e.g. no plan left)
    was assigned the NEXT variable's object literal."""
    html = '<script>var $Fubs = [];\nvar $FubModules = {"2": {}};</script>'

    assert scrape_js_vars(html, page_label="test") == {"Fubs": {}, "FubModules": {"2": {}}}


def test_scrape_js_vars_ignores_declarations_outside_script_blocks() -> None:
    html = '<p>var $Outside = {"a": 1};</p><script>var $Inside = {"b": 2};</script>'

    assert scrape_js_vars(html, page_label="test") == {"Inside": {"b": 2}}


@pytest.mark.parametrize(
    ("html", "expected"),
    [
        ('<script src="/11.0.2/js/cmb_admin.js"></script>', "11.0.2"),
        (
            '<script src="/10.4.17/module/admin/function_function_module/js/cmb_function_function_module.js">',
            "10.4.17",
        ),
        ('<script src="/js/cmb_admin.js"></script>', None),
        ('<script src="/11.0.2/js/other.js"></script>', None),
    ],
)
def test_parse_comexio_version(html: str, expected: str | None) -> None:
    assert parse_comexio_version(html) == expected


@pytest.mark.parametrize(
    "html",
    [
        'var $ioTypes = {"1": {"binary": true,},};',
        'var $IOTypesBinary = {"1": {"binary": true}};',
        # A broken legacy table falls through to the v11+ one.
        'var $ioTypes = {broken}; var $IOTypesBinary = {"1": {"binary": true}};',
        # So does an empty one (PHP's json_encode of an empty array).
        'var $ioTypes = []; var $IOTypesBinary = {"1": {"binary": true}};',
    ],
)
def test_parse_io_types(html: str) -> None:
    assert parse_io_types(html) == {"1": {"binary": True}}


@pytest.mark.parametrize(
    "html",
    [
        "<p>no tables here</p>",
        "var $IOTypesBinary = 5;",  # no object literal after the declaration
        # A non-object value must not grab the NEXT declaration's object.
        'var $IOTypesBinary = 5; var $Other = {"1": {"binary": true}};',
        'var $IOTypesBinary = {"1": {"binary": true}',  # unterminated
    ],
)
def test_parse_io_types_missing_falls_back_to_empty(html: str, caplog: pytest.LogCaptureFixture) -> None:
    assert parse_io_types(html) == {}
    assert "No IO type data found" in caplog.text


def test_parse_io_types_gap_free_array_is_keyed_by_index() -> None:
    # Regression: PHP's json_encode renders a table with the keys 0..n-1 as a JSON array — that
    # was dropped (debug log only) and every IO fell back to its identifier.
    html = 'var $IOTypesBinary = [{"binary": false}, {"binary": true}];'

    assert parse_io_types(html) == {"0": {"binary": False}, "1": {"binary": True}}


def test_scrape_js_vars_array_literal_is_keyed_by_index() -> None:
    # Nested arrays (also inside a record) raise the depth too, so only the outer "]" closes it.
    html = (
        '<script>var $Fubs = [{"Id": 0, "Plans": [[1], []]}, {"Id": 1, "Name": "a]b"}]; var $Next = {"x": 1};</script>'
    )

    assert scrape_js_vars(html, page_label="test") == {
        "Fubs": {"0": {"Id": 0, "Plans": [[1], []]}, "1": {"Id": 1, "Name": "a]b"}},
        "Next": {"x": 1},
    }


@pytest.mark.parametrize(
    "array",
    [
        "[2, 5, 7]",  # a plain list, no records
        '[{"Id": 1}, {"Id": 2}]',  # records whose ids are not their positions
        '[{"binary": true}, null]',
    ],
)
def test_array_that_is_no_id_group_is_skipped(array: str) -> None:
    # A list-valued legacy $ioTypes must not hide the real table, nor positions pass for ids.
    html = f'<script>var $ioTypes = {array}; var $IOTypesBinary = {{"1": {{"binary": true}}}};</script>'

    assert parse_io_types(html) == {"1": {"binary": True}}
    assert scrape_js_vars(html, page_label="test") == {"IOTypesBinary": {"1": {"binary": True}}}


def test_parse_io_input_types() -> None:
    assert parse_io_input_types('var $IOInputTypes = {"2": {"input": false}};') == {"2": {"input": False}}
    assert parse_io_input_types("<p></p>") == {}


def test_scrape_js_vars_non_object_value_does_not_steal_next_object() -> None:
    """Regression: `var $A = 5;` was assigned the object of the NEXT declaration."""
    html = '<script>var $A = 5; var $B = {"x": 1};</script>'

    assert scrape_js_vars(html, page_label="test") == {"B": {"x": 1}}


# --- system dashboard: free storage ------------------------------------------------------------

_DASHBOARD_PAGE = "system_dashboard_page.html"


def _dashboard(
    label: str = "Freier Speicherplatz", rows: str = "<tr><td >Freier Speicherplatz:</td><td >43%</td></tr>"
) -> str:
    """A system dashboard page whose $I18N names the free storage label, with the given table rows."""
    i18n = json.dumps({"module": {"free_flash": label}})
    return f"<html><script>var $I18N = {i18n};</script><table>{rows}</table></html>"


def test_parse_free_storage_percent_reads_the_labelled_row() -> None:
    assert parse_free_storage_percent(load_fixture(_DASHBOARD_PAGE)) == 43.0


@pytest.mark.parametrize(
    ("label", "rows", "expected"),
    [
        ("Free diskspace", "<tr><td >Free diskspace:</td><td >14%</td></tr>", 14.0),
        ("Espace libre", "<tr><td>Espace libre :</td><td> 7 % </td></tr>", 7.0),
        ("Speicher & Co", "<tr><td>Speicher &amp; Co:</td><td>100%</td></tr>", 100.0),
        ("Freier Speicherplatz", "<tr><td>Freier Speicherplatz:</td><td>0%</td></tr>", 0.0),
        # A label that carries the colon itself, in $I18N or without one in the cell.
        ("Freier Speicherplatz:", "<tr><td>Freier Speicherplatz:</td><td>43%</td></tr>", 43.0),
        ("Freier Speicherplatz:", "<tr><td>Freier Speicherplatz</td><td>43%</td></tr>", 43.0),
        ("Freier Speicherplatz", "<tr><td>Freier Speicherplatz:</td><td>43,5%</td></tr>", 43.5),
        ("Freier Speicherplatz", "<tr><td>Freier Speicherplatz:</td><td>43.5%</td></tr>", 43.5),
        # Another row with a label that merely starts like it does not count.
        (
            "Freier Speicherplatz",
            "<tr><td>Freier Speicherplatz SD:</td><td>99%</td></tr><tr><td>Freier Speicherplatz:</td><td>43%</td></tr>",
            43.0,
        ),
    ],
)
def test_parse_free_storage_percent_follows_the_page_label(label: str, rows: str, expected: float) -> None:
    assert parse_free_storage_percent(_dashboard(label, rows)) == expected


@pytest.mark.parametrize(
    "page",
    [
        pytest.param("<html><table><tr><td>Freier Speicherplatz:</td><td>43%</td></tr></table></html>", id="no-i18n"),
        pytest.param(
            '<html><script>var $I18N = {"module": {"serial": "Seriennummer"}};</script>'
            "<tr><td>Freier Speicherplatz:</td><td>43%</td></tr></html>",
            id="no-free-flash-key",
        ),
        pytest.param(
            '<html><script>var $I18N = {"module": {"free_flash": 5}};</script><tr><td>5:</td><td>43%</td></tr></html>',
            id="label-not-a-string",
        ),
        pytest.param(_dashboard(label=" "), id="blank-label"),
        pytest.param(_dashboard(rows="<tr><td>Seriennummer:</td><td>1</td></tr>"), id="no-row"),
        pytest.param(
            _dashboard(
                rows="<tr><td>Freier Speicherplatz:</td><td>43%</td></tr>"
                "<tr><td>Freier Speicherplatz:</td><td>12%</td></tr>"
            ),
            id="two-rows",
        ),
        pytest.param(_dashboard(rows="<tr><td>Freier Speicherplatz:</td><td>n/a</td></tr>"), id="not-a-number"),
        pytest.param(_dashboard(rows="<tr><td>Freier Speicherplatz:</td><td>43</td></tr>"), id="no-percent-sign"),
        pytest.param(_dashboard(rows="<tr><td>Freier Speicherplatz:</td><td>101%</td></tr>"), id="above-100"),
        pytest.param(_dashboard(rows="<tr><td>Freier Speicherplatz:</td><td>-1%</td></tr>"), id="negative"),
        pytest.param(_dashboard(rows="<tr><td>Freier Speicherplatz:</td><td>\u0664\u0663%</td></tr>"), id="non-ascii"),
        pytest.param(_dashboard(rows="<tr><td>Freier Speicherplatz:</td><td>1 GB 43%</td></tr>"), id="extra-text"),
    ],
)
def test_parse_free_storage_percent_returns_none_without_a_clear_value(page: str) -> None:
    assert parse_free_storage_percent(page) is None


# --- system dashboard: uptime ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("12 Tage 21:16:16", timedelta(days=12, hours=21, minutes=16, seconds=16)),
        ("1 Tag 00:00:05", timedelta(days=1, seconds=5)),
        ("3 days 01:02:03", timedelta(days=3, hours=1, minutes=2, seconds=3)),
        ("2 días 10:00:00", timedelta(days=2, hours=10)),
        ("0 Tage 00:00:00", timedelta(0)),
        ("21:16:16", timedelta(hours=21, minutes=16, seconds=16)),
        ("  12 Tage 21:16:16 ", timedelta(days=12, hours=21, minutes=16, seconds=16)),
    ],
)
def test_parse_run_duration(text: str, expected: timedelta) -> None:
    assert parse_run_duration(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "12 Tage",
        "12 Tage 21:16",
        "12 Tage 21:61:16",
        "12 Tage 21:16:60",
        "Tage 21:16:16",
        "12 21:16:16",
        "١ Tag 01:02:03",
    ],
)
def test_parse_run_duration_returns_none_when_unparsable(text: str) -> None:
    assert parse_run_duration(text) is None


def test_parse_free_storage_percent_ignores_a_row_with_nested_markup() -> None:
    # Only plain-text cells are read; a tag inside the cell leaves the value unclear.
    assert (
        parse_free_storage_percent(_dashboard(rows="<tr><td>Freier Speicherplatz:</td><td><b>43%</b></td></tr>"))
        is None
    )


# --- system dashboard: time server -------------------------------------------------------------


def _time_form(checkbox: str = 'checked="checked"', server: str = 'value="ntp.example.org"', options: str = "") -> str:
    """The date & time form of a system dashboard page with the given checkbox, field and interval options."""
    return (
        f'<form><input type="checkbox" id="use_timeserver" name="use_timeserver" {checkbox} />'
        f'<input type="text" id="timeserver" name="timeserver" {server} />'
        f'<select id="timeserver_update_rate">{options}</select></form>'
    )


def test_parse_time_server_reads_the_form() -> None:
    assert parse_time_server(load_fixture(_DASHBOARD_PAGE)) == TimeServerSettings(
        enabled=True, server="ntp.example.org", update_interval=86400
    )


@pytest.mark.parametrize(
    ("page", "expected"),
    [
        pytest.param(_time_form(checkbox=""), TimeServerSettings(False, "ntp.example.org", None), id="unchecked"),
        pytest.param(
            _time_form(checkbox="checked"), TimeServerSettings(True, "ntp.example.org", None), id="bare-checked"
        ),
        pytest.param(_time_form(server='value=""'), TimeServerSettings(True, "", None), id="empty-server"),
        pytest.param(_time_form(server=""), TimeServerSettings(True, "", None), id="no-value-attribute"),
        pytest.param(
            _time_form(server='value="  ntp &amp; co "'), TimeServerSettings(True, "ntp & co", None), id="escaped"
        ),
        pytest.param(
            _time_form(options='<option value="3600" selected>1</option><option value="7200">2</option>'),
            TimeServerSettings(True, "ntp.example.org", 3600),
            id="interval",
        ),
        pytest.param(
            _time_form(options='<option value="3600" selected>1</option><option value="7200" selected>2</option>'),
            TimeServerSettings(True, "ntp.example.org", None),
            id="two-selected-intervals",
        ),
        pytest.param(
            _time_form(options='<option value="1h" selected>1</option>'),
            TimeServerSettings(True, "ntp.example.org", None),
            id="interval-not-a-number",
        ),
        pytest.param(
            _time_form(options='<option value="0" selected>0</option>'),
            TimeServerSettings(True, "ntp.example.org", None),
            id="interval-zero",
        ),
        pytest.param(
            # An unclosed interval select ends at the next non-option tag.
            '<input type="checkbox" id="use_timeserver" checked /><input type="text" id="timeserver" value="a" />'
            '<select id="timeserver_update_rate"><option value="60">1</option><div>'
            '<option value="5" selected>5</option></div>',
            TimeServerSettings(True, "a", None),
            id="unclosed-select",
        ),
        pytest.param(
            # A selected option of another select (the time zone) is not the interval.
            '<select id="timezone_continent"><option value="5" selected>Europa</option></select>' + _time_form(),
            TimeServerSettings(True, "ntp.example.org", None),
            id="other-select",
        ),
    ],
)
def test_parse_time_server_settings(page: str, expected: TimeServerSettings) -> None:
    assert parse_time_server(page) == expected


@pytest.mark.parametrize(
    "page",
    [
        pytest.param("<html><body>kein Formular</body></html>", id="no-form"),
        pytest.param('<input type="text" id="timeserver" value="ntp.example.org" />', id="no-checkbox"),
        pytest.param('<input type="checkbox" id="use_timeserver" checked />', id="no-field"),
        pytest.param(_time_form() + _time_form(), id="two-forms"),
    ],
)
def test_parse_time_server_returns_none_without_a_clear_form(page: str) -> None:
    assert parse_time_server(page) is None
