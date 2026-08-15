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
from datetime import date, datetime, time, timedelta, timezone

from ..config import Settings
from .validate import DegradedTracker

log = logging.getLogger(__name__)

#: Company-news lookback, in calendar days. Upstream TradingAgents uses the
#: same seven days; what is ours is that the window ends at the snapshot's
#: market date rather than at whatever "today" happens to be when the call runs.
NEWS_WINDOW_DAYS = 7


def news_window(as_of: date, days: int = NEWS_WINDOW_DAYS) -> tuple[date, date]:
    """The ``(start, end)`` a caller should ask for, given an as-of date."""
    return as_of - timedelta(days=days), as_of


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

    @property
    def published_at(self) -> date | None:
        """The UTC day the story was filed, or ``None`` when the feed omits it."""
        if not self.datetime_utc:
            return None
        try:
            return datetime.fromtimestamp(self.datetime_utc, tz=timezone.utc).date()
        except (OverflowError, OSError, ValueError):
            return None

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "headline": self.headline,
            "source": self.source,
            "url": self.url,
            "datetime_utc": self.datetime_utc,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "NewsItem":
        return cls(
            symbol=str(raw.get("symbol", "")),
            headline=str(raw.get("headline", "")),
            source=str(raw.get("source", "")),
            url=str(raw.get("url", "")),
            datetime_utc=int(raw.get("datetime_utc") or 0),
        )


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

    def company_news(
        self, symbol: str, start_date: date, end_date: date, limit: int = 5
    ) -> list[NewsItem]:
        """Headlines in an explicit window, filtered to end at ``end_date``.

        The window used to be ``days`` back from ``date.today()``, which meant
        a ``--date 2026-06-01`` re-run read June's prices against August's
        headlines and called it analysis. The caller passes the snapshot's
        market date; Finnhub's ``to`` is a date and its rows carry a UTC
        timestamp, so the tail of the window is trimmed here as well as asked
        for — a same-day story filed after the close is still the future
        relative to a close-priced snapshot.
        """
        if not self.enabled:
            return []
        start, end = start_date, end_date
        # End of the market date, UTC. Anything stamped later is not evidence
        # the snapshot could have seen.
        cutoff = int(
            datetime.combine(end, time.max, tzinfo=timezone.utc).timestamp()
        )
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
        fresh = [n for n in items if n.datetime_utc and n.datetime_utc > cutoff]
        if fresh:
            # Not degraded — the provider answered. Logged because a run that
            # trims a lot of headlines is a run someone should look at.
            log.info(
                "Finnhub news %s: dropped %d headline(s) stamped after %s",
                symbol, len(fresh), end.isoformat(),
            )
        # An undated headline is kept: it is more likely a provider gap than a
        # future story, and it is visibly "undated" everywhere it is printed.
        items = [n for n in items if not n.datetime_utc or n.datetime_utc <= cutoff]
        items.sort(key=lambda n: n.datetime_utc, reverse=True)
        return items[:limit]


def _num(value) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out else None  # reject NaN
