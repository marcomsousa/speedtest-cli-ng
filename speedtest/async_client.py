"""Native asyncio Speedtest.net client engine."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Literal

from .exceptions import (
    SpeedtestBestServerFailure,
    SpeedtestTransferError,
)
from .models import (
    ProtocolName,
    SpeedtestResults,
    build_user_agent,
    do_nothing,
)
from .transport.http import (
    create_session,
    fetch_config,
    fetch_servers,
    measure_http_latency,
    resolve_mini_server,
    run_http_transfer,
    share_results,
)
from .transport.tcp import measure_tcp_latency, run_tcp_transfer

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    import aiohttp

    from .metrics import TransferResult

_LOGGER = logging.getLogger("speedtest")
_VALID_PROTOCOLS: tuple[ProtocolName, ...] = ("auto", "tcp", "http")


class AsyncSpeedtest:
    """Asynchronous Speedtest.net client supporting TCP (:8080) and HTTP."""

    def __init__(
        self,
        *,
        config: dict[str, Any] | None = None,
        source_address: str | None = None,
        timeout: float = 10.0,
        secure: bool = True,
        session: aiohttp.ClientSession | None = None,
        protocol: ProtocolName = "auto",
        shutdown_check: Callable[[], bool] | None = None,
    ) -> None:
        if protocol not in _VALID_PROTOCOLS:
            raise ValueError(
                f"Unsupported protocol {protocol!r}; expected 'auto', 'tcp', or 'http'"
            )

        self._config_overrides: dict[str, Any] | None = (
            dict(config) if config is not None else None
        )
        self._source_address = source_address
        self._timeout = float(timeout)
        self._secure = bool(secure)
        self._session: aiohttp.ClientSession | None = session
        self._external_session = session is not None
        self._protocol: ProtocolName = protocol
        self._shutdown_check: Callable[[], bool] = shutdown_check or (lambda: False)

        self.config: dict[str, Any] = {}
        self.lat_lon: tuple[float, float] = (0.0, 0.0)
        self.servers: dict[float, list[dict[str, Any]]] = {}
        self.closest: list[dict[str, Any]] = []
        self._best: dict[str, Any] = {}
        self.results = SpeedtestResults(secure=self._secure)

    async def __aenter__(self) -> AsyncSpeedtest:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = create_session(
                timeout=self._timeout,
                source_address=self._source_address,
                user_agent=build_user_agent(),
            )
            self._external_session = False
        return self._session

    async def aclose(self) -> None:
        """Close the internally managed ``aiohttp.ClientSession`` if owned."""
        if not self._external_session and self._session is not None:
            if not self._session.closed:
                await self._session.close()
            self._session = None

    @property
    def best(self) -> dict[str, Any]:
        """Return currently selected best server (call ``get_best_server`` first)."""
        return self._best

    async def get_config(self) -> dict[str, Any]:
        """Fetch Speedtest.net configuration XML and merge any user overrides."""
        session = self._get_session()
        config, lat_lon = await fetch_config(
            session, secure=self._secure, request_timeout=self._timeout
        )
        if self._config_overrides is not None:
            config.update(self._config_overrides)
        self.config = config
        self.lat_lon = lat_lon
        self.results.client = dict(config.get("client", {}))
        self.results.touch_timestamp()
        _LOGGER.debug(
            "Loaded speedtest.net config for ISP %s",
            self.results.client.get("isp"),
        )
        return self.config

    async def get_servers(
        self,
        servers: Sequence[int | str] | None = None,
        exclude: Sequence[int | str] | None = None,
    ) -> dict[float, list[dict[str, Any]]]:
        """Retrieve and optionally filter Speedtest.net servers."""
        if not self.config:
            await self.get_config()

        session = self._get_session()
        self.servers.clear()
        fetched = await fetch_servers(
            session,
            config=self.config,
            lat_lon=self.lat_lon,
            secure=self._secure,
            request_timeout=self._timeout,
            servers=servers,
            exclude=exclude,
        )
        self.servers.update(fetched)
        return self.servers

    async def set_mini_server(self, server: str) -> list[dict[str, Any]]:
        """Configure a self-hosted Speedtest Mini server."""
        session = self._get_session()
        mini_servers = await resolve_mini_server(
            session, server, request_timeout=self._timeout
        )
        self.servers = {0.0: mini_servers}
        self.closest = list(mini_servers)
        return mini_servers

    async def get_closest_servers(self, limit: int = 5) -> list[dict[str, Any]]:
        """Select up to ``limit`` geographically closest servers."""
        if not self.servers:
            await self.get_servers()

        self.closest.clear()
        for dist in sorted(self.servers.keys()):
            for srv in self.servers[dist]:
                self.closest.append(srv)
                if len(self.closest) >= limit:
                    _LOGGER.debug("Closest servers: %r", self.closest)
                    return self.closest

        _LOGGER.debug("Closest servers: %r", self.closest)
        return self.closest

    async def _probe_server_latency(
        self,
        server: dict[str, Any],
    ) -> (
        tuple[float, float, float, float, Literal["tcp", "http"], dict[str, Any]] | None
    ):
        tcp_result: tuple[float, float, float] | None = None
        used_proto: Literal["tcp", "http"] = "http"

        if self._protocol in ("auto", "tcp"):
            tcp_result = await measure_tcp_latency(
                server,
                probe_timeout=self._timeout,
                source_address=self._source_address,
            )
            if tcp_result is not None:
                used_proto = "tcp"
            elif self._protocol == "tcp":
                return None

        if tcp_result is None:
            session = self._get_session()
            http_result = await measure_http_latency(
                session,
                server,
                secure=self._secure,
                request_timeout=self._timeout,
            )
            if http_result is None:
                return None
            avg_ms, jitter_ms, packet_loss = http_result
            used_proto = "http"
        else:
            avg_ms, jitter_ms, packet_loss = tcp_result

        dist_km = float(server.get("d", 0.0))
        return avg_ms, dist_km, jitter_ms, packet_loss, used_proto, server

    async def get_best_server(
        self,
        servers: Sequence[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Measure latency to candidate servers and select the fastest server."""
        if not servers:
            if not self.closest:
                await self.get_closest_servers()
            candidates: Sequence[dict[str, Any]] = self.closest
        else:
            candidates = servers

        if not candidates:
            raise SpeedtestBestServerFailure(
                "Unable to connect to servers to test latency."
            )

        sem = asyncio.Semaphore(5)

        async def _bounded_probe(
            srv: dict[str, Any],
        ) -> (
            tuple[float, float, float, float, Literal["tcp", "http"], dict[str, Any]]
            | None
        ):
            async with sem:
                return await self._probe_server_latency(srv)

        probe_results = await asyncio.gather(
            *(_bounded_probe(srv) for srv in candidates)
        )
        valid = [item for item in probe_results if item is not None]
        if not valid:
            raise SpeedtestBestServerFailure(
                "Unable to connect to servers to test latency."
            )

        valid.sort(key=lambda item: (item[0], item[1]))
        latency_ms, _, jitter_ms, packet_loss, used_proto, winner = valid[0]
        winner["latency"] = latency_ms

        self.results.ping = latency_ms
        self.results.jitter = jitter_ms
        self.results.packet_loss = packet_loss
        self.results.server = winner
        self.results.protocol = used_proto
        self.results.touch_timestamp()

        self._best = winner
        _LOGGER.debug("Best server (%s): %r", used_proto, winner)
        return winner

    async def _run_transfer(
        self,
        *,
        direction: Literal["download", "upload"],
        callback: Callable[..., Any],
        threads: int | None,
    ) -> float:
        if not self._best:
            await self.get_best_server()
        if not self.config:
            await self.get_config()

        self.results.touch_timestamp()
        duration = float(self.config.get("length", {}).get(direction, 10.0))

        result: TransferResult
        use_tcp = self._protocol == "tcp" or (
            self._protocol == "auto" and self.results.protocol != "http"
        )
        if use_tcp:
            try:
                result = await run_tcp_transfer(
                    self._best,
                    direction=direction,
                    duration=duration,
                    io_timeout=self._timeout,
                    threads=threads,
                    source_address=self._source_address,
                    is_cancelled=self._shutdown_check,
                    callback=callback,
                )
            except (OSError, TimeoutError, SpeedtestTransferError, ValueError) as err:
                if self._protocol == "tcp":
                    if isinstance(err, SpeedtestTransferError):
                        raise
                    raise SpeedtestTransferError(str(err)) from err
                _LOGGER.debug(
                    "TCP %s failed (%s); falling back to async HTTP", direction, err
                )
                result = await self._run_http_transfer(
                    direction=direction,
                    duration=duration,
                    callback=callback,
                    threads=threads,
                )
        else:
            result = await self._run_http_transfer(
                direction=direction,
                duration=duration,
                callback=callback,
                threads=threads,
            )

        if direction == "download":
            self.results.bytes_received = result.total_bytes
            self.results.download = result.bps
            self.results.loaded_ping_download = result.loaded_ping_ms
        else:
            self.results.bytes_sent = result.total_bytes
            self.results.upload = result.bps
            self.results.loaded_ping_upload = result.loaded_ping_ms
        self.results.protocol = result.protocol
        return result.bps

    async def _run_http_transfer(
        self,
        *,
        direction: Literal["download", "upload"],
        duration: float,
        callback: Callable[..., Any],
        threads: int | None,
    ) -> TransferResult:
        session = self._get_session()
        return await run_http_transfer(
            session,
            self._best,
            direction=direction,
            config=self.config,
            secure=self._secure,
            duration=duration,
            request_timeout=self._timeout,
            threads=threads,
            is_cancelled=self._shutdown_check,
            callback=callback,
        )

    async def download(
        self,
        callback: Callable[..., Any] = do_nothing,
        threads: int | None = None,
    ) -> float:
        """Run download throughput test and return speed in bits/sec."""
        return await self._run_transfer(
            direction="download",
            callback=callback,
            threads=threads,
        )

    async def upload(
        self,
        callback: Callable[..., Any] = do_nothing,
        pre_allocate: bool = True,
        threads: int | None = None,
    ) -> float:
        """Run upload throughput test and return speed in bits/sec."""
        _ = pre_allocate  # Shared zero-copy memoryview buffer never per-allocates
        return await self._run_transfer(
            direction="upload",
            callback=callback,
            threads=threads,
        )

    async def share(self) -> str:
        """Submit results to speedtest.net API and return the share image URL."""
        if self.results._share:
            return self.results._share
        session = self._get_session()
        url = await share_results(
            session,
            self.results,
            secure=self._secure,
            request_timeout=self._timeout,
        )
        self.results._share = url
        return url
