"""A `pytest.raises` block makes at most one call, so only the tested call can satisfy it (SonarCloud S5778).

A block with no explicit call passes on purpose: 20 of the 417 blocks of this suite have none and are intended
(`await request`, `rights[1] = 3`, where the call sits in the awaitable or the operator), so "exactly one" would
flag them. With a second call in the block - a second statement, but also a nested call like `check(build(x))` or
a context manager entered (its `__enter__`) after `pytest.raises` in the same `with` - an unexpected exception from
the other call satisfies the expected one and hides a failure of the tested call. Ruff's PT012 only counts
statements; this test counts the calls, so the nested form is caught as well. Build the arguments before the `with`
and keep only the tested call inside it.

A function or lambda defined in the block is not run by defining it, so the calls in its body do not count (its
decorators, defaults and annotations do, annotations even where Python defers or never evaluates them, and so does
applying a decorator); a lambda passed as a callback runs inside the tested call and is part of it. A call in an
`as` target (`as box[key()]`) runs inside the block as well. A class defined in the block counts as one call:
creating it calls `type`, which can raise by itself (`__slots__ = ("x", "x")`) and can run a base's
`__init_subclass__` or the metaclass. A generator expression's calls do count - stricter than needed (its body may
never run), but it fails loudly. A `pytest.raises(...)` call bound to a name first and entered as
`with expectation:` is followed, module-wide: a name that is rebound to another manager or reused for one in
another function still counts as the raises manager. A manager entered before `pytest.raises` in the same `with`
is outside it (it exits after `pytest.raises` has), so its calls do not count. A bare `raises` or any `.raises(...)`
is read as pytest's: stricter than needed, but a hit fails loudly with file and line. Not followed: such a manager
handed in as a parametrize argument, wrapped in a call (`with wrap(expectation):`), picked by a conditional
(`with expectation if enabled else nullcontext():`) or taken from a container and entered by a loop variable
(`for c in cases: with c:`), an alias handed on by a tuple that a name holds (`pair = (pytest.raises, 1)`, then
`expect, _ = pair`) or by a parameter default (`def helper(expect=pytest.raises)`), a local helper called in the
block that itself makes several calls, a `raises` alias that sits where a star covers (`expect, other = *items,
pytest.raises` binds only `other`: how far a star reaches is not known statically, so only the elements before the
first and after the last star pair up) and one that is rebound to something else later in the file (the name stays
an alias), and the calls Python makes implicitly (a property or descriptor access, a descriptor's `__set_name__`
while a class is made, an iterator protocol call): they cannot be seen without types - the review checklist covers
those.
"""

import ast
from collections.abc import Iterable
from itertools import takewhile
from pathlib import Path

TESTS = Path(__file__).parent
# Every module, not only pytest's python_files patterns: a helper in conftest.py can hold a raises block too.
TEST_FILE_PATTERNS = ("*.py",)
_DEFERRED = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
# Entering a context manager calls its __enter__ and leaving it its __exit__.
_ENTER_EXIT = 2
_RAISES = "raises"
# The first statement the detector cases report for a block that holds the nested call `check(build(1))`.
_NESTED = "check(build(1))"


def _is_star(elt: ast.expr) -> bool:
    return isinstance(elt, ast.Starred)


def _unstarred_run(pairs: Iterable[tuple[ast.expr, ast.expr]]) -> list[tuple[ast.expr, ast.expr]]:
    """The pairs up to the first one that has a starred element on either side."""
    return list(takewhile(lambda pair: not any(map(_is_star, pair)), pairs))


def _element_pairs(targets: list[ast.expr], values: list[ast.expr]) -> list[tuple[ast.expr, ast.expr]]:
    """Which target element takes which value element, as far as it is certain.

    Without a star the lengths must match. With stars, the elements before the first star pair from the front and
    the ones after the last star from the back; what a star covers is not known statically. A side without a star
    that is too short for the other side's unstarred elements would fail at runtime: nothing is paired.
    """
    targets_fixed, values_fixed = not any(map(_is_star, targets)), not any(map(_is_star, values))
    if targets_fixed and values_fixed:
        return list(zip(targets, values, strict=True)) if len(targets) == len(values) else []
    if (targets_fixed and len(targets) < sum(not _is_star(v) for v in values)) or (
        values_fixed and len(values) < sum(not _is_star(t) for t in targets)
    ):
        return []
    front = _unstarred_run(zip(targets, values, strict=False))
    back = _unstarred_run(zip(reversed(targets), reversed(values), strict=False))
    return [*front, *reversed(back)]


def _paired(target: ast.expr, value: ast.expr) -> list[tuple[ast.expr, ast.expr]]:
    """(target, value) pairs of one assignment; `a, b = x, y` binds a to x and b to y.

    A starred element is allowed on either side (`a, b = *x, y` binds b to y, `a, *m, b = x, y, z` binds a to x and
    b to z); the elements a star covers pair nothing. Where nothing can be paired, the whole assignment comes back
    as one (target, value) pair, which binds no name.
    """
    if not (isinstance(target, ast.Tuple | ast.List) and isinstance(value, ast.Tuple | ast.List)):
        return [(target, value)]
    pairs = _element_pairs(target.elts, value.elts)
    return [pair for t, v in pairs for pair in _paired(t, v)] if pairs else [(target, value)]


def _bindings(tree: ast.Module) -> list[tuple[ast.expr, ast.expr]]:
    """(target, value) of every assignment, annotated assignment and walrus in the module."""
    pairs: list[tuple[ast.expr, ast.expr]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                pairs.extend(_paired(target, node.value))
        elif isinstance(node, ast.AnnAssign | ast.NamedExpr) and node.value:
            pairs.append((node.target, node.value))
    return pairs


def _raises_names(tree: ast.Module) -> set[str]:
    """Bare names `pytest.raises` is bound to: `raises`, any `from pytest import raises as <alias>` and any
    `<alias> = pytest.raises` (annotated, walrus, or one element of a tuple assignment) or `<alias> = <a name already
    known>` binding, in whatever order they appear in the file."""
    names = {_RAISES}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "pytest":
            names.update(alias.asname for alias in node.names if alias.name == _RAISES and alias.asname)
    bindings = _bindings(tree)
    known = 0
    while known != len(names):
        known = len(names)
        for target, value in bindings:
            if isinstance(target, ast.Name) and (
                (isinstance(value, ast.Attribute) and value.attr == _RAISES)
                or (isinstance(value, ast.Name) and value.id in names)
            ):
                names.add(target.id)
    return names


def _is_raises_call(node: ast.AST, raises_names: set[str]) -> bool:
    """`pytest.raises(...)` (any module alias) or a bare name bound to it."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    return (isinstance(func, ast.Attribute) and func.attr == _RAISES) or (
        isinstance(func, ast.Name) and func.id in raises_names
    )


def _prebuilt_managers(tree: ast.Module, raises_names: set[str]) -> set[str]:
    """Names a `pytest.raises(...)` call is bound to (or another such name), entered later as `with <name>:`.

    A raises call anywhere in the bound value counts, as in a with-item: a conditional
    `m = pytest.raises(E) if x else nullcontext()` is followed, and so is one inside an argument
    (`m = make(raises(E))`), an over-reach that fails loudly. A name counts module-wide, whatever function it is
    bound in.
    """
    bindings = [(target.id, value) for target, value in _bindings(tree) if isinstance(target, ast.Name)]
    managers: set[str] = set()
    known = -1
    while known != len(managers):
        known = len(managers)
        managers.update(
            name
            for name, value in bindings
            if any(_is_raises_call(sub, raises_names) for sub in ast.walk(value))
            or (isinstance(value, ast.Name) and value.id in managers)
        )
    return managers


def _executed_calls(node: ast.AST) -> int:
    """Count the calls run when `node` runs, skipping the bodies of functions and lambdas it only defines.

    Their decorators, default values and annotations do run at definition, and a lambda called on the spot runs
    its body.
    """
    if isinstance(node, _DEFERRED):
        run_now = [*node.args.defaults, *(default for default in node.args.kw_defaults if default)]
        applied = 0
        if not isinstance(node, ast.Lambda):
            args = node.args
            params = [*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg]
            annotations = [param.annotation for param in params if param and param.annotation]
            run_now += [*node.decorator_list, *annotations, *([node.returns] if node.returns else [])]
            applied = len(node.decorator_list)  # applying a decorator is a call too
        return applied + sum(_executed_calls(expr) for expr in run_now)
    calls = sum(_executed_calls(child) for child in ast.iter_child_nodes(node))
    if isinstance(node, ast.ClassDef):
        # Applying a class decorator is a call too, and so is creating the class: `type(...)` can raise by itself
        # and runs a base's __init_subclass__ or the metaclass.
        calls += len(node.decorator_list) + 1
    if isinstance(node, ast.With | ast.AsyncWith):
        calls += _ENTER_EXIT * len(node.items)
    if isinstance(node, ast.Call):
        calls += 1
        if isinstance(node.func, ast.Lambda):
            calls += _executed_calls(node.func.body)
    return calls


def _raises_blocks(source: str) -> list[tuple[int, int, str]]:
    """Return (line, call count, first statement) of each `with` block that expects a raise.

    A with-item counts when a raises call appears anywhere in it, so a conditional
    `pytest.raises(E) if fails else nullcontext()` is checked too. The count covers the body and the with-items
    entered after that item, since pytest.raises catches what they raise as well.
    """
    tree = ast.parse(source)
    raises_names = _raises_names(tree)
    managers = _prebuilt_managers(tree, raises_names)
    blocks = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.With | ast.AsyncWith):
            continue
        raises_at = next(
            (
                index
                for index, item in enumerate(node.items)
                if (isinstance(item.context_expr, ast.Name) and item.context_expr.id in managers)
                or any(_is_raises_call(sub, raises_names) for sub in ast.walk(item.context_expr))
            ),
            None,
        )
        if raises_at is None:
            continue
        later_items = node.items[raises_at + 1 :]
        # A later item runs its __enter__ and __exit__ inside pytest.raises, even for a manager built before the `with`.
        calls = sum(_ENTER_EXIT + _executed_calls(item.context_expr) for item in later_items)
        # The `as` targets are assigned after the manager is entered: a call in one (`as box[key()]`) runs inside too.
        calls += sum(_executed_calls(item.optional_vars) for item in node.items[raises_at:] if item.optional_vars)
        calls += sum(_executed_calls(stmt) for stmt in node.body)
        blocks.append((node.lineno, calls, ast.unparse(node.body[0])))
    return blocks


def _extra_call_blocks(source: str) -> list[tuple[int, str]]:
    return [(line, stmt) for line, calls, stmt in _raises_blocks(source) if calls > 1]


def test_detector_flags_nested_and_sequential_calls() -> None:
    nested = "with pytest.raises(ValueError):\n    check(build(1))\n"
    sequential = "with pytest.raises(ValueError):\n    x = build(1)\n    check(x)\n"
    bare = "with raises(ValueError):\n    check(build(1))\n"
    aliased = "from pytest import raises as expect\nwith expect(ValueError):\n    check(build(1))\n"
    conditional = "with pytest.raises(ValueError) if fails else nullcontext():\n    check(build(1))\n"
    later_item = "with pytest.raises(ValueError), pause():\n    check(x)\n"
    later_prebuilt = "with pytest.raises(ValueError), pause:\n    check(x)\n"
    nested_manager = "with pytest.raises(ValueError):\n    with pause:\n        check(x)\n"
    in_async = "async def t():\n    async with pytest.raises(ValueError):\n        await check(build(1))\n"
    single = "x = build(1)\nwith pytest.raises(ValueError):\n    check(x)\n"
    earlier_item = "with pause(), pytest.raises(ValueError):\n    check(x)\n"
    deferred = "with pytest.raises(ValueError):\n    check(lambda: build(1))\n"
    called_lambda = "with pytest.raises(ValueError):\n    (lambda: build(1))()\n"
    default_arg = "with pytest.raises(ValueError):\n    def g(x=build(1)):\n        pass\n    check(g)\n"
    decorated = "with pytest.raises(ValueError):\n    @wrap(1)\n    def g():\n        pass\n"
    annotated = "with pytest.raises(ValueError):\n    def g(x: kind(1)) -> kind(2):\n        pass\n"
    other_context = "with patch('a.b'):\n    check(build(1))\n"
    assigned = "expect = pytest.raises\nwith expect(ValueError):\n    check(build(1))\n"
    annotated_alias = "expect: object = pytest.raises\nwith expect(ValueError):\n    check(build(1))\n"
    forward_chain = "alias = source\nsource = pytest.raises\nwith alias(ValueError):\n    check(build(1))\n"
    walrus = "(expect := pytest.raises)\nwith expect(ValueError):\n    check(build(1))\n"
    paired = "other, expect = 1, pytest.raises\nwith expect(ValueError):\n    check(build(1))\n"
    nested_pair = "(expect, other), last = (pytest.raises, 1), 2\nwith expect(ValueError):\n    check(build(1))\n"
    other_slot = "other, expect = pytest.raises, 1\nwith expect(ValueError):\n    check(build(1))\n"
    unequal_length = "expect, other = pytest.raises, 1, 2\nwith expect(ValueError):\n    check(build(1))\n"
    class_decorated = "with pytest.raises(ValueError):\n    @wrap(1)\n    class C:\n        pass\n"
    prebuilt = "expectation = pytest.raises(ValueError)\nwith expectation:\n    check(build(1))\n"
    prebuilt_chain = "first = pytest.raises(ValueError)\nsecond = first\nwith second:\n    check(build(1))\n"
    prebuilt_alias = "expect = pytest.raises\nmanager = expect(ValueError)\nwith manager:\n    check(build(1))\n"
    prebuilt_single = "expectation = pytest.raises(ValueError)\nwith expectation:\n    check(x)\n"
    other_manager = "manager = make(ValueError)\nwith manager:\n    check(build(1))\n"
    class_hook = "with pytest.raises(ValueError):\n    class C(Base):\n        pass\n    check(C)\n"
    class_meta = "with pytest.raises(ValueError):\n    class C(metaclass=Meta):\n        pass\n    check(C)\n"
    class_plain = "with pytest.raises(ValueError):\n    class C:\n        pass\n    check(C)\n"
    class_alone = "with pytest.raises(ValueError):\n    class C(Base):\n        pass\n"
    class_plain_alone = "with pytest.raises(ValueError):\n    class C:\n        pass\n"
    star_value_head = "other, expect = *items, pytest.raises\nwith expect(ValueError):\n    check(build(1))\n"
    star_value_tail = "expect, *rest = pytest.raises, 1, 2\nwith expect(ValueError):\n    check(build(1))\n"
    star_target_tail = "*rest, expect = 1, 2, pytest.raises\nwith expect(ValueError):\n    check(build(1))\n"
    star_value_covered = "expect, other = *items, pytest.raises\nwith expect(ValueError):\n    check(build(1))\n"
    star_target_covered = "*rest, expect = pytest.raises, 1, 2\nwith expect(ValueError):\n    check(build(1))\n"
    star_value_slot = "expect, other = *items, pytest.raises\nwith other(ValueError):\n    check(build(1))\n"
    star_both = "*a, b = *x, pytest.raises\nwith b(ValueError):\n    check(build(1))\n"
    star_both_covered = "a, *b = *x, pytest.raises\nwith a(ValueError):\n    check(build(1))\n"
    star_both_starred_name = "a, *b = *x, pytest.raises\nwith b(ValueError):\n    check(build(1))\n"
    star_target_starred_name = "*rest, expect = pytest.raises, 1, 2\nwith rest(ValueError):\n    check(build(1))\n"
    star_mid_head = "a, *mid, b = pytest.raises, 1, 2\nwith a(ValueError):\n    check(build(1))\n"
    star_mid_tail = "a, *mid, b = 1, 2, pytest.raises\nwith b(ValueError):\n    check(build(1))\n"
    star_mid_covered = "a, *mid, b = 1, pytest.raises, 2\nwith mid(ValueError):\n    check(build(1))\n"
    star_list = "[expect, *rest] = pytest.raises, 1\nwith expect(ValueError):\n    check(build(1))\n"
    star_nested = "(expect, other), *rest = (pytest.raises, 1), 2, 3\nwith expect(ValueError):\n    check(build(1))\n"
    star_empty = "a, b, *rest = pytest.raises, 1\nwith a(ValueError):\n    check(build(1))\n"
    # Too short to unpack at runtime: only the length check keeps the pairing from running past the value's end.
    star_too_short = "a, b, c, *rest = pytest.raises, 1\nwith a(ValueError):\n    check(build(1))\n"
    star_too_short_tail = "a, *rest, b, c = pytest.raises, 1\nwith a(ValueError):\n    check(build(1))\n"
    class_in_def = (
        "with pytest.raises(ValueError):\n    def g():\n        class C(Base):\n            pass\n    check(g)\n"
    )
    prebuilt_tuple = "expectation, other = pytest.raises(ValueError), 1\nwith expectation:\n    check(build(1))\n"
    prebuilt_later = "expectation = pytest.raises(ValueError)\nwith expectation, pause():\n    check(x)\n"
    prebuilt_target = "expectation = pytest.raises(ValueError)\nwith expectation as box[key()]:\n    check(x)\n"
    prebuilt_conditional = (
        "expectation = pytest.raises(ValueError) if fails else nullcontext()\nwith expectation:\n    check(build(1))\n"
    )
    prebuilt_nested_call = "manager = make(pytest.raises(ValueError))\nwith manager:\n    check(build(1))\n"
    prebuilt_looped = "cases = [pytest.raises(A)]\nfor c in cases:\n    with c:\n        check(build(1))\n"
    prebuilt_wrapped = "expectation = pytest.raises(ValueError)\nwith wrap(expectation):\n    check(build(1))\n"
    target_call = "with pytest.raises(ValueError) as box[key()]:\n    check(x)\n"
    plain_target = "with pytest.raises(ValueError) as info:\n    check(x)\n"

    assert _extra_call_blocks(nested) == [(1, _NESTED)]
    assert _extra_call_blocks(sequential) == [(1, "x = build(1)")]
    assert _extra_call_blocks(bare) == [(1, _NESTED)]
    assert _extra_call_blocks(aliased) == [(2, _NESTED)]
    assert _extra_call_blocks(conditional) == [(1, _NESTED)]
    assert _extra_call_blocks(assigned) == [(2, _NESTED)]
    assert _extra_call_blocks(annotated_alias) == [(2, _NESTED)]
    assert _extra_call_blocks(forward_chain) == [(3, _NESTED)]
    assert _extra_call_blocks(walrus) == [(2, _NESTED)]
    assert _extra_call_blocks(paired) == [(2, _NESTED)]
    assert _extra_call_blocks(nested_pair) == [(2, _NESTED)]
    assert _extra_call_blocks(star_value_head) == [(2, _NESTED)]
    assert _extra_call_blocks(star_value_tail) == [(2, _NESTED)]
    assert _extra_call_blocks(star_target_tail) == [(2, _NESTED)]
    assert _extra_call_blocks(star_empty) == [(2, _NESTED)]
    assert _extra_call_blocks(star_value_slot) == [(2, _NESTED)]
    assert _extra_call_blocks(star_both) == [(2, _NESTED)]
    assert _extra_call_blocks(star_mid_head) == [(2, _NESTED)]
    assert _extra_call_blocks(star_mid_tail) == [(2, _NESTED)]
    assert _extra_call_blocks(star_list) == [(2, _NESTED)]
    assert _extra_call_blocks(star_nested) == [(2, _NESTED)]
    assert [line for line, _ in _extra_call_blocks(class_plain)] == [1]  # creating a class is a call
    assert [line for line, _ in _extra_call_blocks(class_decorated)] == [1]
    assert _extra_call_blocks(prebuilt) == [(2, _NESTED)]
    assert _extra_call_blocks(prebuilt_chain) == [(3, _NESTED)]
    assert _extra_call_blocks(prebuilt_alias) == [(3, _NESTED)]
    assert [line for line, _ in _extra_call_blocks(class_hook)] == [1]
    assert [line for line, _ in _extra_call_blocks(class_meta)] == [1]
    assert _extra_call_blocks(prebuilt_conditional) == [(2, _NESTED)]
    assert _extra_call_blocks(prebuilt_nested_call) == [(2, _NESTED)]  # over-reach, see _prebuilt_managers
    assert _extra_call_blocks(prebuilt_tuple) == [(2, _NESTED)]
    assert _extra_call_blocks(prebuilt_later) == [(2, "check(x)")]
    assert _extra_call_blocks(prebuilt_target) == [(2, "check(x)")]
    assert _extra_call_blocks(target_call) == [(1, "check(x)")]
    assert _extra_call_blocks(later_item) == [(1, "check(x)")]
    assert _extra_call_blocks(later_prebuilt) == [(1, "check(x)")]
    assert [line for line, _ in _extra_call_blocks(nested_manager)] == [1]
    assert _extra_call_blocks(in_async) == [(2, "await check(build(1))")]
    assert _extra_call_blocks(called_lambda) == [(1, "(lambda: build(1))()")]
    assert [line for line, _ in _extra_call_blocks(default_arg)] == [1]
    assert [line for line, _ in _extra_call_blocks(decorated)] == [1]
    assert [line for line, _ in _extra_call_blocks(annotated)] == [1]
    assert not _extra_call_blocks(single)
    assert not _extra_call_blocks(earlier_item)
    assert not _extra_call_blocks(deferred)
    assert not _extra_call_blocks(other_context)
    assert not _extra_call_blocks(other_slot)
    assert not _extra_call_blocks(unequal_length)  # no pairing, and no crash from zip(strict=True)
    assert not _extra_call_blocks(plain_target)
    assert not _extra_call_blocks(prebuilt_single)
    assert not _extra_call_blocks(other_manager)
    assert not _extra_call_blocks(class_plain_alone)
    assert not _extra_call_blocks(class_alone)
    assert not _extra_call_blocks(star_value_covered)  # the star reaches the slot of `expect`
    assert not _extra_call_blocks(star_target_covered)
    assert not _extra_call_blocks(star_both_covered)  # the star of `*b` takes the slot of raises
    assert not _extra_call_blocks(star_both_starred_name)  # the starred name itself is never bound to raises
    assert not _extra_call_blocks(star_target_starred_name)
    assert not _extra_call_blocks(star_mid_covered)  # the starred middle is never paired
    assert not _extra_call_blocks(star_too_short)  # no pairing
    assert not _extra_call_blocks(star_too_short_tail)
    assert not _extra_call_blocks(class_in_def)  # a class made only when g runs: its creation is not part of the block
    assert not _extra_call_blocks(prebuilt_wrapped)  # not followed, see the docstring
    assert not _extra_call_blocks(prebuilt_looped)  # not followed, see the docstring


def test_every_pytest_raises_block_makes_one_call() -> None:
    files = sorted({path for pattern in TEST_FILE_PATTERNS for path in TESTS.rglob(pattern)})
    blocks = [(path, block) for path in files for block in _raises_blocks(path.read_text(encoding="utf-8"))]
    offenders = [
        f"{path.relative_to(TESTS.parent)}:{line}: {stmt}" for path, (line, calls, stmt) in blocks if calls > 1
    ]

    # The scan must see the tests it guards, or a moved directory turns it vacuously green.
    assert files
    assert blocks
    assert not offenders, "pytest.raises block with more than one call:\n" + "\n".join(offenders)
