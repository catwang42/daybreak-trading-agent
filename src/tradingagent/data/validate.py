"""Validation helpers — every market-data response passes through here.

CLAUDE.md: "Validate every market-data response (NaN/zero-volume/empty).
Failed sources → visible DEGRADED section, never a silently thin report."
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)


class DataUnavailable(RuntimeError):
    """A data source returned nothing usable."""


@dataclass
class DegradedTracker:
    """Collects every source that failed or returned unusable data."""

    entries: list[tuple[str, str]] = field(default_factory=list)

    def add(self, source: str, reason: str) -> None:
        reason = " ".join(str(reason).split())[:300]
        log.warning("DEGRADED %s: %s", source, reason)
        self.entries.append((source, reason))

    def __bool__(self) -> bool:
        return bool(self.entries)

    @property
    def sources(self) -> list[str]:
        seen: list[str] = []
        for source, _ in self.entries:
            if source not in seen:
                seen.append(source)
        return seen


def is_finite(value: Any) -> bool:
    """True when ``value`` is a real, finite number (rejects None/NaN/inf)."""
    if value is None or isinstance(value, bool):
        return False
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def clean_float(value: Any, default: float | None = None) -> float | None:
    return float(value) if is_finite(value) else default


def valid_price(value: Any) -> bool:
    return is_finite(value) and float(value) > 0


def validate_bars(frame: Any, ticker: str, min_rows: int = 20, require_volume: bool = True) -> None:
    """Reject an OHLCV frame that is empty, too short, or all zero-volume.

    ``require_volume=False`` for pure index series such as ``^VIX``, which
    legitimately report no volume.
    """
    if frame is None or len(frame) == 0:
        raise DataUnavailable(f"{ticker}: empty OHLCV response")
    if len(frame) < min_rows:
        raise DataUnavailable(f"{ticker}: only {len(frame)} bars, need {min_rows}")
    if "Close" in frame and frame["Close"].isna().all():
        raise DataUnavailable(f"{ticker}: all closes are NaN")
    if require_volume and "Volume" in frame:
        recent = frame["Volume"].tail(min_rows)
        if float(recent.fillna(0).sum()) <= 0:
            raise DataUnavailable(f"{ticker}: zero volume over last {min_rows} bars")
