"""Unit tests for async HTTP transport, XML/JSON discovery, and error handling."""

from __future__ import annotations

import copy
import re
from typing import Any

import aiohttp
import pytest
from aioresponses import CallbackResult, aioresponses

from speedtest.exceptions import (
    ConfigRetrievalError,
    InvalidServerIDType,
    InvalidSpeedtestMiniServer,
    NoMatchedServers,
    ServersRetrievalError,
    ShareResultsConnectFailure,
    ShareResultsSubmitFailure,
    SpeedtestConfigError,
    SpeedtestMiniConnectFailure,
    SpeedtestTransferError,
)
from speedtest.models import SpeedtestResults
from speedtest.transport.http import (
    create_session,
    fetch_config,
    fetch_servers,
    measure_http_latency,
    resolve_mini_server,
    resolve_server_url,
    run_http_transfer,
    share_results,
)
from tests.conftest import (
    SAMPLE_CONFIG_XML,
    SAMPLE_SERVERS_JSON,
    SAMPLE_SERVERS_XML,
)


async def test_fetch_config_success_and_errors() -> None:
    """Parse valid XML and raise ConfigRetrievalError/SpeedtestConfigError properly."""
    with aioresponses() as mocked:
        mocked.get(
            re.compile(r"^https://www\.speedtest\.net/speedtest-config\.php"),
            body=SAMPLE_CONFIG_XML,
        )
        mocked.get(
            re.compile(r"^https://www\.speedtest\.net/speedtest-config\.php"),
            status=503,
        )
        mocked.get(
            re.compile(r"^https://www\.speedtest\.net/speedtest-config\.php"),
            exception=aiohttp.ClientConnectionError("down"),
        )
        mocked.get(
            re.compile(r"^https://www\.speedtest\.net/speedtest-config\.php"),
            body="<settings><broken",
        )
        mocked.get(
            re.compile(r"^https://www\.speedtest\.net/speedtest-config\.php"),
            body="<settings><client ip='1.1.1.1' /></settings>",
        )
        mocked.get(
            re.compile(r"^https://www\.speedtest\.net/speedtest-config\.php"),
            body=(
                "<settings><server-config threadcount='4' ignoreids='' />"
                "<download testlength='10' threadsperurl='4' />"
                "<upload testlength='10' ratio='5' maxchunkcount='6' threads='2' />"
                "<client lat='not-a-float' lon='2.0' /></settings>"
            ),
        )

        async with create_session() as session:
            cfg, lat_lon = await fetch_config(session, secure=True)
            assert lat_lon == (41.3874, 2.1686)
            assert cfg["ignore_servers"] == [999]

            with pytest.raises(ConfigRetrievalError, match="HTTP 503"):
                await fetch_config(session)

            with pytest.raises(ConfigRetrievalError, match="down"):
                await fetch_config(session)

            with pytest.raises(SpeedtestConfigError, match="Malformed"):
                await fetch_config(session)

            with pytest.raises(SpeedtestConfigError, match="missing required"):
                await fetch_config(session)

            with pytest.raises(SpeedtestConfigError, match="Unknown location"):
                await fetch_config(session)


async def test_fetch_servers_json_xml_fallback_and_non_mutation() -> None:
    """Verify JSON primary, XML fallback/supplement, and caller list immutability."""
    config = {"ignore_servers": [999], "threads": {"download": 8}}
    origin = (41.3874, 2.1686)
    include_arg: list[int | str] = ["300"]
    exclude_arg: list[int | str] = ["2"]

    with aioresponses() as mocked:
        # 1. JSON returns local servers (without id 300), so XML supplements it!
        mocked.get(
            re.compile(r"^https://www\.speedtest\.net/api/js/servers"),
            payload=copy.deepcopy(SAMPLE_SERVERS_JSON),
        )
        mocked.get(
            re.compile(r"^https://www\.speedtest\.net/speedtest-servers-static\.php"),
            body=SAMPLE_SERVERS_XML,
        )

        # 2. JSON fails with 403 -> falls back to XML completely
        mocked.get(
            re.compile(r"^https://www\.speedtest\.net/api/js/servers"),
            status=403,
        )
        mocked.get(
            re.compile(r"^https://www\.speedtest\.net/speedtest-servers-static\.php"),
            body=SAMPLE_SERVERS_XML,
        )

        # 3. All HTTP endpoints fail when secure=False -> raises ServersRetrievalError
        mocked.get(
            re.compile(r"^http://www\.speedtest\.net/api/js/servers"),
            status=500,
        )
        mocked.get(
            re.compile(r"^http://www\.speedtest\.net/speedtest-servers-static\.php"),
            status=500,
        )
        mocked.get(
            re.compile(r"^http://www\.speedtest\.net/speedtest-servers\.php"),
            body="<not-xml",
        )
        mocked.get(
            re.compile(r"^http://c\.speedtest\.net/speedtest-servers-static\.php"),
            exception=aiohttp.ClientError("unreachable"),
        )
        mocked.get(
            re.compile(r"^http://c\.speedtest\.net/speedtest-servers\.php"),
            status=502,
        )

        async with create_session() as session:
            srv_map = await fetch_servers(
                session,
                config=config,
                lat_lon=origin,
                secure=True,
                servers=include_arg,
                exclude=exclude_arg,
            )
            # Caller lists MUST NOT be mutated into ints!
            assert include_arg == ["300"]
            assert exclude_arg == ["2"]
            flat = [item for grp in srv_map.values() for item in grp]
            assert [item["id"] for item in flat] == ["300"]

            fallback_map = await fetch_servers(
                session,
                config=config,
                lat_lon=origin,
                secure=True,
            )
            fallback_ids = {item["id"] for grp in fallback_map.values() for item in grp}
            assert fallback_ids == {"1", "300"}

            with pytest.raises(ServersRetrievalError, match="HTTP"):
                await fetch_servers(
                    session,
                    config=config,
                    lat_lon=origin,
                    secure=False,
                )


async def test_fetch_servers_validation_and_no_matched() -> None:
    """Validate invalid server IDs and empty filter results."""
    config = {"ignore_servers": [999], "threads": {"download": 4}}
    with aioresponses() as mocked:
        mocked.get(
            re.compile(r"^https://www\.speedtest\.net/api/js/servers"),
            payload=copy.deepcopy(SAMPLE_SERVERS_JSON),
            repeat=True,
        )
        mocked.get(
            re.compile(r"^https://www\.speedtest\.net/speedtest-servers"),
            body=SAMPLE_SERVERS_XML,
            repeat=True,
        )

        async with create_session() as session:
            with pytest.raises(InvalidServerIDType, match="invalid server type"):
                await fetch_servers(
                    session, config=config, lat_lon=(0.0, 0.0), servers=["abc"]
                )
            with pytest.raises(InvalidServerIDType, match="invalid server type"):
                await fetch_servers(
                    session,
                    config=config,
                    lat_lon=(0.0, 0.0),
                    servers=[True],
                )
            with pytest.raises(NoMatchedServers):
                await fetch_servers(
                    session, config=config, lat_lon=(0.0, 0.0), servers=["88888"]
                )


async def test_measure_http_latency_cache_buster_and_dead_server() -> None:
    """Verify .0/.1/.2 cache-busters, true avg RTT, and None on dead servers."""
    seen_urls: list[str] = []

    def _ok_cb(url: Any, **_kwargs: Any) -> CallbackResult:
        seen_urls.append(str(url))
        return CallbackResult(status=200, body="test=test\n")

    server = {
        "url": "http://example.com:8080/speedtest/upload.php",
        "https_functional": 1,
    }
    assert resolve_server_url(server, secure=True).startswith("https://")
    assert resolve_server_url({"url": "://h/upload.php"}, secure=False).startswith(
        "http://"
    )

    with aioresponses() as mocked:
        mocked.get(
            re.compile(r"^https://example\.com:8080/speedtest/latency\.txt\?x=\d+\.0$"),
            callback=_ok_cb,
        )
        mocked.get(
            re.compile(r"^https://example\.com:8080/speedtest/latency\.txt\?x=\d+\.1$"),
            status=500,
        )
        mocked.get(
            re.compile(r"^https://example\.com:8080/speedtest/latency\.txt\?x=\d+\.2$"),
            callback=_ok_cb,
        )

        # Second call: all 3 probes fail -> MUST return None (never 1,800,000 ms!)
        mocked.get(
            re.compile(r"^https://example\.com:8080/speedtest/latency\.txt"),
            status=404,
            repeat=True,
        )

        async with create_session() as session:
            with pytest.raises(ValueError, match="positive"):
                await measure_http_latency(session, server, ping_count=0)

            res = await measure_http_latency(session, server, secure=True, ping_count=3)
            assert res is not None
            avg_ms, jitter_ms, loss = res
            assert 0.0 <= avg_ms < 5000.0
            assert jitter_ms >= 0.0
            assert loss == 33.33
            assert len(seen_urls) == 2

            dead = await measure_http_latency(
                session, server, secure=True, ping_count=3
            )
            assert dead is None


async def test_run_http_transfer_download_upload_and_failures() -> None:
    """Exercise HTTP download, streaming upload generator, and error handling."""
    server = {
        "url": "https://example.com:8080/speedtest/upload.php",
        "https_functional": 1,
    }
    config: dict[str, Any] = {
        "sizes": {"download": [350], "upload": [32768]},
        "counts": {"download": 2, "upload": 2},
        "threads": {"download": 2, "upload": 2},
        "upload_max": 2,
    }
    uploaded_payloads: list[int] = []

    def _upload_cb(_url: Any, **kwargs: Any) -> CallbackResult:
        data = kwargs["data"]
        total = len(data) if isinstance(data, (bytes, bytearray, memoryview)) else 0
        uploaded_payloads.append(total)
        return CallbackResult(status=200, body=f"size={total}")

    with aioresponses() as mocked:
        mocked.get(
            re.compile(r"^https://example\.com:8080/speedtest/random350x350\.jpg"),
            body=b"X" * 65536,
            repeat=True,
        )
        mocked.post(
            re.compile(r"^https://example\.com:8080/speedtest/upload\.php"),
            callback=_upload_cb,
            repeat=True,
        )

        async with create_session() as session:
            dl = await run_http_transfer(
                session,
                server,
                direction="download",
                config=config,
                duration=0.1,
                threads=2,
            )
            assert dl.protocol == "http"
            assert dl.total_bytes == 131_072
            assert dl.bps > 0.0

            ul = await run_http_transfer(
                session,
                server,
                direction="upload",
                config=config,
                duration=1.0,
                threads=2,
            )
            assert ul.protocol == "http"
            assert ul.total_bytes == 65_536
            assert uploaded_payloads == [32768, 32768]

    with aioresponses() as mocked:
        mocked.get(
            re.compile(r"^https://example\.com:8080/speedtest/random350x350\.jpg"),
            status=500,
            repeat=True,
        )
        async with create_session() as session:
            with pytest.raises(SpeedtestTransferError, match="All HTTP download"):
                await run_http_transfer(
                    session,
                    server,
                    direction="download",
                    config=config,
                    duration=0.1,
                    threads=1,
                )


async def test_resolve_mini_server_and_share_results() -> None:
    """Test Speedtest Mini probing and results sharing endpoint."""
    with aioresponses() as mocked:
        mocked.get(
            "https://mini.example.com",
            body='<script>var config = { upload_extension: "jsp" };</script>',
        )
        mocked.get("https://mini2.example.com/speedtest", body="<html>no ext</html>")
        mocked.get(
            "https://mini2.example.com/speedtest/speedtest/upload.php", status=404
        )
        mocked.get(
            "https://mini2.example.com/speedtest/speedtest/upload.asp",
            status=200,
            body="size=500",
        )
        mocked.get("https://bad.example.com", status=500)
        mocked.get("https://invalid.example.com", body="<html>nothing</html>")
        mocked.get(
            re.compile(r"^https://invalid\.example\.com/speedtest/upload\."),
            status=404,
            repeat=True,
        )

        async with create_session() as session:
            m1 = await resolve_mini_server(session, "https://mini.example.com")
            assert m1[0]["url"] == "https://mini.example.com/speedtest/upload.jsp"

            m2 = await resolve_mini_server(
                session, "https://mini2.example.com/speedtest/index.html"
            )
            assert (
                m2[0]["url"]
                == "https://mini2.example.com/speedtest/speedtest/upload.asp"
            )

            with pytest.raises(SpeedtestMiniConnectFailure, match="Failed to connect"):
                await resolve_mini_server(session, "https://bad.example.com")

            with pytest.raises(InvalidSpeedtestMiniServer, match="Invalid Speedtest"):
                await resolve_mini_server(session, "https://invalid.example.com")

    results = SpeedtestResults(
        download=100_000_000.0,
        upload=50_000_000.0,
        ping=12.5,
        server={"id": "1"},
        protocol="tcp",
    )
    with aioresponses() as mocked:
        mocked.post(
            "https://www.speedtest.net/api/api.php",
            body="resultid=123456789&date=2026",
        )
        mocked.post("https://www.speedtest.net/api/api.php", status=500)
        mocked.post("https://www.speedtest.net/api/api.php", body="missing=id")
        mocked.post(
            "https://www.speedtest.net/api/api.php",
            exception=aiohttp.ClientError("reset"),
        )

        async with create_session() as session:
            url = await share_results(session, results, secure=True)
            assert url == "https://www.speedtest.net/result/123456789.png"

            with pytest.raises(ShareResultsSubmitFailure, match="Could not submit"):
                await share_results(session, results, secure=True)

            with pytest.raises(ShareResultsSubmitFailure, match="Could not submit"):
                await share_results(session, results, secure=True)

            with pytest.raises(ShareResultsConnectFailure, match="reset"):
                await share_results(session, results, secure=True)


async def test_http_transport_edge_branches() -> None:
    """Cover edge cases in normalization, config ratios, upload generator, and JSON."""
    from speedtest.exceptions import SpeedtestUploadTimeout
    from speedtest.transport.http import _normalize_server, _upload_body_generator

    # Missing id/lat/lon returns None; missing host falls back to url netloc
    assert _normalize_server({"id": "not-int"}, (0.0, 0.0)) is None
    parsed = _normalize_server(
        {
            "id": "10",
            "lat": "0.0",
            "lon": "0.0",
            "url": "http://edge.example.com:8080/u",
        },
        (0.0, 0.0),
    )
    assert parsed is not None
    assert parsed[1]["host"] == "edge.example.com:8080"

    # Tiny upload body <= len('content1=') and cancelled upload generator
    tiny_chunks = [
        c async for c in _upload_body_generator(4, 9e18, lambda _: None, lambda: False)
    ]
    assert tiny_chunks == [b"cont"]

    with pytest.raises(SpeedtestUploadTimeout):
        _ = [
            c
            async for c in _upload_body_generator(
                1024, 9e18, lambda _: None, lambda: True
            )
        ]

    with aioresponses() as mocked:
        # Config with ratio > len(_UPLOAD_SIZES) and missing attribute (KeyError)
        mocked.get(
            re.compile(r"^https://www\.speedtest\.net/speedtest-config\.php"),
            body=(
                "<settings><server-config threadcount='4' ignoreids='' />"
                "<download testlength='10' threadsperurl='4' />"
                "<upload testlength='10' ratio='99' maxchunkcount='6' threads='2' />"
                "<client lat='1.0' lon='2.0' /></settings>"
            ),
        )
        mocked.get(
            re.compile(r"^https://www\.speedtest\.net/speedtest-config\.php"),
            body=(
                "<settings><server-config threadcount='4' />"
                "<download testlength='10' threadsperurl='4' />"
                "<upload testlength='10' ratio='1' maxchunkcount='6' threads='2' />"
                "<client lat='1.0' lon='2.0' /></settings>"
            ),
        )
        # Non-list JSON server response + non-dict item inside JSON list
        mocked.get(
            re.compile(r"^https://www\.speedtest\.net/api/js/servers"),
            payload={"error": "not a list"},
        )
        mocked.get(
            re.compile(r"^https://www\.speedtest\.net/speedtest-servers-static\.php"),
            body=SAMPLE_SERVERS_XML,
        )
        mocked.get(
            re.compile(r"^https://www\.speedtest\.net/api/js/servers"),
            payload=["not-a-dict", {"id": "bad"}, SAMPLE_SERVERS_JSON[0]],
        )

        async with create_session() as session:
            cfg, _ = await fetch_config(session)
            assert len(cfg["sizes"]["upload"]) == 1

            with pytest.raises(SpeedtestConfigError, match="attributes"):
                await fetch_config(session)

            # Non-list JSON falls back to XML
            s1 = await fetch_servers(session, config=cfg, lat_lon=(0.0, 0.0))
            assert len(s1) > 0

            # Non-dict & invalid items inside JSON list are skipped cleanly
            s2 = await fetch_servers(session, config=cfg, lat_lon=(0.0, 0.0))
            assert len(s2) == 1
