"""Candidate screener — Stockbee-style momentum burst on free OHLCV.

Ported from tradermonty/claude-trading-skills
`stockbee-momentum-burst-screener/scripts/screen_momentum_burst.py` and
`references/scoring_system.md` (MIT, commit 769a6c8). Preserved verbatim: the
three trigger families (4% breakout / dollar breakout / range expansion), the
100-point component budget (trigger 20, volume 15, setup 25, close 10, risk 15,
failure filters 10, market gate 5), the hard-rejection rules, the soft failure
filters, and the A / A- / B / Watch / Reject rating bands.

Deliberate deviations:
- Upstream sources its universe and OHLCV from FMP (paid beyond 250 calls/day).
  We use the bundled S&P 500 universe and one bulk yfinance download. NO PAID
  SERVICE IS ADDED — see `paid_gaps()` for what that costs us.
- The market gate is driven by our own breadth composite rather than a CLI flag.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from ..data.universe import Constituent

# --- thresholds (upstream defaults) -------------------------------------
FOUR_PCT_THRESHOLD = 1.04
DOLLAR_THRESHOLD = 0.90
NINE_MILLION_VOLUME = 9_000_000
MIN_PRICE = 5.0
MIN_VOLUME = 100_000
MAX_RISK_PCT_TO_STOP = 12.0
MAX_BASE_WIDTH_PCT = 20.0
NARROW_PRIOR_DAY_RANGE_PCT = 3.0
MAX_PREV_DAY_GAIN_FOR_RANGE = 3.0
BREAKDOWN_LOOKBACK_DAYS = 5
BREAKDOWN_THRESHOLD_PCT = -4.0

RATING_BANDS: list[tuple[int, str, str]] = [
    (90, "A", "ACTIONABLE_DAY1"),
    (80, "A-", "ACTIONABLE_DAY1"),
    (70, "B", "MANUAL_REVIEW"),
    (55, "Watch", "WATCH_ONLY"),
]


@dataclass
class Candidate:
    symbol: str
    name: str
    sector: str
    industry: str
    price: float
    score: int
    rating: str
    state: str
    primary_trigger: str
    triggers: list[str] = field(default_factory=list)
    day_gain_pct: float = 0.0
    volume_ratio_20d: float = 0.0
    avg_dollar_volume: float = 0.0
    close_location_pct: float = 0.0
    prior_base_days: int = 0
    base_width_pct: float = 0.0
    avg_share_volume: float = 0.0
    entry_ref: float = 0.0
    stop_ref: float = 0.0
    risk_pct: float = 0.0
    dist_52w_high_pct: float | None = None
    above_50dma: bool = False
    above_200dma: bool = False
    rs_vs_spy_3mo: float | None = None
    reject_reasons: list[str] = field(default_factory=list)
    components: dict[str, int] = field(default_factory=dict)
    preferred_sector: bool = False

    @property
    def why(self) -> str:
        """One-line 'why it surfaced' for the shortlist table."""
        bits = [_TRIGGER_LABELS.get(t, t) for t in self.triggers if t in _TRIGGER_LABELS]
        trend = "above 50/200DMA" if self.above_50dma and self.above_200dma else (
            "above 50DMA" if self.above_50dma else "below 50DMA"
        )
        rs = f", RS {self.rs_vs_spy_3mo:+.0f}% vs SPY 3mo" if self.rs_vs_spy_3mo is not None else ""
        return f"{', '.join(bits) or 'trend continuation'}; {trend}{rs}"


_TRIGGER_LABELS = {
    "4pct_breakout": "4% breakout",
    "dollar_breakout": "$ breakout",
    "range_expansion": "range expansion",
    "9m_volume": "9M+ volume",
}


# --- primitives ---------------------------------------------------------


def close_location_pct(bar: pd.Series) -> float:
    day_range = float(bar["High"]) - float(bar["Low"])
    if day_range <= 0:
        return 0.0
    return ((float(bar["Close"]) - float(bar["Low"])) / day_range) * 100.0


def up_streak_before_trigger(closes: pd.Series, max_days: int = 5) -> int:
    streak = 0
    for i in range(2, min(len(closes), max_days + 2)):
        if float(closes.iloc[-i]) > float(closes.iloc[-i - 1]):
            streak += 1
        else:
            break
    return streak


def recent_breakdown(
    closes: pd.Series, lookback: int = BREAKDOWN_LOOKBACK_DAYS, threshold: float = BREAKDOWN_THRESHOLD_PCT
) -> bool:
    for i in range(2, min(len(closes), lookback + 2)):
        prev = float(closes.iloc[-i - 1])
        if prev <= 0:
            continue
        if (float(closes.iloc[-i]) / prev - 1) * 100 <= threshold:
            return True
    return False


def base_profile(frame: pd.DataFrame, base_window: int = 20) -> tuple[int, float, bool]:
    """Prior base length (days), base width %, and volume dry-up flag."""
    prior = frame.iloc[-(base_window + 1) : -1]
    if len(prior) < 5:
        return 0, 100.0, False
    high, low = float(prior["High"].max()), float(prior["Low"].min())
    width = ((high - low) / low) * 100 if low > 0 else 100.0

    # Count back the consecutive days that stayed inside a tight range.
    closes = prior["Close"].astype(float)
    ref = float(closes.iloc[-1])
    days = 0
    for value in reversed(closes.tolist()):
        if ref > 0 and abs(value / ref - 1) * 100 <= MAX_BASE_WIDTH_PCT / 2:
            days += 1
        else:
            break

    volumes = frame["Volume"].astype(float)
    dry_up = False
    if len(volumes) > 25:
        recent5 = float(volumes.iloc[-6:-1].mean())
        avg20 = float(volumes.iloc[-26:-1].mean())
        dry_up = avg20 > 0 and recent5 < 0.85 * avg20
    return days, width, dry_up


# --- scoring components (upstream point budgets) ------------------------


def trigger_score(triggers: list[str], day_gain_pct: float) -> int:
    score = 0
    if "4pct_breakout" in triggers:
        score += 14
        if day_gain_pct >= 7:
            score += 3
    if "range_expansion" in triggers:
        score += 10
    if "dollar_breakout" in triggers:
        score += 8
    if "9m_volume" in triggers:
        score += 2
    return min(20, score)


def volume_score(ratio_1d: float, ratio_20d: float) -> int:
    best = max(ratio_1d, ratio_20d)
    for threshold, points in ((3.0, 15), (2.0, 12), (1.5, 9), (1.0, 6)):
        if best >= threshold:
            return points
    return 0


def setup_score(base_days: int, base_width: float, prev_bar: pd.Series, dry_up: bool) -> int:
    score = 0
    for threshold, points in ((10, 10), (5, 8), (3, 6)):
        if base_days >= threshold:
            score += points
            break
    else:
        if base_days > 0:
            score += 3

    if base_width and base_width <= 8:
        score += 7
    elif base_width <= 12:
        score += 5
    elif base_width <= MAX_BASE_WIDTH_PCT:
        score += 3

    prev_close = float(prev_bar["Close"])
    prev_range_pct = (
        ((float(prev_bar["High"]) - float(prev_bar["Low"])) / prev_close) * 100 if prev_close > 0 else 0
    )
    if prev_range_pct <= NARROW_PRIOR_DAY_RANGE_PCT:
        score += 5
    elif prev_close < float(prev_bar["Open"]):
        score += 4

    if dry_up:
        score += 3
    return min(25, score)


def close_quality_score(location_pct: float) -> int:
    for threshold, points in ((90, 10), (80, 9), (70, 7), (60, 5), (50, 3)):
        if location_pct >= threshold:
            return points
    return 0


def risk_distance_score(risk_pct: float) -> int:
    if risk_pct <= 0:
        return 0
    for threshold, points in ((2.5, 15), (4.0, 12), (6.0, 8), (8.0, 5), (10.0, 2)):
        if risk_pct <= threshold:
            return points
    return 0


def failure_filter_score(
    base_width: float, close_location: float, prior_up_streak: int, breakdown: bool
) -> tuple[int, list[str]]:
    score = 10
    reasons: list[str] = []
    if prior_up_streak >= 3:
        score -= 4
        reasons.append("prior_3day_runup")
    if breakdown:
        score -= 4
        reasons.append("recent_4pct_breakdown")
    if base_width > MAX_BASE_WIDTH_PCT:
        score -= 3
        reasons.append("wide_prior_base")
    if close_location < 50:
        score -= 2
        reasons.append("weak_close_location")
    return max(0, score), reasons


def market_gate_from_breadth(composite: float) -> str:
    """Our breadth composite replaces upstream's manual --market-gate flag."""
    if composite >= 60:
        return "allowed"
    if composite >= 40:
        return "neutral"
    return "restrictive"


def market_gate_score(gate: str) -> int:
    return {"allowed": 5, "neutral": 3}.get(gate, 0)


def score_to_rating(score: int, gate: str) -> tuple[str, str]:
    for threshold, rating, state in RATING_BANDS:
        if score >= threshold:
            if gate == "restrictive" and score >= 70:
                return rating, "MANUAL_REVIEW"
            return rating, state
    return "Reject", "REJECTED"


# --- screening ----------------------------------------------------------


def screen_symbol(
    frame: pd.DataFrame,
    meta: Constituent,
    gate: str,
    spy_close: pd.Series | None = None,
    preferred_sectors: set[str] | None = None,
) -> Candidate | None:
    """Score one symbol. Returns None when a hard rejection rule fires."""
    if len(frame) < 60:
        return None
    latest, prev = frame.iloc[-1], frame.iloc[-2]
    close = float(latest["Close"])
    volume = float(latest["Volume"])
    prev_close = float(prev["Close"])

    # Hard rejection rules (upstream: applied before scoring).
    if close < MIN_PRICE or volume < MIN_VOLUME or prev_close <= 0:
        return None

    closes = frame["Close"].astype(float)
    volumes = frame["Volume"].astype(float)
    avg20_volume = float(volumes.iloc[-21:-1].mean())
    volume_expanded = volume > float(prev["Volume"])
    volume_floor_ok = volume >= MIN_VOLUME

    day_gain_pct = (close / prev_close - 1) * 100
    dollar_gain = close - float(latest["Open"])
    current_range = float(latest["High"]) - float(latest["Low"])
    prior_ranges = (frame["High"] - frame["Low"]).astype(float).iloc[-4:-1]
    prior_range_max = float(prior_ranges.max()) if len(prior_ranges) else 0.0
    prev_day_gain_pct = (
        (prev_close / float(frame.iloc[-3]["Close"]) - 1) * 100 if len(frame) >= 3 else 0.0
    )

    triggers: list[str] = []
    if close / prev_close >= FOUR_PCT_THRESHOLD and volume_expanded and volume_floor_ok:
        triggers.append("4pct_breakout")
    if dollar_gain >= DOLLAR_THRESHOLD and volume_floor_ok:
        triggers.append("dollar_breakout")
    if (
        current_range > prior_range_max
        and prev_day_gain_pct <= MAX_PREV_DAY_GAIN_FOR_RANGE
        and volume_expanded
        and volume_floor_ok
    ):
        triggers.append("range_expansion")
    if volume >= NINE_MILLION_VOLUME:
        triggers.append("9m_volume")

    if not triggers:
        return None  # hard rejection: no trigger family matched

    entry_ref = float(latest["High"])
    stop_ref = float(latest["Low"])
    risk_pct = ((entry_ref - stop_ref) / entry_ref) * 100 if entry_ref > 0 else 0.0
    if risk_pct > MAX_RISK_PCT_TO_STOP:
        return None  # hard rejection: risk to trigger-day low too wide

    base_days, base_width, dry_up = base_profile(frame)
    location = close_location_pct(latest)
    ratio_1d = volume / float(prev["Volume"]) if float(prev["Volume"]) > 0 else 0.0
    ratio_20d = volume / avg20_volume if avg20_volume > 0 else 0.0

    fail_points, reject_reasons = failure_filter_score(
        base_width, location, up_streak_before_trigger(closes), recent_breakdown(closes)
    )
    components = {
        "trigger": trigger_score(triggers, day_gain_pct),
        "volume": volume_score(ratio_1d, ratio_20d),
        "setup": setup_score(base_days, base_width, prev, dry_up),
        "close": close_quality_score(location),
        "risk": risk_distance_score(risk_pct),
        "failure_filters": fail_points,
        "market_gate": market_gate_score(gate),
    }
    total = int(sum(components.values()))
    rating, state = score_to_rating(total, gate)

    ma50 = float(closes.rolling(50).mean().iloc[-1]) if len(closes) >= 50 else None
    ma200 = float(closes.rolling(200).mean().iloc[-1]) if len(closes) >= 200 else None
    high_52w = float(frame["High"].tail(252).max())

    rs = None
    if spy_close is not None and len(spy_close.dropna()) > 63 and len(closes) > 63:
        spy = spy_close.dropna()
        stock_ret = (float(closes.iloc[-1]) / float(closes.iloc[-64]) - 1) * 100
        spy_ret = (float(spy.iloc[-1]) / float(spy.iloc[-64]) - 1) * 100
        rs = stock_ret - spy_ret

    primary = next(
        (t for t in ("4pct_breakout", "range_expansion", "dollar_breakout", "9m_volume") if t in triggers),
        "none",
    )
    return Candidate(
        symbol=meta.symbol,
        name=meta.name,
        sector=meta.sector,
        industry=meta.industry,
        price=close,
        score=total,
        rating=rating,
        state=state,
        primary_trigger=primary,
        triggers=triggers,
        day_gain_pct=day_gain_pct,
        volume_ratio_20d=ratio_20d,
        avg_dollar_volume=avg20_volume * close,
        close_location_pct=location,
        prior_base_days=base_days,
        base_width_pct=base_width,
        avg_share_volume=avg20_volume,
        entry_ref=entry_ref,
        stop_ref=stop_ref,
        risk_pct=risk_pct,
        dist_52w_high_pct=((close / high_52w - 1) * 100) if high_52w > 0 else None,
        above_50dma=bool(ma50 and close > ma50),
        above_200dma=bool(ma200 and close > ma200),
        rs_vs_spy_3mo=rs,
        reject_reasons=reject_reasons,
        components=components,
        preferred_sector=meta.sector in (preferred_sectors or set()),
    )


def screen_universe(
    bars: dict[str, pd.DataFrame],
    constituents: list[Constituent],
    gate: str,
    spy_close: pd.Series | None = None,
    preferred_sectors: set[str] | None = None,
    min_avg_share_volume: float = 1_000_000.0,
) -> list[Candidate]:
    """Screen the whole universe and return candidates best-first.

    ``min_avg_share_volume`` is the liquidity floor from ``preferences.md``
    ("avg daily volume > 1M shares"), measured on the trailing 20-day average.
    """
    meta_by_symbol = {c.symbol: c for c in constituents}
    out: list[Candidate] = []
    for symbol, frame in bars.items():
        meta = meta_by_symbol.get(symbol)
        if meta is None:
            continue
        candidate = screen_symbol(frame, meta, gate, spy_close, preferred_sectors)
        if candidate is None or candidate.state == "REJECTED":
            continue
        if candidate.avg_share_volume < min_avg_share_volume:
            continue
        out.append(candidate)
    out.sort(key=lambda c: (c.preferred_sector, c.score), reverse=True)
    return out


def paid_gaps() -> list[str]:
    """Capabilities the cookbooks provide only behind a paid API.

    Surfaced in the report so the human can decide; never auto-purchased.
    """
    return [
        "Universe is the S&P 500 snapshot only. Upstream screeners (Stockbee, VCP, "
        "CANSLIM) scan the full US market via FMP ($22-29/mo) — small/mid-cap "
        "momentum bursts outside the S&P 500 are invisible to us.",
        "Fundamental CANSLIM components (C/A quarterly earnings acceleration, "
        "I institutional sponsorship) need FMP or Finviz Elite (~$40/mo); the "
        "screener is technical-only for now.",
        "Economic calendar is FMP-gated upstream and premium on Finnhub's free "
        "tier; we substitute a static recurring-release calendar.",
        "Theme detection at industry granularity needs FINVIZ Elite; we "
        "approximate with GICS sector aggregates.",
    ]
