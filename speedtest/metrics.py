"""Statistical bandwidth, jitter, and stream-scaling helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import pairwise
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .models import ActiveProtocol


@dataclass
class TransferProgress:
    """Mutable transfer counter and time-series byte samples."""

    bytes_transferred: int = 0
    samples: list[tuple[float, int]] = field(default_factory=list)

    def record(self, count: int) -> None:
        """Accumulate transferred bytes."""
        if count > 0:
            self.bytes_transferred += count

    def snapshot(self, monotonic_time: float) -> None:
        """Record a ``(monotonic_time, cumulative_bytes)`` sample."""
        self.samples.append((monotonic_time, self.bytes_transferred))


@dataclass(frozen=True)
class TransferResult:
    """Summary returned by a TCP or HTTP download/upload transfer."""

    bps: float
    total_bytes: int
    loaded_ping_ms: float | None
    protocol: ActiveProtocol


def compute_jitter(rtts_ms: Sequence[float]) -> float | None:
    """Compute mean consecutive RTT delta (RFC-style jitter) in milliseconds."""
    if not rtts_ms:
        return None
    if len(rtts_ms) == 1:
        return 0.0
    diffs = [abs(b - a) for a, b in pairwise(rtts_ms)]
    return round(sum(diffs) / len(diffs), 3)


def compute_latency_stats(rtts_ms: Sequence[float]) -> tuple[float, float]:
    """Compute ``(average_ms, jitter_ms)`` from valid RTT samples."""
    if not rtts_ms:
        raise ValueError("rtts_ms must contain at least one sample")
    avg_ms = round(sum(rtts_ms) / len(rtts_ms), 3)
    jitter_ms = compute_jitter(rtts_ms)
    return avg_ms, 0.0 if jitter_ms is None else jitter_ms


def estimate_bandwidth_bps(
    samples: Sequence[tuple[float, int]],
    total_bytes: int,
    elapsed: float,
    *,
    warmup_fraction: float = 0.2,
    trim_low: float = 0.2,
    trim_high: float = 0.1,
) -> float:
    """Estimate sustained throughput in bits/sec using warm-up + trimmed mean."""
    if total_bytes <= 0 or elapsed <= 0.0:
        return 0.0

    interval_rates: list[float] = []
    for (t0, b0), (t1, b1) in pairwise(samples):
        dt = t1 - t0
        db = b1 - b0
        if dt > 0.0 and db >= 0:
            interval_rates.append((db * 8.0) / dt)

    fallback_bps = (total_bytes * 8.0) / elapsed
    if len(interval_rates) < 4:
        return fallback_bps

    warmup_count = int(len(interval_rates) * warmup_fraction)
    steady = sorted(interval_rates[warmup_count:])
    if not steady:
        return fallback_bps

    low_cut = round(len(steady) * trim_low)
    high_cut = round(len(steady) * trim_high)
    end_idx = len(steady) - high_cut
    trimmed = steady[low_cut:end_idx] if low_cut < end_idx else steady
    if not trimmed:
        return fallback_bps

    estimated = sum(trimmed) / len(trimmed)
    return estimated if estimated > 0.0 else fallback_bps


def desired_stream_count(
    current_bps: float,
    current_streams: int,
    *,
    min_streams: int = 4,
    max_streams: int = 16,
) -> int:
    """Determine target concurrent streams based on observed throughput."""
    if current_bps >= 500_000_000.0:
        return max(current_streams, max_streams)
    if current_bps >= 100_000_000.0:
        return max(current_streams, min(8, max_streams))
    return max(current_streams, min_streams)
