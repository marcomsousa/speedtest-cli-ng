"""Exception hierarchy for speedtest-ng.

Preserves full drop-in compatibility with ``speedtest-cli`` 2.1.3 so downstream
consumers such as Home Assistant Core's ``speedtestdotnet`` integration can
catch ``speedtest.SpeedtestException`` and its subclasses without changes.
"""

from __future__ import annotations


class SpeedtestException(Exception):
    """Base exception for this module."""


class SpeedtestCLIError(SpeedtestException):
    """Generic exception for raising errors during CLI operation."""


class SpeedtestHTTPError(SpeedtestException):
    """Base HTTP exception for this module."""


class SpeedtestConfigError(SpeedtestException):
    """Configuration XML is invalid."""


class SpeedtestServersError(SpeedtestException):
    """Servers XML or JSON payload is invalid."""


class ConfigRetrievalError(SpeedtestHTTPError):
    """Could not retrieve speedtest-config.php."""


class ServersRetrievalError(SpeedtestHTTPError):
    """Could not retrieve the Speedtest.net server list."""


class InvalidServerIDType(SpeedtestException):
    """Server ID used for filtering was not an integer."""


class NoMatchedServers(SpeedtestException):
    """No servers matched when filtering."""


class SpeedtestMiniConnectFailure(SpeedtestException):
    """Could not connect to the provided Speedtest Mini server."""


class InvalidSpeedtestMiniServer(SpeedtestException):
    """Server provided as a Speedtest Mini server is invalid."""


class ShareResultsConnectFailure(SpeedtestException):
    """Could not connect to speedtest.net API to POST results."""


class ShareResultsSubmitFailure(SpeedtestException):
    """Unable to successfully POST results to speedtest.net API."""


class SpeedtestUploadTimeout(SpeedtestException):
    """Test length configuration reached during upload."""


class SpeedtestBestServerFailure(SpeedtestException):
    """Unable to determine best server."""


class SpeedtestMissingBestServer(SpeedtestException):
    """get_best_server not called or not able to determine best server."""


class SpeedtestTransferError(SpeedtestException):
    """Transfer failed with zero bytes transferred across all workers."""
