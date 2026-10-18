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
from typing import Any, TypeGuard

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
_DIGIT_LIMIT_GETTER = "get_int_max_str_digits"


def _caught(handler: ast.ExceptHandler) -> set[str]:
    """The exception names a handler catches; a bare except catches everything.

    builtins.ValueError counts as ValueError; a handler naming a tuple constant is not resolved.
    """
    if handler.type is None:
        return {"BaseException"}
    types = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    return {node.attr if isinstance(node, ast.Attribute) else ast.unparse(node) for node in types}


def _conversion_names(tree: ast.AST) -> set[str]:
    """int, float and every alias a "from builtins import int as ..." gives them in the module."""
    return set(_CONVERSIONS).union(
        alias.asname
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "builtins"
        for alias in node.names
        if alias.name in _CONVERSIONS and alias.asname
    )


def _builtins_names(tree: ast.AST) -> set[str]:
    """builtins and every name an "import builtins as ..." gives it in the module."""
    return {"builtins"}.union(
        alias.asname
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
        if alias.name == "builtins" and alias.asname
    )


def _is_conversion(node: ast.AST, names: set[str], modules: set[str]) -> bool:
    """Whether node calls int() or float() by its name, an alias of it or as an attribute of builtins.

    A conversion bound to another name at runtime (to_int = int) is not seen.
    """
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name):
        return func.id in names
    return (
        isinstance(func, ast.Attribute)
        and func.attr in _CONVERSIONS
        and isinstance(func.value, ast.Name)
        and func.value.id in modules
    )


def _violations(source: str, filename: str) -> list[str]:
    """file:line of each try whose handlers catch ValueError but not OverflowError around a conversion.

    Only conversions called in the try body itself count; one inside a called helper is not seen.
    """
    tree = ast.parse(source, filename)
    names, modules = _conversion_names(tree), _builtins_names(tree)
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try | ast.TryStar) or not any(
            _is_conversion(inner, names, modules) for statement in node.body for inner in ast.walk(statement)
        ):
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
        ("try:\n    builtins.float(x)\nexcept ValueError:\n    pass\n", ["m.py:1"]),
        ("import builtins as b\ntry:\n    b.int(x)\nexcept ValueError:\n    pass\n", ["m.py:2"]),
        ("from builtins import int as to_int\ntry:\n    to_int(x)\nexcept ValueError:\n    pass\n", ["m.py:2"]),
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
        "qualified-conversion",
        "aliased-builtins-module",
        "aliased-conversion",
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


def _is_setter(node: ast.AST) -> TypeGuard[ast.expr | ast.stmt]:
    """Whether node references sys.set_int_max_str_digits, however it is reached."""
    return (
        (isinstance(node, ast.Attribute) and node.attr == _DIGIT_LIMIT_SETTER)
        or (isinstance(node, ast.ImportFrom) and any(alias.name == _DIGIT_LIMIT_SETTER for alias in node.names))
        or (isinstance(node, ast.Constant) and node.value == _DIGIT_LIMIT_SETTER)
    )


def _is_fixture(function: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Whether a function is decorated with pytest.fixture or fixture, with or without arguments."""
    return any(
        ast.unparse(decorator.func if isinstance(decorator, ast.Call) else decorator) in {"pytest.fixture", "fixture"}
        for decorator in function.decorator_list
    )


def _saved_limits(function: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """The names a function assigns the result of sys.get_int_max_str_digits() to."""
    return {
        target.id
        for node in ast.walk(function)
        if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Call)
        and ast.unparse(node.value.func).endswith(_DIGIT_LIMIT_GETTER)
        for target in node.targets
        if isinstance(target, ast.Name)
    }


def _restores_after_yield(function: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Whether the function, after its first yield, sets the limit back to a value it saved before."""
    yields = [node.lineno for node in ast.walk(function) if isinstance(node, ast.Yield)]
    saved = _saved_limits(function)
    return bool(yields) and any(
        isinstance(node, ast.Call)
        and _is_setter(node.func)
        and node.lineno > min(yields)
        and len(node.args) == 1
        and isinstance(node.args[0], ast.Name)
        and node.args[0].id in saved
        for node in ast.walk(function)
    )


def _restoring_fixture_lines(tree: ast.AST) -> set[int]:
    """The lines of each fixture that restores the saved limit after its yield, so the test it serves keeps none."""
    return {
        line
        for function in ast.walk(tree)
        if isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef)
        and _is_fixture(function)
        and _restores_after_yield(function)
        for line in range(function.lineno, (function.end_lineno or function.lineno) + 1)
    }


def _digit_limit_setters(source: str, filename: str, *, fixtures_allowed: bool = False) -> list[str]:
    """file:line of each reference to sys.set_int_max_str_digits; fixtures_allowed spares a restoring fixture."""
    tree = ast.parse(source, filename)
    allowed = _restoring_fixture_lines(tree) if fixtures_allowed else set()
    return [f"{filename}:{node.lineno}" for node in ast.walk(tree) if _is_setter(node) and node.lineno not in allowed]


def _digit_limit_callers(root: Path, skip: Path | None = None) -> list[str]:
    """Each setter reference under root; only a restoring fixture in root/conftest.py is spared, not a nested one."""
    return [
        caller
        for path in sorted(root.rglob("*.py"))
        if path != skip
        for caller in _digit_limit_setters(
            path.read_text(encoding="utf-8"),
            path.relative_to(root).as_posix(),
            fixtures_allowed=path == root / "conftest.py",
        )
    ]


def test_only_a_conftest_fixture_changes_the_int_digit_limit() -> None:
    """The limit is global to the interpreter: a test that sets it without restoring it changes every later test."""
    callers = _digit_limit_callers(_TESTS, skip=Path(__file__).resolve())
    assert not callers, "use the int_max_str_digits fixture instead of sys.set_int_max_str_digits: " + ", ".join(
        callers
    )


def test_the_guard_spares_the_top_conftest_fixture_only(tmp_path: Path) -> None:
    (tmp_path / "conftest.py").write_text(_RESTORING_FIXTURE, encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "conftest.py").write_text(_RESTORING_FIXTURE, encoding="utf-8")

    assert _digit_limit_callers(tmp_path) == ["sub/conftest.py:4", "sub/conftest.py:6"]


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


_RESTORING_FIXTURE = (
    "@pytest.fixture\n"
    "def limit():\n"
    "    previous = sys.get_int_max_str_digits()\n"
    "    sys.set_int_max_str_digits(4300)\n"
    "    yield\n"
    "    sys.set_int_max_str_digits(previous)\n"
)


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (_RESTORING_FIXTURE, []),
        (_RESTORING_FIXTURE.replace("@pytest.fixture", "@pytest.fixture(autouse=True)"), []),
        ("@pytest.fixture\ndef limit():\n    sys.set_int_max_str_digits(4300)\n    yield\n", ["m.py:3"]),
        ("@pytest.fixture\ndef limit():\n    sys.set_int_max_str_digits(4300)\n", ["m.py:3"]),
        (
            "def limit():\n    sys.set_int_max_str_digits(4300)\n    yield\n    sys.set_int_max_str_digits(0)\n",
            ["m.py:2", "m.py:4"],
        ),
        (_RESTORING_FIXTURE + "sys.set_int_max_str_digits(0)\n", ["m.py:7"]),
        (_RESTORING_FIXTURE.replace("def limit", "async def limit"), []),
        (_RESTORING_FIXTURE.replace("digits(previous)", "digits(0)"), ["m.py:4", "m.py:6"]),
    ],
    ids=[
        "restoring-fixture",
        "restoring-fixture-with-arguments",
        "no-restore-after-yield",
        "no-yield",
        "not-a-fixture",
        "outside-the-fixture",
        "async-restoring-fixture",
        "sets-another-value-after-yield",
    ],
)
def test_the_guard_spares_only_a_fixture_that_restores_the_digit_limit(source: str, expected: list[str]) -> None:
    assert _digit_limit_setters(source, "m.py", fixtures_allowed=True) == expected
