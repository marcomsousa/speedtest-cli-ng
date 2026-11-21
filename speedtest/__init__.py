"""Modern async & raw-TCP speedtest.net library and CLI (speedtest-ng)."""

from __future__ import annotations

from .async_client import AsyncSpeedtest
from .cli import main, shell
from .client import Speedtest
from .exceptions import (
    ConfigRetrievalError,
    InvalidServerIDType,
    InvalidSpeedtestMiniServer,
    NoMatchedServers,
    ServersRetrievalError,
    ShareResultsConnectFailure,
    ShareResultsSubmitFailure,
    SpeedtestBestServerFailure,
    SpeedtestCLIError,
    SpeedtestConfigError,
    SpeedtestException,
    SpeedtestHTTPError,
    SpeedtestMiniConnectFailure,
    SpeedtestMissingBestServer,
    SpeedtestServersError,
    SpeedtestTransferError,
    SpeedtestUploadTimeout,
)
from .models import (
    ActiveProtocol,
    FakeShutdownEvent,
    ProtocolName,
    ResultDict,
    ServerDict,
    SpeedtestResults,
    __version__,
    build_user_agent,
    distance,
    do_nothing,
)

__all__ = [
    "ActiveProtocol",
    "AsyncSpeedtest",
    "ConfigRetrievalError",
    "FakeShutdownEvent",
    "InvalidServerIDType",
    "InvalidSpeedtestMiniServer",
    "NoMatchedServers",
    "ProtocolName",
    "ResultDict",
    "ServerDict",
    "ServersRetrievalError",
    "ShareResultsConnectFailure",
    "ShareResultsSubmitFailure",
    "Speedtest",
    "SpeedtestBestServerFailure",
    "SpeedtestCLIError",
    "SpeedtestConfigError",
    "SpeedtestException",
    "SpeedtestHTTPError",
    "SpeedtestMiniConnectFailure",
    "SpeedtestMissingBestServer",
    "SpeedtestResults",
    "SpeedtestServersError",
    "SpeedtestTransferError",
    "SpeedtestUploadTimeout",
    "__version__",
    "build_user_agent",
    "distance",
    "do_nothing",
    "main",
    "shell",
]
