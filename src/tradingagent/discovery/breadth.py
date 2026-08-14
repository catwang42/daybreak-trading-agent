"""Market breadth composite score (0-100, 100 = healthy).

Ported from tradermonty/claude-trading-skills `market-breadth-analyzer`
(MIT, commit 769a6c8): the 6-component weighted composite, the weight
redistribution rule when a component lacks data, the 8MA level bands, the
direction modifier, and the health-zone/exposure table.

Deliberate deviation: upstream reads TraderMonty's hosted breadth CSV. We
compute the breadth index ourselves from the universe — the fraction of
constituents trading above their 50-day moving average — so the daily run has
no dependency on a third party's data pipeline. Everything downstream of that
series (8MA, 200MA, trend, percentile, divergence) is the upstream logic.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

COMPONENT_WEIGHTS: dict[str, float] = {
    "breadth_level_trend": 0.25,
    "ma_crossover": 0.20,
    "cycle_position": 0.20,
    "bearish_signal": 0.15,
    "historical_percentile": 0.10,
    "divergence": 0.10,
}

COMPONENT_LABELS: dict[str, str] = {
    "breadth_level_trend": "Breadth Level & Trend",
    "ma_crossover": "8MA vs 200MA Crossover",
    "cycle_position": "Peak/Trough Cycle Position",
    "bearish_signal": "Bearish Signal Status",
    "historical_percentile": "Historical Percentile",
    "divergence": "S&P 500 Divergence",
}

ZONES: list[tuple[float, str, str, str]] = [
    (80, "Strong", "90-100%", "Full position; growth/momentum favoured."),
    (60, "Healthy", "75-90%", "Normal operations."),
    (40, "Neutral", "60-75%", "Selective positioning; tighten stops."),
    (20, "Weakening", "40-60%", "Profit-taking; raise cash."),
    (0, "Critical", "25-40%", "Capital preservation; watch for a trough."),
]


@dataclass
class Component:
    key: str
    score: float
    signal: str
    available: bool = True


@dataclass
class BreadthResult:
    composite: float
    zone: str
    exposure: str
    guidance: str
    components: list[Component] = field(default_factory=list)
    breadth_pct_above_50dma: float | None = None
    breadth_pct_above_200dma: float | None = None
    ma8: float | None = None
    ma200: float | None = None
    universe_size: int = 0
    data_quality: str = ""
    history_sessions: int = 0

    @property
    def history_note(self) -> str:
        """Inline caveat: the percentile and cycle components are window-bound."""
        if self.history_sessions < 120:
            return (
                "Breadth history is too short to rank today's reading against a "
                "meaningful sample; treat the composite as indicative only."
            )
        return (
            f"Percentile and cycle-position components are ranked against the trailing "
            f"{self.history_sessions} sessions ({_years(self.history_sessions)}) of breadth "
            "history we compute ourselves — less than one full market cycle, so an "
            "\"84th percentile\" reading means 84th of this window, not of all time."
        )

    @property
    def strongest(self) -> Component | None:
        avail = [c for c in self.components if c.available]
        return max(avail, key=lambda c: c.score) if avail else None

    @property
    def weakest(self) -> Component | None:
        avail = [c for c in self.components if c.available]
        return min(avail, key=lambda c: c.score) if avail else None


def breadth_series(bars: dict[str, pd.DataFrame], window: int = 50) -> pd.Series:
    """Fraction of the universe closing above its ``window``-day MA, per day."""
    flags: dict[str, pd.Series] = {}
    for symbol, frame in bars.items():
        close = frame["Close"].dropna()
        if len(close) < window + 5:
            continue
        flags[symbol] = (close > close.rolling(window).mean()).astype(float)
    if not flags:
        return pd.Series(dtype=float)
    matrix = pd.DataFrame(flags)
    # Require at least half the universe reporting on a given day.
    counts = matrix.notna().sum(axis=1)
    ratio = matrix.mean(axis=1).where(counts >= max(1, int(0.5 * matrix.shape[1])))
    return ratio.dropna()


# --- components ---------------------------------------------------------


def _score_8ma_level(ma8: float) -> float:
    for threshold, score in ((0.70, 95), (0.60, 80), (0.50, 65), (0.40, 50), (0.30, 35), (0.20, 20)):
        if ma8 >= threshold:
            return score
    return 5


def component_level_trend(ma8: pd.Series, ma200: pd.Series) -> Component:
    if ma8.empty or ma200.dropna().empty:
        return Component("breadth_level_trend", 50, "NO DATA", available=False)
    current = float(ma8.iloc[-1])
    trend_up = len(ma200.dropna()) > 21 and float(ma200.iloc[-1]) > float(ma200.iloc[-22])
    level_score = _score_8ma_level(current)
    trend_score = 80 if trend_up else 20

    modifier = 0
    direction = "flat"
    if len(ma8) >= 6:
        prior = float(ma8.iloc[-6])
        if current > prior:
            direction = "rising"
            if current < 0.60:
                modifier = 5  # early recovery bonus
        elif current < prior:
            direction = "falling"
            if current > 0.60:
                modifier = -10  # deceleration from a high level
            elif current < 0.40:
                modifier = 5  # limited downside near the bottom

    score = max(0.0, min(100.0, round(0.70 * level_score + 0.30 * trend_score) + modifier))
    return Component(
        "breadth_level_trend",
        score,
        f"{current:.0%} of universe above 50DMA (8MA), {direction}; "
        f"long-term trend {'up' if trend_up else 'down'}",
    )


def component_ma_crossover(ma8: pd.Series, ma200: pd.Series) -> Component:
    valid = ma200.dropna()
    if ma8.empty or valid.empty:
        return Component("ma_crossover", 50, "NO DATA", available=False)
    gap = float(ma8.iloc[-1]) - float(valid.iloc[-1])
    if gap >= 0.15:
        score, label = 90, "8MA far above 200MA"
    elif gap >= 0.05:
        score, label = 75, "8MA above 200MA"
    elif gap >= 0.0:
        score, label = 60, "8MA marginally above 200MA"
    elif gap >= -0.05:
        score, label = 40, "8MA marginally below 200MA"
    elif gap >= -0.15:
        score, label = 25, "8MA below 200MA"
    else:
        score, label = 10, "8MA far below 200MA"
    return Component("ma_crossover", score, f"{label} (gap {gap:+.2f})")


def component_cycle(ma8: pd.Series, lookback: int = 252) -> Component:
    """Where the 8MA sits between its trailing trough and peak."""
    window = ma8.tail(lookback)
    if len(window) < 60:
        return Component("cycle_position", 50, "NO DATA", available=False)
    low, high = float(window.min()), float(window.max())
    if high - low < 1e-6:
        return Component("cycle_position", 50, "flat cycle", available=False)
    position = (float(window.iloc[-1]) - low) / (high - low)
    # Mid-cycle rising is healthiest; extremes at either end score lower.
    if position >= 0.90:
        score, label = 45, "near cycle peak (extended)"
    elif position >= 0.65:
        score, label = 80, "upper half of cycle"
    elif position >= 0.35:
        score, label = 65, "mid cycle"
    elif position >= 0.10:
        score, label = 40, "lower half of cycle"
    else:
        score, label = 25, "near cycle trough"
    return Component("cycle_position", score, f"{label} ({position:.0%} of 1y range)")


def component_bearish_signal(ratio: pd.Series, ma8: pd.Series, ma200: pd.Series) -> Component:
    """Upstream's backtested bearish flag, reimplemented on our own series.

    Flags a fresh 8MA/200MA downside cross or a collapse below 30% participation.
    """
    valid = ma200.dropna()
    if len(ma8) < 25 or valid.empty:
        return Component("bearish_signal", 50, "NO DATA", available=False)
    current, prior = float(ma8.iloc[-1]), float(ma8.iloc[-21])
    long_term = float(valid.iloc[-1])
    latest = float(ratio.iloc[-1])

    fresh_cross = current < long_term <= prior
    collapsed = latest < 0.30
    if fresh_cross and collapsed:
        return Component("bearish_signal", 5, "bearish cross AND participation below 30%")
    if fresh_cross:
        return Component("bearish_signal", 25, "fresh 8MA cross below 200MA")
    if collapsed:
        return Component("bearish_signal", 30, "participation below 30%")
    if current > long_term and latest > 0.50:
        return Component("bearish_signal", 90, "no bearish signal active")
    return Component("bearish_signal", 65, "no bearish signal, participation mixed")


def component_percentile(ma8: pd.Series) -> Component:
    """Percentile rank of today's 8MA within the history we actually hold.

    Upstream ranks against years of hosted breadth history. Ours is bounded by
    the OHLCV window we download (2y), which is less than one full market cycle
    — so the signal states its own sample size rather than implying "all time".
    """
    if len(ma8) < 120:
        return Component("historical_percentile", 50, "NO DATA", available=False)
    pct = float((ma8 < float(ma8.iloc[-1])).mean()) * 100
    return Component(
        "historical_percentile",
        pct,
        f"{pct:.0f}th percentile of the trailing {len(ma8)} sessions "
        f"({_years(len(ma8))} of history — less than one full cycle)",
    )


def _years(sessions: int) -> str:
    return f"~{sessions / 252:.1f}y"


def component_divergence(ma8: pd.Series, spx_close: pd.Series | None) -> Component:
    """Multi-window (20d + 60d) price-vs-breadth divergence, as upstream."""
    if spx_close is None or len(ma8) < 65 or len(spx_close.dropna()) < 65:
        return Component("divergence", 50, "NO DATA", available=False)
    price = spx_close.dropna()
    notes: list[str] = []
    penalty = 0
    for window in (20, 60):
        price_up = float(price.iloc[-1]) > float(price.iloc[-1 - window])
        breadth_delta = float(ma8.iloc[-1]) - float(ma8.iloc[-1 - window])
        if price_up and breadth_delta < -0.05:
            penalty += 25
            notes.append(f"{window}d: price up, breadth down {breadth_delta:+.2f}")
        elif not price_up and breadth_delta > 0.05:
            notes.append(f"{window}d: price down, breadth improving {breadth_delta:+.2f}")
        else:
            notes.append(f"{window}d: confirming ({breadth_delta:+.2f})")
    return Component("divergence", max(0, 85 - penalty), "; ".join(notes))


# --- composite ----------------------------------------------------------


def composite(components: list[Component]) -> tuple[float, str]:
    """Weighted composite with proportional redistribution of missing weights."""
    total_available = sum(
        COMPONENT_WEIGHTS[c.key] for c in components if c.available and c.key in COMPONENT_WEIGHTS
    )
    available_count = sum(1 for c in components if c.available)
    total = len(COMPONENT_WEIGHTS)

    if total_available <= 0:
        return 50.0, f"Limited (0/{total} components) - reference value only"

    score = sum(
        c.score * (COMPONENT_WEIGHTS[c.key] / total_available)
        for c in components
        if c.available and c.key in COMPONENT_WEIGHTS
    )
    if available_count == total:
        quality = f"Complete ({available_count}/{total} components)"
    elif available_count >= total - 2:
        quality = f"Partial ({available_count}/{total}) - interpret with caution"
    else:
        quality = f"Limited ({available_count}/{total}) - low confidence"
    return round(score, 1), quality


def interpret_zone(score: float) -> tuple[str, str, str]:
    for threshold, zone, exposure, guidance in ZONES:
        if score >= threshold:
            return zone, exposure, guidance
    return ZONES[-1][1], ZONES[-1][2], ZONES[-1][3]


def analyze_breadth(bars: dict[str, pd.DataFrame], spx_close: pd.Series | None = None) -> BreadthResult:
    """Full breadth assessment from validated universe OHLCV."""
    ratio = breadth_series(bars, window=50)
    ratio_200 = breadth_series(bars, window=200)
    if ratio.empty:
        return BreadthResult(
            composite=50.0,
            zone="Neutral",
            exposure="60-75%",
            guidance="Breadth data unavailable; score is a reference value only.",
            components=[Component(k, 50, "NO DATA", available=False) for k in COMPONENT_WEIGHTS],
            universe_size=len(bars),
            data_quality=f"Limited (0/{len(COMPONENT_WEIGHTS)} components) - low confidence",
        )

    ma8 = ratio.rolling(8).mean().dropna()
    ma200 = ratio.rolling(200).mean()

    components = [
        component_level_trend(ma8, ma200),
        component_ma_crossover(ma8, ma200),
        component_cycle(ma8),
        component_bearish_signal(ratio, ma8, ma200),
        component_percentile(ma8),
        component_divergence(ma8, spx_close),
    ]
    score, quality = composite(components)
    zone, exposure, guidance = interpret_zone(score)
    valid_200 = ma200.dropna()
    return BreadthResult(
        composite=score,
        zone=zone,
        exposure=exposure,
        guidance=guidance,
        components=components,
        breadth_pct_above_50dma=float(ratio.iloc[-1]) * 100,
        breadth_pct_above_200dma=float(ratio_200.iloc[-1]) * 100 if not ratio_200.empty else None,
        ma8=float(ma8.iloc[-1]) if not ma8.empty else None,
        ma200=float(valid_200.iloc[-1]) if not valid_200.empty else None,
        universe_size=len(bars),
        data_quality=quality,
        history_sessions=len(ma8),
    )
