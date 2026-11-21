"""Unit tests for AsyncSpeedtest and statistical bandwidth metrics."""

from __future__ import annotations

import copy
import re
from typing import TYPE_CHECKING

import pytest
from aioresponses import aioresponses

from speedtest.async_client import AsyncSpeedtest
from speedtest.exceptions import SpeedtestBestServerFailure, SpeedtestTransferError
from speedtest.metrics import (
    TransferProgress,
    compute_jitter,
    compute_latency_stats,
    desired_stream_count,
    estimate_bandwidth_bps,
)
from tests.conftest import SAMPLE_CONFIG_XML

if TYPE_CHECKING:
    from tests.conftest import MockOoklaServer


def test_metrics_helpers() -> None:
    """Cover jitter, latency stats, trimmed-mean estimator, and stream scaling."""
    assert compute_jitter([]) is None
    assert compute_jitter([10.0]) == 0.0
    assert compute_jitter([10.0, 14.0, 12.0]) == 3.0

    with pytest.raises(ValueError, match="at least one sample"):
        compute_latency_stats([])
    avg, jit = compute_latency_stats([10.0, 20.0])
    assert avg == 15.0
    assert jit == 10.0

    assert estimate_bandwidth_bps([], 0, 1.0) == 0.0
    assert estimate_bandwidth_bps([], 1000, 0.0) == 0.0
    # Fewer than 4 intervals falls back to simple average
    assert estimate_bandwidth_bps([(0.0, 0), (1.0, 1000)], 1000, 1.0) == 8000.0

    # 10 intervals: initial warm-up + low/high outliers trimmed
    samples = [
        (0.0, 0),
        (0.1, 10_000),  # slow start
        (0.2, 20_000),  # slow start
        (0.3, 120_000),  # 1,000,000 B/s = 8 Mbps
        (0.4, 220_000),  # 8 Mbps
        (0.5, 320_000),  # 8 Mbps
        (0.6, 420_000),  # 8 Mbps
        (0.7, 520_000),  # 8 Mbps
        (0.8, 620_000),  # 8 Mbps
        (0.9, 630_000),  # low dip
        (1.0, 1_130_000),  # spike
    ]
    bps = estimate_bandwidth_bps(samples, 1_130_000, 1.0)
    assert bps == pytest.approx(8_000_000.0)

    assert desired_stream_count(20_000_000.0, 4) == 4
    assert desired_stream_count(150_000_000.0, 4) == 8
    assert desired_stream_count(700_000_000.0, 8) == 16

    prog = TransferProgress()
    prog.record(0)
    assert prog.bytes_transferred == 0
    assert (
        estimate_bandwidth_bps(
            [(0.0, 0), (0.0, 100), (0.1, 200), (0.2, 300), (0.3, 400), (0.4, 500)],
            500,
            0.4,
            warmup_fraction=1.0,
        )
        == 10000.0
    )
    assert (
        estimate_bandwidth_bps(
            [(0.0, 0), (0.1, 100), (0.2, 200), (0.3, 300), (0.4, 400)],
            400,
            0.4,
            trim_low=0.9,
            trim_high=0.9,
        )
        == 8000.0
    )


async def test_async_client_invalid_protocol() -> None:
    """Reject unknown protocol modes on construction."""
    with pytest.raises(ValueError, match="Unsupported protocol"):
        AsyncSpeedtest(protocol="udp")  # type: ignore[arg-type]


async def test_async_client_auto_falls_back_to_http_when_tcp_blocked() -> None:
    """When TCP port is unreachable, protocol='auto' falls back to HTTP."""
    servers_payload = [
        {
            "url": "https://fallback.example.com:8080/speedtest/upload.php",
            "lat": "41.39",
            "lon": "2.17",
            "name": "Fallback",
            "country": "Spain",
            "cc": "ES",
            "sponsor": "HTTP Only",
            "id": "55",
            "https_functional": 1,
            "host": "127.0.0.1:1",  # Refused TCP port
        }
    ]
    with aioresponses() as mocked:
        mocked.get(
            re.compile(r"^https://www\.speedtest\.net/speedtest-config\.php"),
            body=SAMPLE_CONFIG_XML,
        )
        mocked.get(
            re.compile(r"^https://www\.speedtest\.net/api/js/servers"),
            payload=copy.deepcopy(servers_payload),
        )
        mocked.get(
            re.compile(r"^https://fallback\.example\.com:8080/speedtest/latency\.txt"),
            body=b"test=test",
            repeat=True,
        )
        mocked.get(
            re.compile(
                r"^https://fallback\.example\.com:8080/speedtest/random\d+x\d+\.jpg"
            ),
            body=b"Z" * 16384,
            repeat=True,
        )
        mocked.post(
            re.compile(r"^https://fallback\.example\.com:8080/speedtest/upload\.php"),
            status=200,
            body="size=1",
            repeat=True,
        )

        async with AsyncSpeedtest(
            protocol="auto",
            config={
                "length": {"download": 0.1, "upload": 0.1},
                "sizes": {"download": [350], "upload": [32]},
                "counts": {"download": 1, "upload": 1},
                "upload_max": 1,
            },
        ) as client:
            best = await client.get_best_server()
            assert best["id"] == "55"
            assert client.results.protocol == "http"

            dl = await client.download()
            assert dl > 0.0
            assert client.results.protocol == "http"


async def test_async_client_explicit_tcp_errors_and_tie_breaking(
    mock_ookla_server: MockOoklaServer,
) -> None:
    """Explicit protocol='tcp' does not fall back; best server breaks ties by dist."""
    servers_payload = [
        {
            "url": f"http://127.0.0.1:{mock_ookla_server.port}/speedtest/upload.php",
            "lat": "42.5",
            "lon": "2.5",
            "name": "Far",
            "country": "ES",
            "cc": "ES",
            "sponsor": "Far",
            "id": "20",
            "host": f"127.0.0.1:{mock_ookla_server.port}",
        },
        {
            "url": f"http://127.0.0.1:{mock_ookla_server.port}/speedtest/upload.php",
            "lat": "41.3874",
            "lon": "2.1686",
            "name": "Near",
            "country": "ES",
            "cc": "ES",
            "sponsor": "Near",
            "id": "10",
            "host": f"127.0.0.1:{mock_ookla_server.port}",
        },
    ]

    with aioresponses() as mocked:
        mocked.get(
            re.compile(r"^https://www\.speedtest\.net/speedtest-config\.php"),
            body=SAMPLE_CONFIG_XML,
        )
        mocked.get(
            re.compile(r"^https://www\.speedtest\.net/api/js/servers"),
            payload=copy.deepcopy(servers_payload),
        )

        async with AsyncSpeedtest(protocol="tcp") as client:
            # Repeated calls to get_closest_servers are idempotent
            c1 = await client.get_closest_servers(limit=2)
            c2 = await client.get_closest_servers(limit=2)
            assert len(c1) == 2
            assert len(c2) == 2
            assert c2[0]["id"] == "10"

            best = await client.get_best_server()
            assert client.best["id"] == best["id"]

            # Empty server list -> SpeedtestBestServerFailure
            mock_ookla_server.handshake_banner = b"ERR\n"
            with pytest.raises(SpeedtestBestServerFailure, match="Unable to connect"):
                await client.get_best_server()

            # Explicit TCP failure during download raises SpeedtestTransferError
            with pytest.raises(SpeedtestTransferError, match="All TCP download"):
                await client.download()


async def test_async_client_get_best_server_empty_candidates() -> None:
    """Raise SpeedtestBestServerFailure if server list resolves empty."""
    with aioresponses() as mocked:
        mocked.get(
            re.compile(r"^https://www\.speedtest\.net/speedtest-config\.php"),
            body=SAMPLE_CONFIG_XML,
        )
        mocked.get(
            re.compile(r"^https://www\.speedtest\.net/api/js/servers"),
            payload=[],
        )
        mocked.get(
            re.compile(r"^https://www\.speedtest\.net/speedtest-servers-static\.php"),
            body="<settings><servers></servers></settings>",
        )
        mocked.get(
            re.compile(r"^https://www\.speedtest\.net/speedtest-servers\.php"),
            body="<settings><servers></servers></settings>",
        )

        async with AsyncSpeedtest(protocol="http") as client:
            await client.get_config()
            with pytest.raises(SpeedtestBestServerFailure, match="Unable to connect"):
                await client.get_best_server([{"host": "127.0.0.1:1", "url": ""}])

            # When closest remains empty -> SpeedtestBestServerFailure
            client.servers = {1.0: []}
            with pytest.raises(SpeedtestBestServerFailure, match="Unable to connect"):
                await client.get_best_server()


async def test_async_client_transfer_auto_fallback_mini_and_share(
    mock_ookla_server: MockOoklaServer,
) -> None:
    """Cover lazy setup, mid-transfer TCP->HTTP fallback, mini, and share."""
    servers_payload = [
        {
            "url": "https://fallback.example.com:8080/speedtest/upload.php",
            "lat": "41.39",
            "lon": "2.17",
            "name": "Server1",
            "country": "ES",
            "cc": "ES",
            "sponsor": "S1",
            "id": "1",
            "https_functional": 1,
            "host": f"127.0.0.1:{mock_ookla_server.port}",
        }
    ]
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
        mocked.get(
            re.compile(
                r"^https://fallback\.example\.com:8080/speedtest/random\d+x\d+\.jpg"
            ),
            body=b"A" * 16384,
            repeat=True,
        )
        mocked.get(
            "https://mini.example.com",
            body='<script>var cfg = { upload_extension: "php" };</script>',
        )
        mocked.post(
            "https://www.speedtest.net/api/api.php",
            body="resultid=98765",
        )

        async with AsyncSpeedtest(
            config={
                "length": {"download": 0.1, "upload": 0.1},
                "sizes": {"download": [350], "upload": [32768]},
                "counts": {"download": 1, "upload": 1},
                "upload_max": 1,
            }
        ) as client:
            mini = await client.set_mini_server("https://mini.example.com")
            assert mini[0]["sponsor"] == "Speedtest Mini"
            client.servers.clear()
            client.closest.clear()
            client._best.clear()

            # Calling download() before get_best_server() auto-selects best server;
            # then if TCP download fails (EOF immediately), auto falls back to HTTP!
            await client.get_best_server()
            assert client.results.protocol == "tcp"
            mock_ookla_server.handshake_banner = b"BROKEN\n"
            dl = await client.download()
            assert dl > 0.0
            assert client.results.protocol == "http"

            share_url = await client.share()
            assert share_url == "https://www.speedtest.net/result/98765.png"
            assert await client.share() == share_url
