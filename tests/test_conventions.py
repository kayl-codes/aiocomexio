"""Conventions of the package and its tests that a review would otherwise have to remember.

Every handler that catches ValueError around an int()/float() conversion also catches OverflowError:
int(float("inf")) and float(10**400) raise OverflowError, not ValueError, so a handler that only names ValueError
lets a value from outside crash the caller instead of being rejected.

Only a conftest fixture changes the interpreter's int-to-str digit limit, so every test restores it.
"""

from __future__ import annotations

import ast
import logging
import math
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from aiocomexio.config import clean_value
from aiocomexio.function_plan import canvas, render_values
from aiocomexio.reference_catalog import _as_int

_TESTS = Path(__file__).resolve().parent
_PACKAGE = _TESTS.parent / "src" / "aiocomexio"
_CONVERSIONS = frozenset({"int", "float"})
# Names whose handler also catches OverflowError.
_COVERS_OVERFLOW = frozenset({"OverflowError", "ArithmeticError", "Exception", "BaseException"})
_DIGIT_LIMIT_SETTER = "set_int_max_str_digits"


def _caught(handler: ast.ExceptHandler) -> set[str]:
    """The exception names a handler catches; a bare except catches everything.

    builtins.ValueError counts as ValueError; a handler naming a tuple constant is not resolved.
    """
    if handler.type is None:
        return {"BaseException"}
    types = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    return {node.attr if isinstance(node, ast.Attribute) else ast.unparse(node) for node in types}


def _converts(statements: list[ast.stmt]) -> bool:
    """Whether the statements call int() or float() directly; a conversion inside a called helper is not seen."""
    return any(
        isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _CONVERSIONS
        for statement in statements
        for node in ast.walk(statement)
    )


def _violations(source: str, filename: str) -> list[str]:
    """file:line of each try whose handlers catch ValueError but not OverflowError around a conversion."""
    found = []
    for node in ast.walk(ast.parse(source, filename)):
        if not isinstance(node, ast.Try | ast.TryStar) or not _converts(node.body):
            continue
        caught = set().union(*(_caught(handler) for handler in node.handlers))
        if "ValueError" in caught and not caught & _COVERS_OVERFLOW:
            found.append(f"{filename}:{node.lineno}")
    return found


def test_value_error_handlers_around_conversions_also_catch_overflow_error() -> None:
    violations = [
        violation
        for path in sorted(_PACKAGE.rglob("*.py"))
        for violation in _violations(path.read_text(encoding="utf-8"), path.relative_to(_PACKAGE.parent).as_posix())
    ]
    assert not violations, "except ValueError around int()/float() without OverflowError: " + ", ".join(violations)


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("try:\n    int(x)\nexcept ValueError:\n    pass\n", ["m.py:1"]),
        ("try:\n    y = float(x) * 2\nexcept (TypeError, ValueError):\n    pass\n", ["m.py:1"]),
        ("try:\n    int(x)\nexcept ValueError:\n    pass\nexcept TypeError:\n    pass\n", ["m.py:1"]),
        ("try:\n    int(x)\nexcept* ValueError:\n    pass\n", ["m.py:1"]),
        ("try:\n    int(x)\nexcept builtins.ValueError:\n    pass\n", ["m.py:1"]),
        ("try:\n    int(x)\nexcept (ValueError, OverflowError):\n    pass\n", []),
        ("try:\n    int(x)\nexcept ValueError:\n    pass\nexcept OverflowError:\n    pass\n", []),
        ("try:\n    int(x)\nexcept ArithmeticError:\n    pass\nexcept ValueError:\n    pass\n", []),
        ("try:\n    int(x)\nexcept Exception:\n    pass\n", []),
        ("try:\n    int(x)\nexcept BaseException:\n    pass\n", []),
        ("try:\n    int(x)\nexcept:\n    pass\n", []),
        ("try:\n    json.loads(x)\nexcept ValueError:\n    pass\n", []),  # no conversion in the try
        ("try:\n    pass\nexcept ValueError:\n    int(x)\n", []),  # the conversion is not guarded by it
    ],
    ids=[
        "value-error-only",
        "tuple-without-overflow",
        "separate-unrelated-handler",
        "except-star",
        "qualified-name",
        "tuple-with-overflow",
        "separate-overflow-handler",
        "arithmetic-error",
        "exception",
        "base-exception",
        "bare-except",
        "no-call",
        "call-in-handler",
    ],
)
def test_the_guard_flags_value_error_handlers_without_overflow_error(source: str, expected: list[str]) -> None:
    assert _violations(source, "m.py") == expected


_HUGE = 10**400  # float() of it raises OverflowError


@pytest.mark.parametrize(
    ("convert", "value", "expected"),
    [
        (clean_value, _HUGE, 0.0),
        (lambda value: canvas._int(value, 7), math.inf, 7),
        (lambda value: canvas._float(value, 1.5), _HUGE, 1.5),
        (render_values._is_high, _HUGE, False),
        (render_values._fmt_value, _HUGE, ""),
        (_as_int, math.inf, None),
    ],
    ids=["clean-value", "canvas-int", "canvas-float", "is-high", "fmt-value", "catalog-as-int"],
)
def test_an_overflowing_value_falls_back(convert: Callable[[Any], Any], value: Any, expected: Any) -> None:
    assert convert(value) == expected


@pytest.mark.usefixtures("int_max_str_digits")
def test_a_value_beyond_the_digit_limit_falls_back_and_is_logged_by_size(caplog: pytest.LogCaptureFixture) -> None:
    beyond = 10**5000  # str() of it raises ValueError

    assert render_values._fmt_value(beyond) == ""
    with caplog.at_level(logging.WARNING, logger="aiocomexio.config"):
        assert clean_value(beyond) == 0.0
    (warning,) = caplog.records
    assert warning.getMessage() == f"Failed to clean value: an int of {beyond.bit_length()} bits"


def _digit_limit_setters(source: str, filename: str) -> list[str]:
    """file:line of each reference to sys.set_int_max_str_digits, however it is reached."""
    return [
        f"{filename}:{node.lineno}"
        for node in ast.walk(ast.parse(source, filename))
        if (isinstance(node, ast.Attribute) and node.attr == _DIGIT_LIMIT_SETTER)
        or (isinstance(node, ast.ImportFrom) and any(alias.name == _DIGIT_LIMIT_SETTER for alias in node.names))
        or (isinstance(node, ast.Constant) and node.value == _DIGIT_LIMIT_SETTER)
    ]


def test_only_a_conftest_fixture_changes_the_int_digit_limit() -> None:
    """The limit is global to the interpreter: a test that sets it without restoring it changes every later test."""
    callers = [
        caller
        for path in sorted(_TESTS.rglob("*.py"))
        if path.name not in {"conftest.py", Path(__file__).name}
        for caller in _digit_limit_setters(path.read_text(encoding="utf-8"), path.name)
    ]
    assert not callers, "use the int_max_str_digits fixture instead of sys.set_int_max_str_digits: " + ", ".join(
        callers
    )


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("sys.set_int_max_str_digits(0)\n", ["m.py:1"]),
        ("from sys import set_int_max_str_digits as limit\nlimit(0)\n", ["m.py:1"]),
        ('getattr(sys, "set_int_max_str_digits")(0)\n', ["m.py:1"]),
        ("sys.get_int_max_str_digits()\n", []),
    ],
    ids=["attribute", "import-from", "getattr", "getter-only"],
)
def test_the_guard_flags_every_way_to_set_the_digit_limit(source: str, expected: list[str]) -> None:
    assert _digit_limit_setters(source, "m.py") == expected
