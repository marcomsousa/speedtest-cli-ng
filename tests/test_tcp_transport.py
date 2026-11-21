"""Unit tests for the clean-room OoklaServer raw TCP transport."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from speedtest.exceptions import SpeedtestTransferError
from speedtest.transport.tcp import (
    OoklaTCPConnection,
    measure_tcp_latency,
    parse_tcp_target,
    run_tcp_transfer,
)

if TYPE_CHECKING:
    from tests.conftest import MockOoklaServer


@pytest.mark.parametrize(
    ("server", "expected"),
    [
        ({"host": "speedtest.example.com:8080"}, ("speedtest.example.com", 8080)),
        ({"host": "speedtest.example.com"}, ("speedtest.example.com", 8080)),
        ({"host": ("[::1]", 5060)}, ("[::1]", 5060)),
        ({"host": "[::1]:9090"}, ("::1", 9090)),
        (
            {"url": "https://host.example.com:8443/speedtest/upload.php"},
            ("host.example.com", 8443),
        ),
        (
            {"url": "://host.example.com/speedtest/upload.php"},
            ("host.example.com", 8080),
        ),
    ],
)
def test_parse_tcp_target_variants(
    server: dict[str, object],
    expected: tuple[str, int],
) -> None:
    """Parse host/port from tuple, IPv4/hostname, IPv6, and fallback URL."""
    assert parse_tcp_target(server) == expected


def test_parse_tcp_target_missing() -> None:
    """Raise ValueError when neither host nor url provides a hostname."""
    with pytest.raises(ValueError, match="valid TCP host"):
        parse_tcp_target({"name": "no-host"})


async def test_tcp_connection_requires_open() -> None:
    """Calling commands before connecting raises SpeedtestTransferError."""
    conn = OoklaTCPConnection("127.0.0.1", 8080)
    with pytest.raises(SpeedtestTransferError, match="not open"):
        await conn.handshake()
    await conn.close()


async def test_tcp_handshake_ping_download_upload_and_quit(
    mock_ookla_server: MockOoklaServer,
) -> None:
    """Exercise full wire framing including header-inclusive UPLOAD counts."""
    async with OoklaTCPConnection("127.0.0.1", mock_ookla_server.port) as conn:
        banner = await conn.handshake()
        assert banner.startswith("HELLO ")

        with pytest.raises(ValueError, match="positive"):
            await conn.ping(count=0)

        avg_ms, jitter_ms, loss = await conn.ping(count=3)
        assert avg_ms >= 0.0
        assert jitter_ms >= 0.0
        assert loss == 0.0

        received: list[int] = []
        dl_bytes = await conn.download_chunk(
            32768,
            deadline=asyncio.get_running_loop().time() + 5.0,
            on_bytes=received.append,
            is_cancelled=lambda: False,
        )
        assert dl_bytes == 32768
        assert sum(received) == 32768

        with pytest.raises(ValueError, match="exceed UPLOAD header"):
            await conn.upload_chunk(
                5,
                deadline=asyncio.get_running_loop().time() + 5.0,
                on_bytes=lambda _: None,
                is_cancelled=lambda: False,
            )

        sent: list[int] = []
        ul_bytes = await conn.upload_chunk(
            16384,
            deadline=asyncio.get_running_loop().time() + 5.0,
            on_bytes=sent.append,
            is_cancelled=lambda: False,
        )
        assert ul_bytes == 16384
        assert sum(sent) == 16384
        assert mock_ookla_server.recorded_uploads == [16384]

        # Confirm next PING on the SAME socket succeeds (proves exact wire framing)
        avg2, _, _ = await conn.ping(count=1)
        assert avg2 >= 0.0

    await asyncio.sleep(0.02)
    assert "QUIT" in mock_ookla_server.commands


async def test_tcp_bad_handshake_and_ping_failures(
    mock_ookla_server: MockOoklaServer,
) -> None:
    """Handle invalid handshake banners, dropped PINGs, and total ping loss."""
    mock_ookla_server.handshake_banner = b"NOT_HELLO\n"
    res = await measure_tcp_latency(
        {"host": f"127.0.0.1:{mock_ookla_server.port}"},
        probe_timeout=1.0,
    )
    assert res is None

    mock_ookla_server.handshake_banner = b"HELLO 2.11 test\n"
    mock_ookla_server.ping_drop_indices = {1}
    mock_ookla_server.ping_bad_indices = {2}
    res_partial = await measure_tcp_latency(
        {"host": f"127.0.0.1:{mock_ookla_server.port}"},
        probe_timeout=0.15,
        ping_count=4,
    )
    assert res_partial is not None
    assert res_partial[2] == 50.0

    mock_ookla_server.ping_drop_indices = {0, 1}
    mock_ookla_server.ping_bad_indices = set()
    res_all_fail = await measure_tcp_latency(
        {"host": f"127.0.0.1:{mock_ookla_server.port}"},
        probe_timeout=0.1,
        ping_count=2,
    )
    assert res_all_fail is None

    assert await measure_tcp_latency({"name": "bad"}, probe_timeout=0.2) is None


async def test_tcp_transfer_download_upload_and_callbacks(
    mock_ookla_server: MockOoklaServer,
) -> None:
    """Run multi-stream TCP download/upload with loaded ping and callbacks."""
    events: list[tuple[int, bool, bool]] = []

    def _cb(idx: int, _total: int, start: bool = False, end: bool = False) -> None:
        events.append((idx, start, end))

    server = {"host": f"127.0.0.1:{mock_ookla_server.port}"}
    dl = await run_tcp_transfer(
        server,
        direction="download",
        duration=0.2,
        io_timeout=2.0,
        threads=2,
        callback=_cb,
        measure_loaded_ping=True,
    )
    assert dl.protocol == "tcp"
    assert dl.total_bytes > 0
    assert dl.bps > 0.0
    assert dl.loaded_ping_ms is not None
    assert any(start for _, start, _ in events)
    assert any(end for _, _, end in events)

    ul = await run_tcp_transfer(
        server,
        direction="upload",
        duration=0.2,
        io_timeout=2.0,
        threads=2,
        measure_loaded_ping=False,
    )
    assert ul.protocol == "tcp"
    assert ul.total_bytes > 0
    assert ul.bps > 0.0
    assert ul.loaded_ping_ms is None


async def test_tcp_transfer_total_failure_and_cancellation(
    mock_ookla_server: MockOoklaServer,
) -> None:
    """Raise SpeedtestTransferError when all TCP workers fail with 0 bytes."""
    mock_ookla_server.handshake_banner = b"INVALID\n"
    server = {"host": f"127.0.0.1:{mock_ookla_server.port}"}

    with pytest.raises(SpeedtestTransferError, match="All TCP download workers failed"):
        await run_tcp_transfer(
            server,
            direction="download",
            duration=0.15,
            io_timeout=1.0,
            threads=1,
            measure_loaded_ping=False,
        )

    # Cancelled run with 0 bytes should NOT raise SpeedtestTransferError
    res_cancelled = await run_tcp_transfer(
        server,
        direction="download",
        duration=0.1,
        io_timeout=1.0,
        threads=1,
        is_cancelled=lambda: True,
        measure_loaded_ping=False,
    )
    assert res_cancelled.total_bytes == 0
    assert res_cancelled.bps == 0.0
