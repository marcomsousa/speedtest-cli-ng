"""Data models, shared helpers, and result container for speedtest-cli-ng."""

from __future__ import annotations

import csv
import json
import math
import platform
from datetime import UTC, datetime
from io import StringIO
from typing import TYPE_CHECKING, Any, Literal, TypedDict

from .exceptions import ShareResultsConnectFailure

if TYPE_CHECKING:
    from collections.abc import Callable

__version__ = "3.0.0"

ProtocolName = Literal["auto", "tcp", "http"]
ActiveProtocol = Literal["tcp", "http"]


class FakeShutdownEvent:
    """Drop-in no-op event matching ``threading.Event.is_set``."""

    @staticmethod
    def is_set() -> bool:
        """Always return False."""
        return False

    isSet = is_set


class ServerDict(TypedDict, total=False):
    """Typed representation of a Speedtest.net server entry."""

    url: str
    lat: str
    lon: str
    name: str
    country: str
    cc: str
    sponsor: str
    id: str
    host: str
    d: float
    latency: float
    https_functional: int | bool


def utc_timestamp_iso() -> str:
    """Return current UTC timestamp formatted as an ISO-8601 ``...Z`` string."""
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def distance(
    origin: tuple[float, float],
    destination: tuple[float, float],
) -> float:
    """Calculate great-circle distance between two ``(lat, lon)`` pairs in km."""
    lat1, lon1 = origin
    lat2, lon2 = destination
    radius = 6371.0

    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2.0) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(dlon / 2.0) ** 2
    )
    a = max(0.0, min(1.0, a))
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    return radius * c


def build_user_agent(version: str = __version__) -> str:
    """Build a Speedtest.net-friendly User-Agent header string."""
    return f"speedtest-cli-ng/{version} Python/{platform.python_version()}"


def do_nothing(*args: Any, **kwargs: Any) -> None:
    """No-op progress callback."""


class SpeedtestResults:
    """Container holding speedtest measurements and serialization helpers."""

    def __init__(
        self,
        download: float = 0.0,
        upload: float = 0.0,
        ping: float = 0.0,
        server: dict[str, Any] | None = None,
        client: dict[str, Any] | None = None,
        secure: bool = True,
        *,
        jitter: float | None = None,
        loaded_ping_download: float | None = None,
        loaded_ping_upload: float | None = None,
        packet_loss: float | None = None,
        protocol: str | None = None,
        share_callback: Callable[[SpeedtestResults], str] | None = None,
    ) -> None:
        self.download = float(download)
        self.upload = float(upload)
        self.ping = float(ping)
        self.server: dict[str, Any] = {} if server is None else server
        self.client: dict[str, Any] = {} if client is None else client

        self._share: str | None = None
        self.timestamp: str = utc_timestamp_iso()
        self.bytes_received: int = 0
        self.bytes_sent: int = 0

        self.jitter: float | None = jitter
        self.loaded_ping_download: float | None = loaded_ping_download
        self.loaded_ping_upload: float | None = loaded_ping_upload
        self.packet_loss: float | None = packet_loss
        self.protocol: str | None = protocol

        self._secure: bool = secure
        self._share_callback = share_callback

    def __repr__(self) -> str:
        """Return string representation of ``self.dict()``."""
        return repr(self.dict())

    def touch_timestamp(self) -> None:
        """Refresh the result timestamp and invalidate any cached share link."""
        self.timestamp = utc_timestamp_iso()
        self._share = None

    def share(self) -> str:
        """Submit results to speedtest.net API and return the share image URL."""
        if self._share:
            return self._share
        if self._share_callback is None:
            raise ShareResultsConnectFailure(
                "No share callback configured for SpeedtestResults"
            )
        self._share = self._share_callback(self)
        return self._share

    def dict(self) -> dict[str, Any]:
        """Return dictionary of result data (preserving all legacy keys)."""
        return {
            "download": self.download,
            "upload": self.upload,
            "ping": self.ping,
            "server": self.server,
            "timestamp": self.timestamp,
            "bytes_sent": self.bytes_sent,
            "bytes_received": self.bytes_received,
            "share": self._share,
            "client": self.client,
            "jitter": self.jitter,
            "loaded_ping_download": self.loaded_ping_download,
            "loaded_ping_upload": self.loaded_ping_upload,
            "packet_loss": self.packet_loss,
            "protocol": self.protocol,
        }

    @staticmethod
    def csv_header(delimiter: str = ",") -> str:
        """Return CSV header row."""
        row = [
            "Server ID",
            "Sponsor",
            "Server Name",
            "Timestamp",
            "Distance",
            "Ping",
            "Download",
            "Upload",
            "Share",
            "IP Address",
        ]
        out = StringIO()
        writer = csv.writer(out, delimiter=delimiter, lineterminator="")
        writer.writerow(row)
        return out.getvalue()

    def csv(self, delimiter: str = ",") -> str:
        """Return result data formatted as CSV."""
        out = StringIO()
        writer = csv.writer(out, delimiter=delimiter, lineterminator="")
        row = [
            self.server.get("id", ""),
            self.server.get("sponsor", ""),
            self.server.get("name", ""),
            self.timestamp,
            self.server.get("d", ""),
            self.ping,
            self.download,
            self.upload,
            self._share or "",
            self.client.get("ip", ""),
        ]
        writer.writerow(row)
        return out.getvalue()

    def json(self, pretty: bool = False) -> str:
        """Return result data serialized as JSON."""
        if pretty:
            return json.dumps(self.dict(), indent=4, sort_keys=True)
        return json.dumps(self.dict())
