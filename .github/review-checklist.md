# Review checklist — aiocomexio

The local review agents check every diff against these rules before a push.

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
- **Strict types for parsed values:** values from JSON files or Comexio/PHP responses are type-checked strictly
  before use. Never coerce silently with `int()`/`float()`: `int(True)` is `1` and `int(1.5)` is `1`, so a bool
  or fractional float turns into a wrong id. Accept only the types the source really sends (e.g. `int` or an
  integer string), reject `bool` explicitly (it is a subclass of `int`), and treat anything else as malformed.
- **Every scraped value, keys included:** the strict check covers dict *keys* of a scraped structure as well as its
  values; a key is not trusted because it came out of the same answer. Python refuses to convert an `int` with more
  than 4300 digits: `json.loads` raises a plain `ValueError` for one (not `JSONDecodeError`), so a decode of scraped
  or received text catches `ValueError`. Data that does not come through `json.loads` (caller-built, regex
  captures) can still hold such an int, and `str()`/`repr()`/`_excerpt` raise on it, also inside a container: a type
  check alone does not help there, so the int is range-checked (e.g. a plausible id range) before it is rendered.
- **Name-keyed stores:** a dict or store keyed by a name (command name, device name, title) needs an explicit rule
  for two entries with the same name — report them, keep both under distinct keys, or reject — and that rule
  applies when stored data is read back as well, not only when new data is added. A plain assignment silently
  drops the first entry.
- **Public builders take every input kind:** when a diff adds a new kind of parser entry, or a tagged variant of an
  existing kind, every exported builder that accepts entries of that shape (the Web-IO command builders in
  `webio.py`) routes it correctly or raises a typed error. It must not quietly build the payload of a different kind.
- **Fallback guesses check their basis:** a *new* fallback that fills in a missing value with a guess (a default
  type, range or class) names what the guess is based on and logs it. A guess the data at hand cannot support is
  reported and the entry skipped, or a typed error raised, instead of emitting a plausible-looking but wrong value.
  The documented fallbacks — the generic KNX range when the DPT chain or its `KNX_DPT_ANALOG_RANGES` entry is
  missing, and the identifier-based IO classification when the type row is missing — are intentional and not
  findings unless the diff changes them.
- **One bad entry does not stop the other classes:** a filter or conversion that runs over every class (markers,
  IOs, KNX objects) must not fail on malformed data of one class: an invalid value falls back to a default with a
  warning instead of aborting the upload or build of all the others.
- **Public API:** fully typed; flag breaking changes to public names or signatures that are not called out in the PR.
- **Complexity:** cognitive complexity ≤ 15 per function (SonarQube S3776); no duplicated string literals — extract
  constants (S1192).
- **Dependencies:** a dependency change must come with an updated `uv.lock` (OSV-Scanner scans the lock file).

## Parsing Comexio answers, logging and error messages

- **Numbers from outside:** a float from Comexio or the caller is checked with `math.isfinite` before use; `nan`
  and `inf` pass every `isinstance` check and every `<`/`>` range check (a comparison with `nan` is always false).
  A handler that catches `ValueError` around `int()`/`float()` also catches `OverflowError`: `int(float("inf"))`
  and `float(10**400)` raise it, not `ValueError` (`tests/test_conventions.py` enforces this).
- **Digits in protocol regexes:** write `\d` (SonarQube S6353 wants it instead of `[0-9]`) and compile with
  `re.ASCII`; without it `\d` also matches non-ASCII digits such as `٣`, which Comexio never sends.
- **HTML forms are read with `html.parser`, not regexes:** a form field (a `<select>`, its `<option>`s, an `<input>`)
  is read as a browser reads it: attribute values unquoted and entities resolved, the first of two equal attribute
  names wins, a bare attribute is `""`, a `>` inside a quoted value does not end the tag. An element is matched by its
  exact `id`/`name` (case included), not a prefix, and the first element of an id counts. Markup inside a `<textarea>`
  or `<title>` is text, not a field (older 3.13 releases of `html.parser` report it as tags; `_FormParser` in
  `client.py` skips it). In a single `<select>` (no `multiple`) the last of several `selected` options counts, and
  without one the first enabled one (not `disabled`, not in a disabled `<optgroup>`) — unless its `size` makes it a
  list box, which selects none; a `<select multiple>` keeps every selected option and none when none is selected, so a
  field that holds one value reads it as unknown. An `<option>` without a `value` attribute has its text (whitespace
  collapsed) as value, and a `<select>`, `<input>` or `<textarea>` start tag inside an open select ends that select.
  The slash of a start tag is ignored: `<select/>` and `<option/>` open their element. A cut-off answer — a `<select>`
  without its `</select>`, so its options may be incomplete — reads as unknown, never as the first option: that would
  be a plausible-looking but wrong value. Void elements such as `<input>` have no end tag and are read as they are; a
  cut-off `<input>` tag is dropped, so its field reads as unknown too. A regex that tries to cover these cases
  outgrows SonarQube's regex complexity limit (S5843) and still misses some of them.
- **Live readings stay at DEBUG:** a WARNING or higher about an unexpected answer to a *value* request (live states,
  IO/marker values — anything that may wrap a reading of the installation) names only ids, keys, counts and the
  answer *type*; the content itself goes to DEBUG. Raw Comexio content in any log line or exception message goes
  through `_excerpt` in `client.py` (repr + length cap), never unbounded.
- **Scraped keys and names count as content:** the length cap above applies to dict keys and names used as ids as
  well, not only to values; `%r` alone quotes them but does not cap their length. `_excerpt` caps the *output*
  only — it builds the full `repr` first — so it is not a guard against huge or non-JSON values (see "Every scraped
  value, keys included").
- **Caller values in new messages:** an id, name or value that comes from the caller or from Comexio is written as
  repr (`%r` in log calls, `!r` in f-strings for exceptions), so a line break in it cannot forge a log line and an
  empty string stays visible. Older `%s` calls are not a finding unless the diff touches them.
- **Log tests:** a test of a log line or an exception message asserts the whole message (or `record.getMessage()`
  against an exact string), not a substring — a substring check misses leaked content and lost repr quoting.

## Rendering (SVG and other visible output)

- **Text in SVG/XML:** `html.escape` is not enough for text from Comexio or the caller (names, titles, settings).
  Characters XML 1.0 forbids (e.g. U+0001) must be removed, or one of them makes the whole SVG unparsable; text
  that must stay on one line (hover lines, labels) needs every Unicode line break neutralized (CR, LF, NEL, U+2028
  …), not only `\n`, or a value can forge extra lines. Text that is meant to span lines keeps its breaks: a plan
  comment is split into one `<tspan>` per line, so flattening it first would lose that layout.
- **Tooltips in the plan preview (`render_plan_svg`):** a node's `<title>` is the first child of the node's `<g>`
  group, so it covers every part of the node (body, title bar, texts, pin glyphs). No `pointer-events` rules to
  route that hover; they break other targets such as the comment text. The signal-flow diagram (`render_flow.py`)
  is a different case: its `.flow-label` keeps `pointer-events: none` so the hover reaches the box's `<title>`.
- **External values before use:** check the type of a value from outside (a `Mapping` where a mapping is expected,
  a `str` where a name or value is expected) before sorting it, formatting it or passing it to `str()`. A sort key
  that meets a non-`str` raises `TypeError`, and `str()` hides a malformed value instead of rejecting it.
- **Stable order:** a sort that decides visible output (hover lines, labels, reports) needs a tie-break, e.g.
  `key=lambda name: (name.casefold(), name)`; otherwise names that differ only in case keep their input order, so
  the output depends on the order Comexio happened to send.

## Tests

- Test data is synthetic, never real installation data. Every bug fix in pure logic needs a regression test that
  fails without the fix.
- Flag deleted or loosened assertions and snapshot updates that are not explained in the PR.
- **Dropped means gone everywhere it is meant to be dropped:** a test that asserts an entry is skipped also asserts
  it is absent from every result list the contract drops it from, not only from the one list the fix touched — and
  present where the contract keeps it (e.g. an inactive IO leaves `io` but stays in `io_all` for labels). A test
  that asserts entries are kept compares the complete expected result — `==` on a list, a sorted list or a
  `collections.Counter`, not on a `set`, which hides duplicates — not a subset or a single member.
- **Global interpreter settings are restored:** a test that changes one (e.g. `sys.set_int_max_str_digits`) does it
  in a fixture or `try`/`finally`, never bare; the digit limit goes through the `int_max_str_digits` fixture in
  `conftest.py` (`tests/test_conventions.py` enforces this). A `pytest.mark.parametrize` with an int beyond 4300
  digits gets explicit `ids=`: `str()`/`repr()` of it raise `ValueError`.

## GitHub Actions

- Pin actions to full commit SHAs with a `# vX.Y.Z` comment; every workflow has a least-privilege `permissions:`
  block; secrets go through `env:`, never inline in `run:`.
- Fork-controlled code must never run with `SONAR_TOKEN`: the `pull_request` job only builds coverage and PR
  metadata, and the trusted `workflow_run` scan (`sonar-pr.yml`) uses the token but treats that metadata as
  untrusted input and never installs or executes code from the fork. PyPI publishing uses Trusted Publishing
  restricted to `v*` tags.
- Checks on the published artifact live in `scripts/check_dist.py` and run in both `ci.yml` and `pypi.yaml` (on the
  exact `dist/` that gets uploaded) — never only in `ci.yml`, since a release is built anew from its tag.
