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
from collections.abc import Callable, Iterator
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
# Names whose handler also catches ValueError.
_CATCHES_VALUE_ERROR = frozenset({"ValueError", "Exception", "BaseException"})
_DIGIT_LIMIT_GETTER = "get_int_max_str_digits"
# Derived, not written out: this file is scanned too, and the literal would count as a reference to the setter.
_DIGIT_LIMIT_SETTER = _DIGIT_LIMIT_GETTER.replace("get_", "set_", 1)


class _Builtins:
    """Which names of a module stand for which builtins: the module itself and its import aliases."""

    def __init__(self, tree: ast.AST) -> None:
        imports = [node for node in ast.walk(tree) if isinstance(node, ast.Import | ast.ImportFrom)]
        self.modules = {"builtins"}.union(
            alias.asname
            for node in imports
            if isinstance(node, ast.Import)
            for alias in node.names
            if alias.name == "builtins" and alias.asname
        )
        self.aliases = {
            alias.asname: alias.name
            for node in imports
            if isinstance(node, ast.ImportFrom) and node.module == "builtins"
            for alias in node.names
            if alias.asname
        }

    def name(self, node: ast.expr) -> str | None:
        """The builtin a name or builtins attribute stands for; None for anything else (e.g. errors.ValueError)."""
        if isinstance(node, ast.Name):
            return self.aliases.get(node.id, node.id)
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in self.modules:
            return node.attr
        return None


def _caught(handler: ast.ExceptHandler, builtins: _Builtins) -> set[str]:
    """The builtin exceptions a handler catches; a bare except catches everything.

    A handler naming a tuple constant is not resolved.
    """
    if handler.type is None:
        return {"BaseException"}
    types = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    return {name for node in types if (name := builtins.name(node)) is not None}


def _catches_value_error(node: ast.Try | ast.TryStar, builtins: _Builtins) -> bool:
    return any(_caught(handler, builtins) & _CATCHES_VALUE_ERROR for handler in node.handlers)


def _guarded_nodes(statements: list[ast.stmt], builtins: _Builtins) -> Iterator[ast.AST]:
    """The nodes a try body runs under its handlers.

    Not the bodies of nested functions and lambdas (they run later; their decorators, defaults and annotations
    run now, and a class body runs now too), nor the body of a nested try that catches ValueError itself (that
    try is checked on its own).
    """
    stack: list[ast.AST] = list(statements)
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
            if not isinstance(node, ast.Lambda):
                stack.extend([*node.decorator_list, *([node.returns] if node.returns else [])])
            arguments = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
            arguments += [argument for argument in (node.args.vararg, node.args.kwarg) if argument]
            stack.extend(argument.annotation for argument in arguments if argument.annotation)
            stack.extend([*node.args.defaults, *(default for default in node.args.kw_defaults if default)])
            continue
        if isinstance(node, ast.Try | ast.TryStar) and _catches_value_error(node, builtins):
            stack.extend([*node.handlers, *node.orelse, *node.finalbody])
            continue
        stack.extend(ast.iter_child_nodes(node))


def _is_conversion(node: ast.AST, builtins: _Builtins) -> bool:
    """Whether node calls int() or float() by its name, an import alias of it or as an attribute of builtins.

    A conversion bound to another name at runtime (to_int = int) is not seen.
    """
    return isinstance(node, ast.Call) and builtins.name(node.func) in _CONVERSIONS


def _violations(source: str, filename: str) -> list[str]:
    """file:line of each try whose handlers catch ValueError but not OverflowError around a conversion.

    Only conversions called in the try body itself count; one inside a called helper is not seen.
    """
    tree = ast.parse(source, filename)
    builtins = _Builtins(tree)
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try | ast.TryStar) or not any(
            _is_conversion(inner, builtins) for inner in _guarded_nodes(node.body, builtins)
        ):
            continue
        caught = set().union(*(_caught(handler, builtins) for handler in node.handlers))
        if "ValueError" in caught and not caught & _COVERS_OVERFLOW:
            found.append(f"{filename}:{node.lineno}")
    return found


def test_value_error_handlers_around_conversions_also_catch_overflow_error() -> None:
    paths = sorted(_PACKAGE.rglob("*.py"))
    assert _PACKAGE / "client.py" in paths  # the scan reaches the package, so an empty result means something
    violations = [
        violation
        for path in paths
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
        ("from builtins import ValueError as VE\ntry:\n    int(x)\nexcept VE:\n    pass\n", ["m.py:2"]),
        ("try:\n    int(x)\nexcept errors.ValueError:\n    pass\n", []),  # not the builtin
        ("try:\n    def f():\n        int(x)\nexcept ValueError:\n    pass\n", []),  # f runs later
        (
            "try:\n    try:\n        int(x)\n    except (ValueError, OverflowError):\n        pass\n"
            "except ValueError:\n    pass\n",
            [],
        ),
        (
            "try:\n    try:\n        int(x)\n    except TypeError:\n        pass\nexcept ValueError:\n    pass\n",
            ["m.py:1"],
        ),
        ("try:\n    try:\n        int(x)\n    except Exception:\n        pass\nexcept ValueError:\n    pass\n", []),
        (
            "try:\n    try:\n        pass\n    except ValueError:\n        int(x)\nexcept ValueError:\n    pass\n",
            ["m.py:1"],
        ),
        (
            "try:\n    try:\n        pass\n    except ValueError:\n        pass\n    finally:\n        int(x)\n"
            "except ValueError:\n    pass\n",
            ["m.py:1"],
        ),
        ("try:\n    class A:\n        y = int(x)\nexcept ValueError:\n    pass\n", ["m.py:1"]),
        ("try:\n    def f(a=int(x)):\n        pass\nexcept ValueError:\n    pass\n", ["m.py:1"]),
        ("try:\n    def f(a: int(x)):\n        pass\nexcept ValueError:\n    pass\n", ["m.py:1"]),
        ("try:\n    def f(*, a: int(x)):\n        pass\nexcept ValueError:\n    pass\n", ["m.py:1"]),
        ("try:\n    def f(a: int(x), /):\n        pass\nexcept ValueError:\n    pass\n", ["m.py:1"]),
        ("try:\n    def f(*a: int(x)):\n        pass\nexcept ValueError:\n    pass\n", ["m.py:1"]),
        ("try:\n    def f(**a: int(x)):\n        pass\nexcept ValueError:\n    pass\n", ["m.py:1"]),
        ("try:\n    def f() -> int(x):\n        pass\nexcept ValueError:\n    pass\n", ["m.py:1"]),
        ("try:\n    async def f():\n        int(x)\nexcept ValueError:\n    pass\n", []),
        ("try:\n    f = lambda: int(x)\nexcept ValueError:\n    pass\n", []),
        ("import os as b\ntry:\n    b.int(x)\nexcept ValueError:\n    pass\n", []),
        ("from errors import ValueError as VE\ntry:\n    int(x)\nexcept VE:\n    pass\n", []),
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
        "aliased-exception",
        "foreign-value-error",
        "conversion-in-a-nested-function",
        "inner-try-handles-it",
        "inner-try-without-value-error",
        "inner-try-catches-everything",
        "conversion-in-the-inner-handler",
        "conversion-in-the-inner-finally",
        "class-body-runs-now",
        "default-argument-runs-now",
        "annotation-runs-now",
        "keyword-only-annotation-runs-now",
        "positional-only-annotation-runs-now",
        "varargs-annotation-runs-now",
        "kwargs-annotation-runs-now",
        "return-annotation-runs-now",
        "conversion-in-a-nested-async-function",
        "conversion-in-a-lambda",
        "other-module-alias",
        "foreign-import-alias",
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


def _saved_limits(statements: list[ast.stmt]) -> set[str]:
    """The names these statements (not nested ones) assign the result of sys.get_int_max_str_digits() to."""
    return {
        target.id
        for statement in statements
        if isinstance(statement, ast.Assign)
        and isinstance(statement.value, ast.Call)
        and ast.unparse(statement.value.func).endswith(_DIGIT_LIMIT_GETTER)
        for target in statement.targets
        if isinstance(target, ast.Name)
    }


def _is_yield(statement: ast.stmt) -> bool:
    return isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Yield)


def _is_setter_call(statement: ast.stmt) -> bool:
    return isinstance(statement, ast.Expr) and isinstance(call := statement.value, ast.Call) and _is_setter(call.func)


def _is_restore(statement: ast.stmt, saved: set[str]) -> bool:
    """Whether the statement is a call that sets the limit to one of the saved names."""
    return (
        isinstance(statement, ast.Expr)
        and isinstance(call := statement.value, ast.Call)
        and _is_setter(call.func)
        and len(call.args) == 1
        and isinstance(call.args[0], ast.Name)
        and call.args[0].id in saved
    )


def _may_exit(statement: ast.stmt) -> bool:
    """Whether a return or raise in the statement can leave the function; one in a nested function cannot.

    A class body runs where the class is defined, so a raise in it does leave the function.
    """
    pending: list[ast.AST] = [statement]
    while pending:
        node = pending.pop()
        if isinstance(node, ast.Return | ast.Raise):
            return True
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
            pending.extend(ast.iter_child_nodes(node))
    return False


def _spared_setters(function: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.stmt]:
    """The setter calls of a function that saves the limit before its yield and always sets it back after it.

    Saving, yield and restore must be statements of the function body itself, or the yield sits in a try whose
    finally restores; a restore in a branch, a loop or a nested function may not run, so it does not count. A save
    after the first setter would keep the changed limit, so only a save before it counts. Only the setter calls of
    the body before the yield and the restores are spared; any other setter in the function still counts.
    """
    body = function.body
    first_setter = next(
        (index for index, statement in enumerate(body) if any(_is_setter(node) for node in ast.walk(statement))),
        len(body),
    )
    for index, statement in enumerate(body):
        saved = _saved_limits(body[: min(index, first_setter)])
        if _is_yield(statement):
            after = body[index + 1 :]
        elif isinstance(statement, ast.Try) and any(_is_yield(inner) for inner in statement.body):
            after = [*statement.finalbody, *body[index + 1 :]]
        else:
            continue
        # A return or raise before the restore may skip it, so only restores before the first one count.
        first_exit = next((position for position, later in enumerate(after) if _may_exit(later)), len(after))
        restores = [restore for restore in after[:first_exit] if _is_restore(restore, saved)]
        return [*filter(_is_setter_call, body[:index]), *restores] if restores else []
    return []


def _spared_setter_lines(tree: ast.AST) -> set[int]:
    """The lines of the setter calls a fixture restores after its yield, so the test it serves keeps no change."""
    return {
        line
        for function in ast.walk(tree)
        if isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef) and _is_fixture(function)
        for statement in _spared_setters(function)
        for line in range(statement.lineno, (statement.end_lineno or statement.lineno) + 1)
    }


def _digit_limit_setters(source: str, filename: str, *, fixtures_allowed: bool = False) -> list[str]:
    """file:line of each reference to sys.set_int_max_str_digits; fixtures_allowed spares a restoring fixture."""
    tree = ast.parse(source, filename)
    allowed = _spared_setter_lines(tree) if fixtures_allowed else set()
    return [f"{filename}:{node.lineno}" for node in ast.walk(tree) if _is_setter(node) and node.lineno not in allowed]


def _digit_limit_callers(root: Path) -> list[str]:
    """Each setter reference under root; only a restoring fixture in root/conftest.py is spared, not a nested one."""
    return [
        caller
        for path in sorted(root.rglob("*.py"))
        for caller in _digit_limit_setters(
            path.read_text(encoding="utf-8"),
            path.relative_to(root).as_posix(),
            fixtures_allowed=path == root / "conftest.py",
        )
    ]


def test_only_a_conftest_fixture_changes_the_int_digit_limit() -> None:
    """The limit is global to the interpreter: a test that sets it without restoring it changes every later test."""
    callers = _digit_limit_callers(_TESTS)
    assert not callers, "use the int_max_str_digits fixture instead of sys.set_int_max_str_digits: " + ", ".join(
        callers
    )


def test_the_guard_spares_the_top_conftest_fixture_only(tmp_path: Path) -> None:
    (tmp_path / "conftest.py").write_text(_RESTORING_FIXTURE, encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "conftest.py").write_text(_RESTORING_FIXTURE, encoding="utf-8")

    assert _digit_limit_callers(tmp_path) == ["sub/conftest.py:4", "sub/conftest.py:6"]


def test_the_guard_recognises_the_real_restoring_fixture() -> None:
    """Without this, a guard that no longer sees the fixture would only show up as a failing scan."""
    tree = ast.parse((_TESTS / "conftest.py").read_text(encoding="utf-8"))
    (fixture,) = (
        node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "int_max_str_digits"
    )
    assert len(_spared_setters(fixture)) == 2  # the setter before the yield and the restore after it


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
        (
            "@pytest.fixture\ndef limit():\n    sys.set_int_max_str_digits(4300)\n    yield\n"
            "    previous = sys.get_int_max_str_digits()\n    sys.set_int_max_str_digits(previous)\n",
            ["m.py:3", "m.py:6"],
        ),
        (
            _RESTORING_FIXTURE.replace(
                "    sys.set_int_max_str_digits(previous)\n",
                "    if flag:\n        sys.set_int_max_str_digits(previous)\n",
            ),
            ["m.py:4", "m.py:7"],
        ),
        (
            _RESTORING_FIXTURE.replace(
                "    yield\n    sys.set_int_max_str_digits(previous)\n",
                "    try:\n        yield\n    finally:\n        sys.set_int_max_str_digits(previous)\n",
            ),
            [],
        ),
        (
            _RESTORING_FIXTURE.replace(
                "    previous = sys.get_int_max_str_digits()\n    sys.set_int_max_str_digits(4300)\n",
                "    sys.set_int_max_str_digits(4300)\n    previous = sys.get_int_max_str_digits()\n",
            ),
            ["m.py:3", "m.py:6"],
        ),
        (_RESTORING_FIXTURE + "    sys.set_int_max_str_digits(0)\n", ["m.py:7"]),
        (
            _RESTORING_FIXTURE.replace(
                "    yield\n", "    if flag:\n        sys.set_int_max_str_digits(0)\n    yield\n"
            ),
            ["m.py:6"],
        ),
        (
            _RESTORING_FIXTURE.replace("    yield\n", "    yield\n    if flag:\n        return\n"),
            ["m.py:4", "m.py:8"],
        ),
        (
            _RESTORING_FIXTURE.replace("    yield\n", "    yield\n    if flag:\n        raise ValueError\n"),
            ["m.py:4", "m.py:8"],
        ),
        (
            _RESTORING_FIXTURE.replace(
                "    yield\n    sys.set_int_max_str_digits(previous)\n",
                "    try:\n        yield\n        return\n    finally:\n        sys.set_int_max_str_digits(previous)\n",
            ),
            [],
        ),
        (
            _RESTORING_FIXTURE.replace(
                "    yield\n",
                "    yield\n    def helper():\n        return 1\n    async def other():\n        raise ValueError\n",
            ),
            [],
        ),
        (
            _RESTORING_FIXTURE.replace("    yield\n", "    yield\n    class Helper:\n        raise ValueError\n"),
            ["m.py:4", "m.py:8"],
        ),
        (
            _RESTORING_FIXTURE.replace("    yield\n", "    yield\n    with context:\n        return\n"),
            ["m.py:4", "m.py:8"],
        ),
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
        "saves-the-limit-after-yield",
        "restore-in-a-branch",
        "restore-in-finally",
        "saves-the-limit-after-setting-it",
        "another-setter-after-the-restore",
        "another-setter-in-a-branch",
        "return-before-the-restore",
        "raise-before-the-restore",
        "return-in-the-try-body",
        "return-in-a-nested-function",
        "raise-in-a-class-body",
        "return-in-a-with-block",
    ],
)
def test_the_guard_spares_only_a_fixture_that_restores_the_digit_limit(source: str, expected: list[str]) -> None:
    assert _digit_limit_setters(source, "m.py", fixtures_allowed=True) == expected
