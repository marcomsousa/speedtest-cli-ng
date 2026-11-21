"""Shared fixtures and threaded loopback mock OoklaServer for offline tests."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from unittest.mock import Mock

import aiohttp
import pytest

if TYPE_CHECKING:
    from collections.abc import Coroutine, Iterator

if "stream_writer" in inspect.signature(aiohttp.ClientResponse.__init__).parameters:
    _orig_client_response_init = aiohttp.ClientResponse.__init__

    def _compat_client_response_init(
        self: aiohttp.ClientResponse,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        kwargs.setdefault("stream_writer", Mock())
        _orig_client_response_init(self, *args, **kwargs)

    aiohttp.ClientResponse.__init__ = _compat_client_response_init  # type: ignore[method-assign]

SAMPLE_CONFIG_XML = """<?xml version="1.0" encoding="UTF-8"?>
<settings>
<client ip="203.0.113.10" lat="41.3874" lon="2.1686" isp="FiberTest ISP" country="ES" />
<server-config threadcount="4" ignoreids="999" notonmap="" />
<times dl1="5000000" dl2="35000000" dl3="800000000"
       ul1="1000000" ul2="32000000" ul3="35000000" />
<download testlength="1" initialtest="250K" mintestsize="250K" threadsperurl="2" />
<upload testlength="1" ratio="5" initialtest="0" mintestsize="32K" threads="2"
        maxchunksize="512K" maxchunkcount="6" threadsperurl="4" />
</settings>
"""

SAMPLE_SERVERS_JSON: list[dict[str, Any]] = [
    {
        "url": "http://127.0.0.1:8080/speedtest/upload.php",
        "lat": "41.3900",
        "lon": "2.1700",
        "distance": 1,
        "name": "Barcelona",
        "country": "Spain",
        "cc": "ES",
        "sponsor": "Sponsor One",
        "id": "1",
        "preferred": 0,
        "isp_id": "100",
        "https_functional": 1,
        "host": "127.0.0.1:8080",
    },
    {
        "url": "http://127.0.0.1:8081/speedtest/upload.php",
        "lat": "41.5000",
        "lon": "2.2000",
        "distance": 15,
        "name": "Girona",
        "country": "Spain",
        "cc": "ES",
        "sponsor": "Sponsor Two",
        "id": "2",
        "preferred": 0,
        "isp_id": "200",
        "https_functional": 0,
        "host": "127.0.0.1:8081",
    },
    {
        "url": "http://ignored.example.com:8080/speedtest/upload.php",
        "lat": "41.4000",
        "lon": "2.1800",
        "distance": 2,
        "name": "Ignored",
        "country": "Spain",
        "cc": "ES",
        "sponsor": "Ignored Sponsor",
        "id": "999",
        "https_functional": 1,
        "host": "ignored.example.com:8080",
    },
]

SAMPLE_SERVERS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<settings>
<servers>
<server url="http://xml-fallback.example.com:8080/speedtest/upload.php"
        lat="40.4168" lon="-3.7038" name="Madrid" country="Spain" cc="ES"
        sponsor="XML Fallback Sponsor" id="300"
        host="xml-fallback.example.com:8080" />
<server url="http://127.0.0.1:8080/speedtest/upload.php"
        lat="41.3900" lon="2.1700" name="Barcelona" country="Spain" cc="ES"
        sponsor="Sponsor One" id="1" host="127.0.0.1:8080" />
</servers>
</settings>
"""


class _LoopThread:
    """Dedicated background event loop thread for MockOoklaServer."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self._thread.start()

    def run(self, coro: Coroutine[Any, Any, Any]) -> Any:
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout=10.0)

    def close(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(timeout=10.0)
        self.loop.close()


@dataclass
class MockOoklaServer:
    """In-process loopback OoklaServer simulator for deterministic TCP tests."""

    handshake_banner: bytes = b"HELLO 2.11 (2.11.7) test\n"
    ping_drop_indices: set[int] = field(default_factory=set)
    ping_bad_indices: set[int] = field(default_factory=set)
    ping_delay: float = 0.0
    download_eof_immediately: bool = False
    download_chunk_limit: int | None = None
    close_on_upload: bool = False
    upload_ack: bool = True
    commands: list[str] = field(default_factory=list)
    recorded_uploads: list[int] = field(default_factory=list)
    _server: asyncio.Server | None = field(default=None, init=False)
    _tasks: set[asyncio.Task[None]] = field(default_factory=set, init=False)
    _writers: set[asyncio.StreamWriter] = field(default_factory=set, init=False)
    port: int = field(default=0, init=False)

    async def start(self) -> None:
        """Bind loopback server on an ephemeral port."""
        self._server = await asyncio.start_server(
            self._handle_client,
            host="127.0.0.1",
            port=0,
        )
        sock = self._server.sockets[0]
        self.port = int(sock.getsockname()[1])

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)
        self._writers.add(writer)
        ping_index = 0
        payload_chunk = b"D" * 65536
        try:
            while True:
                raw_line = await reader.readline()
                if not raw_line:
                    break
                text = raw_line.decode("ascii", errors="replace").strip()
                self.commands.append(text)
                if text == "HI":
                    if self.handshake_banner:
                        writer.write(self.handshake_banner)
                        await writer.drain()
                    else:
                        break
                elif text.startswith("PING "):
                    idx = ping_index
                    ping_index += 1
                    if self.ping_delay > 0.0:
                        await asyncio.sleep(self.ping_delay)
                    if idx in self.ping_drop_indices:
                        continue
                    if idx in self.ping_bad_indices:
                        writer.write(b"ERR\n")
                        await writer.drain()
                        continue
                    stamp = text.split(" ", 1)[1]
                    writer.write(f"PONG {stamp}\n".encode("ascii"))
                    await writer.drain()
                elif text.startswith("DOWNLOAD "):
                    if self.download_eof_immediately:
                        break
                    requested = int(text.split(" ", 1)[1])
                    if self.download_chunk_limit is not None:
                        requested = min(requested, self.download_chunk_limit)
                    remaining = requested
                    while remaining > 0:
                        n = min(len(payload_chunk), remaining)
                        writer.write(payload_chunk[:n])
                        await writer.drain()
                        remaining -= n
                elif text.startswith("UPLOAD "):
                    if self.close_on_upload:
                        break
                    parts = text.split()
                    total_bytes = int(parts[1])
                    remaining = total_bytes - len(raw_line)
                    while remaining > 0:
                        chunk = await reader.read(min(65536, remaining))
                        if not chunk:
                            break
                        remaining -= len(chunk)
                    if remaining == 0 and self.upload_ack:
                        self.recorded_uploads.append(total_bytes)
                        writer.write(f"OK {total_bytes} 1700000000\n".encode("ascii"))
                        await writer.drain()
                elif text == "QUIT":
                    break
        except (OSError, ConnectionError, asyncio.CancelledError):
            pass
        finally:
            self._writers.discard(writer)
            if task is not None:
                self._tasks.discard(task)
            with contextlib.suppress(OSError, RuntimeError):
                writer.close()
                await writer.wait_closed()

    async def stop(self) -> None:
        """Close all active client writers, tasks, and listening server."""
        if self._server is not None:
            self._server.close()
        for writer in list(self._writers):
            with contextlib.suppress(OSError, RuntimeError):
                writer.close()
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._server is not None:
            await self._server.wait_closed()
            self._server = None


@pytest.fixture
def sample_config_xml() -> str:
    """Return representative speedtest-config.php XML payload."""
    return SAMPLE_CONFIG_XML


@pytest.fixture
def sample_servers_json() -> list[dict[str, Any]]:
    """Return representative /api/js/servers JSON payload."""
    return [dict(item) for item in SAMPLE_SERVERS_JSON]


@pytest.fixture
def sample_servers_xml() -> str:
    """Return representative speedtest-servers-static.php XML payload."""
    return SAMPLE_SERVERS_XML


@pytest.fixture
def mock_ookla_server() -> Iterator[MockOoklaServer]:
    """Start and tear down an in-process loopback OoklaServer on its own thread."""
    loop_thread = _LoopThread()
    server = MockOoklaServer()
    loop_thread.run(server.start())
    try:
        yield server
    finally:
        loop_thread.run(server.stop())
        loop_thread.close()
