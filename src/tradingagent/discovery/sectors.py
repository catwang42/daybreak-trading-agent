"""Sector rotation and opportunity map.

Ported from tradermonty/claude-trading-skills `sector-analyst`
(`scripts/analyze_sector_rotation.py`, MIT, commit 769a6c8): the cyclical /
defensive / commodity buckets, the risk-regime score, the overbought and
oversold thresholds on the uptrend ratio, and the four-phase cycle model with
leader/laggard matching.

Deliberate deviation: upstream reads TraderMonty's hosted `sector_summary.csv`.
We compute each sector's uptrend ratio ourselves as the fraction of that
sector's S&P 500 constituents trading above their 50-day MA, and pair it with
sector-ETF returns from yfinance. Free, self-contained, same downstream logic.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from ..data.market import Quote
from ..data.universe import (
    COMMODITY_SECTORS,
    CYCLICAL_SECTORS,
    DEFENSIVE_SECTORS,
    Constituent,
    normalize_sector,
)

# Overbought/oversold thresholds.
#
# Upstream uses 0.37 / 0.097 against TraderMonty's own "uptrend ratio", which
# is a much stricter membership test than ours and therefore lives on a lower
# scale. Our ratio is simply "% of sector members above their 50DMA", which
# sits in the 40-80% band on an ordinary day — applying 0.37 verbatim tagged
# nine of eleven sectors "Overbought" and made the column meaningless. These
# thresholds are the upstream bands rescaled to our metric.
OVERBOUGHT_THRESHOLD = 0.80
OVERSOLD_THRESHOLD = 0.20

CYCLE_PHASES: dict[str, dict[str, list[str]]] = {
    "Early Cycle Recovery": {
        "leaders": ["Information Technology", "Consumer Discretionary", "Industrials", "Financials"],
        "laggards": ["Utilities", "Consumer Staples", "Health Care"],
    },
    "Mid Cycle Expansion": {
        "leaders": ["Information Technology", "Industrials", "Consumer Discretionary", "Energy"],
        "laggards": ["Utilities", "Consumer Staples"],
    },
    "Late Cycle": {
        "leaders": ["Energy", "Materials", "Health Care"],
        "laggards": ["Information Technology", "Consumer Discretionary", "Industrials"],
    },
    "Recession": {
        "leaders": ["Utilities", "Consumer Staples", "Health Care"],
        "laggards": ["Information Technology", "Consumer Discretionary", "Industrials", "Financials"],
    },
}


@dataclass
class SectorRow:
    sector: str
    etf: str | None
    uptrend_ratio: float
    members: int
    ret_1d: float | None = None
    ret_5d: float | None = None
    ret_1mo: float | None = None
    ret_3mo: float | None = None
    bucket: str = "Other"
    status: str = "Neutral"
    preferred: bool = False

    @property
    def momentum(self) -> float:
        """Blended momentum used for ranking; missing windows contribute zero."""
        return 0.5 * (self.ret_5d or 0.0) + 0.3 * (self.ret_1mo or 0.0) + 0.2 * (self.ret_3mo or 0.0)


@dataclass
class SectorMap:
    rows: list[SectorRow] = field(default_factory=list)
    risk_regime: str = "Neutral"
    risk_score: float = 0.0
    cycle_phase: str = "Undetermined"
    cycle_confidence: str = "Low"
    overbought: list[str] = field(default_factory=list)
    oversold: list[str] = field(default_factory=list)

    def leaders(self, n: int = 3) -> list[SectorRow]:
        return sorted(self.rows, key=lambda r: r.momentum, reverse=True)[:n]

    def laggards(self, n: int = 3) -> list[SectorRow]:
        return sorted(self.rows, key=lambda r: r.momentum)[:n]


def bucket_for(sector: str) -> str:
    if sector in CYCLICAL_SECTORS:
        return "Cyclical"
    if sector in DEFENSIVE_SECTORS:
        return "Defensive"
    if sector in COMMODITY_SECTORS:
        return "Commodity"
    return "Other"


def uptrend_ratio(bars: dict[str, pd.DataFrame], symbols: list[str], window: int = 50) -> tuple[float, int]:
    """Fraction of ``symbols`` closing above their ``window``-day MA."""
    above = 0
    counted = 0
    for symbol in symbols:
        frame = bars.get(symbol)
        if frame is None:
            continue
        close = frame["Close"].dropna()
        if len(close) < window:
            continue
        counted += 1
        if float(close.iloc[-1]) > float(close.rolling(window).mean().iloc[-1]):
            above += 1
    return (above / counted if counted else 0.0), counted


def _status(ratio: float) -> str:
    if ratio >= OVERBOUGHT_THRESHOLD:
        return "Overbought"
    if ratio <= OVERSOLD_THRESHOLD:
        return "Oversold"
    return "Neutral"


def risk_regime(rows: list[SectorRow]) -> tuple[str, float]:
    """Cyclical-minus-defensive momentum spread (upstream's risk-regime score)."""
    cyc = [r.momentum for r in rows if r.bucket == "Cyclical"]
    dfn = [r.momentum for r in rows if r.bucket == "Defensive"]
    if not cyc or not dfn:
        return "Neutral", 0.0
    spread = sum(cyc) / len(cyc) - sum(dfn) / len(dfn)
    if spread >= 2.0:
        return "Risk-On", round(spread, 2)
    if spread <= -2.0:
        return "Risk-Off", round(spread, 2)
    return "Neutral", round(spread, 2)


def estimate_cycle_phase(rows: list[SectorRow]) -> tuple[str, str]:
    """Score each phase by how well observed leaders/laggards match it."""
    if len(rows) < 6:
        return "Undetermined", "Low"
    ranked = sorted(rows, key=lambda r: r.momentum, reverse=True)
    top = {r.sector for r in ranked[:4]}
    bottom = {r.sector for r in ranked[-4:]}

    scores: dict[str, int] = {}
    for phase, spec in CYCLE_PHASES.items():
        hits = len(top & set(spec["leaders"])) + len(bottom & set(spec["laggards"]))
        scores[phase] = hits
    best = max(scores, key=lambda k: scores[k])
    best_score = scores[best]
    runner_up = sorted(scores.values(), reverse=True)[1] if len(scores) > 1 else 0

    if best_score >= 5 and best_score - runner_up >= 2:
        confidence = "High"
    elif best_score >= 3:
        confidence = "Medium"
    else:
        confidence = "Low"
    return (best if best_score >= 2 else "Undetermined"), confidence


def build_sector_map(
    constituents: list[Constituent],
    bars: dict[str, pd.DataFrame],
    etf_quotes: list[Quote],
    preferred_sectors: list[str] | None = None,
) -> SectorMap:
    quotes = {q.label: q for q in etf_quotes}
    preferred = {normalize_sector(s) for s in (preferred_sectors or [])}

    members: dict[str, list[str]] = {}
    for c in constituents:
        members.setdefault(c.sector, []).append(c.symbol)

    rows: list[SectorRow] = []
    for sector, symbols in sorted(members.items()):
        ratio, counted = uptrend_ratio(bars, symbols)
        if counted == 0:
            continue
        quote = quotes.get(sector)
        rows.append(
            SectorRow(
                sector=sector,
                etf=quote.symbol if quote else None,
                uptrend_ratio=ratio,
                members=counted,
                ret_1d=quote.ret("1d") if quote else None,
                ret_5d=quote.ret("5d") if quote else None,
                ret_1mo=quote.ret("1mo") if quote else None,
                ret_3mo=quote.ret("3mo") if quote else None,
                bucket=bucket_for(sector),
                status=_status(ratio),
                preferred=sector in preferred,
            )
        )

    regime, score = risk_regime(rows)
    phase, confidence = estimate_cycle_phase(rows)
    return SectorMap(
        rows=sorted(rows, key=lambda r: r.momentum, reverse=True),
        risk_regime=regime,
        risk_score=score,
        cycle_phase=phase,
        cycle_confidence=confidence,
        overbought=[r.sector for r in rows if r.status == "Overbought"],
        oversold=[r.sector for r in rows if r.status == "Oversold"],
    )
