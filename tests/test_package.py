"""Smoke tests for the package metadata."""

import tomllib
from importlib.metadata import version
from importlib.resources import files
from pathlib import Path

import aiocomexio

PYPROJECT = Path(__file__).parent.parent / "pyproject.toml"


def test_version_matches_pyproject() -> None:
    # Catches a stale (editable) install whose metadata lags behind pyproject.toml.
    declared = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["version"]
    assert aiocomexio.__version__ == declared == version("aiocomexio")


def test_package_ships_py_typed_marker() -> None:
    assert files("aiocomexio").joinpath("py.typed").is_file()
