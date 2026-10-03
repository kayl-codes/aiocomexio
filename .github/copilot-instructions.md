# Copilot instructions — aiocomexio

Standalone, asyncio-native Python client library for the Comexio IO-Server (local building-automation controller),
published on PyPI and consumed by the Home Assistant integration
[`kayl-codes/homeassistant-comexio`](https://github.com/kayl-codes/homeassistant-comexio). Python ≥ 3.13, typed
(`py.typed`), mypy strict. `CLAUDE.md` in the repo root is the reference for these rules.

## Code review focus

- Start each review comment with a one-sentence summary of the suggested fix.
- Focus on correctness: logic errors, unhandled edge cases in parsing/scraping of Comexio responses, resource
  leaks, silent failures (swallowed exceptions, fallbacks that hide missing or malformed data), and async misuse.
- Do **not** comment on formatting, import order or line length — ruff (`ruff check` + `ruff format`, line length
  120, rule sets ASYNC, B, C4, E, F, I, SIM, UP, W) and mypy strict enforce those in CI.
- Where a contract guarantees a dict key exists, prefer `data["key"]` over `data.get("key")` so contract violations
  surface instead of being masked.

## Library rules to check

- **No Home Assistant dependency:** nothing may `import homeassistant.*`, read `hass.config` or a `ConfigEntry`.
  Consumer settings come in as explicit parameters (e.g. `ParseOptions`).
- **Injected session:** the caller passes an `aiohttp.ClientSession`; the library never creates its own session.
- **Errors:** failures are raised as typed exceptions from `aiocomexio.exceptions`, not returned as error codes,
  `None` sentinels or empty results that hide the failure.
- **Public API:** fully typed; flag breaking changes to public names or signatures that are not called out in the PR.
- **Complexity:** cognitive complexity ≤ 15 per function (SonarQube S3776); no duplicated string literals — extract
  constants (S1192).
- **Dependencies:** a dependency change must come with an updated `uv.lock` (OSV-Scanner scans the lock file).

## Tests

- Test data is synthetic, never real installation data. Every bug fix in pure logic needs a regression test that
  fails without the fix.
- Flag deleted or loosened assertions and snapshot updates that are not explained in the PR.

## GitHub Actions

- Pin actions to full commit SHAs with a `# vX.Y.Z` comment; every workflow has a least-privilege `permissions:`
  block; secrets go through `env:`, never inline in `run:`.
- Fork-controlled code must never run with `SONAR_TOKEN`: the `pull_request` job only builds coverage and PR
  metadata, and the trusted `workflow_run` scan (`sonar-pr.yml`) uses the token but treats that metadata as
  untrusted input and never installs or executes code from the fork. PyPI publishing uses Trusted Publishing
  restricted to `v*` tags.
