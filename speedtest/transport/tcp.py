"""Clean-room OoklaServer raw TCP transport (port 8080)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
import time
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlsplit

from ..exceptions import SpeedtestTransferError
from ..metrics import (
    TransferProgress,
    TransferResult,
    compute_latency_stats,
    desired_stream_count,
    estimate_bandwidth_bps,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

_LOGGER = logging.getLogger("speedtest.transport.tcp")

CHUNK_SIZE = 262_144  # 256 KiB buffer
UPLOAD_PAYLOAD = memoryview(b"0" * CHUNK_SIZE)

_TCP_DOWNLOAD_CHUNK_BYTES = 10_000_000  # 10 MB per DOWNLOAD command
_TCP_UPLOAD_CHUNK_BYTES = 2_097_152  # 2 MiB per UPLOAD command
_DEFAULT_PORT = 8080


def parse_tcp_target(server: Mapping[str, Any]) -> tuple[str, int]:
    """Extract ``(host, port)`` for an OoklaServer raw TCP connection."""
    host_val = server.get("host")
    if isinstance(host_val, tuple) and len(host_val) == 2:
        return str(host_val[0]), int(host_val[1])

    if isinstance(host_val, str) and host_val.strip():
        parsed = urlsplit(f"//{host_val.strip()}")
        if parsed.hostname:
            return parsed.hostname, parsed.port or _DEFAULT_PORT

    url_val = server.get("url")
    if isinstance(url_val, str) and url_val.strip():
        raw_url = f"http{url_val}" if url_val.startswith("://") else url_val.strip()
        parsed = urlsplit(raw_url)
        if parsed.hostname:
            return parsed.hostname, parsed.port or _DEFAULT_PORT

    raise ValueError("Server dictionary does not contain a valid TCP host or url")


class OoklaTCPConnection:
    """Async client speaking the clean-room OoklaServer text/binary TCP protocol."""

    def __init__(
        self,
        host: str,
        port: int = _DEFAULT_PORT,
        *,
        timeout: float = 10.0,
        source_address: str | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.source_address = source_address
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

    async def __aenter__(self) -> OoklaTCPConnection:
        await self.connect()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    def _require_streams(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        if self._reader is None or self._writer is None:
            raise SpeedtestTransferError("TCP connection is not open")
        return self._reader, self._writer

    async def connect(self) -> None:
        """Open a TCP connection to the target OoklaServer."""
        local_addr = (self.source_address, 0) if self.source_address else None
        async with asyncio.timeout(self.timeout):
            reader, writer = await asyncio.open_connection(
                self.host,
                self.port,
                limit=CHUNK_SIZE,
                local_addr=local_addr,
            )
        sock = writer.get_extra_info("socket")
        if sock is not None:
            with contextlib.suppress(OSError):
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._reader = reader
        self._writer = writer

    async def handshake(self) -> str:
        """Send ``HI`` and validate the server ``HELLO`` banner."""
        reader, writer = self._require_streams()
        async with asyncio.timeout(self.timeout):
            writer.write(b"HI\n")
            await writer.drain()
            line = await reader.readline()

        banner = line.decode("ascii", errors="replace").strip()
        if not banner.startswith("HELLO "):
            raise SpeedtestTransferError(
                f"Unexpected OoklaServer handshake response: {banner!r}"
            )
        _LOGGER.debug("TCP handshake %s:%s -> %s", self.host, self.port, banner)
        return banner

    async def ping(
        self,
        count: int = 5,
        probe_timeout: float | None = None,
    ) -> tuple[float, float, float]:
        """Measure ``(avg_ms, jitter_ms, packet_loss_pct)`` via ``PING``/``PONG``."""
        if count <= 0:
            raise ValueError("ping count must be positive")
        reader, writer = self._require_streams()
        effective_timeout = self.timeout if probe_timeout is None else probe_timeout
        rtts_ms: list[float] = []
        failed = 0

        for _ in range(count):
            stamp = int(time.time() * 1000)
            start = time.perf_counter()
            try:
                async with asyncio.timeout(effective_timeout):
                    writer.write(f"PING {stamp}\n".encode("ascii"))
                    await writer.drain()
                    line = await reader.readline()
            except (TimeoutError, OSError):
                failed += 1
                continue

            elapsed_ms = (time.perf_counter() - start) * 1000.0
            if line.startswith(b"PONG "):
                rtts_ms.append(elapsed_ms)
            else:
                failed += 1

        if not rtts_ms:
            raise SpeedtestTransferError("All TCP ping probes failed")

        avg_ms, jitter_ms = compute_latency_stats(rtts_ms)
        packet_loss = round((failed / count) * 100.0, 2)
        return avg_ms, jitter_ms, packet_loss

    async def download_chunk(
        self,
        size_bytes: int,
        deadline: float,
        on_bytes: Callable[[int], None],
        is_cancelled: Callable[[], bool],
    ) -> int:
        """Issue ``DOWNLOAD <size>`` and read payload bytes until EOF or deadline."""
        reader, writer = self._require_streams()
        async with asyncio.timeout(self.timeout):
            writer.write(f"DOWNLOAD {size_bytes}\n".encode("ascii"))
            await writer.drain()

        remaining = size_bytes
        received = 0
        while remaining > 0 and not is_cancelled():
            now = time.monotonic()
            if now >= deadline:
                break
            step_timeout = min(self.timeout, max(0.05, deadline - now))
            try:
                async with asyncio.timeout(step_timeout):
                    data = await reader.read(min(CHUNK_SIZE, remaining))
            except TimeoutError:
                break
            if not data:
                break
            n = len(data)
            received += n
            remaining -= n
            on_bytes(n)

        return received

    async def upload_chunk(
        self,
        total_bytes: int,
        deadline: float,
        on_bytes: Callable[[int], None],
        is_cancelled: Callable[[], bool],
    ) -> int:
        """Issue ``UPLOAD <total_bytes> 0`` and stream header-inclusive payload."""
        reader, writer = self._require_streams()
        header = f"UPLOAD {total_bytes} 0\n".encode("ascii")
        payload_remaining = total_bytes - len(header)
        if payload_remaining <= 0:
            raise ValueError("total_bytes must exceed UPLOAD header length")

        writer.write(header)
        sent = len(header)
        on_bytes(len(header))

        while payload_remaining > 0 and not is_cancelled():
            now = time.monotonic()
            if now >= deadline:
                break
            n = min(CHUNK_SIZE, payload_remaining)
            writer.write(UPLOAD_PAYLOAD[:n])
            step_timeout = min(self.timeout, max(0.05, deadline - now))
            try:
                async with asyncio.timeout(step_timeout):
                    await writer.drain()
            except TimeoutError:
                break
            payload_remaining -= n
            sent += n
            on_bytes(n)

        if payload_remaining == 0 and not is_cancelled():
            remaining_time = deadline - time.monotonic()
            if remaining_time > 0.0:
                ack_timeout = min(self.timeout, max(0.1, remaining_time))
                with contextlib.suppress(TimeoutError, OSError):
                    async with asyncio.timeout(ack_timeout):
                        await reader.readline()

        return sent

    async def close(self) -> None:
        """Send best-effort ``QUIT`` and close the underlying TCP writer."""
        writer = self._writer
        self._reader = None
        self._writer = None
        if writer is None:
            return

        with contextlib.suppress(OSError, RuntimeError):
            if not writer.is_closing():
                writer.write(b"QUIT\n")
                writer.close()
        with contextlib.suppress(OSError, RuntimeError):
            await writer.wait_closed()


async def measure_tcp_latency(
    server: Mapping[str, Any],
    *,
    probe_timeout: float,
    source_address: str | None = None,
    ping_count: int = 5,
) -> tuple[float, float, float] | None:
    """Probe raw TCP latency to a server, returning ``None`` on failure."""
    try:
        host, port = parse_tcp_target(server)
    except ValueError:
        return None

    try:
        async with OoklaTCPConnection(
            host,
            port,
            timeout=probe_timeout,
            source_address=source_address,
        ) as conn:
            await conn.handshake()
            return await conn.ping(count=ping_count, probe_timeout=probe_timeout)
    except (OSError, TimeoutError, SpeedtestTransferError) as err:
        _LOGGER.debug("TCP latency probe failed for %s:%s: %s", host, port, err)
        return None


async def _tcp_loaded_ping_loop(
    host: str,
    port: int,
    *,
    io_timeout: float,
    source_address: str | None,
    deadline: float,
    is_cancelled: Callable[[], bool],
) -> float | None:
    """Measure RTT under load during an active transfer (bufferbloat probe)."""
    rtts_ms: list[float] = []
    probe_limit = min(io_timeout, 2.0)
    try:
        async with OoklaTCPConnection(
            host,
            port,
            timeout=probe_limit,
            source_address=source_address,
        ) as conn:
            await conn.handshake()
            while not is_cancelled() and time.monotonic() < deadline:
                try:
                    avg_ms, _, _ = await conn.ping(count=1, probe_timeout=probe_limit)
                except (OSError, TimeoutError, SpeedtestTransferError):
                    break
                rtts_ms.append(avg_ms)
                sleep_for = min(0.25, max(0.0, deadline - time.monotonic()))
                if sleep_for > 0.0:
                    await asyncio.sleep(sleep_for)
    except (OSError, TimeoutError, SpeedtestTransferError, asyncio.CancelledError):
        pass

    if not rtts_ms:
        return None
    return round(sum(rtts_ms) / len(rtts_ms), 3)


async def _tcp_worker(
    worker_id: int,
    host: str,
    port: int,
    *,
    direction: Literal["download", "upload"],
    chunk_bytes: int,
    io_timeout: float,
    source_address: str | None,
    deadline: float,
    progress: TransferProgress,
    is_cancelled: Callable[[], bool],
    errors: list[Exception],
    callback: Callable[..., Any] | None,
    total_planned: int,
) -> None:
    if callback is not None:
        callback(worker_id, total_planned, start=True)
    try:
        async with OoklaTCPConnection(
            host,
            port,
            timeout=io_timeout,
            source_address=source_address,
        ) as conn:
            await conn.handshake()
            while not is_cancelled() and time.monotonic() < deadline:
                if direction == "download":
                    transferred = await conn.download_chunk(
                        chunk_bytes,
                        deadline,
                        progress.record,
                        is_cancelled,
                    )
                else:
                    transferred = await conn.upload_chunk(
                        chunk_bytes,
                        deadline,
                        progress.record,
                        is_cancelled,
                    )
                if transferred == 0:
                    break
    except asyncio.CancelledError:
        raise
    except (OSError, TimeoutError, SpeedtestTransferError) as err:
        errors.append(err)
        _LOGGER.debug("TCP %s worker %d error: %s", direction, worker_id, err)
    finally:
        if callback is not None:
            callback(worker_id, total_planned, end=True)


async def run_tcp_transfer(
    server: Mapping[str, Any],
    *,
    direction: Literal["download", "upload"],
    duration: float,
    io_timeout: float,
    threads: int | None = None,
    source_address: str | None = None,
    is_cancelled: Callable[[], bool] = lambda: False,
    callback: Callable[..., Any] | None = None,
    measure_loaded_ping: bool = True,
) -> TransferResult:
    """Execute a multi-stream raw TCP download or upload speed measurement."""
    host, port = parse_tcp_target(server)
    chunk_bytes = (
        _TCP_DOWNLOAD_CHUNK_BYTES
        if direction == "download"
        else _TCP_UPLOAD_CHUNK_BYTES
    )

    initial_streams = max(1, threads) if threads is not None else 4
    progress = TransferProgress()
    errors: list[Exception] = []
    start = time.monotonic()
    deadline = start + max(0.05, duration)
    progress.snapshot(start)

    worker_tasks: list[asyncio.Task[None]] = []
    next_worker_id = 0

    def spawn_worker() -> None:
        nonlocal next_worker_id
        wid = next_worker_id
        next_worker_id += 1
        task = asyncio.create_task(
            _tcp_worker(
                wid,
                host,
                port,
                direction=direction,
                chunk_bytes=chunk_bytes,
                io_timeout=io_timeout,
                source_address=source_address,
                deadline=deadline,
                progress=progress,
                is_cancelled=is_cancelled,
                errors=errors,
                callback=callback,
                total_planned=initial_streams,
            )
        )
        worker_tasks.append(task)

    for _ in range(initial_streams):
        spawn_worker()

    async def sampler_loop() -> None:
        interval = min(0.25, max(0.05, duration / 10.0))
        scale_until = start + (duration * 0.4)
        while not is_cancelled() and time.monotonic() < deadline:
            await asyncio.sleep(interval)
            now = time.monotonic()
            prev_t, prev_b = progress.samples[-1]
            progress.snapshot(now)
            dt = now - prev_t
            if threads is None and now < scale_until and dt > 0.0:
                recent_bps = ((progress.bytes_transferred - prev_b) * 8.0) / dt
                target = desired_stream_count(recent_bps, len(worker_tasks))
                while len(worker_tasks) < target:
                    spawn_worker()

    sampler_task = asyncio.create_task(sampler_loop())
    loaded_ping_task: asyncio.Task[float | None] | None = None
    if measure_loaded_ping:
        loaded_ping_task = asyncio.create_task(
            _tcp_loaded_ping_loop(
                host,
                port,
                io_timeout=io_timeout,
                source_address=source_address,
                deadline=deadline,
                is_cancelled=is_cancelled,
            )
        )

    loaded_ping_ms: float | None = None
    try:
        idx = 0
        while idx < len(worker_tasks):
            await worker_tasks[idx]
            idx += 1
    finally:
        sampler_task.cancel()
        if loaded_ping_task is not None and not loaded_ping_task.done():
            loaded_ping_task.cancel()
        pending_workers = [t for t in worker_tasks if not t.done()]
        for task in pending_workers:
            task.cancel()
        await asyncio.gather(
            sampler_task,
            *pending_workers,
            return_exceptions=True,
        )
        if loaded_ping_task is not None:
            results = await asyncio.gather(loaded_ping_task, return_exceptions=True)
            if isinstance(results[0], float):
                loaded_ping_ms = results[0]

    stop = time.monotonic()
    progress.snapshot(stop)
    elapsed = max(stop - start, 1e-6)

    if progress.bytes_transferred == 0 and errors and not is_cancelled():
        raise SpeedtestTransferError(
            f"All TCP {direction} workers failed: {errors[0]}"
        ) from errors[0]

    bps = estimate_bandwidth_bps(progress.samples, progress.bytes_transferred, elapsed)
    return TransferResult(
        bps=bps,
        total_bytes=progress.bytes_transferred,
        loaded_ping_ms=loaded_ping_ms,
        protocol="tcp",
    )
