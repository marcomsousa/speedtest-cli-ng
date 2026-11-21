# speedtest-cli-ng

[![CI](https://github.com/marcomsousa/speedtest-cli-ng/actions/workflows/ci.yml/badge.svg)](https://github.com/marcomsousa/speedtest-cli-ng/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache--2.0-green.svg)](https://github.com/marcomsousa/speedtest-cli-ng/blob/master/LICENSE)

Modern, actively maintained **Python 3.11+** `asyncio` and raw-TCP (`OoklaServer`) successor to `sivel/speedtest-cli`, providing both a CLI (`speedtest` / `speedtest-cli`) and a drop-in library API (`import speedtest`) engineered for Gigabit+ fiber accuracy and Home Assistant's `speedtestdotnet` integration.

> **Attribution:** `speedtest-cli-ng` is an independent continuation of Matt Martz's archived [`sivel/speedtest-cli`](https://github.com/sivel/speedtest-cli) project (`Copyright 2012 Matt Martz`), preserving the **Apache License 2.0** and full Git history.

## What's New in `3.0.0`

- **Native `AsyncSpeedtest` + Injectable `aiohttp.ClientSession`:** Run directly on an existing `asyncio` event loop without thread-pool blocking, while keeping the synchronous `Speedtest` facade 100% drop-in compatible.
- **Clean-Room `OoklaServer` Raw TCP (`:8080`) Transport + Automatic HTTP Fallback:** Speaks `HI`/`HELLO`, `PING`/`PONG`, `DOWNLOAD`, `UPLOAD`, `QUIT` with 256 KiB shared `memoryview` buffers and transparent fallback to keep-alive `aiohttp` when port `8080` is unreachable.
- **Dynamic Multi-Stream Scaling & Statistical Windowing:** Scales streams dynamically on Gigabit+ links, discards TCP slow-start warm-up and outliers via trimmed mean, and reports additive `jitter`, `packet_loss`, `loaded_ping_download`, `loaded_ping_upload`, and `protocol` fields.
- **Modern JSON Server Discovery:** Queries Speedtest.net's `/api/js/servers` endpoint first (fixing stale/distant 10-server XML fallbacks) with automatic XML fallback and HTTPS (`secure=True`) by default.
- **Reliability & Leak Fixes:** Eliminates phantom `1,800,000 ms` latency values on unreachable servers, removes multi-MB per-request upload buffer allocations and stdout/stderr fd wrapping leaks, refreshes timestamps between reused runs, and removes Python 2.4–3.10 shims.

## Installation

```bash
uv add speedtest-cli-ng
```

or with `pip`:

```bash
pip install speedtest-cli-ng
```

## CLI Usage

```bash
speedtest --simple
speedtest --json
speedtest --protocol tcp
speedtest --protocol http --no-secure
speedtest --list
speedtest --server 19911 --bytes
```

Key CLI options:

- `--protocol {auto,tcp,http}` — Transfer transport (`auto` prefers raw TCP `:8080` with automatic HTTP fallback).
- `--simple`, `--json`, `--csv`, `--csv-delimiter`, `--csv-header` — Output formats.
- `--bytes` — Report human-readable output in MB/s instead of Mbit/s.
- `--no-download`, `--no-upload`, `--single` — Run partial or single-stream tests.
- `--server ID`, `--exclude ID`, `--list`, `--mini URL` — Server discovery and selection.
- `--secure` (default) / `--no-secure`, `--source IP`, `--timeout SEC`, `--share`, `--version`.

## Python API

### Async API (Recommended)

```python
import asyncio
import aiohttp
from speedtest import AsyncSpeedtest


async def main() -> None:
    async with aiohttp.ClientSession() as session:
        client = AsyncSpeedtest(session=session)
        await client.get_servers()
        best = await client.get_best_server()
        download_bps = await client.download()
        upload_bps = await client.upload()
        print(
            best["sponsor"], client.results.ping, download_bps / 1e6, upload_bps / 1e6
        )


asyncio.run(main())
```

### Synchronous Drop-In API (`speedtest-cli` Compatible)

```python
import speedtest

api = speedtest.Speedtest(secure=True)
api.get_servers()
best = api.get_best_server()
api.download()
api.upload()
results = api.results.dict()
print(results["ping"], results["download"] / 1e6, results["upload"] / 1e6)
```

## Development

```bash
uv sync --all-groups
uv run ruff check --no-fix .
uv run ruff format --check .
uv run mypy speedtest tests
uv run pytest
```

Opt-in live Speedtest.net tests:

```bash
uv run pytest -m real_api_call --no-cov
```
