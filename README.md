# aiocomexio

Asyncio-native Python client for the [Comexio IO-Server](https://www.comexio.com/), a local
building-automation controller.

No dependency on Home Assistant or any other framework — usable as a standalone library. It will
provide the communication layer of the
[homeassistant-comexio](https://github.com/kayl-codes/homeassistant-comexio) integration.

> **Status: pre-alpha.** The code is being moved over from the integration step by step. So far:
> admin login, read access (including live values of physical IOs), value writes, the Web-IO
> lifecycle, marker / KNX object management and function plan editing — plans, elements, wires,
> block instance settings, run / stop (`ComexioClient`), admin page scraping
> (`aiocomexio.scrape`), config parsing (`aiocomexio.config`), KNX DPT tables
> (`aiocomexio.knx`), Web-IO command builders (`aiocomexio.webio`), function plan rendering
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
session's cookie jar. Value writes (`set_marker_value`, `set_io_value`, `set_knx_value`) go
through Comexio's `/api/` with a separate API user: pass `api_username` / `api_password` to the
client. Every call either returns data or raises a `ComexioError` subclass
(`ComexioAuthenticationError`, `ComexioConnectionError`, `ComexioResponseError`,
`ComexioDataError`, `ComexioRequestRejectedError` for a write the server refused,
`ComexioCreatedWithoutIdError` for a create the server confirmed but whose id could not be read
back — the object exists, do not create it again). One exception: the value setters raise a
plain `ValueError`, which is not a `ComexioError`, for a non-finite float (`nan`, `inf`) before
sending anything — catch it separately if your values can be non-finite.

### HTTPS with a pinned certificate

Comexio serves HTTPS with a self-signed certificate, so the client pins its SHA-256 fingerprint
instead of verifying a CA. `fetch_certificate_fingerprint(host)` reads it from a bare TLS
handshake — no HTTP request, no credentials. Pass it as `fingerprint=` and every request goes over
HTTPS; a server presenting any other certificate raises `ComexioCertificateMismatchError` (not a
`ComexioConnectionError`: retrying cannot help, the certificate was replaced or something else
answers) before anything is sent. Without `fingerprint` the client uses plain HTTP. Store the
fingerprint and reuse it; fetching it anew on every start would accept whatever certificate
answers. `format_fingerprint()` renders it the way Comexio's admin page shows it.

```python
import asyncio

import aiohttp

from aiocomexio import ComexioClient, fetch_certificate_fingerprint, session_kwargs
from aiocomexio.config import parse_config


async def main() -> None:
    host = "192.168.0.20"
    async with aiohttp.ClientSession(**session_kwargs()) as session:
        fingerprint = await fetch_certificate_fingerprint(host)  # once, at setup; then store it
        client = ComexioClient(host, "admin", "secret", session=session, fingerprint=fingerprint)
        await client.login()
        raw = await client.get_raw_config()
        config = parse_config(raw.variables, io_types=raw.io_types, io_input_types=raw.io_input_types)
        print(len(config["markers"]), "markers on Comexio", raw.comexio_version)


asyncio.run(main())
```

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
