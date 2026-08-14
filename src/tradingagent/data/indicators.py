"""Technical indicators for the deep-analysis evidence pack.

Ported from `reference/TradingAgents/tradingagents/agents/analysts/market_analyst.py`
and `dataflows/stockstats_utils.py` (Apache-2.0, commit a33fd4c): the indicator
*menu* and the selection discipline are upstream's, the arithmetic is ours.

Deliberate deviation. Upstream hands the analyst a tool that calls `stockstats`
against a cached CSV; the analyst then picks up to eight indicators by name over
several tool-calling turns. We have no tool-calling loop (no LangGraph in the
runtime), and adding `stockstats` would pull in a dependency to recompute what
pandas already does. So we compute the same menu here, once, and inject the
whole block into the analyst prompt as pre-computed evidence. The analyst is
told in its prompt to *select* the ones that matter rather than narrate all of
them — that is where upstream's discipline survives the port.

Every function tolerates a short or gappy series and returns ``None`` rather
than a NaN, so a thin history degrades one line of the report instead of the run.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from .validate import clean_float


@dataclass
class Indicator:
    key: str
    label: str
    value: float | None
    note: str = ""

    @property
    def display(self) -> str:
        if self.value is None:
            return "unavailable"
        return f"{self.value:,.2f}"


@dataclass
class IndicatorSet:
    """The indicator menu for one ticker, plus the context to read it."""

    symbol: str
    close: float
    indicators: list[Indicator] = field(default_factory=list)
    sessions: int = 0

    def get(self, key: str) -> float | None:
        return next((i.value for i in self.indicators if i.key == key), None)

    def markdown(self) -> str:
        lines = [
            f"Last close: ${self.close:,.2f} (computed over {self.sessions} sessions of daily bars)",
            "",
            "| Indicator | Value | Reading |",
            "|---|---:|---|",
        ]
        for ind in self.indicators:
            lines.append(f"| {ind.label} | {ind.display} | {ind.note or '—'} |")
        return "\n".join(lines)


def sma(series: pd.Series, window: int) -> float | None:
    if len(series) < window:
        return None
    return clean_float(series.rolling(window).mean().iloc[-1])


def ema_series(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def rsi(series: pd.Series, window: int = 14) -> float | None:
    """Wilder's RSI. ``ewm(alpha=1/window)`` is Wilder smoothing, not a simple mean."""
    if len(series) < window + 1:
        return None
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1 / window, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / window, adjust=False).mean()
    last_loss = float(avg_loss.iloc[-1])
    if last_loss == 0:
        # No down-closes in the window: RSI is 100 by definition, not a divide-by-zero.
        return 100.0 if float(avg_gain.iloc[-1]) > 0 else 50.0
    return clean_float(100.0 - 100.0 / (1.0 + float(avg_gain.iloc[-1]) / last_loss))


def macd(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> tuple[float | None, ...]:
    """(macd, signal, histogram) — the classic 12/26/9 triple."""
    if len(series) < slow + signal:
        return (None, None, None)
    line = ema_series(series, fast) - ema_series(series, slow)
    sig = ema_series(line, signal)
    hist = line - sig
    return (clean_float(line.iloc[-1]), clean_float(sig.iloc[-1]), clean_float(hist.iloc[-1]))


def bollinger(series: pd.Series, window: int = 20, stdevs: float = 2.0) -> tuple[float | None, ...]:
    """(middle, upper, lower) on a 20-period simple moving average."""
    if len(series) < window:
        return (None, None, None)
    mid = series.rolling(window).mean()
    sd = series.rolling(window).std(ddof=0)
    return (
        clean_float(mid.iloc[-1]),
        clean_float((mid + stdevs * sd).iloc[-1]),
        clean_float((mid - stdevs * sd).iloc[-1]),
    )


def atr(frame: pd.DataFrame, window: int = 14) -> float | None:
    """Average true range, Wilder-smoothed. Needs High/Low/Close."""
    if len(frame) < window + 1 or not {"High", "Low", "Close"} <= set(frame.columns):
        return None
    high, low, prev_close = frame["High"], frame["Low"], frame["Close"].shift(1)
    true_range = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    return clean_float(true_range.ewm(alpha=1 / window, adjust=False).mean().iloc[-1])


def vwma(frame: pd.DataFrame, window: int = 20) -> float | None:
    """Volume-weighted moving average — where the volume actually traded."""
    if len(frame) < window or not {"Close", "Volume"} <= set(frame.columns):
        return None
    volume = frame["Volume"].rolling(window).sum()
    if float(volume.iloc[-1]) <= 0:
        return None
    weighted = (frame["Close"] * frame["Volume"]).rolling(window).sum()
    return clean_float(weighted.iloc[-1] / volume.iloc[-1])


def money_flow_index(frame: pd.DataFrame, window: int = 14) -> float | None:
    """MFI — RSI weighted by dollar volume, so it reads accumulation not just price."""
    if len(frame) < window + 1 or not {"High", "Low", "Close", "Volume"} <= set(frame.columns):
        return None
    typical = (frame["High"] + frame["Low"] + frame["Close"]) / 3.0
    flow = typical * frame["Volume"]
    up = flow.where(typical.diff() > 0, 0.0).rolling(window).sum()
    down = flow.where(typical.diff() < 0, 0.0).rolling(window).sum()
    last_down = float(down.iloc[-1])
    if last_down == 0:
        return 100.0 if float(up.iloc[-1]) > 0 else 50.0
    return clean_float(100.0 - 100.0 / (1.0 + float(up.iloc[-1]) / last_down))


def _pct_from(close: float, level: float | None) -> str:
    if level is None or level <= 0:
        return "unavailable"
    return f"price is {(close / level - 1.0) * 100:+.1f}% vs this level"


def compute_indicators(symbol: str, frame: pd.DataFrame) -> IndicatorSet:
    """Build the full indicator menu for one ticker from its OHLCV frame."""
    close_series = frame["Close"].dropna()
    close = float(close_series.iloc[-1])
    macd_line, macd_signal, macd_hist = macd(close_series)
    boll_mid, boll_up, boll_low = bollinger(close_series)
    rsi14 = rsi(close_series)
    atr14 = atr(frame)
    sma50 = sma(close_series, 50)
    sma200 = sma(close_series, 200)
    ema10 = clean_float(ema_series(close_series, 10).iloc[-1]) if len(close_series) >= 10 else None
    vwma20 = vwma(frame)

    def _hist_note() -> str:
        if macd_hist is None:
            return "unavailable"
        direction = "bullish" if macd_hist > 0 else "bearish"
        return f"{direction} crossover state; momentum {'building' if macd_hist > 0 else 'fading'}"

    def _rsi_note() -> str:
        if rsi14 is None:
            return "unavailable"
        if rsi14 >= 70:
            return "overbought by the conventional threshold — in a strong trend this can persist"
        if rsi14 <= 30:
            return "oversold by the conventional threshold"
        return "neither overbought nor oversold"

    def _atr_note() -> str:
        if atr14 is None:
            return "unavailable"
        return f"one average day's range is {atr14 / close * 100:.1f}% of price — size stops against this"

    def _boll_note() -> str:
        if boll_up is None or boll_low is None or boll_mid is None:
            return "unavailable"
        if close >= boll_up:
            return "at or above the upper band — extended, or breaking out on strength"
        if close <= boll_low:
            return "at or below the lower band"
        span = boll_up - boll_low
        pos = (close - boll_low) / span * 100 if span > 0 else 50.0
        return f"{pos:.0f}% of the way up the band, band width {span / boll_mid * 100:.1f}% of the mean"

    indicators = [
        Indicator("close_50_sma", "50-day SMA", sma50, _pct_from(close, sma50)),
        Indicator("close_200_sma", "200-day SMA", sma200, _pct_from(close, sma200)),
        Indicator("close_10_ema", "10-day EMA", ema10, _pct_from(close, ema10)),
        Indicator("macd", "MACD line (12/26)", macd_line, "positive = fast EMA above slow" if macd_line is not None else "unavailable"),
        Indicator("macds", "MACD signal (9)", macd_signal, "the line's own 9-period EMA"),
        Indicator("macdh", "MACD histogram", macd_hist, _hist_note()),
        Indicator("rsi", "RSI (14)", rsi14, _rsi_note()),
        Indicator("boll", "Bollinger middle (20)", boll_mid, _pct_from(close, boll_mid)),
        Indicator("boll_ub", "Bollinger upper", boll_up, _boll_note()),
        Indicator("boll_lb", "Bollinger lower", boll_low, _pct_from(close, boll_low)),
        Indicator("atr", "ATR (14)", atr14, _atr_note()),
        Indicator("vwma", "VWMA (20)", vwma20, _pct_from(close, vwma20)),
        Indicator("mfi", "Money Flow Index (14)", money_flow_index(frame), "volume-weighted RSI; >80 distribution, <20 accumulation"),
    ]
    return IndicatorSet(symbol=symbol, close=close, indicators=indicators, sessions=len(close_series))
