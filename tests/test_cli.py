"""Unit tests for the CLI (speedtest.cli)."""

from __future__ import annotations

import copy
import json
import re
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import aiohttp
import pytest
from aioresponses import aioresponses

from speedtest.cli import main, shell
from speedtest.exceptions import SpeedtestCLIError
from tests.conftest import SAMPLE_CONFIG_XML

if TYPE_CHECKING:
    from tests.conftest import MockOoklaServer


def _cli_servers(port: int) -> list[dict[str, Any]]:
    return [
        {
            "url": f"http://127.0.0.1:{port}/speedtest/upload.php",
            "lat": "41.39",
            "lon": "2.17",
            "name": "Barcelona",
            "country": "Spain",
            "cc": "ES",
            "sponsor": "CLI Sponsor",
            "id": "1",
            "host": f"127.0.0.1:{port}",
        }
    ]


def test_cli_version_csv_header_and_validation_errors(capsys: Any) -> None:
    """Verify --version, --csv-header, and invalid flag combinations."""
    main(["--version"])
    out = capsys.readouterr().out
    assert "speedtest-cli-ng 3.0.0" in out

    main(["--csv-header"])
    assert capsys.readouterr().out.startswith("Server ID,Sponsor")

    with pytest.raises(SpeedtestCLIError, match="Cannot supply both"):
        shell(["--no-download", "--no-upload"])

    with pytest.raises(SpeedtestCLIError, match="single character"):
        shell(["--csv-delimiter", "::"])

    with pytest.raises(SystemExit, match="ERROR: Cannot supply both"):
        main(["--no-download", "--no-upload"])


def test_cli_list_json_csv_simple_bytes_and_share(
    mock_ookla_server: MockOoklaServer,
    capsys: Any,
) -> None:
    """Verify --list, --json, --csv, --simple --bytes, --no-download, and --share."""
    servers = _cli_servers(mock_ookla_server.port)
    fast_xml = SAMPLE_CONFIG_XML.replace('testlength="1"', 'testlength="1"')

    with aioresponses() as mocked:
        mocked.get(
            re.compile(r"^https?://www\.speedtest\.net/speedtest-config\.php"),
            body=fast_xml,
            repeat=True,
        )
        mocked.get(
            re.compile(r"^https?://www\.speedtest\.net/api/js/servers"),
            payload=copy.deepcopy(servers),
            repeat=True,
        )
        mocked.post(
            re.compile(r"^https?://www\.speedtest\.net/api/api\.php"),
            body="resultid=424242",
            repeat=True,
        )

        # --list
        main(["--list"])
        list_out = capsys.readouterr().out
        assert "1) CLI Sponsor (Barcelona, Spain)" in list_out

        # Patch config testlength to keep CLI tests sub-second
        with patch(
            "speedtest.async_client.fetch_config",
            side_effect=_fast_config,
        ):
            # --json --single --protocol tcp
            main(["--json", "--single", "--protocol", "tcp"])
            json_out = capsys.readouterr().out
            parsed = json.loads(json_out)
            assert parsed["protocol"] == "tcp"
            assert parsed["download"] > 0.0
            assert parsed["upload"] > 0.0

            # --csv --no-upload --share
            main(["--csv", "--no-upload", "--share"])
            csv_out = capsys.readouterr().out
            assert "CLI Sponsor,Barcelona" in csv_out
            assert "424242.png" in csv_out

            # --simple --bytes --no-download --debug --no-secure
            main(["--simple", "--bytes", "--no-download", "--debug", "--no-secure"])
            simple_out = capsys.readouterr().out
            assert "Mbyte/s" in simple_out

            # Default interactive mode with progress dots, --server, and --share
            main(["--server", "1", "--share"])
            default_out = capsys.readouterr().out
            assert "Share results:" in default_out


async def _fast_config(
    *_args: Any,
    **_kwargs: Any,
) -> tuple[dict[str, Any], tuple[float, float]]:
    return (
        {
            "client": {"ip": "203.0.113.10", "isp": "FiberTest ISP"},
            "ignore_servers": [],
            "sizes": {"upload": [32768], "download": [350]},
            "counts": {"upload": 1, "download": 1},
            "threads": {"upload": 1, "download": 2},
            "length": {"upload": 0.1, "download": 0.1},
            "upload_max": 1,
        },
        (41.3874, 2.1686),
    )


def test_cli_exclude_all_no_matched_servers_and_error_paths(
    mock_ookla_server: MockOoklaServer,
) -> None:
    """Verify --exclude without --server does not crash with TypeError."""
    servers = _cli_servers(mock_ookla_server.port)
    with aioresponses() as mocked:
        mocked.get(
            re.compile(r"^https://www\.speedtest\.net/speedtest-config\.php"),
            body=SAMPLE_CONFIG_XML,
        )
        mocked.get(
            re.compile(r"^https://www\.speedtest\.net/api/js/servers"),
            payload=copy.deepcopy(servers),
        )
        with pytest.raises(SpeedtestCLIError, match="all servers excluded"):
            shell(["--exclude", "1"])

    with aioresponses() as mocked:
        mocked.get(
            re.compile(r"^https://www\.speedtest\.net/speedtest-config\.php"),
            exception=aiohttp.ClientError("offline"),
        )
        with pytest.raises(SpeedtestCLIError, match="offline"):
            shell(["--simple"])

    with aioresponses() as mocked:
        mocked.get(
            re.compile(r"^https://www\.speedtest\.net/speedtest-config\.php"),
            body=SAMPLE_CONFIG_XML,
        )
        mocked.get(
            re.compile(r"^https://www\.speedtest\.net/api/js/servers"),
            status=500,
        )
        mocked.get(
            re.compile(r"^https://www\.speedtest\.net/speedtest-servers"),
            status=500,
            repeat=True,
        )
        with pytest.raises(SpeedtestCLIError, match="HTTP 500"):
            shell(["--list"])


def test_cli_edge_cases_signals_mini_and_main_module(
    mock_ookla_server: MockOoklaServer,
    capsys: Any,
) -> None:
    """Cover SIGINT handler, KeyboardInterrupt, SystemExit(2), --mini, and EPIPE."""
    import errno
    import runpy
    import threading

    from speedtest.cli import _format_server_ids, _handle_sigint, _print_dots
    from speedtest.exceptions import InvalidServerIDType

    assert _format_server_ids([10, 20]) == "10, 20"

    evt = threading.Event()
    dots = _print_dots(evt)
    dots(0, 1, start=True, end=True)
    assert capsys.readouterr().out == ".\n"
    evt.set()
    dots(0, 1, end=True)
    assert capsys.readouterr().out == ""

    handler = _handle_sigint(threading.Event())
    with pytest.raises(SystemExit):
        handler(2, None)

    with patch("speedtest.cli.shell", side_effect=KeyboardInterrupt):
        main([])
    assert "Cancelling" in capsys.readouterr().err

    with pytest.raises(SystemExit):
        main(["--unknown-flag"])

    with patch("speedtest.cli.main") as mocked_main:
        runpy.run_module("speedtest", run_name="__main__")
        assert mocked_main.called

    servers = _cli_servers(mock_ookla_server.port)
    with aioresponses() as mocked:
        mocked.get(
            re.compile(r"^https://www\.speedtest\.net/speedtest-config\.php"),
            body=SAMPLE_CONFIG_XML,
            repeat=True,
        )
        mocked.get(
            re.compile(r"^https://www\.speedtest\.net/api/js/servers"),
            payload=copy.deepcopy(servers),
            repeat=True,
        )
        mocked.get(
            "https://mini.example.com",
            body='<script>var x = { upload_extension: "php" };</script>',
        )

        # --list handling EPIPE cleanly
        with patch(
            "speedtest.cli._printer",
            side_effect=[None, OSError(errno.EPIPE, "broken pipe")],
        ):
            main(["--list"])

        # --list re-raising non-EPIPE OSError
        with (
            patch(
                "speedtest.cli._printer",
                side_effect=[None, OSError(errno.EIO, "io err")],
            ),
            pytest.raises(OSError, match="io err"),
        ):
            shell(["--list"])

        # InvalidServerIDType in shell()
        with (
            patch(
                "speedtest.client.Speedtest.get_servers",
                side_effect=InvalidServerIDType("bad"),
            ),
            pytest.raises(SpeedtestCLIError, match="invalid server type"),
        ):
            shell(["--server", "1"])

        # ServersRetrievalError during normal run (non --list)
        from speedtest.exceptions import ServersRetrievalError

        with (
            patch(
                "speedtest.client.Speedtest.get_servers",
                side_effect=ServersRetrievalError("list down"),
            ),
            pytest.raises(SpeedtestCLIError, match="list down"),
        ):
            shell(["--simple"])

        # --mini branch
        with (
            patch("speedtest.async_client.fetch_config", side_effect=_fast_config),
            patch(
                "speedtest.client.Speedtest.set_mini_server",
                return_value=copy.deepcopy(servers),
            ),
        ):
            main(["--mini", "https://mini.example.com", "--simple", "--no-upload"])
            assert "Ping:" in capsys.readouterr().out
