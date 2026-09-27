# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

`aiocomexio` is a standalone, asyncio-native Python client library for the Comexio IO-Server (local
building-automation controller). It has **zero dependency on Home Assistant** — nothing in this repo
may `import homeassistant.*`. It is the communication layer split out of `api.py` in
[`kayl-codes/homeassistant-comexio`](https://github.com/kayl-codes/homeassistant-comexio), which will
consume it as a PyPI dependency (prerequisite for a later Home Assistant Core submission).

## Status

Scaffold only (v0.0.1): packaging, CI and quality gates are in place, no client code yet. Planned
order of the move:

1. Pure parsing/building logic (config parsing, Web-IO command builders, KNX DPT handling, function
   plan diff/render/analysis) together with its unit tests, snapshots and synthetic fixtures from the
   integration's `tests/unit/` and `tests/fixtures/comexio/`.
2. The client itself (RSA admin login, config scraping, Web-IO lifecycle, API writes), decoupled from
   Home Assistant: the caller injects an `aiohttp.ClientSession` (HA Core requirement — never create
   a session inside the library), CPU-bound work is not pushed to a HA executor, and nothing reads
   `hass.config` or a `ConfigEntry`.

## Commands

```bash
pip install -e ".[dev]"   # once
ruff check .              # lint (ASYNC, B, C4, E, F, I, SIM, UP, W; py313, line-length 120)
ruff format --check .     # formatting check; drop --check to auto-format
mypy                      # strict, blocking in CI
pytest                    # tests; pytest --cov=aiocomexio for coverage
pre-commit run --all-files
```

- Python: `requires-python = ">=3.13"`; CI tests 3.13 and 3.14.
- Dev tool versions are pinned in `pyproject.toml` `[dev]`; keep `ruff` in sync with the
  `ruff-pre-commit` rev in `.pre-commit-config.yaml`.
- `uv.lock` holds the fully resolved dependency set and is what OSV-Scanner scans (it cannot read
  `pyproject.toml`). After changing dependencies run `uv lock` (the `uv-lock` pre-commit hook does it
  automatically; CI fails on a stale lock via `uv lock --check`).

## Code quality rules

- ruff-clean, mypy-strict-clean, all tests green before a change counts as done (pre-commit hooks
  `mypy` and `pytest` enforce the same locally).
- Cognitive complexity ≤ 15 (SonarQube S3776); no duplicated string literals (S1192) — extract
  constants.
- A red test is a finding, not an obstacle — never delete or loosen an assertion to make it pass.
  Snapshots: regenerate with `pytest --snapshot-update` only for intended output changes and state the
  reviewed diff in the PR.
- Test data must be synthetic, never real installation data.
- Public API is typed (`py.typed`); failures are raised as typed exceptions, not returned as error
  codes.

## CI / quality gates

- `ci.yml`: ruff, mypy, pytest (3.13 + 3.14), Bandit.
- `build.yml` + `sonar-pr.yml`: SonarCloud scan with coverage (`kayl-codes_aiocomexio`, CI-based —
  Automatic Analysis is off in SonarCloud; PR scans run via `workflow_run` so fork PRs never see
  `SONAR_TOKEN`).
- `osv-scanner.yml`, `codeql.yml`, `zizmor.yml`: dependency, code and workflow security scanning.
- `pypi.yaml`: publishes to PyPI via Trusted Publishing when a GitHub release with a `v*` tag is
  published (environment `pypi`, restricted to `v*` tags).
- Actions are pinned to full commit SHAs with a `# vX.Y.Z` comment; every workflow has a
  least-privilege `permissions:` block.

## Workflow

Branch off `main` and open a PR (template in `.github/PULL_REQUEST_TEMPLATE.md`); Sourcery and
SonarCloud review every PR.
