"""Price/volume client built on yfinance (free tier, no key).

One bulk download serves breadth, sector rotation and the screener, so the
daily scan makes a handful of HTTP round-trips rather than one per ticker.
"""

from __future__ import annotations

import logging
import warnings
from dataclasses import dataclass
from typing import Iterable

import pandas as pd

from .universe import SECTOR_ETFS
from .validate import DataUnavailable, DegradedTracker, clean_float, validate_bars

log = logging.getLogger(__name__)

INDEX_PROXIES: dict[str, str] = {
    "S&P 500": "SPY",
    "Nasdaq 100": "QQQ",
    "Dow 30": "DIA",
    "Russell 2000": "IWM",
}
VIX_SYMBOL = "^VIX"

# Trading-day lookbacks used for the return columns in the report.
LOOKBACKS: dict[str, int] = {"1d": 1, "5d": 5, "1mo": 21, "3mo": 63}


@dataclass
class Quote:
    symbol: str
    label: str
    price: float
    returns: dict[str, float | None]

    def ret(self, window: str) -> float | None:
        return self.returns.get(window)


def pct_change(series: pd.Series, bars: int) -> float | None:
    """Percent change over ``bars`` trading days, or None if unavailable."""
    clean = series.dropna()
    if len(clean) <= bars:
        return None
    now, then = float(clean.iloc[-1]), float(clean.iloc[-1 - bars])
    if then <= 0:
        return None
    return (now / then - 1.0) * 100.0


class MarketData:
    """yfinance-backed OHLCV access with validation and DEGRADED reporting."""

    def __init__(self, degraded: DegradedTracker | None = None, period: str = "1y"):
        self.degraded = degraded if degraded is not None else DegradedTracker()
        self.period = period
        self._cache: dict[tuple[str, ...], pd.DataFrame] = {}

    # -- raw ------------------------------------------------------------
    def download(self, symbols: Iterable[str], period: str | None = None) -> pd.DataFrame:
        """Bulk-download OHLCV. Returns a column-MultiIndex frame (field, symbol)."""
        import yfinance as yf

        syms = tuple(sorted({s.strip().upper() for s in symbols if s and s.strip()}))
        if not syms:
            raise DataUnavailable("no symbols requested")
        key = (period or self.period, *syms)
        if key in self._cache:
            return self._cache[key]

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            frame = yf.download(
                list(syms),
                period=period or self.period,
                interval="1d",
                auto_adjust=True,
                progress=False,
                threads=True,
                group_by="column",
            )
        if frame is None or frame.empty:
            raise DataUnavailable(f"yfinance returned no rows for {len(syms)} symbols")
        if not isinstance(frame.columns, pd.MultiIndex):  # single-symbol shape
            frame.columns = pd.MultiIndex.from_product([frame.columns, [syms[0]]])
        self._cache[key] = frame
        return frame

    def bars_for(
        self, frame: pd.DataFrame, symbol: str, min_rows: int = 20, require_volume: bool = True
    ) -> pd.DataFrame:
        """Slice one symbol out of a bulk frame as an OHLCV frame, validated."""
        fields = ["Open", "High", "Low", "Close", "Volume"]
        available = [f for f in fields if (f, symbol) in frame.columns]
        if not available:
            raise DataUnavailable(f"{symbol}: absent from download")
        single = frame.loc[:, [(f, symbol) for f in available]].copy()
        single.columns = available
        single = single.dropna(subset=["Close"])
        validate_bars(single, symbol, min_rows=min_rows, require_volume=require_volume)
        return single

    def load_many(
        self,
        symbols: Iterable[str],
        min_rows: int = 60,
        period: str | None = None,
        require_volume: bool = True,
    ) -> dict[str, pd.DataFrame]:
        """Download and validate many symbols; unusable ones are dropped."""
        syms = sorted({s.strip().upper() for s in symbols if s and s.strip()})
        try:
            frame = self.download(syms, period=period)
        except Exception as exc:  # noqa: BLE001
            self.degraded.add("yfinance OHLCV", f"bulk download failed: {exc}")
            return {}

        out: dict[str, pd.DataFrame] = {}
        dropped: list[str] = []
        for symbol in syms:
            try:
                out[symbol] = self.bars_for(
                    frame, symbol, min_rows=min_rows, require_volume=require_volume
                )
            except DataUnavailable as exc:
                dropped.append(str(exc))
        if dropped:
            preview = "; ".join(dropped[:5])
            more = f" (+{len(dropped) - 5} more)" if len(dropped) > 5 else ""
            self.degraded.add(
                "yfinance OHLCV", f"{len(dropped)}/{len(syms)} symbols unusable: {preview}{more}"
            )
        return out

    # -- derived --------------------------------------------------------
    def quotes(self, mapping: dict[str, str], period: str = "1y") -> list[Quote]:
        """Quotes for a {label: symbol} mapping, e.g. indices or sector ETFs."""
        bars = self.load_many(mapping.values(), min_rows=25, period=period)
        quotes: list[Quote] = []
        for label, symbol in mapping.items():
            frame = bars.get(symbol)
            if frame is None:
                continue
            close = frame["Close"]
            price = clean_float(close.iloc[-1])
            if price is None:
                continue
            quotes.append(
                Quote(
                    symbol=symbol,
                    label=label,
                    price=price,
                    returns={w: pct_change(close, n) for w, n in LOOKBACKS.items()},
                )
            )
        return quotes

    def index_snapshot(self) -> tuple[list[Quote], float | None]:
        """Headline index quotes plus the current VIX level."""
        quotes = self.quotes(INDEX_PROXIES)
        if not quotes:
            self.degraded.add("yfinance indices", "no index proxy returned usable data")
        vix = None
        try:
            vix_bars = self.load_many([VIX_SYMBOL], min_rows=25, require_volume=False)
            if VIX_SYMBOL in vix_bars:
                vix = clean_float(vix_bars[VIX_SYMBOL]["Close"].iloc[-1])
        except Exception as exc:  # noqa: BLE001
            self.degraded.add("yfinance VIX", str(exc))
        if vix is None:
            self.degraded.add("yfinance VIX", "VIX level unavailable")
        return quotes, vix

    def sector_etf_quotes(self) -> list[Quote]:
        quotes = self.quotes(SECTOR_ETFS)
        missing = set(SECTOR_ETFS) - {q.label for q in quotes}
        if missing:
            self.degraded.add("yfinance sector ETFs", f"missing {', '.join(sorted(missing))}")
        return quotes

    def latest_session(self, frame: pd.DataFrame) -> pd.Timestamp | None:
        try:
            return frame.index[-1]
        except (IndexError, AttributeError):
            return None
