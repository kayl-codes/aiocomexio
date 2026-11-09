# aiocomexio

Asyncio-native Python client for the [Comexio IO-Server](https://www.comexio.com/), a local
building-automation controller.

No dependency on Home Assistant or any other framework — usable as a standalone library. It will
provide the communication layer of the
[homeassistant-comexio](https://github.com/kayl-codes/homeassistant-comexio) integration.

> **Status: pre-alpha.** The code is being moved over from the integration step by step. So far:
> admin login, read access (including live values of physical IOs and system info — free
> storage, time server settings, uptime), value writes, the Web-IO
> lifecycle (including the command protocol, HTTP or HTTPS), marker / KNX object management and
> function plan editing — plans, elements, wires, block instance settings, scene blocks and
> Quantum modules with their configs, run / stop
> (`ComexioClient`), admin page scraping (`aiocomexio.scrape`), config parsing
> (`aiocomexio.config`, including Quickbus devices such as Multitouch panels and their IOs), KNX
> DPT tables (`aiocomexio.knx`), Web-IO command builders (`aiocomexio.webio`, with separate
> classes for Quickbus IOs and 1-Wire sensors), function plan rendering
> (with block settings in the hover text) and diffing (`aiocomexio.function_plan`) and the
> block-type reference catalog that resolves Comexio catalog ids per server by stable key
> (`aiocomexio.reference_catalog`). The API may still change.

## Installation

```bash
pip install aiocomexio
```

## Usage

The client works on an `aiohttp.ClientSession` you create and close yourself.
`session_kwargs()` gives it the settings Comexio needs (a cookie jar that keeps the admin
login for an IP-address host, a request timeout; `progress_log_interval=10` adds a "still
waiting" log line for slow requests). Give every client its own session: `login()` clears the
session's cookie jar. Several tasks can share one client while it logs in: `login()` waits for
the requests already in flight (at most 60 s), and requests that come up meanwhile wait until the
login is done, so renewing the session does not cut them off. Two exceptions: a request still
running after those 60 s may fail as not logged in, and requests that waited for a login that
failed or was cancelled may run without an admin session. Marker and KNX writes
(`set_marker_value`, `set_knx_value`) go through Comexio's `/api/` with a separate API user: pass
`api_username` / `api_password` to the client.
`set_io_value` sends only the relay and dimmer outputs (`Q1`, `Q2`, …) and the Dali groups
(`DG1`, …) to `/api/`, which refuses nearly every other IO. It writes the others (the 0-10 V
analog outputs `AO1`, …, and the IOs Comexio writes itself, such as a Multiroom-Server's
`Alarm1` or a Touchpanel's `Bright1`) over the admin session like Comexio's own IO page, so
they need a `login()` first
(`io_written_over_admin_session(identifier)` tells which IOs). Do not pass Quickbus IOs to
`set_io_value`: their identifiers repeat across devices, so they cannot be addressed by name.
Every call either returns data or raises a `ComexioError` subclass
(`ComexioAuthenticationError`, `ComexioConnectionError`, `ComexioResponseError`,
`ComexioDataError`, `ComexioRequestRejectedError` for a write the server refused,
`ComexioCreatedWithoutIdError` for a create the server confirmed but whose id could not be read
back — the object exists, do not create it again). One exception: the value setters raise a
plain `ValueError`, which is not a `ComexioError`, for a non-finite float (`nan`, `inf`) before
sending anything — catch it separately if your values can be non-finite.

An answer is read up to 16 MiB (the largest one measured on an installation with 30 function plans is
865 KiB); a successful answer that is longer, or that the device sends compressed although every request
asks for `Accept-Encoding: identity`, is refused with `ComexioDataError`, never cut. An error-status answer
sent compressed keeps its status (`ComexioResponseError`); its
`body` is then not the page but the note `('gzip'-encoded body not read)`, naming the coding. The request was
sent by then, so for a write a `ComexioDataError` does not prove it failed — as with `ComexioConnectionError`,
look at the object before repeating a create. An answer is decoded with its declared charset, else UTF-8: a
declared charset that is no text encoding (`hex`, `zlib`, ...) or that transforms the page instead of reading it
(`unicode_escape`, `raw_unicode_escape`, `punycode`, `idna`) counts as undeclared, and a
`fallback_charset_resolver` of an injected `aiohttp.ClientSession` is not consulted.

### HTTPS with a pinned certificate

Comexio serves HTTPS with a self-signed certificate, so the client pins its SHA-256 fingerprint
instead of verifying a CA. `fetch_certificate_fingerprint(host)` reads it from a bare TLS
handshake — no HTTP request, no credentials. Pass it as `fingerprint=` and every request goes over
HTTPS; a server presenting any other certificate raises `ComexioCertificateMismatchError` (not a
`ComexioConnectionError`: retrying cannot help, the certificate was replaced or something else
answers) before the request that met it is sent — if that request was a redirect hop, the hops
before it did reach the pinned server. Without `fingerprint` the client uses plain HTTP and
follows redirects only on the Comexio server: on the same scheme, host and port, or on to HTTPS on
port 443 of the same host. A redirect anywhere else (another host, or another port of the same
host) raises `ComexioResponseError` before anything is sent there. A redirect on to HTTPS checks
the certificate against the system's CAs, so Comexio's self-signed one fails there with
`ComexioConnectionError` — pin it instead.

Fetching the fingerprint is trust on first use: whatever answers that one handshake gets pinned.
So fetch it **once, at setup**, store it, and pass the stored value on every later start —
fetching it anew on each start would accept whatever certificate answers. If you want to rule out
an impostor at setup too, compare `format_fingerprint()` with the SHA-256 value Comexio's admin
page lists under its certificate hashes. A pinned client follows redirects only within its HTTPS
origin (same scheme, host and port). For a host name that resolves to several addresses, aiohttp
reports only the last failure, so a mismatch there can arrive as `ComexioConnectionError`; compare
a fresh `fetch_certificate_fingerprint()` with the pin to tell the two apart.

```python
import asyncio

import aiohttp

from aiocomexio import ComexioClient, fetch_certificate_fingerprint, format_fingerprint, session_kwargs
from aiocomexio.config import parse_config

HOST = "192.168.0.20"


async def setup() -> str:
    """Run once: read the fingerprint, check it, store it with the rest of your configuration."""
    fingerprint = await fetch_certificate_fingerprint(HOST)
    print("Comexio certificate:", format_fingerprint(fingerprint))
    return fingerprint


async def main(stored_fingerprint: str) -> None:
    """Every later start: use the stored fingerprint, never a freshly fetched one."""
    async with aiohttp.ClientSession(**session_kwargs()) as session:
        client = ComexioClient(HOST, "admin", "secret", session=session, fingerprint=stored_fingerprint)
        await client.login()
        raw = await client.get_raw_config()
        config = parse_config(raw.variables, io_types=raw.io_types, io_input_types=raw.io_input_types)
        print(len(config["markers"]), "markers on Comexio", raw.comexio_version)


asyncio.run(main(stored_fingerprint="<the value setup() returned>"))
```

### 1-Wire sensors

The three 1-Wire calls all need the admin session (`login()` first):

- `get_onewire_device_count()` counts the 1-Wire devices on the IO home page. `0` means there
  are none, so the status poll can be skipped. The count and the status request are separate
  requests with no shared snapshot: a sensor added between them can already appear in the current
  status, and a sensor added after both is seen by the next poll.
- `get_onewire_sensors()` returns the active configured sensors keyed by address (`OT1`, `OH3`, …);
  inactive entries are left out. An active entry without a usable `Prefix` or `Id` is skipped (and
  logged); if active entries exist but none is usable, the call raises `ComexioDataError`. Two
  active entries with the same address collapse to one: the later one wins (logged). Two entries
  with the same `Id` but different prefixes both stay, and a warning is logged, because a function
  plan names a sensor by its `Id` alone. It costs one config fetch.
- `get_onewire_status()` returns the live state of the sensors Comexio reports, keyed by address.
  It is read from Comexio's 1-Wire monitor, which is not an official interface, so a changed answer
  raises `ComexioDataError`. An item without an address is skipped (and logged), so a sensor can be
  missing from the map; if the answer has items but none has an address, the call raises
  `ComexioDataError`. Two items with the same address collapse to one, the later one wins (logged).
  Compare the keys with `get_onewire_sensors()` if you need every sensor.

Each `OneWireStatus` has `visible` (False: the sensor was not found), `online` (False: read error,
for example a CRC error) and `value` (the reading as a float). `value` is `None` if there is none,
if it is not finite, or if it is not a number at all (that case is logged). `ok` is True only when
the sensor is visible and online. A read error can still carry a value, so check `ok` before
trusting `value`.

```python
import asyncio

import aiohttp

from aiocomexio import ComexioClient, session_kwargs


async def report_1wire_problems(client: ComexioClient) -> None:
    """Print the sensors whose status says not found or read error (not the ones missing from it)."""
    if not await client.get_onewire_device_count():
        return
    for address, status in (await client.get_onewire_status()).items():
        if not status.ok:
            print(address, "read error" if status.visible else "not found")


async def main(stored_fingerprint: str) -> None:
    async with aiohttp.ClientSession(**session_kwargs()) as session:
        client = ComexioClient("192.168.0.20", "admin", "secret", session=session, fingerprint=stored_fingerprint)
        await client.login()
        await report_1wire_problems(client)


asyncio.run(main(stored_fingerprint="<the value setup() returned>"))
```

The status is for spotting broken sensors. To get the values pushed, build Web-IO commands for the
sensors. `config.parse_config` does not read them, so add them under the key `"onewire"` of the
parsed config. Take them from the same config fetch, with
`onewire.parse_onewire_config(raw.variables["FubModules"])`: `get_onewire_sensors()` fetches a config
of its own, which can differ if the 1-Wire setup changes in between. Then `webio.build_webio_commands` (or
`generate_webio_json` with `WebioClass.ONEWIRE`, class `<webio name> [1Wire]`) builds one command per
sensor:

- The command is named `HA 1Wire <address>`.
- It pushes `{"id": "<address>", "value": …, "type": "onewire"}`.
- An analog sensor gets the generic analog range and a digital one gets 0/1.
- A sensor of unknown kind gets no command.

In a function plan, a sensor is element type 12 with its `config_id` as `ref_id`. Wire it to its
command like a marker. To show the sensors by name in `function_plan.render_plan_svg`, pass
`onewire_by_id`: the sensors keyed by `str(config_id)`, each `{"name": …, "address": …, "type":
sensor.kind, "value": …}`. Without it a sensor renders as `1-Wire ref=<config_id>`. A plan
cannot tell apart sensors that share an `Id` under different prefixes (`parse_onewire_config` logs
them): leave such an Id out of the map, and its elements render as `1-Wire ref=<config_id>`
instead of showing one of the sensors.
`render_flow_svg` takes the same map (and `knx_by_id`) for its box labels. A value that is not
finite (`nan`, `inf`) shows as no value.

### Doorstation

A Comexio Doorstation is an extension of the IO-Server with a camera. Its IOs (`PIR1`, the bell
buttons `DI1`–`DI3`, `NameLED1`, …) come through `config.parse_config` like those of any other
extension. The camera calls need the admin session (`login()` first):

- `get_doorstations()` reads the IO home page and returns the Doorstations keyed by extension id.
  Each `Doorstation` has `ext_id`, `camera_id` and `name` (the extension name, `None` if the page
  carries none). An empty map means the page lists extensions but no Doorstation; a page with neither
  the device counters of its extension blocks (the ones `get_onewire_device_count()` reads) nor an
  element with a non-empty `data-camera_id` raises `ComexioDataError`. Elements whose ids are not positive integers are skipped (and logged); if there are
  such elements but none is usable, the call raises `ComexioDataError`. Detection follows the IO home page
  of Comexio OS 11.0.2: the elements of a Doorstation tab carry `data-extid`, `data-extname` and
  `data-camera_id`.
- `doorstation_stream_url(camera_id)` is the URL of the camera's MJPEG stream on the IO-Server
  (`/proxy/cam/<camera_id>/`, `multipart/x-mixed-replace`, one JPEG per part).
- `get_doorstation_snapshot(camera_id)` returns the current image: the first complete JPEG of that
  stream, read with the admin session. A stream without a frame within 10 seconds, or one that breaks
  off, raises `ComexioConnectionError`; one that ends or passes 4 MB without a frame (a frame must end
  within the first 4 MB; bytes past it are not kept), a malformed frame (also one with an empty scan
  or one that ends before its first scan), or a stream sent compressed (it is asked for unencoded and
  never decompressed, so the 4 MB cap holds) raises `ComexioDataError`; the login form raises
  `ComexioAuthenticationError`.

The Doorstation serves the same stream under its own address, reportedly without a login. If you
use it, keep the Doorstation in a network segment that only trusted hosts can reach.

## Development

```bash
pip install -e ".[dev]"
pre-commit install

ruff check .            # lint
ruff format --check .   # formatting check; drop --check to auto-format
mypy                    # strict type check (src/aiocomexio)
pytest                  # tests
```

## License

[MIT](LICENSE)
