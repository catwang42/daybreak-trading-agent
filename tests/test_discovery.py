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


def test_percentile_component_states_its_own_sample_window():
    ma8 = pd.Series(np.linspace(0.3, 0.8, 400))
    component = B.component_percentile(ma8)
    assert "400 sessions" in component.signal
    assert "~1.6y" in component.signal
    assert "less than one full cycle" in component.signal


def test_breadth_history_note_scales_with_available_history():
    long_run = B.BreadthResult(75.0, "Healthy", "75-90%", "", history_sessions=500)
    assert "500 sessions" in long_run.history_note and "~2.0y" in long_run.history_note
    thin = B.BreadthResult(50.0, "Neutral", "60-75%", "", history_sessions=10)
    assert "too short" in thin.history_note


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


def test_sector_cap_keeps_the_best_three_and_lets_other_sectors_through():
    def c(symbol, sector, score):
        return S.Candidate(
            symbol=symbol, name=symbol, sector=sector, industry="x", price=10.0, score=score,
            rating="A", state="ACTIONABLE_DAY1", primary_trigger="4pct_breakout",
        )

    pool = [c(f"F{i}", "Financials", 90 - i) for i in range(5)] + [c("E1", "Energy", 60)]
    capped = S.cap_per_sector(pool, max_per_sector=3)
    assert [x.symbol for x in capped] == ["F0", "F1", "F2", "E1"]
    assert S.cap_per_sector(pool, max_per_sector=0) == pool


def test_deep_queue_round_robins_across_sectors_before_repeating_one():
    from tradingagent.discovery.shortlist import QuickTake, ShortlistEntry, deep_dive_queue

    def entry(symbol, sector, priority):
        candidate = S.Candidate(
            symbol=symbol, name=symbol, sector=sector, industry="x", price=10.0, score=80,
            rating="A", state="ACTIONABLE_DAY1", primary_trigger="4pct_breakout",
        )
        take = QuickTake(rating="Hold", confidence="M", thesis="t", key_risk="r",
                         deep_dive_priority=priority)
        return ShortlistEntry(candidate=candidate, take=take, earnings_flag="—", news_headline=None)

    # Financials sweeps the priority ranking; Energy leads on sector momentum.
    entries = [
        entry("F1", "Financials", 9), entry("F2", "Financials", 8),
        entry("F3", "Financials", 7), entry("E1", "Energy", 6), entry("T1", "Information Technology", 5),
    ]
    sector_map = SEC.SectorMap(rows=[
        SEC.SectorRow("Energy", "XLE", 0.8, 20, ret_5d=5.0),
        SEC.SectorRow("Financials", "XLF", 0.9, 70, ret_5d=1.0),
        SEC.SectorRow("Information Technology", "XLK", 0.6, 60, ret_5d=0.5),
    ])
    assert [e.symbol for e in deep_dive_queue(entries, sector_map, cap=3)] == ["E1", "F1", "T1"]
    # Beyond one name per sector it falls back to depth, still leader-first.
    assert [e.symbol for e in deep_dive_queue(entries, sector_map, cap=5)] == [
        "E1", "F1", "T1", "F2", "F3",
    ]


def test_deep_queue_skips_degraded_entries_and_survives_a_missing_sector_map():
    from tradingagent.discovery.shortlist import QuickTake, ShortlistEntry, deep_dive_queue

    def entry(symbol, sector, take):
        candidate = S.Candidate(
            symbol=symbol, name=symbol, sector=sector, industry="x", price=10.0, score=80,
            rating="A", state="ACTIONABLE_DAY1", primary_trigger="4pct_breakout",
        )
        return ShortlistEntry(candidate=candidate, take=take, earnings_flag="—", news_headline=None)

    good = QuickTake(rating="Buy", confidence="H", thesis="t", key_risk="r", deep_dive_priority=9)
    entries = [entry("BAD", "Energy", None), entry("OK", "Financials", good)]
    assert [e.symbol for e in deep_dive_queue(entries, None, cap=3)] == ["OK"]
    assert deep_dive_queue([entry("BAD", "Energy", None)], None) == []


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


# --- relative ranking and countable confidence (Gate 2 pre-work) ----------


def _pool_candidate(symbol, score, sector="Information Technology", **kw):
    fields = dict(
        symbol=symbol, name=symbol, sector=sector, industry="x", price=10.0, score=score,
        rating="A", state="ACTIONABLE_DAY1", primary_trigger="4pct_breakout",
    )
    fields.update(kw)
    return S.Candidate(**fields)


def test_pool_note_places_a_candidate_relative_to_the_day():
    from tradingagent.discovery.shortlist import PoolStats

    pool = [_pool_candidate(f"S{i}", 95 - i * 5) for i in range(9)]  # 95 down to 55
    stats = PoolStats.build(pool)
    assert (stats.size, stats.best, stats.worst) == (9, 95, 55)

    top = stats.note(pool[0], pool)
    assert "ranks 1 of 9" in top and "top third" in top
    assert "middle third" in stats.note(pool[4], pool)
    assert "bottom third" in stats.note(pool[8], pool)


def test_pool_note_counts_sector_crowding_and_survives_an_empty_pool():
    from tradingagent.discovery.shortlist import PoolStats

    pool = [_pool_candidate("A", 90), _pool_candidate("B", 80), _pool_candidate("C", 70, sector="Energy")]
    note = PoolStats.build(pool).note(pool[0], pool)
    assert "2 of today's candidates are in Information Technology" in note
    assert "is in Energy" in PoolStats.build(pool).note(pool[2], pool)  # singular
    assert PoolStats.build([]).note(pool[0], []) == "pool statistics unavailable"


def test_confirmation_checklist_is_countable_and_reaches_both_ends():
    from tradingagent.discovery.shortlist import CONFIRMATIONS, confirmation_checklist

    strong = _pool_candidate(
        "STRONG", 90, volume_ratio_20d=2.4, close_location_pct=93.0, above_50dma=True,
        above_200dma=True, rs_vs_spy_3mo=12.0, base_width_pct=8.0,
    )
    lines, held = confirmation_checklist(strong, earnings_flag="—")
    assert held == len(CONFIRMATIONS) == 6
    assert all(line.startswith("- [x]") for line in lines)

    # A confirmed earnings date alone knocks the count off the H band.
    _, with_earnings = confirmation_checklist(strong, earnings_flag="2026-08-20 AMC")
    assert with_earnings == 5

    weak = _pool_candidate("WEAK", 60)  # every screener metric at its default
    lines, held = confirmation_checklist(weak, earnings_flag="2026-08-20 AMC")
    assert held == 0
    assert all(line.startswith("- [ ]") for line in lines)


# --- shortlist selection while the signal layer is shadowed (M6 item 1) ---


class _OneSignalSource:
    """A source that shouts +1 at whichever symbol it is told to like."""

    name = "loud"
    scope = "ticker"
    describes = "test double"

    def __init__(self, likes: str):
        self.likes = likes

    def available(self):
        return True, ""

    def fetch(self, symbols, run_date):
        from tradingagent.signals.base import Signal, SourceResult

        return SourceResult(
            source=self.name,
            signals=[
                Signal(source=self.name, kind="k", direction=1, strength=1.0,
                       headline="h", as_of=run_date, symbol=self.likes)
            ],
        )


def _hub(likes, weights=None, caps=None):
    from tradingagent.signals.bundle import SignalHub

    return SignalHub(sources=[_OneSignalSource(likes)], weights=weights, caps=caps)


def test_an_ungraded_signal_cannot_promote_a_name_into_the_shortlist():
    """The M6 hotfix, at the level that mattered: four of ten shortlist names
    had entered this way, on sources with no resolved record at all."""
    from tradingagent.discovery.shortlist import select_with_signals

    pool = [_pool_candidate(s, score) for s, score in
            [("AAA", 90), ("BBB", 80), ("CCC", 70), ("DDD", 60)]]
    hub = _hub(likes="DDD")  # no weights, no caps: shadowed
    chosen = select_with_signals(pool, hub, date(2026, 8, 14), size=2)

    assert [c.symbol for c, _, _ in chosen] == ["AAA", "BBB"]
    assert all(b.score_adjustment() == 0.0 for _, b, _ in chosen)


def test_the_promotion_it_would_have_made_is_recorded_not_discarded():
    from tradingagent.discovery.shortlist import select_with_signals

    pool = [_pool_candidate(s, score) for s, score in
            [("AAA", 90), ("BBB", 80), ("CCC", 79), ("DDD", 78)]]
    hub = _hub(likes="DDD")
    select_with_signals(pool, hub, date(2026, 8, 14), size=2)

    shadow = hub.shadow
    assert shadow is not None
    assert shadow.chosen == ["AAA", "BBB"]
    assert shadow.would_promote == ["DDD"] and shadow.would_drop == ["BBB"]
    assert "SHADOW — would have changed: in DDD / out BBB." == shadow.note()
    assert shadow.adjustments["DDD"] == 8.0


def test_a_graduated_source_takes_its_influence_back():
    """The shadow is a cold start, not a permanent muzzle: the same code path
    resumes the moment the accuracy tracker says the source has earned it."""
    from tradingagent.discovery.shortlist import select_with_signals

    pool = [_pool_candidate(s, score) for s, score in
            [("AAA", 90), ("BBB", 80), ("CCC", 79), ("DDD", 78)]]
    hub = _hub(likes="DDD", weights={"loud": 1.0}, caps={"loud": 5.0})
    chosen = select_with_signals(pool, hub, date(2026, 8, 14), size=2)

    assert [c.symbol for c, _, _ in chosen] == ["AAA", "DDD"]
    assert hub.shadow.would_reorder is False


def test_no_signal_hub_at_all_is_still_plain_screener_order():
    from tradingagent.discovery.shortlist import select_with_signals

    pool = [_pool_candidate(s, score) for s, score in [("AAA", 90), ("BBB", 80)]]
    chosen = select_with_signals(pool, None, date(2026, 8, 14), size=1)
    assert [(c.symbol, b, rank) for c, b, rank in chosen] == [("AAA", None, 1)]
