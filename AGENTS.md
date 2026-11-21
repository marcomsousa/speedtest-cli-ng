# AGENTS.md — `speedtest-cli-ng`

## 1. Project Identity & Mission

- **Repository:** `marcomsousa/speedtest-cli-ng` (100% independent public mirror, `isFork: false`)
- **Upstream Origin:** `sivel/speedtest-cli` (Matt Martz) — **officially archived (read-only) on April 30, 2026**; last pushed July 20, 2024; `master` at `2.1.4b1`, last PyPI release `2.1.3`.
- **License & Attribution:** **Apache License 2.0** (`LICENSE`). Preserve original copyright attribution (`Copyright 2012 Matt Martz`) and full Git history.
- **Primary Target:** Modern, drop-in, actively maintained Python 3.11+ successor to `speedtest-cli` on PyPI (`speedtest-cli-ng`, exposing `import speedtest`) — engineered to modernize Home Assistant Core's built-in `speedtestdotnet` integration (`requirements = ["speedtest-cli==2.1.3"]`) and dramatically improve throughput accuracy on Gigabit+ fiber connections.

---

## 2. Next-Gen (`ng`) Goals & Modernization Priorities

### Phase A — Packaging, Runtime & Tooling Modernization
1. **Migrate Packaging to `uv` + `hatchling` (PEP 621 / 639 / 735):**
   - Replace legacy `setup.py`, `setup.cfg` (`universal=1`), `MANIFEST.in`, `tox.ini`, and dead `.travis.yml` with `pyproject.toml` + `uv.lock`.
   - Distribution name: `speedtest-cli-ng`; Python import package: `speedtest` (converted from monolithic `speedtest.py` into a structured `speedtest/` package with `py.typed` for PEP 561).
   - Keep CLI entry points: `speedtest = "speedtest.cli:main"` and `speedtest-cli = "speedtest.cli:main"`.
   - Target `requires-python = ">=3.11"` (matching Home Assistant Core).
2. **Remove Python 2.4–3.10 Legacy Shims (~300+ lines of dead code):**
   - Strip `urllib2`, `httplib`, `Queue`, `urlparse`, `optparse`, `cStringIO`, `__builtin__`, `_Py3Utf8Output`, `FakeShutdownEvent.isSet`, `minidom` fallback, `get_exception()`, and `PY25PLUS`/`PY32PLUS` guards.
   - Replace `datetime.utcnow()` with `datetime.now(UTC)` and `timeit.time.sleep()` with `time.sleep()` / `asyncio.sleep()`.
3. **Strict Static Typing, Linting & Formatting:**
   - Configure `ruff` (`line-length = 88`, rules: `ASYNC`, `B`, `C4`, `I`, `PTH`, `RUF`, `SIM`, `TCH`, `UP`) and strict `mypy` across `speedtest/` and `tests/`.

### Phase B — Engine, Protocol & Accuracy Improvements (High-Impact)
1. **Native `asyncio` Transport Layer (Zero GIL / Thread-Polling Bottlenecks):**
   - Replace blocking `urllib` + `threading` + `10240`-byte (10 KB) chunk reads + `while in_flight >= max_threads: sleep(0.001)` busy-wait loops with native `asyncio`.
   - Expose an **`AsyncSpeedtest` API** (allowing Home Assistant's `speedtestdotnet` coordinator to run natively on the event loop without `hass.async_add_executor_job`), while keeping the synchronous `Speedtest` facade 100% backward-compatible.
   - Use **64–256 KB `memoryview` / pre-allocated buffers** and HTTP/1.1 keep-alive connection pooling (`aiohttp.ClientSession`, injectable for Home Assistant).
2. **Clean-Room `OoklaServer` Raw TCP Protocol (Inspired by MIT-licensed `showwin/speedtest-go`):**
   - Implement an optional/default **raw TCP transport** against `OoklaServer` port `8080` (and `5060`) using `asyncio.open_connection` / `asyncio.Protocol`:
     - Handshake: `HI\n` -> `HELLO <version> ...`
     - Latency / Jitter: `PING <ts>\n` -> `PONG <ts>` (supports both idle latency and concurrent loaded latency / bufferbloat)
     - Download: `DOWNLOAD <bytes>\n` -> raw binary stream (no per-chunk HTTP framing overhead)
     - Upload: `UPLOAD <bytes> 0\n` + raw payload -> `OK <bytes> <ts>`
     - Close: `QUIT\n`
   - **Automatic Fallback:** Fall back transparently to async HTTP (`aiohttp`) whenever port `8080` is unreachable, firewalled, or behind an HTTP-only reverse proxy.
   - **Do NOT reverse-engineer Ookla's proprietary CLI binary** (violates Ookla EULA). Rely strictly on open-source clean-room references (e.g., `showwin/speedtest-go`).
3. **Improved Measurement Algorithm & Richer Metrics:**
   - **Dynamic multi-stream scaling:** Scale concurrent TCP/HTTP streams to saturate high-BDP Gigabit+ fiber links (e.g., Raspberry Pi 5 ARM64).
   - **Warm-up & statistical trimming:** Discard TCP slow-start ramp-up and tail outliers (trimmed mean / percentile window) instead of naive `total_bytes / (stop - start)`.
   - **Richer Result Fields (additive, non-breaking):** Add `jitter`, `loaded_ping_download`, `loaded_ping_upload` (bufferbloat), `packet_loss`, and `protocol` (`tcp` / `http`) to `SpeedtestResults` while preserving all existing fields.
4. **Hardened Server Discovery & Error Handling:**
   - Default to HTTPS (`secure=True`), modernize request headers/User-Agent to avoid `HTTP 403: Forbidden`, support Speedtest.net's modern JSON server list endpoint (`/api/js/servers`) alongside legacy XML endpoints, and enforce strict per-phase timeouts & clean cancellation.

### Phase C — Testing & CI/CD
1. **Deterministic Offline Unit Test Suite (`pytest`):**
   - Upstream has **zero unit tests** (only `tests/scripts/source.py`). Build comprehensive mocked tests for config parsing, server discovery/filtering, latency selection, TCP protocol framing, HTTP fallback, error handling, and CLI flags.
   - Enforce `filterwarnings = ["error"]` and `--cov=speedtest --cov-branch` with `fail_under = 95`.
2. **Opt-In Live Integration Tests (`@pytest.mark.real_api_call`):**
   - Separate live network speedtest checks from default offline `uv run pytest`.
3. **GitHub Actions Workflows:**
   - `.github/workflows/main.yml`: CI matrix (`Python 3.11`–`3.14+`), `astral-sh/setup-uv` pinned to exact semver tag, running `ruff check`, `ruff format --check`, `mypy`, `pytest`, and `uv build`.
   - `.github/workflows/publish.yml`: PyPI OIDC Trusted Publishing (`pypa/gh-action-pypi-publish@release/v1`, `environment: pypi`) triggered only on GitHub Release published or `workflow_dispatch`.

---

## 3. Repository Architecture Map

### Current Upstream Layout (Mirrored Baseline)
```text
speedtest-cli-ng/
├── AGENTS.md                  # This file (modernization blueprint & invariants)
├── speedtest.py               # Legacy 2,013-line monolith (to be modularized into speedtest/)
├── setup.py / setup.cfg       # Legacy setuptools packaging (to be replaced by pyproject.toml)
├── MANIFEST.in / tox.ini      # Legacy build/test configs (to be removed)
├── .travis.yml                # Dead Travis CI config (to be replaced by .github/workflows/)
├── tests/scripts/source.py    # Only existing upstream test script
├── speedtest-cli.1            # Man page
├── README.rst                 # Upstream documentation (to be updated / migrated to README.md)
├── CONTRIBUTING.md            # Contribution guidelines
└── LICENSE                    # Apache License 2.0
```

### Target Modular Layout (Post-Modernization)
```text
speedtest-cli-ng/
├── pyproject.toml             # uv + hatchling + ruff + mypy + pytest config
├── uv.lock                    # Locked dependencies
├── speedtest/
│   ├── __init__.py            # Public exports (drop-in compatible with speedtest-cli 2.1.3)
│   ├── py.typed               # PEP 561 marker
│   ├── exceptions.py          # SpeedtestException hierarchy (NoMatchedServers, etc.)
│   ├── models.py              # SpeedtestResults, Server, Config dataclasses
│   ├── client.py              # Synchronous Speedtest facade (wraps async core or sync)
│   ├── async_client.py        # Native asyncio AsyncSpeedtest engine
│   ├── transport/
│   │   ├── tcp.py             # Clean-room OoklaServer port 8080 raw TCP protocol
│   │   └── http.py            # Async aiohttp HTTP/HTTPS fallback transport
│   └── cli.py                 # Modern argparse CLI (speedtest / speedtest-cli)
├── tests/
│   ├── conftest.py            # Shared fixtures & mocked servers/sockets
│   ├── test_client.py         # Sync facade & HA Core compatibility tests
│   ├── test_async_client.py   # Async engine & cancellation/timeout tests
│   ├── test_tcp_transport.py  # OoklaServer HI/PING/DOWNLOAD/UPLOAD framing tests
│   ├── test_http_transport.py # HTTP fallback & error handling tests
│   ├── test_cli.py            # CLI flags, JSON/CSV/simple output tests
│   └── test_real_api_calls.py # @pytest.mark.real_api_call live tests
└── .github/workflows/
    ├── main.yml               # Lint, typecheck, test matrix, build
    └── publish.yml            # PyPI OIDC trusted publish
```

---

## 4. Domain Invariants (Must Not Break)

Home Assistant Core's `homeassistant/components/speedtestdotnet/coordinator.py` depends directly on the following contract. **Never break these without a coordinated major version bump:**

1. **Top-Level Import:** `import speedtest` must expose `speedtest.Speedtest`, `speedtest.SpeedtestException`, and `speedtest.NoMatchedServers` (plus all existing exception subclasses).
2. **HA Core Call Sequence (`SpeedTestDataCoordinator.update_data`):**
   - `api = speedtest.Speedtest(...)`
   - `api.get_servers()` -> returns `dict[float, list[dict[str, Any]]]` keyed by distance, where each server dict contains `"country"`, `"name"`, `"sponsor"`, and `"id"`.
   - `api.closest.clear()` -> `api.closest` must remain a mutable `list`.
   - `api.get_servers(servers=[server_id])` -> filters by server ID (accepts `int` or numeric `str`); raises `speedtest.NoMatchedServers` when none match.
   - `best_server = api.get_best_server()` -> returns server dict with `"id"` and `"latency"`.
   - `api.download()` and `api.upload()` -> populate `api.results` and return speeds in **bits per second (bps)** as `float`.
   - `api.results.dict()` -> returns a `dict[str, Any]` containing at least `"download"` (bps), `"upload"` (bps), `"ping"` (ms), `"server"` (dict), `"timestamp"` (ISO-8601 UTC string), `"bytes_sent"`, `"bytes_received"`, `"share"`, and `"client"`.
3. **Exception Hierarchy:** All runtime errors must inherit from `speedtest.SpeedtestException` so HA's `except speedtest.SpeedtestException` catches them cleanly and surfaces `UpdateFailed`.
4. **Units:** `download` and `upload` are always stored in **bits per second**; `ping` / `jitter` in **milliseconds**.
5. **CLI Compatibility:** Preserve common flags (`--simple`, `--json`, `--csv`, `--bytes`, `--server`, `--exclude`, `--list`, `--no-download`, `--no-upload`, `--secure`, `--source`, `--timeout`, `--version`) while adding `--protocol {auto,tcp,http}` for the new TCP engine.

---

## 5. Development, Testing & Release Guidelines

- **Git Identity:** Always commit under `Marco Sousa <marcomsousa@gmail.com>` (`marcomsousa` GitHub account). Follow Conventional Commits (`chore:`, `feat:`, `fix:`, `refactor:`, `test:`, `ci:`, `docs:`).
- **Standard Verification Commands (post-`uv` migration):**
  ```bash
  uv sync --all-groups
  uv run ruff check --no-fix .
  uv run ruff format --check .
  uv run mypy speedtest tests
  uv run pytest
  uv build
  ```
- **Live Network Tests (opt-in only):**
  ```bash
  uv run pytest -m real_api_call --no-cov
  ```
- **Publishing to PyPI:**
  - Use **PyPI OIDC Trusted Publishing** (`marcomsousa/speedtest-cli-ng`, workflow `publish.yml`, environment `pypi`). Never commit PyPI tokens.
  - Trigger releases via GitHub Releases (`gh release create vX.Y.Z ...`). Never reuse a deleted PyPI version filename.
- **Upstream Home Assistant Core PR Strategy (2-PR Rule):**
  - **PR 1:** Minimal dependency swap only (`speedtest-cli==2.1.3` -> `speedtest-cli-ng==X.Y.Z` in `manifest.json` and `requirements_*.txt`) leveraging drop-in compatibility.
  - **PR 2:** Follow-up PR after PR 1 merges to adopt `AsyncSpeedtest`, injectable `aiohttp.ClientSession`, and new jitter/bufferbloat sensors.
- **Legacy Upstream Branches Note:** The mirror preserves 21 historical upstream branches (`24test`, `devel`, `modularize`, `socket`, etc.) for reference; confirm before pruning non-bot branches from `origin`.
