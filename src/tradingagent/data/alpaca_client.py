"""Alpaca client — PAPER ENDPOINTS ONLY.

Guardrail (CLAUDE.md): this module never imports or exposes a trading client.
It uses the market-data and *paper* trading-clock APIs for session context and
cross-checks yfinance quotes. Any live-order code path is refused by design:
:class:`~alpaca.trading.client.TradingClient` is constructed with
``paper=True`` and only read-only calls (clock, calendar) are made.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from ..config import Settings
from .validate import DegradedTracker, clean_float, valid_price

log = logging.getLogger(__name__)


@dataclass
class MarketSession:
    is_open: bool
    next_open: datetime | None
    next_close: datetime | None
    previous_close_date: date | None


class AlpacaPaper:
    """Read-only paper-account view of session state and recent snapshots."""

    def __init__(self, settings: Settings, degraded: DegradedTracker | None = None):
        if not settings.alpaca_paper:
            raise RuntimeError("Refusing to construct an Alpaca client with ALPACA_PAPER != true")
        self.settings = settings
        self.degraded = degraded if degraded is not None else DegradedTracker()
        self._enabled = bool(settings.alpaca_key and settings.alpaca_secret)
        if not self._enabled:
            self.degraded.add("Alpaca paper", "ALPACA_API_KEY/SECRET not set")

    @property
    def enabled(self) -> bool:
        return self._enabled

    def market_session(self) -> MarketSession | None:
        """Current clock plus the most recent completed session date."""
        if not self._enabled:
            return None
        try:
            from alpaca.trading.client import TradingClient
            from alpaca.trading.requests import GetCalendarRequest

            client = TradingClient(
                self.settings.alpaca_key, self.settings.alpaca_secret, paper=True
            )
            clock = client.get_clock()
            today = clock.timestamp.date()
            calendar = client.get_calendar(
                GetCalendarRequest(start=today - timedelta(days=10), end=today)
            )
            past = [d.date for d in calendar if d.date < today] or [None]
            return MarketSession(
                is_open=bool(clock.is_open),
                next_open=getattr(clock, "next_open", None),
                next_close=getattr(clock, "next_close", None),
                previous_close_date=past[-1],
            )
        except Exception as exc:  # noqa: BLE001
            self.degraded.add("Alpaca clock", str(exc))
            return None

    def snapshots(self, symbols: list[str]) -> dict[str, dict]:
        """Latest trade/daily-bar snapshot per symbol (IEX free feed)."""
        if not self._enabled or not symbols:
            return {}
        try:
            from alpaca.data.historical import StockHistoricalDataClient
            from alpaca.data.requests import StockSnapshotRequest

            client = StockHistoricalDataClient(
                self.settings.alpaca_key, self.settings.alpaca_secret
            )
            raw = client.get_stock_snapshot(StockSnapshotRequest(symbol_or_symbols=symbols))
        except Exception as exc:  # noqa: BLE001
            self.degraded.add("Alpaca snapshots", str(exc))
            return {}

        out: dict[str, dict] = {}
        for symbol, snap in (raw or {}).items():
            if snap is None:
                continue
            trade = getattr(snap, "latest_trade", None)
            daily = getattr(snap, "daily_bar", None)
            prev = getattr(snap, "previous_daily_bar", None)
            price = clean_float(getattr(trade, "price", None)) or clean_float(
                getattr(daily, "close", None)
            )
            if not valid_price(price):
                continue
            prev_close = clean_float(getattr(prev, "close", None))
            out[symbol] = {
                "price": price,
                "volume": clean_float(getattr(daily, "volume", None), 0.0),
                "prev_close": prev_close,
                "change_pct": (
                    (price / prev_close - 1) * 100 if prev_close and prev_close > 0 else None
                ),
                "as_of": getattr(trade, "timestamp", None) or getattr(daily, "timestamp", None),
            }
        if not out:
            self.degraded.add("Alpaca snapshots", "no usable snapshots returned")
        return out

    def quote_crosscheck(self, symbol: str, reference_price: float, tolerance_pct: float = 5.0):
        """Flag when Alpaca and yfinance disagree materially on last price."""
        snap = self.snapshots([symbol]).get(symbol)
        if not snap or not valid_price(reference_price):
            return None
        drift = abs(snap["price"] / reference_price - 1) * 100
        if drift > tolerance_pct:
            self.degraded.add(
                "price crosscheck",
                f"{symbol}: yfinance {reference_price:.2f} vs Alpaca {snap['price']:.2f} ({drift:.1f}% apart)",
            )
        return drift


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
