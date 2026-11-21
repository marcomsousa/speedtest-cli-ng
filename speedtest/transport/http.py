"""Async HTTP/HTTPS fallback transport and Speedtest.net server discovery."""

from __future__ import annotations

import asyncio
import logging
import math
import posixpath
import re
import time
import xml.etree.ElementTree as ET
from hashlib import md5
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import parse_qs, urlsplit, urlunsplit

import aiohttp

from ..exceptions import (
    ConfigRetrievalError,
    InvalidServerIDType,
    InvalidSpeedtestMiniServer,
    NoMatchedServers,
    ServersRetrievalError,
    ShareResultsConnectFailure,
    ShareResultsSubmitFailure,
    SpeedtestConfigError,
    SpeedtestMiniConnectFailure,
    SpeedtestServersError,
    SpeedtestTransferError,
    SpeedtestUploadTimeout,
)
from ..metrics import (
    TransferProgress,
    TransferResult,
    compute_latency_stats,
    estimate_bandwidth_bps,
)
from ..models import build_user_agent, distance

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Mapping, Sequence

    from ..models import SpeedtestResults

_LOGGER = logging.getLogger("speedtest.transport.http")

CHUNK_SIZE = 262_144  # 256 KiB
_UPLOAD_CHUNK = memoryview(b"0" * CHUNK_SIZE)
_UPLOAD_SIZES = (32_768, 65_536, 131_072, 262_144, 524_288, 1_048_576, 7_340_032)
_DOWNLOAD_SIZES = (350, 500, 750, 1000, 1500, 2000, 2500, 3000, 3500, 4000)


def create_session(
    *,
    timeout: float = 10.0,
    source_address: str | None = None,
    user_agent: str | None = None,
) -> aiohttp.ClientSession:
    """Create an ``aiohttp.ClientSession`` with pooling and optional source IP."""
    local_addr = (source_address, 0) if source_address else None
    connector = aiohttp.TCPConnector(
        limit=32,
        limit_per_host=16,
        local_addr=local_addr,
    )
    ua = user_agent or build_user_agent()
    return aiohttp.ClientSession(
        connector=connector,
        timeout=aiohttp.ClientTimeout(total=timeout),
        headers={"User-Agent": ua},
        trust_env=True,
    )


SPEEDTEST_HOST = "www.speedtest.net"
LEGACY_SPEEDTEST_HOST = "c.speedtest.net"


def _scheme(secure: bool) -> str:
    return "https" if secure else "http"


def speedtest_url(path: str, *, secure: bool, host: str = SPEEDTEST_HOST) -> str:
    """Build an absolute speedtest.net endpoint URL.

    Centralizing endpoint construction keeps every request honoring ``secure``
    and gives the offline test-suite a single seam to redirect to loopback.
    """
    return f"{_scheme(secure)}://{host}{path}"


def _server_base_url(url: str) -> str:
    parsed = urlsplit(url)
    parent = posixpath.dirname(parsed.path).rstrip("/")
    return urlunsplit((parsed.scheme, parsed.netloc, parent, "", ""))


def resolve_server_url(server: Mapping[str, Any], *, secure: bool) -> str:
    """Normalize a server URL and upgrade HTTP -> HTTPS when supported."""
    raw_url = str(server.get("url", "")).strip()
    if raw_url.startswith("://"):
        return f"{_scheme(secure)}{raw_url}"
    if secure and raw_url.startswith("http://") and server.get("https_functional"):
        return f"https://{raw_url.removeprefix('http://')}"
    return raw_url


def _parse_server_id_list(values: Sequence[int | str] | None) -> set[int]:
    if not values:
        return set()
    parsed: set[int] = set()
    for val in values:
        if isinstance(val, bool) or not isinstance(val, (int, str)):
            raise InvalidServerIDType(f"{val} is an invalid server type, must be int")
        try:
            parsed.add(int(val))
        except ValueError as err:
            raise InvalidServerIDType(
                f"{val} is an invalid server type, must be int"
            ) from err
    return parsed


def _normalize_server(
    raw: Mapping[str, Any],
    origin: tuple[float, float],
) -> tuple[int, dict[str, Any]] | None:
    try:
        server_id = int(raw["id"])
        lat = float(raw["lat"])
        lon = float(raw["lon"])
    except (KeyError, TypeError, ValueError):
        return None

    url = str(raw.get("url", ""))
    host = str(raw.get("host", ""))
    if not host and url:
        host = urlsplit(url).netloc

    normalized: dict[str, Any] = dict(raw)
    normalized.update(
        {
            "id": str(server_id),
            "url": url,
            "lat": str(raw["lat"]),
            "lon": str(raw["lon"]),
            "name": str(raw.get("name", "")),
            "country": str(raw.get("country", "")),
            "cc": str(raw.get("cc", "")),
            "sponsor": str(raw.get("sponsor", "")),
            "host": host,
            "d": distance(origin, (lat, lon)),
        }
    )
    return server_id, normalized


async def fetch_config(
    session: aiohttp.ClientSession,
    *,
    secure: bool = True,
    request_timeout: float = 10.0,
) -> tuple[dict[str, Any], tuple[float, float]]:
    """Fetch and parse ``speedtest-config.php`` XML."""
    stamp = int(time.time() * 1000)
    url = speedtest_url(f"/speedtest-config.php?x={stamp}", secure=secure)

    try:
        async with (
            asyncio.timeout(request_timeout),
            session.get(url, headers={"Cache-Control": "no-cache"}) as resp,
        ):
            if resp.status != 200:
                raise ConfigRetrievalError(
                    f"HTTP {resp.status} retrieving speedtest configuration"
                )
            payload = await resp.read()
    except ConfigRetrievalError:
        raise
    except (aiohttp.ClientError, TimeoutError, OSError) as err:
        raise ConfigRetrievalError(err) from err

    try:
        root = ET.fromstring(payload)
    except ET.ParseError as err:
        raise SpeedtestConfigError(
            f"Malformed speedtest.net configuration: {err}"
        ) from err

    server_cfg_el = root.find("server-config")
    download_el = root.find("download")
    upload_el = root.find("upload")
    client_el = root.find("client")
    if (
        server_cfg_el is None
        or download_el is None
        or upload_el is None
        or client_el is None
    ):
        raise SpeedtestConfigError(
            "Malformed speedtest.net configuration: missing required XML elements"
        )

    server_config = server_cfg_el.attrib
    download = download_el.attrib
    upload = upload_el.attrib
    client = dict(client_el.attrib)

    try:
        ignore_servers = [int(i) for i in server_config["ignoreids"].split(",") if i]
        ratio = int(upload["ratio"])
        upload_max = int(upload["maxchunkcount"])
        up_slice = list(_UPLOAD_SIZES[max(0, ratio - 1) :])
        if not up_slice:
            up_slice = [_UPLOAD_SIZES[-1]]
        upload_count = math.ceil(upload_max / len(up_slice))
        threads_ul = int(upload["threads"])
        threads_dl = int(server_config["threadcount"]) * 2
        counts = {
            "upload": upload_count,
            "download": int(download["threadsperurl"]),
        }
        threads = {
            "upload": threads_ul,
            "download": threads_dl,
        }
        length = {
            "upload": int(upload["testlength"]),
            "download": int(download["testlength"]),
        }
    except (KeyError, ValueError, ZeroDivisionError) as err:
        raise SpeedtestConfigError(
            f"Malformed speedtest.net configuration attributes: {err}"
        ) from err

    try:
        lat_lon = (float(client["lat"]), float(client["lon"]))
    except (KeyError, ValueError) as err:
        raise SpeedtestConfigError(
            f"Unknown location: lat={client.get('lat')!r} lon={client.get('lon')!r}"
        ) from err

    config: dict[str, Any] = {
        "client": client,
        "ignore_servers": ignore_servers,
        "sizes": {
            "upload": up_slice,
            "download": list(_DOWNLOAD_SIZES),
        },
        "counts": counts,
        "threads": threads,
        "length": length,
        "upload_max": upload_count * len(up_slice),
    }
    return config, lat_lon


async def _fetch_json_servers(
    session: aiohttp.ClientSession,
    *,
    secure: bool,
    request_timeout: float,
    origin: tuple[float, float],
) -> list[tuple[int, dict[str, Any]]]:
    query = (
        "engine=js&https_functional=true&limit=100" if secure else "engine=js&limit=100"
    )
    url = speedtest_url(f"/api/js/servers?{query}", secure=secure)
    async with (
        asyncio.timeout(request_timeout),
        session.get(url, headers={"Cache-Control": "no-cache"}) as resp,
    ):
        if resp.status != 200:
            raise ServersRetrievalError(
                f"HTTP {resp.status} retrieving JSON server list"
            )
        data = await resp.json(content_type=None)

    if not isinstance(data, list):
        raise SpeedtestServersError("Speedtest.net JSON server list must be a list")

    parsed: list[tuple[int, dict[str, Any]]] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        item = _normalize_server(entry, origin)
        if item is not None:
            parsed.append(item)
    return parsed


async def _fetch_xml_servers(
    session: aiohttp.ClientSession,
    *,
    url: str,
    request_timeout: float,
    origin: tuple[float, float],
) -> list[tuple[int, dict[str, Any]]]:
    async with (
        asyncio.timeout(request_timeout),
        session.get(url, headers={"Cache-Control": "no-cache"}) as resp,
    ):
        if resp.status != 200:
            raise ServersRetrievalError(
                f"HTTP {resp.status} retrieving XML server list from {url}"
            )
        payload = await resp.read()

    try:
        root = ET.fromstring(payload)
    except ET.ParseError as err:
        raise SpeedtestServersError(
            f"Malformed speedtest.net server list: {err}"
        ) from err

    parsed: list[tuple[int, dict[str, Any]]] = []
    for elem in root.iter("server"):
        item = _normalize_server(elem.attrib, origin)
        if item is not None:
            parsed.append(item)
    return parsed


async def fetch_servers(
    session: aiohttp.ClientSession,
    *,
    config: Mapping[str, Any],
    lat_lon: tuple[float, float],
    secure: bool = True,
    request_timeout: float = 10.0,
    servers: Sequence[int | str] | None = None,
    exclude: Sequence[int | str] | None = None,
) -> dict[float, list[dict[str, Any]]]:
    """Retrieve and filter Speedtest.net servers (JSON primary + XML fallback)."""
    include_ids = _parse_server_id_list(servers)
    exclude_ids = _parse_server_id_list(exclude)
    ignore_ids = set(config.get("ignore_servers", ()))

    by_id: dict[int, dict[str, Any]] = {}
    errors: list[str] = []
    successful_fetches = 0

    try:
        json_items = await _fetch_json_servers(
            session,
            secure=secure,
            request_timeout=request_timeout,
            origin=lat_lon,
        )
        if json_items:
            successful_fetches += 1
            for server_id, entry in json_items:
                by_id.setdefault(server_id, entry)
    except (
        aiohttp.ClientError,
        TimeoutError,
        OSError,
        ValueError,
        ServersRetrievalError,
        SpeedtestServersError,
    ) as err:
        errors.append(str(err))
        _LOGGER.debug("Primary JSON server discovery failed: %s", err)

    missing_requested = bool(include_ids - by_id.keys())
    if not by_id or missing_requested:
        threads_dl = int(config.get("threads", {}).get("download", 8))
        stamp = int(time.time() * 1000)
        query = f"?threads={threads_dl}&x={stamp}"
        xml_urls = [
            speedtest_url(f"/speedtest-servers-static.php{query}", secure=secure),
            speedtest_url(f"/speedtest-servers.php{query}", secure=secure),
        ]
        if not secure:
            xml_urls.extend(
                [
                    speedtest_url(
                        f"/speedtest-servers-static.php{query}",
                        secure=False,
                        host=LEGACY_SPEEDTEST_HOST,
                    ),
                    speedtest_url(
                        f"/speedtest-servers.php{query}",
                        secure=False,
                        host=LEGACY_SPEEDTEST_HOST,
                    ),
                ]
            )

        for idx, xml_url in enumerate(xml_urls):
            try:
                xml_items = await _fetch_xml_servers(
                    session,
                    url=f"{xml_url}.{idx}",
                    request_timeout=request_timeout,
                    origin=lat_lon,
                )
            except (
                aiohttp.ClientError,
                TimeoutError,
                OSError,
                ServersRetrievalError,
                SpeedtestServersError,
            ) as err:
                errors.append(str(err))
                _LOGGER.debug("XML server endpoint %s failed: %s", xml_url, err)
                continue

            if xml_items:
                successful_fetches += 1
                for server_id, entry in xml_items:
                    by_id.setdefault(server_id, entry)
                if not (include_ids - by_id.keys()):
                    break

    if successful_fetches == 0:
        raise ServersRetrievalError(
            "; ".join(errors) if errors else "Unable to retrieve server list"
        )

    grouped: dict[float, list[dict[str, Any]]] = {}
    for server_id, entry in by_id.items():
        if include_ids and server_id not in include_ids:
            continue
        if server_id in ignore_ids or server_id in exclude_ids:
            continue
        dist = float(entry["d"])
        grouped.setdefault(dist, []).append(entry)

    if (include_ids or exclude_ids) and not grouped:
        raise NoMatchedServers()

    return grouped


async def measure_http_latency(
    session: aiohttp.ClientSession,
    server: Mapping[str, Any],
    *,
    secure: bool = True,
    request_timeout: float = 10.0,
    ping_count: int = 3,
) -> tuple[float, float, float] | None:
    """Measure HTTP ``latency.txt`` RTT, returning ``None`` when all probes fail."""
    if ping_count <= 0:
        raise ValueError("ping_count must be positive")
    base_url = _server_base_url(resolve_server_url(server, secure=secure))
    stamp = int(time.time() * 1000)

    rtts_ms: list[float] = []
    failed = 0
    for idx in range(ping_count):
        latency_url = f"{base_url}/latency.txt?x={stamp}.{idx}"
        start = time.perf_counter()
        try:
            async with (
                asyncio.timeout(request_timeout),
                session.get(latency_url, headers={"Cache-Control": "no-cache"}) as resp,
            ):
                body = await resp.read()
        except (aiohttp.ClientError, TimeoutError, OSError):
            failed += 1
            continue

        elapsed_ms = (time.perf_counter() - start) * 1000.0
        if resp.status == 200 and body[:9] == b"test=test":
            rtts_ms.append(elapsed_ms)
        else:
            failed += 1

    if not rtts_ms:
        return None

    avg_ms, jitter_ms = compute_latency_stats(rtts_ms)
    packet_loss = round((failed / ping_count) * 100.0, 2)
    return avg_ms, jitter_ms, packet_loss


async def _upload_body_generator(
    size: int,
    deadline: float,
    on_bytes: Callable[[int], None],
    is_cancelled: Callable[[], bool],
) -> AsyncIterator[bytes | memoryview]:
    prefix = b"content1="
    if size <= len(prefix):
        chunk = prefix[:size]
        yield chunk
        on_bytes(len(chunk))
        return

    yield prefix
    on_bytes(len(prefix))
    remaining = size - len(prefix)

    while remaining > 0:
        if is_cancelled() or time.monotonic() >= deadline:
            raise SpeedtestUploadTimeout()
        n = min(CHUNK_SIZE, remaining)
        yield bytes(_UPLOAD_CHUNK[:n])
        remaining -= n
        on_bytes(n)
        await asyncio.sleep(0)


async def run_http_transfer(
    session: aiohttp.ClientSession,
    server: Mapping[str, Any],
    *,
    direction: Literal["download", "upload"],
    config: Mapping[str, Any],
    secure: bool = True,
    duration: float = 10.0,
    request_timeout: float = 10.0,
    threads: int | None = None,
    is_cancelled: Callable[[], bool] = lambda: False,
    callback: Callable[..., Any] | None = None,
) -> TransferResult:
    """Execute a concurrent keep-alive HTTP download or upload transfer."""
    target_url = resolve_server_url(server, secure=secure)
    base_url = _server_base_url(target_url)

    if direction == "download":
        dl_sizes = list(config.get("sizes", {}).get("download", _DOWNLOAD_SIZES))
        dl_count = int(config.get("counts", {}).get("download", 4))
        stamp = int(time.time() * 1000)
        urls: list[str] = []
        bump = 0
        for sz in reversed(dl_sizes):
            for _ in range(dl_count):
                urls.append(f"{base_url}/random{sz}x{sz}.jpg?x={stamp}.{bump}")
                bump += 1
        max_workers = threads or int(config.get("threads", {}).get("download", 8))
    else:
        ul_sizes = list(config.get("sizes", {}).get("upload", _UPLOAD_SIZES))
        ul_count = int(config.get("counts", {}).get("upload", 2))
        ul_max = int(config.get("upload_max", ul_count * len(ul_sizes)))
        upload_sizes = [sz for sz in ul_sizes for _ in range(ul_count)][:ul_max]
        max_workers = threads or int(config.get("threads", {}).get("upload", 8))

    progress = TransferProgress()
    errors: list[Exception] = []
    start = time.monotonic()
    deadline = start + max(0.05, duration)
    progress.snapshot(start)
    sem = asyncio.Semaphore(max(1, max_workers))

    async def download_one(idx: int, url: str, total_jobs: int) -> None:
        if callback is not None:
            callback(idx, total_jobs, start=True)
        try:
            async with sem:
                if is_cancelled() or time.monotonic() >= deadline:
                    return
                rem = max(0.1, deadline - time.monotonic())
                async with (
                    asyncio.timeout(min(request_timeout, rem)),
                    session.get(url, headers={"Cache-Control": "no-cache"}) as resp,
                ):
                    if resp.status != 200:
                        errors.append(
                            SpeedtestTransferError(f"HTTP {resp.status} on {url}")
                        )
                        return
                    async for chunk in resp.content.iter_chunked(CHUNK_SIZE):
                        progress.record(len(chunk))
                        if is_cancelled() or time.monotonic() >= deadline:
                            break
        except asyncio.CancelledError:
            raise
        except (aiohttp.ClientError, TimeoutError, OSError) as err:
            errors.append(err)
        finally:
            if callback is not None:
                callback(idx, total_jobs, end=True)

    async def upload_one(idx: int, size: int, total_jobs: int) -> None:
        if callback is not None:
            callback(idx, total_jobs, start=True)
        stamp = int(time.time() * 1000)
        url = f"{target_url}{'&' if '?' in target_url else '?'}x={stamp}.{idx}"
        try:
            async with sem:
                if is_cancelled() or time.monotonic() >= deadline:
                    return
                rem = max(0.1, deadline - time.monotonic())
                async with (
                    asyncio.timeout(min(request_timeout, rem)),
                    session.post(
                        url,
                        data=_upload_body_generator(
                            size, deadline, progress.record, is_cancelled
                        ),
                        headers={
                            "Content-Length": str(size),
                            "Cache-Control": "no-cache",
                        },
                    ) as resp,
                ):
                    await resp.read()
        except asyncio.CancelledError:
            raise
        except SpeedtestUploadTimeout:
            pass
        except (aiohttp.ClientError, TimeoutError, OSError) as err:
            errors.append(err)
        finally:
            if callback is not None:
                callback(idx, total_jobs, end=True)

    if direction == "download":
        tasks = [
            asyncio.create_task(download_one(i, u, len(urls)))
            for i, u in enumerate(urls)
        ]
    else:
        tasks = [
            asyncio.create_task(upload_one(i, sz, len(upload_sizes)))
            for i, sz in enumerate(upload_sizes)
        ]

    async def sampler_loop() -> None:
        interval = min(0.25, max(0.05, duration / 10.0))
        while not is_cancelled() and time.monotonic() < deadline:
            await asyncio.sleep(interval)
            progress.snapshot(time.monotonic())

    sampler_task = asyncio.create_task(sampler_loop())
    try:
        await asyncio.gather(*tasks)
    finally:
        sampler_task.cancel()
        pending = [t for t in tasks if not t.done()]
        for task in pending:
            task.cancel()
        await asyncio.gather(sampler_task, *pending, return_exceptions=True)

    stop = time.monotonic()
    progress.snapshot(stop)
    elapsed = max(stop - start, 1e-6)

    if progress.bytes_transferred == 0 and errors and not is_cancelled():
        raise SpeedtestTransferError(
            f"All HTTP {direction} workers failed: {errors[0]}"
        ) from errors[0]

    bps = estimate_bandwidth_bps(progress.samples, progress.bytes_transferred, elapsed)
    return TransferResult(
        bps=bps,
        total_bytes=progress.bytes_transferred,
        loaded_ping_ms=None,
        protocol="http",
    )


async def resolve_mini_server(
    session: aiohttp.ClientSession,
    server_url: str,
    *,
    request_timeout: float = 10.0,
) -> list[dict[str, Any]]:
    """Probe and normalize a self-hosted Speedtest Mini server."""
    urlparts = urlsplit(server_url)
    _, ext = posixpath.splitext(urlparts.path)
    base_url = posixpath.dirname(server_url) if ext else server_url
    base_url = base_url.rstrip("/")

    try:
        async with (
            asyncio.timeout(request_timeout),
            session.get(base_url, headers={"Cache-Control": "no-cache"}) as resp,
        ):
            if resp.status != 200:
                raise SpeedtestMiniConnectFailure(f"Failed to connect to {server_url}")
            text = await resp.text(errors="replace")
    except SpeedtestMiniConnectFailure:
        raise
    except (aiohttp.ClientError, TimeoutError, OSError) as err:
        raise SpeedtestMiniConnectFailure(f"Failed to connect to {server_url}") from err

    extensions = re.findall(r'upload_?[Ee]xtension:\s*"([^"]+)"', text)
    if not extensions:
        for candidate in ("php", "asp", "aspx", "jsp"):
            probe_url = f"{base_url}/speedtest/upload.{candidate}"
            try:
                async with (
                    asyncio.timeout(request_timeout),
                    session.get(probe_url) as resp,
                ):
                    body = (await resp.text(errors="replace")).strip()
                if (
                    resp.status == 200
                    and len(body.splitlines()) == 1
                    and re.match(r"size=[0-9]", body)
                ):
                    extensions = [candidate]
                    break
            except (aiohttp.ClientError, TimeoutError, OSError):
                continue

    if not urlparts.netloc or not extensions:
        raise InvalidSpeedtestMiniServer(f"Invalid Speedtest Mini Server: {server_url}")

    return [
        {
            "sponsor": "Speedtest Mini",
            "name": urlparts.netloc,
            "d": 0.0,
            "url": f"{base_url}/speedtest/upload.{extensions[0]}",
            "latency": 0.0,
            "id": "0",
            "country": "",
            "cc": "",
            "host": urlparts.netloc,
        }
    ]


async def share_results(
    session: aiohttp.ClientSession,
    results: SpeedtestResults,
    *,
    secure: bool = True,
    request_timeout: float = 10.0,
) -> str:
    """Submit results to speedtest.net API and return the share image URL."""
    download_kbps = round(results.download / 1000.0)
    ping_ms = round(results.ping)
    upload_kbps = round(results.upload / 1000.0)
    server_id = str(results.server.get("id", ""))
    digest = md5(
        f"{ping_ms}-{upload_kbps}-{download_kbps}-297aae72".encode(),
        usedforsecurity=False,
    ).hexdigest()

    api_data = [
        f"recommendedserverid={server_id}",
        f"ping={ping_ms}",
        "screenresolution=",
        "promo=",
        f"download={download_kbps}",
        "screendpi=",
        f"upload={upload_kbps}",
        f"testmethod={results.protocol or 'http'}",
        f"hash={digest}",
        "touchscreen=none",
        "startmode=pingselect",
        "accuracy=1",
        f"bytesreceived={results.bytes_received}",
        f"bytessent={results.bytes_sent}",
        f"serverid={server_id}",
    ]

    scheme = _scheme(secure)
    url = speedtest_url("/api/api.php", secure=secure)
    headers = {
        "Referer": "https://c.speedtest.net/flash/speedtest.swf",
        "Content-Type": "application/x-www-form-urlencoded",
        "Cache-Control": "no-cache",
    }

    try:
        async with (
            asyncio.timeout(request_timeout),
            session.post(
                url,
                data="&".join(api_data).encode("ascii"),
                headers=headers,
            ) as resp,
        ):
            status = resp.status
            body = await resp.text(errors="replace")
    except (aiohttp.ClientError, TimeoutError, OSError) as err:
        raise ShareResultsConnectFailure(err) from err

    if status != 200:
        raise ShareResultsSubmitFailure("Could not submit results to speedtest.net")

    qsargs = parse_qs(body)
    result_ids = qsargs.get("resultid")
    if not result_ids or len(result_ids) != 1:
        raise ShareResultsSubmitFailure("Could not submit results to speedtest.net")

    return f"{scheme}://www.speedtest.net/result/{result_ids[0]}.png"
