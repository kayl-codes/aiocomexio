"""Shared test helpers (fixture loading)."""

import json
from pathlib import Path
from typing import Any

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> str:
    """Return the raw text of a fixture file under tests/fixtures/."""
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


def load_json_fixture(name: str) -> Any:
    """Return a JSON fixture under tests/fixtures/, parsed."""
    return json.loads(load_fixture(name))
