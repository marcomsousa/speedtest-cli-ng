# AGENTS.md — `speedtest-cli-ng`

## 1. Project Identity & Mission

- **Repository:** `marcomsousa/speedtest-cli-ng` (100% independent public mirror, `isFork: false`)
- **Upstream Origin:** `sivel/speedtest-cli` (Matt Martz) — **officially archived (read-only) on April 30, 2026**; last pushed July 20, 2024; `master` at `2.1.4b1`, last PyPI release `2.1.3`.
- **License & Attribution:** **Apache License 2.0** (`LICENSE`). Preserve original copyright attribution (`Copyright 2012 Matt Martz`) and full Git history.
- **Primary Target:** Modern, drop-in, actively maintained Python 3.11+ successor to `speedtest-cli` on PyPI (`speedtest-cli-ng`, exposing `import speedtest`) — engineered to modernize Home Assistant Core's built-in `speedtestdotnet` integration (`requirements = ["speedtest-cli==2.1.3"]`) and dramatically improve throughput accuracy on Gigabit+ fiber connections.

---

## 2. Next-Gen (`ng`) Goals & Resolved Upstream Debt

### Phase A — Packaging, Runtime & Tooling Modernization (Implemented in `3.0.0`)
1. **Packaging on `uv` + `hatchling` (PEP 621 / 639 / 735):**
   - `pyproject.toml` + `uv.lock` with `requires-python = ">=3.11"`, `name = "speedtest-cli-ng"`, `speedtest/` package + `py.typed` (PEP 561), and CLI entry points `speedtest = "speedtest.cli:main"` / `speedtest-cli = "speedtest.cli:main"`.
2. **Legacy Shims & Deprecations Removed:**
   - Removed ~500 lines of Python 2.4–3.10 shims (`urllib2`, `httplib`, `Queue`, `urlparse`, `cgi`, `optparse`, `cStringIO`, `__builtin__`, `_Py3Utf8Output`, `FakeShutdownEvent.isSet` retained as thin alias only, `minidom` fallback `NameError`, `get_exception()`, `PY25PLUS`–`PY310PLUS`, `timeit.time.sleep()`).
   - Replaced `datetime.utcnow()` with `datetime.now(UTC)` and `global DEBUG` / `print_()` with standard `logging.getLogger("speedtest")` (enabling `"loggers": ["speedtest"]` in HA Core).
3. **Strict Static Typing, Linting & Formatting:**
   - `ruff` (`line-length = 88`, rules: `ASYNC`, `B`, `C4`, `E`, `F`, `I`, `PT`, `PTH`, `RUF`, `SIM`, `TC`, `UP`, `W` — note `TC`, not deprecated `TCH`) and `mypy --strict` (`warn_unreachable = true`) across `speedtest/` and `tests/`.

### Phase B — Engine, Protocol & Accuracy Improvements (Implemented in `3.0.0`)
1. **Native `asyncio` Engine (`AsyncSpeedtest`) + Injectable `aiohttp.ClientSession`:**
   - Replaces blocking `urllib` + `threading` + 10 KiB reads + `sleep(0.001)` busy-wait loops with native `asyncio`, 256 KiB `memoryview` shared buffers (eliminating per-request upload allocations linked to HA `#191133`), and keep-alive `aiohttp.ClientSession` (injectable by HA Core without closing external sessions).
   - Drop-in synchronous `Speedtest` facade executes safely across arbitrary `hass.async_add_executor_job` worker threads and inside active event loops, closing internal sessions cleanly before loop exit.
2. **Clean-Room `OoklaServer` Raw TCP Protocol (`:8080`) with Transparent HTTP Fallback:**
   - Pure-Python `asyncio` implementation (`HI` -> `HELLO`, `PING` -> `PONG`, `DOWNLOAD`, `UPLOAD` with header-inclusive payload length, `QUIT`) informed by open-source clean-room references (`origin/socket` prototype + MIT-licensed `showwin/speedtest-go`).
   - Automatically falls back to async `aiohttp` whenever port `8080` is unreachable/firewalled.
   - **Never reverse-engineer Ookla's proprietary CLI binary** (violates Ookla EULA).
3. **Measurement & Bug Fixes Mapped to Open HA Core Issues:**
   - **HA `#174958` (`1,800,000 ms` ping):** Discards failed probes instead of appending `3600` and dividing by `6`, preserves `.i` latency cache-busters, honors timeouts/proxies, breaks ties cleanly, and raises `SpeedtestBestServerFailure` when all probes fail.
   - **HA `#179173`, `#115775`, `#135862` (distant/disappearing servers):** Uses `/api/js/servers` JSON discovery first with XML fallback/supplement, never mutates caller `servers`/`exclude` lists, raises `ServersRetrievalError` when all endpoints fail, and clears `self.closest` idempotently.
   - **HA `#178446` (multi-gigabit under-reporting):** Dynamic multi-stream scaling + warm-up/trimmed-mean bandwidth estimation (`speedtest/metrics.py`), plus additive non-breaking fields (`jitter`, `loaded_ping_download`, `loaded_ping_upload`, `packet_loss`, `protocol`) in `SpeedtestResults`.
   - **HA `#191133` (OOM / fd leaks):** Shared zero-copy upload payload buffer, no import-time stdout/stderr `FileIO(..., closefd=True)` wrapping, and guaranteed `finally` cleanup.
   - **State & crash fixes:** Refreshes `results.timestamp` and clears `_share` on each run; enforces `secure=True` by default and modernizes `User-Agent` to avoid HTTP 403 rejections.

### Phase C — Testing & CI/CD (Implemented in `3.0.0`)
1. **Deterministic Offline Unit & HA Compat Suite (`pytest`):**
   - `filterwarnings = ["error"]`, `--cov=speedtest --cov-branch`, `fail_under = 95`, loopback mock `OoklaServer`, `aioresponses`, and `tests/test_ha_compat.py`.
2. **Opt-In Live Integration Tests (`@pytest.mark.real_api_call`):**
   - Excluded from default `uv run pytest`; runnable locally or via weekly `.github/workflows/live_api.yml`.
3. **GitHub Actions Workflows:**
   - `.github/workflows/ci.yml`: matrix (`Python 3.11`–`3.14`), `astral-sh/setup-uv@v10.4.0`, `ruff check`, `ruff format --check`, `mypy`, `pytest`, `uv build`.
   - `.github/workflows/publish.yml`: PyPI OIDC Trusted Publishing (`pypa/gh-action-pypi-publish@release/v1`, `environment: pypi`, `attestations: true`) on release or `workflow_dispatch`.
   - `.github/workflows/live_api.yml`: weekly cron + `workflow_dispatch` live verification.

---

## 3. Repository Architecture Map

```text
speedtest-cli-ng/
├── AGENTS.md                        # Modernization blueprint & HA invariants
├── CHANGELOG.md                     # Release history (3.0.0+)
├── CONTRIBUTING.md                  # Contribution guidelines
├── LICENSE                          # Apache License 2.0
├── README.md                        # Documentation & usage examples
├── pyproject.toml                   # uv + hatchling + ruff + mypy + pytest config
├── uv.lock                          # Locked dependencies
├── speedtest-cli.1                  # Man page
├── speedtest/
│   ├── __init__.py                  # Public exports (drop-in compatible with 2.1.3)
│   ├── __main__.py                  # `python -m speedtest` entrypoint
│   ├── py.typed                     # PEP 561 marker
│   ├── exceptions.py                # SpeedtestException hierarchy
│   ├── models.py                    # SpeedtestResults, ServerDict, helpers
│   ├── metrics.py                   # Trimmed-mean estimator, jitter, stream scaling
│   ├── async_client.py              # Native asyncio AsyncSpeedtest engine
│   ├── client.py                    # Drop-in synchronous Speedtest facade
│   ├── cli.py                       # Modern argparse CLI (speedtest / speedtest-cli)
│   └── transport/
│       ├── __init__.py
│       ├── tcp.py                   # Clean-room OoklaServer port 8080 TCP protocol
│       └── http.py                  # Async aiohttp HTTP/HTTPS fallback & discovery
├── tests/
│   ├── conftest.py                  # Fixtures & loopback mock OoklaServer
│   ├── test_ha_compat.py            # Live HA Core speedtestdotnet contract tests
│   ├── test_tcp_transport.py        # OoklaServer TCP protocol & error tests
│   ├── test_http_transport.py       # HTTP fallback, JSON/XML discovery & error tests
│   ├── test_async_client.py         # AsyncSpeedtest & metrics tests
│   ├── test_client.py               # Sync Speedtest facade & results tests
│   ├── test_cli.py                  # CLI flags & exit handling tests
│   └── test_real_api_calls.py       # @pytest.mark.real_api_call live tests
└── .github/workflows/
    ├── ci.yml                       # Lint, format, mypy, pytest matrix, build
    ├── live_api.yml                 # Scheduled & manual live API verification
    └── publish.yml                  # PyPI OIDC trusted publish
```

---

## 4. Domain Invariants (Must Not Break)

Verified against live `homeassistant/components/speedtestdotnet` (`__init__.py`, `coordinator.py`, `diagnostics.py`, `sensor.py`, `quality_scale.yaml`, and `tests/components/speedtestdotnet/test_init.py`):

1. **Top-Level Imports:** `import speedtest` must expose `Speedtest`, `SpeedtestException`, `ConfigRetrievalError`, `NoMatchedServers`, `SpeedtestBestServerFailure`, and all existing exception subclasses.
2. **HA Core Call Sequence:**
   - `api = speedtest.Speedtest(secure=True)` (runs in executor; raises `ConfigRetrievalError` on setup failure).
   - `api.get_servers()` -> returns `dict[float, list[dict[str, Any]]]` keyed by distance, where each server dict contains `"country"`, `"name"`, `"sponsor"`, `"cc"`, and `"id"`.
   - `api.closest.clear()` -> `api.closest` must remain a mutable `list`.
   - `api.get_servers(servers=[server_id])` -> filters by server ID (accepts `int` or numeric `str`); raises `speedtest.NoMatchedServers` when none match.
   - `best_server = api.get_best_server()` -> returns server dict with `"id"`, `"name"`, `"country"`, `"cc"`, `"sponsor"`, and `"latency"`.
   - `api.download()` and `api.upload()` -> populate `api.results` and return speeds in **bits per second (bps)** as `float`.
   - `api.results.dict()` -> returns `dict[str, Any]` containing `"download"` (bps), `"upload"` (bps), `"ping"` (ms), `"server"` (dict with `"id"`, `"name"`, `"country"`, `"cc"`, `"sponsor"`, `"latency"`), `"timestamp"` (ISO-8601 UTC string), `"bytes_sent"`, `"bytes_received"`, `"share"`, and `"client"` (plus additive metrics).
3. **Exception Hierarchy:** All runtime errors inherit from `speedtest.SpeedtestException`.
4. **Units:** `download` and `upload` are always **bits per second**; `ping`, `jitter`, and loaded latency are **milliseconds**.
5. **CLI Compatibility:** Preserves `--simple`, `--json`, `--csv`, `--csv-delimiter`, `--csv-header`, `--bytes`, `--server`, `--exclude`, `--list`, `--mini`, `--no-download`, `--no-upload`, `--single`, `--secure`, `--no-secure`, `--source`, `--timeout`, `--version`, `--no-pre-allocate`, and `--protocol {auto,tcp,http}`.
6. **Platinum Quality Scale Target in HA Core:** `quality_scale.yaml` lists `async-dependency`, `inject-websession`, and `strict-typing` as remaining Platinum blockers — addressed by `AsyncSpeedtest(session=...)` and `py.typed`.

---

## 5. Development, Testing & Release Guidelines

- **Git Identity:** Always commit under `Marco Sousa <marcomsousa@gmail.com>` (`marcomsousa` GitHub account). Follow Conventional Commits (`chore:`, `feat:`, `fix:`, `refactor:`, `test:`, `ci:`, `docs:`).
- **Standard Verification Commands:**
  ```bash
  uv sync --locked --all-groups
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
  - **PR 1:** Minimal dependency swap only (`speedtest-cli==2.1.3` -> `speedtest-cli-ng==X.Y.Z` in `manifest.json` and `requirements_all.txt`) leveraging drop-in compatibility.
  - **PR 2:** Follow-up PR after PR 1 merges to adopt `AsyncSpeedtest`, injectable `aiohttp.ClientSession`, `"loggers": ["speedtest"]`, jitter/bufferbloat sensors, and companion HA cleanup (`coordinator.servers` clearing, `DEFAULT_SERVER` constant in `config_flow.py`, upload assertion in `test_sensor.py`).
- **Legacy Upstream Branches Note:** The mirror preserves 21 historical upstream branches (`24test`, `devel`, `modularize`, `socket`, etc.) for reference; confirm before pruning non-bot branches from `origin`.
