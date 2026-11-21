# Execution Plan — `speedtest-cli-ng` `3.0.0` Modernization

## Scope & Hard Constraints

- Implement all improvements from Sections 1–4 of the audit:
  1. **Live Bugs & Runtime Defects:** Primary JSON server discovery (`/api/js/servers`) with XML fallback/supplement (HA `#179173`, `#115775`, `#135862`), removal of phantom `1,800,000 ms` latency values (HA `#174958`), elimination of memory/socket/fd leaks and import-time fd wrapping (HA `#191133`), refreshed timestamps & `_share` across reused instances, XML missing-tag `NameError` fix, non-200 config `KeyError` fix, caller list immutability, idempotent `get_closest_servers()`, CLI `--exclude` `TypeError` fix, and modern User-Agent to avoid HTTP `403`.
  2. **Engine, Protocol & Accuracy Upgrades:** Native `asyncio` engine (`AsyncSpeedtest`) with optional injectable `aiohttp.ClientSession` (unblocking HA Platinum `async-dependency`, `inject-websession`, and `strict-typing`), clean-room `OoklaServer` raw TCP (`:8080`) transport with automatic async HTTP fallback, dynamic multi-stream scaling + warm-up/trimmed-mean bandwidth estimation, concurrent loaded latency (bufferbloat) & jitter metrics, standard `logging.getLogger("speedtest")`, and 100% drop-in synchronous `Speedtest` facade safe across executor threads and active event loops.
  3. **Modularization, Typing & Packaging Modernization:** Split the 2,013-line `speedtest.py` monolith into a strictly typed (`mypy --strict`, `py.typed`) `speedtest/` package, remove Python 2.4–3.10 shims, migrate packaging to `uv` + `hatchling` (`pyproject.toml` + `uv.lock`, `requires-python = ">=3.11"`), add `README.md` + `CHANGELOG.md` + `PLAN.md`, and update `AGENTS.md`.
  4. **Testing & CI/CD:** Deterministic 100%-offline `pytest` suite (`filterwarnings = ["error"]`, `--cov=speedtest --cov-branch`, `fail_under = 95`), `tests/test_ha_compat.py`, opt-in `@pytest.mark.real_api_call` live integration tests, and GitHub Actions workflows (`.github/workflows/ci.yml`, `publish.yml`, `live_api.yml`).
- **Exclusions:**
  - Do **not** create any Home Assistant Core PR.
  - Do **not** create a GitHub Release or publish to PyPI.
  - Do **not** delete any of the 21 legacy upstream branches on `origin`.
- **Attribution & Git Identity:** Preserve Apache License 2.0 (`LICENSE`) and original attribution (`Copyright 2012 Matt Martz`). Commit under `Marco Sousa <marcomsousa@gmail.com>` with atomic Conventional Commits.

---

## Step 1 — Packaging & Repo Configuration (`uv` + `hatchling`)

1. Remove obsolete legacy files: `setup.py`, `setup.cfg`, `MANIFEST.in`, `tox.ini`, `.travis.yml`, `README.rst`, `tests/scripts/source.py`.
2. Update `.gitignore` for `.venv/`, `.mypy_cache/`, `.pytest_cache/`, `.ruff_cache/`, `.env`, `__pycache__/`, `htmlcov/`, `coverage.xml`, `*.egg-info/`.
3. Create `pyproject.toml` (PEP 621 / 639 / 735) with `name = "speedtest-cli-ng"`, `version = "3.0.0"`, `requires-python = ">=3.11"`, `license = "Apache-2.0"`, `dependencies = ["aiohttp>=3.10.0"]`, `[project.scripts]` (`speedtest` and `speedtest-cli`), `[dependency-groups].dev` (`aioresponses`, `mypy`, `pytest`, `pytest-asyncio`, `pytest-cov`, `ruff`), `ruff` (`TC`, not deprecated `TCH`), `mypy --strict` (`warn_unreachable = true`), and `pytest` (`filterwarnings = ["error"]`, `--cov-branch`, `fail_under = 95`).
4. Run `uv lock && uv sync --all-groups`.

---

## Step 2 — Modular `speedtest/` Package Architecture

Replace `speedtest.py` with the `speedtest/` package:

- `speedtest/py.typed` — PEP 561 marker.
- `speedtest/exceptions.py` — Full drop-in exception hierarchy (`SpeedtestException` base and all upstream subclasses plus `SpeedtestTransferError`).
- `speedtest/models.py` — `ProtocolName`, `ActiveProtocol`, `ServerDict`, `utc_timestamp_iso()`, `distance()`, `build_user_agent()`, `do_nothing()`, `FakeShutdownEvent`, and `SpeedtestResults` (`dict()`, `csv_header()`, `csv()`, `json()`, `share()`, `touch_timestamp()`, plus additive `jitter`, `loaded_ping_download`, `loaded_ping_upload`, `packet_loss`, `protocol` fields).
- `speedtest/metrics.py` — `TransferProgress`, `TransferResult`, `compute_jitter()`, `compute_latency_stats()`, `estimate_bandwidth_bps()` (warm-up discard + trimmed mean), and `desired_stream_count()`.
- `speedtest/transport/tcp.py` — Clean-room `OoklaServer` raw TCP (`:8080`) protocol (`HI`/`HELLO`, `PING`/`PONG`, `DOWNLOAD`, header-inclusive `UPLOAD <total_bytes> 0`, `QUIT`), shared 256 KiB `memoryview` `UPLOAD_PAYLOAD`, dynamic stream scaling, and concurrent loaded-latency probing.
- `speedtest/transport/http.py` — Keep-alive `aiohttp` transport, `fetch_config()`, `fetch_servers()` (primary `/api/js/servers` JSON + XML fallback/supplement, non-mutating filters, `ServersRetrievalError` when all endpoints fail), `measure_http_latency()` (preserving `.i` cache-busters, returning `None` on dead servers), streaming `run_http_transfer()`, `resolve_mini_server()`, and `share_results()`.
- `speedtest/async_client.py` — `AsyncSpeedtest` with optional injected `aiohttp.ClientSession`, `protocol={"auto","tcp","http"}`, automatic TCP -> HTTP fallback under `"auto"`, and clean `aclose()` lifecycle.
- `speedtest/client.py` — Drop-in synchronous `Speedtest` facade executing safely across arbitrary worker threads (`hass.async_add_executor_job`) and inside running event loops via a per-call closed loop/executor helper.
- `speedtest/cli.py`, `speedtest/__main__.py`, `speedtest/__init__.py` — Modern `argparse` CLI preserving all legacy flags plus `--protocol {auto,tcp,http}` and `--no-secure`, standard `logging`, safe signal restoration, and full top-level exports.

---

## Step 3 — Documentation

- `README.md` — Badges, installation (`uv add` / `pip`), CLI & async/sync examples, Apache-2.0 attribution to `sivel/speedtest-cli` and Matt Martz.
- `CHANGELOG.md` — `3.0.0` release notes mapping bug fixes to HA Core `#191133`, `#179173`, `#178446`, `#174958`, `#115775`, `#135862`.
- `AGENTS.md` — Updated architecture map, resolved debt summary, live HA Core compatibility invariants, and development/release rules.
- `PLAN.md` — This modernization plan.

---

## Step 4 — Deterministic Offline & Opt-In Live Test Suite

- `tests/conftest.py` — Sample XML/JSON fixtures and in-process loopback `MockOoklaServer` simulator (`HI`/`HELLO`, `PING`/`PONG`, `DOWNLOAD`, header-inclusive `UPLOAD`, `QUIT`, packet loss, bad handshake, EOF/error modes).
- `tests/test_ha_compat.py` — Live HA Core `speedtestdotnet` call-sequence contract across separate `asyncio.to_thread` workers, timestamp refresh across reused polls, external `aiohttp.ClientSession` preservation, logger verification, and zero file-descriptor leak verification (`/proc/self/fd`).
- `tests/test_tcp_transport.py` — Target parsing, wire framing, loaded ping, packet loss, errors, and cancellation.
- `tests/test_http_transport.py` — `aioresponses` coverage for config/server XML & JSON parsing, fallback/supplement, non-mutation, cache-busters, dead servers, streaming transfers, Mini servers, and sharing.
- `tests/test_async_client.py` — `AsyncSpeedtest` fallback, explicit protocol failures, tie-breaking, idempotent `get_closest_servers()`, and metrics edge cases.
- `tests/test_client.py` — Sync `Speedtest` facade (outside and inside active event loops), `.best`, `SpeedtestResults`, and helpers.
- `tests/test_cli.py` — CLI flags, `--exclude` without `--server`, error exits, JSON/CSV/simple outputs.
- `tests/test_real_api_calls.py` — Opt-in `@pytest.mark.real_api_call` live Speedtest.net smoke tests.

---

## Step 5 — GitHub Actions Workflows

- `.github/workflows/ci.yml` — `push` (`master`), `pull_request`, `workflow_dispatch`; Python `3.11`–`3.14` matrix; `actions/checkout@v7`, `astral-sh/setup-uv@v10.4.0`, `uv sync --locked --all-groups`, `ruff check`, `ruff format --check`, `mypy`, `pytest`, `uv build`.
- `.github/workflows/publish.yml` — `release: types: [published]` + `workflow_dispatch`; PyPI OIDC Trusted Publishing (`id-token: write`, `environment: pypi`, `pypa/gh-action-pypi-publish@release/v1`, `attestations: true`).
- `.github/workflows/live_api.yml` — Weekly cron + `workflow_dispatch` running `uv run pytest -m real_api_call --no-cov`.

---

## Step 6 — Verification, Commits & CI Watch

1. `uv lock && uv sync --locked --all-groups`
2. `uv run ruff check --no-fix . && uv run ruff format --check . && uv run mypy speedtest tests && uv run pytest`
3. Multi-Python matrix across `3.11`, `3.12`, `3.13`, `3.14` (`uv run --python "$py" pytest -q`)
4. `uvx yamllint ... .github/workflows`
5. `uv build` + isolated wheel check via `python -I` + `speedtest --version`
6. Opt-in live test (`uv run pytest -m real_api_call --no-cov -v`)
7. Atomic Conventional Commits under `Marco Sousa <marcomsousa@gmail.com>`, push `origin master`, and watch `.github/workflows/ci.yml` until green.

---

## Pitfalls Recorded

- **`OoklaServer` `UPLOAD` framing:** `UPLOAD <total_bytes> 0\n` includes the ASCII header length inside `<total_bytes>`; send `total_bytes - len(header)` payload bytes.
- **Sync `Speedtest` across worker threads:** Close any internal `aiohttp.ClientSession` before exiting `asyncio.run()` so subsequent calls on other HA executor threads never touch a closed loop.
- **Zero-warning policy (`filterwarnings = ["error"]`):** Cleanly close all loopback servers, client writers, and `aiohttp` sessions in `finally` blocks.
