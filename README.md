# aiocomexio

Asyncio-native Python client for the [Comexio IO-Server](https://www.comexio.com/), a local
building-automation controller.

No dependency on Home Assistant or any other framework — usable as a standalone library. It will
provide the communication layer of the
[homeassistant-comexio](https://github.com/kayl-codes/homeassistant-comexio) integration.

> **Status: pre-alpha.** The code is being moved over from the integration step by step. So far:
> admin login, read access, value writes, the Web-IO lifecycle, marker / KNX object management
> and function plan editing — plans, elements, wires, run / stop (`ComexioClient`), admin page
> scraping (`aiocomexio.scrape`), config parsing (`aiocomexio.config`), KNX DPT tables
> (`aiocomexio.knx`), Web-IO command builders (`aiocomexio.webio`) and function plan rendering
> and diffing (`aiocomexio.function_plan`). The API may still change.

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
`ComexioDataError`, `ComexioRequestRejectedError` for a write the server refused).

```python
import asyncio

import aiohttp

from aiocomexio import ComexioClient, session_kwargs
from aiocomexio.config import parse_config


async def main() -> None:
    host = "192.168.0.20"
    async with aiohttp.ClientSession(**session_kwargs()) as session:
        client = ComexioClient(host, "admin", "secret", session=session)
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
