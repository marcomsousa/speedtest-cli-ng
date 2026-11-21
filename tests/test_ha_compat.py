"""Home Assistant Core ``speedtestdotnet`` compatibility contract tests."""

from __future__ import annotations

import asyncio
import copy
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import aiohttp
from aioresponses import aioresponses

import speedtest
from tests.conftest import SAMPLE_CONFIG_XML

if TYPE_CHECKING:
    from tests.conftest import MockOoklaServer


def _make_ha_servers(tcp_port: int) -> list[dict[str, Any]]:
    return [
        {
            "url": f"http://127.0.0.1:{tcp_port}/speedtest/upload.php",
            "lat": "41.3900",
            "lon": "2.1700",
            "distance": 1,
            "name": "Server1",
            "country": "Country1",
            "cc": "LL1",
            "sponsor": "Sponsor1",
            "id": "1",
            "https_functional": 0,
            "host": f"127.0.0.1:{tcp_port}",
        },
        {
            "url": f"http://127.0.0.1:{tcp_port}/speedtest/upload.php",
            "lat": "42.0000",
            "lon": "2.5000",
            "distance": 50,
            "name": "Server2",
            "country": "Country2",
            "cc": "LL2",
            "sponsor": "Sponsor2",
            "id": "2",
            "https_functional": 0,
            "host": f"127.0.0.1:{tcp_port}",
        },
    ]


def test_top_level_exports_and_exception_hierarchy() -> None:
    """Ensure all exports and exception subclasses imported by HA Core exist."""
    assert hasattr(speedtest, "Speedtest")
    assert hasattr(speedtest, "AsyncSpeedtest")
    assert hasattr(speedtest, "SpeedtestResults")
    for exc_cls in (
        speedtest.ConfigRetrievalError,
        speedtest.ServersRetrievalError,
        speedtest.InvalidServerIDType,
        speedtest.NoMatchedServers,
        speedtest.SpeedtestBestServerFailure,
        speedtest.SpeedtestMissingBestServer,
        speedtest.SpeedtestConfigError,
        speedtest.SpeedtestServersError,
        speedtest.SpeedtestTransferError,
    ):
        assert issubclass(exc_cls, speedtest.SpeedtestException)


async def test_ha_coordinator_executor_lifecycle(
    mock_ookla_server: MockOoklaServer,
) -> None:
    """Replicate HA Core SpeedTestDataCoordinator calls across executor threads."""
    servers_payload = _make_ha_servers(mock_ookla_server.port)
    config_xml = SAMPLE_CONFIG_XML.replace('testlength="1"', 'testlength="0.1"')
    # Use integer testlength="1" in XML and override length to keep fast in unit tests
    _ = config_xml

    with aioresponses() as mocked:
        mocked.get(
            re.compile(r"^https://www\.speedtest\.net/speedtest-config\.php"),
            body=SAMPLE_CONFIG_XML,
            repeat=True,
        )
        mocked.get(
            re.compile(r"^https://www\.speedtest\.net/api/js/servers"),
            payload=copy.deepcopy(servers_payload),
            repeat=True,
        )

        # Matches HA __init__.py setup in executor thread
        api = await asyncio.to_thread(
            speedtest.Speedtest,
            config={"length": {"download": 0.15, "upload": 0.15}},
            secure=True,
        )

        def _ha_update_data(server_id: str) -> dict[str, Any]:
            all_servers = api.get_servers()
            assert isinstance(all_servers, dict)
            for group in all_servers.values():
                for srv in group:
                    assert {"country", "name", "sponsor", "id", "cc"} <= srv.keys()

            assert isinstance(api.closest, list)
            api.closest.clear()

            opt_server_id: str | None = server_id
            filtered = api.get_servers(servers=[opt_server_id])
            assert len(filtered) == 1

            best_server = api.get_best_server()
            assert best_server["id"] == server_id
            assert isinstance(best_server["latency"], float)

            dl_bps = api.download()
            ul_bps = api.upload()
            assert isinstance(dl_bps, float)
            assert isinstance(ul_bps, float)
            assert dl_bps > 0.0
            assert ul_bps > 0.0

            return cast("dict[str, Any]", api.results.dict())

        first = await asyncio.to_thread(_ha_update_data, "1")
        for required_key in (
            "download",
            "upload",
            "ping",
            "server",
            "timestamp",
            "bytes_sent",
            "bytes_received",
            "share",
            "client",
        ):
            assert required_key in first
        assert {"id", "name", "country", "cc", "sponsor", "latency"} <= first[
            "server"
        ].keys()
        assert first["protocol"] == "tcp"
        assert first["jitter"] is not None

        # Simulate second hourly poll on the SAME instance: timestamp refreshes
        await asyncio.sleep(0.01)
        second = await asyncio.to_thread(_ha_update_data, "2")
        assert second["timestamp"] != first["timestamp"]
        assert second["server"]["id"] == "2"

        # NoMatchedServers maps to UpdateFailed in HA coordinator
        try:
            await asyncio.to_thread(api.get_servers, servers=["99999"])
        except speedtest.NoMatchedServers:
            pass
        else:  # pragma: no cover
            raise AssertionError("Expected NoMatchedServers")


async def test_async_speedtest_preserves_external_session_and_logs(
    mock_ookla_server: MockOoklaServer,
    caplog: Any,
) -> None:
    """Verify injectable ClientSession is never closed by AsyncSpeedtest."""
    servers_payload = _make_ha_servers(mock_ookla_server.port)
    caplog.set_level(logging.DEBUG, logger="speedtest")

    with aioresponses() as mocked:
        mocked.get(
            re.compile(r"^https://www\.speedtest\.net/speedtest-config\.php"),
            body=SAMPLE_CONFIG_XML,
        )
        mocked.get(
            re.compile(r"^https://www\.speedtest\.net/api/js/servers"),
            payload=copy.deepcopy(servers_payload),
        )

        async with aiohttp.ClientSession() as external_session:
            async with speedtest.AsyncSpeedtest(
                session=external_session,
                config={"length": {"download": 0.15, "upload": 0.15}},
            ) as client:
                await client.get_servers()
                best = await client.get_best_server()
                assert best["id"] in {"1", "2"}
                assert await client.download() > 0.0
                assert await client.upload() > 0.0

            assert not external_session.closed

    assert any(rec.name.startswith("speedtest") for rec in caplog.records)


def test_no_file_descriptor_leaks(mock_ookla_server: MockOoklaServer) -> None:
    """Verify synchronous Speedtest runs leave open fd count unchanged."""
    fd_dir = Path("/proc/self/fd")
    if not fd_dir.exists():  # pragma: no cover
        return

    servers_payload = _make_ha_servers(mock_ookla_server.port)
    before = len(list(fd_dir.iterdir()))

    with aioresponses() as mocked:
        mocked.get(
            re.compile(r"^https://www\.speedtest\.net/speedtest-config\.php"),
            body=SAMPLE_CONFIG_XML,
        )
        mocked.get(
            re.compile(r"^https://www\.speedtest\.net/api/js/servers"),
            payload=copy.deepcopy(servers_payload),
        )
        with speedtest.Speedtest(
            config={"length": {"download": 0.1, "upload": 0.1}},
        ) as api:
            api.get_servers()
            api.get_best_server()
            api.download()
            api.upload()

    after = len(list(fd_dir.iterdir()))
    assert after <= before
