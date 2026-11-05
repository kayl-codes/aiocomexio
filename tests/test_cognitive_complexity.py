"""No new function exceeds cognitive complexity 15 (SonarCloud S3776), and no listed one grows.

SonarCloud only sees a function once the PR is scanned, so a too-complex function costs a bot round. complexipy
measures cognitive complexity locally, but not exactly like SonarCloud: it scores some functions higher and some
lower. The functions it already scores above the limit, while SonarCloud reports none of them, are listed in
`BASELINE` with their current score as a ceiling: they may get simpler, not more complex. Every other function stays
at or below `MAX_COMPLEXITY`. The cloud scan remains the binding gate - this test only catches most findings earlier.

Simplified a baseline function? Lower its entry, or drop it once within the limit; the test names stale ones.
"""

from __future__ import annotations

import sys
from pathlib import Path

import complexipy
import pytest

PACKAGE = Path(__file__).parents[1] / "src" / "aiocomexio"
MAX_COMPLEXITY = 15

# "<path below src/aiocomexio>::<function>": complexipy score at the time it was listed.
BASELINE = {
    "config.py::_ambiguous_ios": 16,
    "config.py::_process_device_info": 21,
    "function_plan/render_flow.py::_render_edges_svg": 16,
    "function_plan/render_wiring.py::_junction_points": 19,
    "function_plan/render_wiring.py::_render_net": 22,
}

_BUSY_FUNCTION = (
    "def busy(items, limit):{comment}\n"
    "    for item in items:\n"
    "        if item:\n"
    "            while limit:\n"
    "                limit -= 1\n"
    "    return limit\n"
)
_BUSY_SCORE = 6  # for +1, nested if +2, nested while +3


def _measured() -> list[tuple[str, int]]:
    """(key like `BASELINE`, complexipy score) of every function; a repeated name yields several.

    `no_ignore`: an ignore comment would otherwise drop its function from the scan, and `BASELINE` is the only
    permitted exception.
    """
    measured = []
    for path in sorted(PACKAGE.rglob("*.py")):
        prefix = path.relative_to(PACKAGE).as_posix()
        functions = complexipy.file_complexity(str(path), no_ignore=True).functions
        measured += [(f"{prefix}::{f.name}", f.complexity) for f in functions]
    return measured


def _scores() -> dict[str, int]:
    """complexipy score of every function, keyed like `BASELINE` (the highest one on a repeated name)."""
    scores: dict[str, int] = {}
    for key, score in _measured():
        scores[key] = max(scores.get(key, 0), score)
    return scores


def _too_complex(scores: dict[str, int]) -> list[str]:
    """Every function above its `BASELINE` ceiling, or above `MAX_COMPLEXITY` when it has none."""
    return [
        f"{key}: {score} (limit {BASELINE.get(key, MAX_COMPLEXITY)})"
        for key, score in sorted(scores.items())
        if score > BASELINE.get(key, MAX_COMPLEXITY)
    ]


def _stale_entries(scores: dict[str, int]) -> list[str]:
    """`BASELINE` entries whose ceiling is above the current score or within the limit; a score above the ceiling is
    the limit test's finding."""
    return [
        f"{key}: drop it" if scores.get(key, 0) <= MAX_COMPLEXITY else f"{key}: lower it to {scores[key]}"
        for key, ceiling in sorted(BASELINE.items())
        if scores.get(key, 0) < ceiling or ceiling <= MAX_COMPLEXITY
    ]


def test_no_function_exceeds_its_cognitive_complexity_limit() -> None:
    scores = _scores()
    too_complex = _too_complex(scores)

    # The scan must see the code it guards, or a moved directory turns it vacuously green.
    assert scores
    assert not too_complex, "Cognitive complexity above the limit (SonarCloud S3776) - split the function:\n" + (
        "\n".join(too_complex)
    )


def test_a_function_above_the_limit_or_its_ceiling_is_named(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys.modules[__name__], "BASELINE", {"listed": 18, "grown": 18})
    scores = {"new": MAX_COMPLEXITY + 1, "fine": MAX_COMPLEXITY, "listed": 18, "grown": 19}

    assert _too_complex(scores) == ["grown: 19 (limit 18)", "new: 16 (limit 15)"]


def test_no_baseline_ceiling_is_above_the_current_score() -> None:
    # A ceiling above the current score would let the function grow back unnoticed.
    stale = _stale_entries(_scores())

    assert not stale, "Update these BASELINE entries:\n" + "\n".join(stale)


def test_the_stale_check_lowers_or_drops_a_ceiling_but_never_raises_it(monkeypatch: pytest.MonkeyPatch) -> None:
    ceilings = {"grown": 16, "exact": 16, "simpler": 18, "at_limit": 16, "fixed": 17, "gone": 16, "listed_low": 15}
    scores = {"grown": 17, "exact": 16, "simpler": 16, "at_limit": 15, "fixed": 12, "listed_low": 15}
    monkeypatch.setattr(sys.modules[__name__], "BASELINE", ceilings)

    stale = _stale_entries(scores)

    assert stale == [
        "at_limit: drop it",
        "fixed: drop it",
        "gone: drop it",
        "listed_low: drop it",
        "simpler: lower it to 16",
    ]


@pytest.mark.parametrize("marker", ["# complexipy: ignore", "# noqa: complexipy"])
def test_an_inline_ignore_does_not_hide_a_function(
    marker: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "module.py").write_text(_BUSY_FUNCTION.format(comment=f"  {marker}"), encoding="utf-8")
    monkeypatch.setattr(sys.modules[__name__], "PACKAGE", tmp_path)

    assert _scores() == {"module.py::busy": _BUSY_SCORE}


def test_the_scan_scores_nesting(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A scorer that stopped counting would turn the limit test vacuously green.
    (tmp_path / "module.py").write_text(_BUSY_FUNCTION.format(comment=""), encoding="utf-8")
    monkeypatch.setattr(sys.modules[__name__], "PACKAGE", tmp_path)

    assert _scores() == {"module.py::busy": _BUSY_SCORE}


def test_baseline_names_are_unique_per_file() -> None:
    # A second function of the same name in the file would share the listed function's ceiling.
    keys = [key for key, _ in _measured()]
    shared = sorted({key for key in BASELINE if keys.count(key) > 1})

    assert not shared, "BASELINE names that more than one function in the file carries:\n" + "\n".join(shared)
