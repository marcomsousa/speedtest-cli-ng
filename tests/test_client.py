"""Unit tests for the synchronous Speedtest facade, results, and helpers."""

from __future__ import annotations

import copy
import json
import re
import threading
from typing import TYPE_CHECKING

import pytest
from aioresponses import aioresponses

import speedtest
from speedtest.exceptions import ShareResultsConnectFailure
from speedtest.models import FakeShutdownEvent, SpeedtestResults, distance
from tests.conftest import SAMPLE_CONFIG_XML

if TYPE_CHECKING:
    from tests.conftest import MockOoklaServer


def test_distance_and_fake_shutdown_event() -> None:
    """Verify Haversine edge cases (identical & antipodal) and FakeShutdownEvent."""
    assert distance((41.0, 2.0), (41.0, 2.0)) == 0.0
    assert distance((0.0, 0.0), (0.0, 180.0)) == pytest.approx(20015.08, rel=1e-3)
    evt = FakeShutdownEvent()
    assert evt.is_set() is False
    assert evt.isSet() is False
    assert speedtest.build_user_agent("3.0.0").startswith("speedtest-ng/3.0.0")
    speedtest.do_nothing(1, a=2)


def test_speedtest_results_formats_and_share_guard() -> None:
    """Verify dict(), csv_header(), csv(), json(), repr(), and share()."""
    results = SpeedtestResults(
        download=12_345_678.0,
        upload=6_543_210.0,
        ping=14.2,
        server={
            "id": "101",
            "sponsor": "Fiber Co",
            "name": "Barcelona",
            "d": 1.25,
        },
        client={"ip": "203.0.113.5"},
        jitter=1.1,
        protocol="tcp",
    )
    results.bytes_received = 5_000_000
    results.bytes_sent = 2_500_000

    assert "download" in repr(results)
    header = SpeedtestResults.csv_header()
    assert header.startswith("Server ID,Sponsor,Server Name")
    csv_line = results.csv(delimiter=";")
    assert csv_line.startswith("101;Fiber Co;Barcelona;")

    parsed = json.loads(results.json(pretty=True))
    assert parsed["download"] == 12_345_678.0
    assert parsed["protocol"] == "tcp"
    assert parsed["jitter"] == 1.1

    with pytest.raises(ShareResultsConnectFailure, match="No share callback"):
        results.share()

    results._share = "https://www.speedtest.net/result/999.png"
    assert results.share() == "https://www.speedtest.net/result/999.png"
    results.touch_timestamp()
    assert results._share is None


def test_sync_speedtest_facade_and_lazy_best(
    mock_ookla_server: MockOoklaServer,
) -> None:
    """Exercise sync Speedtest outside an event loop including lazy .best."""
    servers_payload = [
        {
            "url": f"http://127.0.0.1:{mock_ookla_server.port}/speedtest/upload.php",
            "lat": "41.39",
            "lon": "2.17",
            "name": "Barcelona",
            "country": "Spain",
            "cc": "ES",
            "sponsor": "Sync Sponsor",
            "id": "1",
            "https_functional": 0,
            "host": f"127.0.0.1:{mock_ookla_server.port}",
        }
    ]
    evt = threading.Event()

    with aioresponses() as mocked:
        mocked.get(
            re.compile(r"^https://www\.speedtest\.net/speedtest-config\.php"),
            body=SAMPLE_CONFIG_XML,
        )
        mocked.get(
            re.compile(r"^https://www\.speedtest\.net/api/js/servers"),
            payload=copy.deepcopy(servers_payload),
        )
        mocked.post(
            "https://www.speedtest.net/api/api.php",
            body="resultid=777",
        )
        mocked.get(
            "https://mini.example.com",
            body='<script>var cfg = { upload_extension: "php" };</script>',
        )

        with speedtest.Speedtest(
            config={"length": {"download": 0.1, "upload": 0.1}},
            shutdown_event=evt,
        ) as api:
            assert api.lat_lon == (41.3874, 2.1686)
            assert api.best["id"] == "1"
            # Second access uses already cached _best without re-probing
            assert api.best["id"] == "1"
            assert api.download() > 0.0
            assert api.upload() > 0.0
            assert api.results.share() == "https://www.speedtest.net/result/777.png"
            # Cached second call returns immediately
            assert api.results.share() == "https://www.speedtest.net/result/777.png"

            # Exercise property setters and set_mini_server delegation
            api.config = dict(api.config)
            api.lat_lon = (40.0, 3.0)
            assert api.lat_lon == (40.0, 3.0)
            api.servers = dict(api.servers)
            api.closest = list(api.closest)
            api._best = dict(api._best)
            assert api._best["id"] == "1"
            api.results = api.results
            mini = api.set_mini_server("https://mini.example.com")
            assert mini[0]["sponsor"] == "Speedtest Mini"


async def test_sync_speedtest_works_inside_running_event_loop(
    mock_ookla_server: MockOoklaServer,
) -> None:
    """Ensure sync Speedtest never crashes when called from an active event loop."""
    with aioresponses() as mocked:
        mocked.get(
            re.compile(r"^https://www\.speedtest\.net/speedtest-config\.php"),
            body=SAMPLE_CONFIG_XML,
        )
        mocked.get(
            re.compile(r"^https://www\.speedtest\.net/api/js/servers"),
            payload=[
                {
                    "url": f"http://127.0.0.1:{mock_ookla_server.port}/speedtest/upload.php",
                    "lat": "41.39",
                    "lon": "2.17",
                    "name": "Loop",
                    "country": "ES",
                    "cc": "ES",
                    "sponsor": "Loop",
                    "id": "1",
                    "host": f"127.0.0.1:{mock_ookla_server.port}",
                }
            ],
        )
        api = speedtest.Speedtest()
        assert len(api.get_closest_servers()) == 1
        api.close()
