"""Command-line interface for speedtest-ng."""

from __future__ import annotations

import argparse
import errno
import logging
import signal
import sys
import threading
from typing import TYPE_CHECKING, Any

from .client import Speedtest
from .exceptions import (
    ConfigRetrievalError,
    InvalidServerIDType,
    NoMatchedServers,
    ServersRetrievalError,
    SpeedtestCLIError,
    SpeedtestException,
)
from .models import SpeedtestResults, __version__, do_nothing

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence


def _print_dots(shutdown_event: threading.Event) -> Callable[..., None]:
    def inner(
        current: int,
        total: int,
        start: bool = False,
        end: bool = False,
    ) -> None:
        _ = start
        if shutdown_event.is_set():
            return
        sys.stdout.write(".")
        if current + 1 == total and end:
            sys.stdout.write("\n")
        sys.stdout.flush()

    return inner


def _printer(
    text: str,
    *,
    quiet: bool = False,
    error: bool = False,
    end: str = "\n",
) -> None:
    if quiet and not error:
        return
    stream = sys.stderr if error else sys.stdout
    print(text, file=stream, end=end, flush=True)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Build and parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description=(
            "Command line interface for testing internet bandwidth using "
            "speedtest.net.\n"
            "https://github.com/marcomsousa/speedtest-ng"
        )
    )
    parser.add_argument(
        "--no-download",
        dest="download",
        default=True,
        action="store_const",
        const=False,
        help="Do not perform download test",
    )
    parser.add_argument(
        "--no-upload",
        dest="upload",
        default=True,
        action="store_const",
        const=False,
        help="Do not perform upload test",
    )
    parser.add_argument(
        "--single",
        default=False,
        action="store_true",
        help="Only use a single connection instead of multiple.",
    )
    parser.add_argument(
        "--bytes",
        dest="units",
        action="store_const",
        const=("byte", 8),
        default=("bit", 1),
        help="Display values in bytes instead of bits.",
    )
    parser.add_argument(
        "--share",
        action="store_true",
        help="Generate and provide a URL to the speedtest.net share results image.",
    )
    parser.add_argument(
        "--simple",
        action="store_true",
        default=False,
        help="Suppress verbose output, only show basic information.",
    )
    parser.add_argument(
        "--csv",
        action="store_true",
        default=False,
        help="Output results in CSV format (bits/s).",
    )
    parser.add_argument(
        "--csv-delimiter",
        default=",",
        type=str,
        help='Single character delimiter for CSV output. Default ","',
    )
    parser.add_argument(
        "--csv-header",
        action="store_true",
        default=False,
        help="Print CSV headers and exit.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="Output results in JSON format (bits/s).",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="Display a list of speedtest.net servers sorted by distance.",
    )
    parser.add_argument(
        "--server",
        type=int,
        action="append",
        help="Specify a server ID to test against. Can be supplied multiple times.",
    )
    parser.add_argument(
        "--exclude",
        type=int,
        action="append",
        help="Exclude a server from selection. Can be supplied multiple times.",
    )
    parser.add_argument("--mini", help="URL of the Speedtest Mini server.")
    parser.add_argument("--source", help="Source IP address to bind to.")
    parser.add_argument(
        "--timeout",
        default=10.0,
        type=float,
        help="Network timeout in seconds. Default 10.",
    )
    parser.add_argument(
        "--secure",
        dest="secure",
        action="store_true",
        default=True,
        help="Use HTTPS when communicating with speedtest.net (default).",
    )
    parser.add_argument(
        "--no-secure",
        dest="secure",
        action="store_false",
        help="Use plain HTTP instead of HTTPS when talking to speedtest.net.",
    )
    parser.add_argument(
        "--protocol",
        choices=("auto", "tcp", "http"),
        default="auto",
        help="Transfer protocol to use: auto (TCP with HTTP fallback), tcp, or http.",
    )
    parser.add_argument(
        "--no-pre-allocate",
        dest="pre_allocate",
        action="store_const",
        default=True,
        const=False,
        help="Retained for CLI compatibility; uploads use shared zero-copy buffers.",
    )
    parser.add_argument(
        "--version",
        action="store_true",
        help="Show the version number and exit.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        default=False,
        help=argparse.SUPPRESS,
    )
    return parser.parse_args(argv)


def _format_server_ids(ids: Sequence[int] | None) -> str:
    if not ids:
        return "all servers excluded"
    return ", ".join(str(item) for item in ids)


def _handle_sigint(
    shutdown_event: threading.Event,
) -> Callable[[int, Any], None]:
    def _handler(_signum: int, _frame: Any) -> None:
        shutdown_event.set()
        _printer("\nCancelling...", error=True)
        raise SystemExit(0)

    return _handler


def shell(argv: Sequence[str] | None = None) -> None:
    """Run the full speedtest-ng CLI workflow."""
    shutdown_event = threading.Event()
    previous_sigint: Any = None
    in_main_thread = threading.current_thread() is threading.main_thread()
    if in_main_thread:
        previous_sigint = signal.signal(signal.SIGINT, _handle_sigint(shutdown_event))

    try:
        _run_shell(argv, shutdown_event)
    finally:
        if in_main_thread and previous_sigint is not None:
            signal.signal(signal.SIGINT, previous_sigint)


def _run_shell(
    argv: Sequence[str] | None,
    shutdown_event: threading.Event,
) -> None:
    args = parse_args(argv)

    if args.version:
        _printer(f"speedtest-ng {__version__}")
        _printer(f"Python {sys.version.replace(chr(10), '')}")
        raise SystemExit(0)

    if not args.download and not args.upload:
        raise SpeedtestCLIError("Cannot supply both --no-download and --no-upload")

    if len(args.csv_delimiter) != 1:
        raise SpeedtestCLIError("--csv-delimiter must be a single character")

    if args.csv_header:
        _printer(SpeedtestResults.csv_header(delimiter=args.csv_delimiter))
        raise SystemExit(0)

    if args.debug:
        logging.basicConfig(
            level=logging.DEBUG,
            format="DEBUG: %(name)s: %(message)s",
        )

    quiet = bool(args.simple or args.csv or args.json)
    machine_format = bool(args.csv or args.json)
    callback: Callable[..., None] = (
        do_nothing if (quiet or args.debug) else _print_dots(shutdown_event)
    )

    _printer("Retrieving speedtest.net configuration...", quiet=quiet)
    try:
        client = Speedtest(
            source_address=args.source,
            timeout=args.timeout,
            secure=args.secure,
            shutdown_event=shutdown_event,
            protocol=args.protocol,
        )
    except ConfigRetrievalError as err:
        _printer("Cannot retrieve speedtest configuration", error=True)
        raise SpeedtestCLIError(err) from err

    if args.list:
        try:
            client.get_servers()
        except ServersRetrievalError as err:
            _printer("Cannot retrieve speedtest server list", error=True)
            raise SpeedtestCLIError(err) from err

        for _, servers in sorted(client.servers.items()):
            for server in servers:
                line = (
                    f"{int(server['id']):5d}) {server['sponsor']} "
                    f"({server['name']}, {server['country']}) "
                    f"[{float(server['d']):0.2f} km]"
                )
                try:
                    _printer(line)
                except OSError as err:
                    if err.errno != errno.EPIPE:
                        raise
        raise SystemExit(0)

    isp = client.config.get("client", {}).get("isp", "Unknown")
    ip_addr = client.config.get("client", {}).get("ip", "Unknown")
    _printer(f"Testing from {isp} ({ip_addr})...", quiet=quiet)

    if not args.mini:
        _printer("Retrieving speedtest.net server list...", quiet=quiet)
        try:
            client.get_servers(servers=args.server, exclude=args.exclude)
        except NoMatchedServers as err:
            raise SpeedtestCLIError(
                f"No matched servers: {_format_server_ids(args.server)}"
            ) from err
        except ServersRetrievalError as err:
            _printer("Cannot retrieve speedtest server list", error=True)
            raise SpeedtestCLIError(err) from err
        except InvalidServerIDType as err:
            raise SpeedtestCLIError(
                f"{_format_server_ids(args.server)} is an invalid server type, "
                "must be an int"
            ) from err

        if args.server and len(args.server) == 1:
            _printer("Retrieving information for the selected server...", quiet=quiet)
        else:
            _printer("Selecting best server based on ping...", quiet=quiet)
        client.get_best_server()
    else:
        client.get_best_server(client.set_mini_server(args.mini))

    results = client.results
    srv = results.server
    _printer(
        f"Hosted by {srv.get('sponsor', '')} ({srv.get('name', '')}) "
        f"[{float(srv.get('d', 0.0)):0.2f} km]: {srv.get('latency', 0.0)} ms",
        quiet=quiet,
    )

    unit_name, unit_divisor = args.units
    threads = 1 if args.single else None

    if args.download:
        _printer("Testing download speed", quiet=quiet, end="\n" if args.debug else "")
        client.download(callback=callback, threads=threads)
        dl_mbps = (results.download / 1_000_000.0) / unit_divisor
        _printer(f"Download: {dl_mbps:0.2f} M{unit_name}/s", quiet=quiet)
    else:
        _printer("Skipping download test", quiet=quiet)

    if args.upload:
        _printer("Testing upload speed", quiet=quiet, end="\n" if args.debug else "")
        client.upload(
            callback=callback,
            pre_allocate=args.pre_allocate,
            threads=threads,
        )
        ul_mbps = (results.upload / 1_000_000.0) / unit_divisor
        _printer(f"Upload: {ul_mbps:0.2f} M{unit_name}/s", quiet=quiet)
    else:
        _printer("Skipping upload test", quiet=quiet)

    if not args.simple and args.share:
        results.share()

    if args.simple:
        dl_mbps = (results.download / 1_000_000.0) / unit_divisor
        ul_mbps = (results.upload / 1_000_000.0) / unit_divisor
        _printer(
            f"Ping: {results.ping} ms\n"
            f"Download: {dl_mbps:0.2f} M{unit_name}/s\n"
            f"Upload: {ul_mbps:0.2f} M{unit_name}/s"
        )
    elif args.csv:
        _printer(results.csv(delimiter=args.csv_delimiter))
    elif args.json:
        _printer(results.json())

    if args.share and not machine_format:
        _printer(f"Share results: {results.share()}")


def main(argv: Sequence[str] | None = None) -> None:
    """Entry point for ``speedtest`` and ``speedtest-cli`` console scripts."""
    try:
        shell(argv)
    except KeyboardInterrupt:
        _printer("\nCancelling...", error=True)
    except (SpeedtestException, SystemExit) as err:
        if isinstance(err, SystemExit):
            if err.code in (0, None):
                return
            if err.code == 2:
                raise
        msg = str(err) or repr(err)
        raise SystemExit(f"ERROR: {msg}") from err
