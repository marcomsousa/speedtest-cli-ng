"""Drop-in synchronous ``Speedtest`` facade wrapping ``AsyncSpeedtest``."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any, TypeVar

from .async_client import AsyncSpeedtest
from .models import FakeShutdownEvent, ProtocolName, SpeedtestResults, do_nothing

if TYPE_CHECKING:
    import threading
    from collections.abc import Callable, Coroutine, Sequence

_T = TypeVar("_T")


class Speedtest:
    """Synchronous drop-in facade compatible with ``speedtest-cli`` 2.1.3."""

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        source_address: str | None = None,
        timeout: float = 10.0,
        secure: bool = True,
        shutdown_event: threading.Event | None = None,
        protocol: ProtocolName = "auto",
    ) -> None:
        self._shutdown_event: threading.Event | FakeShutdownEvent = (
            shutdown_event if shutdown_event is not None else FakeShutdownEvent()
        )
        self._async = AsyncSpeedtest(
            config=config,
            source_address=source_address,
            timeout=timeout,
            secure=secure,
            protocol=protocol,
            shutdown_check=self._shutdown_event.is_set,
        )
        self._async.results._share_callback = self._share_sync
        self.get_config()

    def __enter__(self) -> Speedtest:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _run_coro(
        self,
        coro_factory: Callable[[], Coroutine[Any, Any, _T]],
    ) -> _T:
        async def _runner() -> _T:
            try:
                return await coro_factory()
            finally:
                await self._async.aclose()

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(_runner())

        with ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, _runner()).result()

    def _share_sync(self, _: SpeedtestResults) -> str:
        return self._run_coro(self._async.share)

    def close(self) -> None:
        """Close any internally held resources."""
        self._run_coro(self._async.aclose)

    @property
    def config(self) -> dict[str, Any]:
        """Current Speedtest configuration dictionary."""
        return self._async.config

    @config.setter
    def config(self, value: dict[str, Any]) -> None:
        self._async.config = value

    @property
    def lat_lon(self) -> tuple[float, float]:
        """Client ``(lat, lon)`` coordinates."""
        return self._async.lat_lon

    @lat_lon.setter
    def lat_lon(self, value: tuple[float, float]) -> None:
        self._async.lat_lon = value

    @property
    def servers(self) -> dict[float, list[dict[str, Any]]]:
        """Discovered servers keyed by distance in km."""
        return self._async.servers

    @servers.setter
    def servers(self, value: dict[float, list[dict[str, Any]]]) -> None:
        self._async.servers = value

    @property
    def closest(self) -> list[dict[str, Any]]:
        """Mutable list of closest candidate servers."""
        return self._async.closest

    @closest.setter
    def closest(self, value: list[dict[str, Any]]) -> None:
        self._async.closest = value

    @property
    def _best(self) -> dict[str, Any]:
        return self._async._best

    @_best.setter
    def _best(self, value: dict[str, Any]) -> None:
        self._async._best = value

    @property
    def results(self) -> SpeedtestResults:
        """Result container holding ping, download, upload, and metadata."""
        return self._async.results

    @results.setter
    def results(self, value: SpeedtestResults) -> None:
        self._async.results = value

    @property
    def best(self) -> dict[str, Any]:
        """Best server selected by latency (lazily probed on first access)."""
        if not self._async._best:
            self.get_best_server()
        return self._async._best

    def get_config(self) -> dict[str, Any]:
        """Download and parse speedtest.net configuration."""
        return self._run_coro(self._async.get_config)

    def get_servers(
        self,
        servers: Sequence[int | str | None] | None = None,
        exclude: Sequence[int | str | None] | None = None,
    ) -> dict[float, list[dict[str, Any]]]:
        """Retrieve and optionally filter speedtest.net servers."""
        return self._run_coro(
            lambda: self._async.get_servers(servers=servers, exclude=exclude)
        )

    def set_mini_server(self, server: str) -> list[dict[str, Any]]:
        """Configure a self-hosted Speedtest Mini server."""
        return self._run_coro(lambda: self._async.set_mini_server(server))

    def get_closest_servers(self, limit: int = 5) -> list[dict[str, Any]]:
        """Select up to ``limit`` geographically closest servers."""
        return self._run_coro(lambda: self._async.get_closest_servers(limit=limit))

    def get_best_server(
        self,
        servers: Sequence[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Probe candidate servers and select the one with lowest latency."""
        return self._run_coro(lambda: self._async.get_best_server(servers=servers))

    def download(
        self,
        callback: Callable[..., Any] = do_nothing,
        threads: int | None = None,
    ) -> float:
        """Measure download speed in bits/sec."""
        return self._run_coro(
            lambda: self._async.download(callback=callback, threads=threads)
        )

    def upload(
        self,
        callback: Callable[..., Any] = do_nothing,
        pre_allocate: bool = True,
        threads: int | None = None,
    ) -> float:
        """Measure upload speed in bits/sec."""
        return self._run_coro(
            lambda: self._async.upload(
                callback=callback,
                pre_allocate=pre_allocate,
                threads=threads,
            )
        )
