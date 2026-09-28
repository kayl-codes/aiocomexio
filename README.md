# aiocomexio

Asyncio-native Python client for the [Comexio IO-Server](https://www.comexio.com/), a local
building-automation controller.

No dependency on Home Assistant or any other framework — usable as a standalone library. It will
provide the communication layer of the
[homeassistant-comexio](https://github.com/kayl-codes/homeassistant-comexio) integration.

> **Status: pre-alpha.** The code is being moved over from the integration step by step. So far
> only the network-free parts exist: admin page scraping (`aiocomexio.scrape`), config parsing
> (`aiocomexio.config`), KNX DPT tables (`aiocomexio.knx`) and function plan rendering
> (`aiocomexio.function_plan`). The client itself (login, polling, writes) is not there yet, and
> the API may still change.

## Installation

```bash
pip install aiocomexio
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
