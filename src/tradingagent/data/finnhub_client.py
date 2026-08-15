"""Finnhub free-tier client: earnings calendar, economic calendar, company news.

Free-tier reality (flagged in the report rather than papered over):
- ``/calendar/earnings`` IS on the free tier.
- ``/calendar/economic`` is premium-only; free keys get 403. We fall back to a
  static recurring-release calendar (see :mod:`tradingagent.discovery.calendar`)
  and mark the source DEGRADED. No paid service is added.
- 60 calls/minute; we stay far below that by batching at the calendar level.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta

from ..config import Settings
from .validate import DegradedTracker

log = logging.getLogger(__name__)


@dataclass
class EarningsEvent:
    symbol: str
    date: date
    hour: str  # "bmo" | "amc" | "dmh" | ""
    eps_estimate: float | None
    revenue_estimate: float | None

    @property
    def timing(self) -> str:
        return {"bmo": "Before open", "amc": "After close", "dmh": "During market"}.get(
            self.hour, "Unconfirmed"
        )


@dataclass
class NewsItem:
    symbol: str
    headline: str
    source: str
    url: str
    datetime_utc: int


class FinnhubFree:
    """Thin wrapper; every call degrades gracefully instead of raising."""

    def __init__(self, settings: Settings, degraded: DegradedTracker | None = None):
        self.degraded = degraded if degraded is not None else DegradedTracker()
        self._key = settings.finnhub_key
        self._client = None
        if not self._key:
            self.degraded.add("Finnhub", "FINNHUB_API_KEY not set")

    @property
    def enabled(self) -> bool:
        return bool(self._key)

    def _get_client(self):
        if self._client is None:
            import finnhub

            self._client = finnhub.Client(api_key=self._key)
        return self._client

    def earnings_calendar(self, start: date, end: date, symbol: str = "") -> list[EarningsEvent]:
        """Earnings dates in a window; ``symbol`` narrows the pull to one name.

        A market-wide pull that comes back empty is degraded — the window
        always contains *someone's* results. A single-symbol pull that comes
        back empty is the answer: that company does not report in this window.
        """
        events = self._earnings(start, end, symbol)
        if events is None:
            return []
        if not events and not symbol:
            self.degraded.add("Finnhub earnings calendar", f"no events {start}..{end}")
        return events

    def earnings_for(self, symbol: str, start: date, end: date) -> list[EarningsEvent] | None:
        """Per-symbol earnings, or ``None`` when the source could not be read.

        The options stage needs that distinction: "no print before expiry" and
        "we could not check" are different facts about a sold option, and the
        second must never be scored as the first.
        """
        return self._earnings(start, end, symbol)

    def _earnings(self, start: date, end: date, symbol: str) -> list[EarningsEvent] | None:
        if not self.enabled:
            return None
        label = f"Finnhub earnings calendar{f' {symbol}' if symbol else ''}"
        try:
            payload = self._get_client().earnings_calendar(
                _from=start.isoformat(), to=end.isoformat(), symbol=symbol
            )
        except Exception as exc:  # noqa: BLE001
            self.degraded.add(label, str(exc))
            return None

        events: list[EarningsEvent] = []
        for row in (payload or {}).get("earningsCalendar", []):
            try:
                when = date.fromisoformat(str(row.get("date")))
            except (TypeError, ValueError):
                continue
            ticker = str(row.get("symbol", "")).strip().upper()
            if not ticker:
                continue
            events.append(
                EarningsEvent(
                    symbol=ticker,
                    date=when,
                    hour=str(row.get("hour") or "").lower(),
                    eps_estimate=_num(row.get("epsEstimate")),
                    revenue_estimate=_num(row.get("revenueEstimate")),
                )
            )
        return events

    def economic_calendar(self, start: date, end: date) -> list[dict]:
        """Premium on the free tier — expected to fail; caller uses a fallback."""
        if not self.enabled:
            return []
        try:
            payload = self._get_client().calendar_economic()
        except Exception as exc:  # noqa: BLE001
            self.degraded.add(
                "Finnhub economic calendar",
                f"premium endpoint unavailable on free tier ({str(exc)[:120]}) — using static release calendar",
            )
            return []
        rows = [
            r
            for r in (payload or {}).get("economicCalendar", [])
            if start.isoformat() <= str(r.get("time", ""))[:10] <= end.isoformat()
        ]
        if not rows:
            self.degraded.add("Finnhub economic calendar", "no events in window")
        return rows

    def company_news(self, symbol: str, days: int = 7, limit: int = 5) -> list[NewsItem]:
        if not self.enabled:
            return []
        end = date.today()
        start = end - timedelta(days=days)
        try:
            payload = self._get_client().company_news(
                symbol, _from=start.isoformat(), to=end.isoformat()
            )
        except Exception as exc:  # noqa: BLE001
            self.degraded.add(f"Finnhub news {symbol}", str(exc))
            return []
        items = [
            NewsItem(
                symbol=symbol,
                headline=str(r.get("headline", "")).strip(),
                source=str(r.get("source", "")).strip(),
                url=str(r.get("url", "")).strip(),
                datetime_utc=int(r.get("datetime") or 0),
            )
            for r in (payload or [])
            if str(r.get("headline", "")).strip()
        ]
        items.sort(key=lambda n: n.datetime_utc, reverse=True)
        return items[:limit]


def _num(value) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out else None  # reject NaN
