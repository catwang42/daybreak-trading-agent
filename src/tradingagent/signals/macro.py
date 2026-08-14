"""Macro regime from FRED series.

Market-wide, not per-ticker: nothing here says anything about one company, but
it says a great deal about whether a momentum breakout is running with the
tape or against it. The discovery stage already computes breadth from prices;
this is the other half — the rates, credit and volatility backdrop those
prices are reacting to.

Each series carries its own direction rule, because "up" is bullish for some
and bearish for others (a falling VIX is risk-on; a falling 10-year is
ambiguous but a widening high-yield spread is not). The rules are stated in
:data:`SERIES` rather than inferred, so a reader can disagree with one without
reverse-engineering it.

Free tier: FRED is free with a key and has no meaningful rate limit for a
dozen series a day. Without ``FRED_API_KEY`` the source skips cleanly.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Literal

from ..config import env
from .base import Signal, SignalSource

log = logging.getLogger(__name__)

OBSERVATIONS_URL = "https://api.stlouisfed.org/fred/series/observations"

# Direction: what a RISE in this series means for equity risk appetite.
RiseMeans = Literal["bullish", "bearish"]


@dataclass(frozen=True)
class Series:
    series_id: str
    label: str
    rise_means: RiseMeans
    units: str
    #: Change over the window (in the series' own units) at which we call it a move.
    material: float
    weight: float = 1.0


SERIES: tuple[Series, ...] = (
    Series("BAMLH0A0HYM2", "High-yield credit spread", "bearish", "pp", material=0.25, weight=1.4),
    Series("VIXCLS", "VIX", "bearish", "pts", material=2.0, weight=1.2),
    Series("T10Y2Y", "10y–2y Treasury spread", "bullish", "pp", material=0.10, weight=0.8),
    Series("DGS10", "10-year Treasury yield", "bearish", "pp", material=0.20, weight=0.9),
    Series("ICSA", "Initial jobless claims", "bearish", "claims", material=25_000, weight=0.9),
    Series("DTWEXBGS", "Trade-weighted dollar", "bearish", "index", material=1.5, weight=0.6),
)

LOOKBACK_DAYS = 30


@dataclass
class Observation:
    series: Series
    latest: float
    prior: float
    latest_date: date

    @property
    def change(self) -> float:
        return self.latest - self.prior

    @property
    def material(self) -> bool:
        return abs(self.change) >= self.series.material

    @property
    def direction(self) -> int:
        """+1 risk-on, -1 risk-off, 0 when the move is inside the noise band."""
        if not self.material:
            return 0
        rising = self.change > 0
        bullish = rising if self.series.rise_means == "bullish" else not rising
        return 1 if bullish else -1

    def line(self) -> str:
        arrow = "↑" if self.change > 0 else "↓" if self.change < 0 else "→"
        verdict = "risk-on" if self.direction > 0 else "risk-off" if self.direction < 0 else "unchanged"
        return (
            f"  - {self.series.label}: {self.latest:,.2f} {self.series.units} "
            f"({arrow} {self.change:+,.2f} over {LOOKBACK_DAYS}d, as of "
            f"{self.latest_date.isoformat()}) — {verdict}"
        )


class MacroSource(SignalSource):
    """FRED rates, credit and volatility series as one market-wide regime read."""

    name = "macro_fred"
    scope = "market"
    describes = "FRED: credit spreads, VIX, curve, yields, claims, dollar"

    def __init__(self, degraded=None, series: tuple[Series, ...] = SERIES, session: Any = None):
        super().__init__(degraded)
        self.series = series
        self._session = session

    def available(self) -> tuple[bool, str]:
        if not env("FRED_API_KEY"):
            return False, "FRED_API_KEY not set — macro series skipped"
        return True, ""

    def session(self):
        if self._session is None:
            import requests

            self._session = requests.Session()
        return self._session

    def observe(self, series: Series, run_date: date) -> Observation | None:
        start = run_date - timedelta(days=LOOKBACK_DAYS * 3)  # slack for holidays and revisions
        payload = self.session().get(
            OBSERVATIONS_URL,
            params={
                "series_id": series.series_id,
                "api_key": env("FRED_API_KEY"),
                "file_type": "json",
                "observation_start": start.isoformat(),
                "observation_end": run_date.isoformat(),
            },
            timeout=20,
        )
        payload.raise_for_status()
        points = [
            (date.fromisoformat(row["date"]), float(row["value"]))
            for row in payload.json().get("observations", [])
            if row.get("value") not in (None, "", ".")
        ]
        if len(points) < 2:
            log.info("FRED %s returned %d usable observations", series.series_id, len(points))
            return None
        points.sort()
        latest_date, latest = points[-1]
        target = latest_date - timedelta(days=LOOKBACK_DAYS)
        prior = min(points, key=lambda p: abs((p[0] - target).days))[1]
        return Observation(series=series, latest=latest, prior=prior, latest_date=latest_date)

    def collect(self, symbols: list[str], run_date: date) -> list[Signal]:
        observations: list[Observation] = []
        for series in self.series:
            try:
                observation = self.observe(series, run_date)
            except Exception as exc:  # noqa: BLE001 - one retired series is not a dead source
                log.info("FRED series %s unavailable: %s", series.series_id, exc)
                continue
            if observation:
                observations.append(observation)
        if not observations:
            return []
        return [regime_signal(observations, run_date)]


def regime_signal(observations: list[Observation], run_date: date) -> Signal:
    """Weighted vote across the series, rendered as one market-wide signal."""
    moving = [o for o in observations if o.direction]
    total_weight = sum(o.series.weight for o in observations) or 1.0
    net = sum(o.direction * o.series.weight for o in moving) / total_weight
    direction = 1 if net > 0.15 else -1 if net < -0.15 else 0
    risk_on = [o.series.label for o in moving if o.direction > 0]
    risk_off = [o.series.label for o in moving if o.direction < 0]
    headline = (
        f"{len(moving)} of {len(observations)} macro series moved materially over "
        f"{LOOKBACK_DAYS} days; net {net:+.2f} "
        f"(risk-on: {', '.join(risk_on) or 'none'}; risk-off: {', '.join(risk_off) or 'none'})"
    )
    return Signal(
        source=MacroSource.name,
        kind="macro_regime",
        symbol=None,
        direction=direction,
        strength=min(1.0, abs(net)),
        headline=headline,
        detail="\n".join(o.line() for o in observations),
        as_of=run_date,
    )
