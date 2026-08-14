"""Tests for the ported discovery logic (breadth, sectors, screener, calendar)."""

from datetime import date

import numpy as np
import pandas as pd
import pytest

from tradingagent.data.universe import Constituent, load_snapshot, normalize_sector
from tradingagent.discovery import breadth as B
from tradingagent.discovery import screener as S
from tradingagent.discovery import sectors as SEC
from tradingagent.discovery.calendar import _business_day, _nth_weekday, static_release_calendar


def make_frame(closes, volumes=None, spread=0.01):
    n = len(closes)
    closes = np.asarray(closes, dtype=float)
    volumes = np.asarray(volumes if volumes is not None else [1_000_000] * n, dtype=float)
    return pd.DataFrame(
        {
            "Open": closes * (1 - spread / 2),
            "High": closes * (1 + spread),
            "Low": closes * (1 - spread),
            "Close": closes,
            "Volume": volumes,
        },
        index=pd.date_range("2024-01-01", periods=n, freq="B"),
    )


# --- breadth ------------------------------------------------------------


def test_composite_redistributes_weight_of_missing_components():
    """Two available components at 100 and 0 must not be diluted toward 50."""
    components = [
        B.Component("breadth_level_trend", 100, "", available=True),  # weight .25
        B.Component("ma_crossover", 0, "", available=True),  # weight .20
        B.Component("cycle_position", 50, "", available=False),
        B.Component("bearish_signal", 50, "", available=False),
        B.Component("historical_percentile", 50, "", available=False),
        B.Component("divergence", 50, "", available=False),
    ]
    score, quality = B.composite(components)
    assert score == pytest.approx(100 * (0.25 / 0.45), abs=0.1)
    assert quality.startswith("Limited (2/6)")


def test_composite_all_missing_returns_neutral_reference():
    components = [B.Component(k, 50, "", available=False) for k in B.COMPONENT_WEIGHTS]
    score, quality = B.composite(components)
    assert score == 50.0
    assert "reference value only" in quality


def test_zone_boundaries_match_upstream_table():
    assert B.interpret_zone(80)[0] == "Strong"
    assert B.interpret_zone(79.9)[0] == "Healthy"
    assert B.interpret_zone(60)[0] == "Healthy"
    assert B.interpret_zone(59.9)[0] == "Neutral"
    assert B.interpret_zone(39.9)[0] == "Weakening"
    assert B.interpret_zone(19.9)[0] == "Critical"


@pytest.mark.parametrize(
    ("ma8", "expected"),
    [(0.75, 95), (0.65, 80), (0.55, 65), (0.45, 50), (0.35, 35), (0.25, 20), (0.10, 5)],
)
def test_8ma_level_bands_match_upstream(ma8, expected):
    assert B._score_8ma_level(ma8) == expected


def test_breadth_series_needs_half_the_universe_reporting():
    long_up = make_frame(np.linspace(100, 200, 300))
    short = make_frame(np.linspace(100, 110, 30))
    series = B.breadth_series({"A": long_up, "B": long_up, "C": short}, window=50)
    assert not series.empty
    assert series.iloc[-1] == pytest.approx(1.0)


def test_analyze_breadth_degrades_when_no_history():
    result = B.analyze_breadth({"A": make_frame(np.linspace(100, 101, 10))})
    assert result.composite == 50.0
    assert result.zone == "Neutral"
    assert all(not c.available for c in result.components)


def test_rising_broad_market_scores_healthy():
    rng = np.random.default_rng(0)
    bars = {
        f"T{i}": make_frame(np.linspace(100, 180, 400) + rng.normal(0, 1, 400)) for i in range(20)
    }
    result = B.analyze_breadth(bars, spx_close=bars["T0"]["Close"])
    assert result.composite >= 60
    assert result.zone in {"Healthy", "Strong"}
    assert result.breadth_pct_above_50dma > 80


# --- sectors ------------------------------------------------------------


def test_bucket_classification_covers_all_gics_sectors():
    sectors = {c.sector for c in load_snapshot()}
    assert sectors, "snapshot must not be empty"
    assert all(SEC.bucket_for(s) != "Other" for s in sectors), sectors


def test_normalize_sector_maps_human_spellings():
    assert normalize_sector("Technology") == "Information Technology"
    assert normalize_sector("financials") == "Financials"
    assert normalize_sector("Consumer Cyclical") == "Consumer Discretionary"


def test_uptrend_ratio_counts_only_symbols_with_enough_history():
    up = make_frame(np.linspace(100, 200, 120))
    down = make_frame(np.linspace(200, 100, 120))
    tooshort = make_frame(np.linspace(100, 110, 10))
    ratio, counted = SEC.uptrend_ratio({"A": up, "B": down, "C": tooshort}, ["A", "B", "C"])
    assert counted == 2
    assert ratio == pytest.approx(0.5)


def test_status_thresholds_are_calibrated_to_our_metric():
    assert SEC._status(0.85) == "Overbought"
    assert SEC._status(0.50) == "Neutral"
    assert SEC._status(0.15) == "Oversold"
    # A typical everyday reading must NOT be flagged.
    assert SEC._status(0.55) == "Neutral"


def test_risk_regime_reads_cyclical_minus_defensive():
    rows = [
        SEC.SectorRow("Information Technology", "XLK", 0.7, 60, ret_5d=10, ret_1mo=10, ret_3mo=10, bucket="Cyclical"),
        SEC.SectorRow("Utilities", "XLU", 0.3, 30, ret_5d=0, ret_1mo=0, ret_3mo=0, bucket="Defensive"),
    ]
    regime, score = SEC.risk_regime(rows)
    assert regime == "Risk-On" and score == pytest.approx(10.0)

    for row in rows:
        row.ret_5d, row.ret_1mo, row.ret_3mo = -row.ret_5d, -row.ret_1mo, -row.ret_3mo
    assert SEC.risk_regime(rows)[0] == "Risk-Off"


def test_risk_regime_neutral_without_both_buckets():
    rows = [SEC.SectorRow("Energy", "XLE", 0.5, 20, ret_5d=5, bucket="Commodity")]
    assert SEC.risk_regime(rows) == ("Neutral", 0.0)


def test_cycle_phase_matches_late_cycle_leadership():
    def row(sector, momentum):
        return SEC.SectorRow(sector, None, 0.5, 10, ret_5d=momentum * 2, bucket=SEC.bucket_for(sector))

    rows = [
        row("Energy", 10), row("Materials", 9), row("Health Care", 8), row("Utilities", 7),
        row("Consumer Staples", 3), row("Financials", 1),
        row("Information Technology", -5), row("Consumer Discretionary", -6), row("Industrials", -7),
    ]
    phase, confidence = SEC.estimate_cycle_phase(rows)
    assert phase == "Late Cycle"
    assert confidence in {"Medium", "High"}


def test_cycle_phase_undetermined_with_too_few_sectors():
    rows = [SEC.SectorRow("Energy", None, 0.5, 5, ret_5d=1, bucket="Commodity")]
    assert SEC.estimate_cycle_phase(rows) == ("Undetermined", "Low")


# --- screener -----------------------------------------------------------

META = Constituent("TST", "Test Co", "Information Technology", "Software")


def burst_frame():
    """A quiet base, then a 6% breakout day on 3x volume closing at the high."""
    base = list(np.full(70, 100.0) + np.random.default_rng(1).normal(0, 0.3, 70))
    frame = make_frame(base + [106.0], volumes=[1_500_000] * 70 + [4_500_000], spread=0.005)
    frame.iloc[-1, frame.columns.get_loc("Open")] = 100.5
    frame.iloc[-1, frame.columns.get_loc("Low")] = 100.2
    frame.iloc[-1, frame.columns.get_loc("High")] = 106.2
    frame.iloc[-1, frame.columns.get_loc("Close")] = 106.0
    return frame


def test_momentum_burst_is_detected_and_rated():
    c = S.screen_symbol(burst_frame(), META, gate="allowed")
    assert c is not None
    assert "4pct_breakout" in c.triggers
    assert c.primary_trigger == "4pct_breakout"
    assert c.score >= 70
    assert c.rating in {"A", "A-", "B"}
    assert c.state == "ACTIONABLE_DAY1" or c.state == "MANUAL_REVIEW"


def test_quiet_stock_matches_no_trigger():
    quiet = make_frame(np.full(80, 100.0), volumes=[1_000_000] * 80, spread=0.001)
    assert S.screen_symbol(quiet, META, gate="allowed") is None


def test_penny_stock_is_hard_rejected():
    cheap = burst_frame() * 0.01
    cheap["Volume"] = 4_500_000
    assert S.screen_symbol(cheap, META, gate="allowed") is None


def test_wide_risk_to_stop_is_hard_rejected():
    frame = burst_frame()
    frame.iloc[-1, frame.columns.get_loc("Low")] = 80.0  # ~25% risk, above the 12% cap
    assert S.screen_symbol(frame, META, gate="allowed") is None


def test_restrictive_gate_downgrades_state_but_not_rating():
    allowed = S.screen_symbol(burst_frame(), META, gate="allowed")
    restricted = S.screen_symbol(burst_frame(), META, gate="restrictive")
    assert restricted.score == allowed.score - 5  # market-gate component
    assert restricted.state == "MANUAL_REVIEW"


@pytest.mark.parametrize(
    ("composite", "gate"), [(85, "allowed"), (60, "allowed"), (59, "neutral"), (40, "neutral"), (39, "restrictive")]
)
def test_market_gate_derives_from_breadth(composite, gate):
    assert S.market_gate_from_breadth(composite) == gate


def test_component_budget_never_exceeds_one_hundred():
    c = S.screen_symbol(burst_frame(), META, gate="allowed")
    assert sum(c.components.values()) == c.score
    assert c.score <= 100
    assert set(c.components) == {
        "trigger", "volume", "setup", "close", "risk", "failure_filters", "market_gate",
    }


def test_liquidity_floor_drops_thin_names():
    thin = burst_frame()
    # 150k avg shares: above the 100k hard-rejection floor, below the 1M preference.
    thin["Volume"] = thin["Volume"] / 10
    bars = {"TST": thin}
    assert S.screen_universe(bars, [META], gate="allowed", min_avg_share_volume=1_000_000) == []
    kept = S.screen_universe(bars, [META], gate="allowed", min_avg_share_volume=1_000)
    assert [c.symbol for c in kept] == ["TST"]


def test_preferred_sector_sorts_ahead_of_a_higher_score():
    tech = Constituent("AAA", "A", "Information Technology", "Software")
    energy = Constituent("BBB", "B", "Energy", "Oil")
    bars = {"AAA": burst_frame(), "BBB": burst_frame()}
    ranked = S.screen_universe(
        bars, [tech, energy], gate="allowed",
        preferred_sectors={"Information Technology"}, min_avg_share_volume=1_000,
    )
    assert ranked[0].symbol == "AAA" and ranked[0].preferred_sector


def test_up_streak_and_breakdown_detection():
    rising = pd.Series([100, 101, 102, 103, 104, 110.0])
    assert S.up_streak_before_trigger(rising) >= 3
    dropped = pd.Series([100, 95.0, 96, 97, 98, 105.0])
    assert S.recent_breakdown(dropped) is True
    assert S.recent_breakdown(pd.Series([100, 100.5, 101, 101.5, 102, 105.0])) is False


def test_paid_gaps_are_declared():
    gaps = S.paid_gaps()
    assert len(gaps) >= 3
    assert any("FMP" in g for g in gaps)
    assert any("FINVIZ" in g or "Finviz" in g for g in gaps)


# --- calendar -----------------------------------------------------------


def test_nth_weekday_finds_first_friday():
    assert _nth_weekday(2026, 8, 4, 1) == date(2026, 8, 7)
    assert _nth_weekday(2026, 5, 4, 1) == date(2026, 5, 1)


def test_business_day_skips_weekends():
    assert _business_day(2026, 8, 1) == date(2026, 8, 3)  # Aug 1 2026 is a Saturday
    assert _business_day(2026, 8, 3) == date(2026, 8, 5)


def test_static_calendar_covers_the_window_and_flags_impact():
    events = static_release_calendar(date(2026, 8, 1), date(2026, 8, 31))
    assert events
    assert all(date(2026, 8, 1) <= e.date <= date(2026, 8, 31) for e in events)
    assert any(e.name.startswith("Employment Situation") and e.impact == "High" for e in events)
    assert sum(1 for e in events if e.name == "Initial Jobless Claims") == 4
    assert events == sorted(events, key=lambda e: (e.date, e.name))


def test_static_calendar_spans_a_month_boundary():
    events = static_release_calendar(date(2026, 12, 20), date(2027, 1, 10))
    assert any(e.date.year == 2027 for e in events)
