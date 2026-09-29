"""Scraping of Comexio's inline `var $Name = {...}` JS object literals out of admin pages."""

import pytest

from aiocomexio.scrape import (
    extract_js_object_literal,
    normalize_js_like_object,
    parse_comexio_version,
    parse_io_input_types,
    parse_io_types,
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
