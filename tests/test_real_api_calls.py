"""Opt-in live Speedtest.net integration tests (run with ``-m real_api_call``)."""

from __future__ import annotations

import pytest

from speedtest import AsyncSpeedtest, Speedtest


@pytest.mark.real_api_call
async def test_live_async_speedtest_tcp() -> None:
    """Verify live config, JSON discovery, and short TCP transfer."""
    async with AsyncSpeedtest(
        config={"length": {"download": 2.0, "upload": 2.0}},
        secure=True,
        protocol="auto",
    ) as client:
        cfg = await client.get_config()
        assert "client" in cfg

        servers = await client.get_servers()
        assert len(servers) > 0

        best = await client.get_best_server()
        assert best["latency"] > 0.0

        dl_bps = await client.download()
        ul_bps = await client.upload()
        assert dl_bps > 0.0
        assert ul_bps > 0.0
        assert client.results.protocol in ("tcp", "http")


@pytest.mark.real_api_call
def test_live_sync_speedtest_http_latency_and_short_download() -> None:
    """Verify drop-in sync facade works cleanly against live HTTPS endpoints."""
    with Speedtest(
        config={"length": {"download": 1.5, "upload": 1.5}},
        secure=True,
        protocol="http",
    ) as client:
        client.get_servers()
        best = client.get_best_server()
        assert best["latency"] > 0.0
        assert client.download() > 0.0
        data = client.results.dict()
        assert data["protocol"] == "http"
        assert data["bytes_received"] > 0
