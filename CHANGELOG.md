# Changelog

All notable changes to `speedtest-ng` are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [3.0.0] - 2026-11-21

### Added

- Modular, PEP 561-typed `speedtest` package (`speedtest/py.typed`) exposing both `AsyncSpeedtest` (with optional injected `aiohttp.ClientSession`) and the drop-in synchronous `Speedtest` facade.
- Clean-room `OoklaServer` raw TCP (`:8080`) transport (`speedtest/transport/tcp.py`) supporting `HI`/`HELLO`, `PING`/`PONG`, `DOWNLOAD`, `UPLOAD` (header-inclusive framing), and `QUIT` with automatic fallback to keep-alive `aiohttp` (`speedtest/transport/http.py`).
- Dynamic multi-stream scaling, warm-up + trimmed-mean throughput estimation, concurrent loaded-latency (bufferbloat) probing, and additive `SpeedtestResults` fields (`jitter`, `loaded_ping_download`, `loaded_ping_upload`, `packet_loss`, `protocol`).
- Primary JSON server discovery via `/api/js/servers` with automatic XML fallback and `--protocol {auto,tcp,http}` / `--no-secure` CLI flags.
- Deterministic offline `pytest` suite (`>= 95%` branch coverage, `filterwarnings = ["error"]`), `tests/test_ha_compat.py`, opt-in `@pytest.mark.real_api_call` live tests, and GitHub Actions CI, OIDC publish, and weekly live API workflows.

### Fixed

- **Unreachable servers returning `1,800,000 ms` ping** (Home Assistant `#174958`): failed latency probes are discarded instead of appending `3600`, RTT averages divide by actual probe count instead of `6`, per-probe `.i` cache-busters are preserved, `timeout` and proxy settings are honored, and `SpeedtestBestServerFailure` is raised when no candidate responds.
- **Memory growth and socket/fd leaks** (Home Assistant `#191133`): replaced per-request `HTTPUploaderData.pre_allocate()` buffers with a single shared read-only `memoryview` slice, removed import-time `_Py3Utf8Output` / `FileIO(..., closefd=True)` stdout/stderr wrapping (`ResourceWarning` on Python 3.14), and closed all sockets/sessions in `finally` blocks.
- **Stale/distant 10-server XML discovery & disappearing pinned servers** (Home Assistant `#179173`, `#115775`, `#135862`): `/api/js/servers` discovers local servers accurately while XML endpoints serve as fallback/supplement; all-failed discovery now raises `ServersRetrievalError` rather than returning `{}`.
- **Multi-gigabit throughput under-reporting** (Home Assistant `#178446`): replaced blocking `urllib` + `10240`-byte reads + `sleep(0.001)` busy-wait loops with native `asyncio` raw TCP and 256 KiB chunked streaming.
- **Stale `results.timestamp` & `_share` across reused instances:** `SpeedtestResults.touch_timestamp()` refreshes the ISO-8601 UTC timestamp (`datetime.now(UTC)`) and resets cached `_share` on each measurement cycle.
- **Crash & mutation bugs:** removed dead `DOM.parseString` fallback (`NameError`), non-200 config `None` return (`KeyError: 'client'`), in-place mutation of caller `servers`/`exclude` lists, non-idempotent `get_closest_servers()`, CLI `--exclude` without `--server` `NoMatchedServers` `TypeError`, and bare `Mozilla/5.0` `HTTP 403` rejections.

### Changed

- **Minimum Python version:** requires Python `>= 3.11` (all Python 2.4–3.10 compatibility shims removed).
- **HTTPS by default:** `secure=True` is now the default for both library calls and CLI invocations (`--no-secure` opts into plain HTTP).
- **Packaging & tooling:** migrated from `setup.py` / `setup.cfg` / `tox.ini` / `.travis.yml` to `uv` + `hatchling` (`pyproject.toml` + `uv.lock`), `ruff`, `mypy --strict`, and standard `logging.getLogger("speedtest")`.
