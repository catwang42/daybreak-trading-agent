"""Outcome resolution — the arithmetic that decides whether a call was right.

Every test here defends a decision that changes the numbers:

* the reference close is the first session *at or after* the decision date, so
  the research is never credited with a move that had already happened;
* horizons are trading days counted in the bar index, so a holiday week does
  not silently shorten a window;
* excess is computed against the benchmark's move *between the same sessions*,
  not the same row offsets;
* a horizon that has not matured is absent, never zero;
* MFE/MAE are signed by direction, so a profitable short is not filed as a loss.
"""

from datetime import date

import pandas as pd
import pytest

from tradingagent.evaluation import outcomes as O
from tradingagent.snapshot import ResearchSnapshot

DECISION_DATE = date(2026, 6, 1)


def frame(closes, *, start="2026-06-01", highs=None, lows=None, sessions=None):
    """Daily bars on business days unless explicit sessions are given."""
    index = (
        pd.DatetimeIndex([pd.Timestamp(s) for s in sessions])
        if sessions
        else pd.bdate_range(start=start, periods=len(closes))
    )
    return pd.DataFrame(
        {
            "Open": closes,
            "High": highs if highs is not None else [c * 1.01 for c in closes],
            "Low": lows if lows is not None else [c * 0.99 for c in closes],
            "Close": closes,
            "Volume": [1_000_000] * len(closes),
        },
        index=index,
    )


def snapshot_for(bars, run_date=DECISION_DATE, name="outcomes"):
    return ResearchSnapshot.from_bars(bars, run_date, name=name)


def decision(**overrides):
    base = {
        "decision_id": "2026-06-01:AAA:deep",
        "ticker": "AAA",
        "date": DECISION_DATE.isoformat(),
        "stage": "deep",
        "rating": "Buy",
        "sector": "Information Technology",
    }
    base.update(overrides)
    return base


def test_the_reference_close_is_the_first_session_the_reader_could_have_traded():
    # The brief lands pre-market on the 1st. Measuring from the 29th's close —
    # the snapshot the analysis read — would hand the research a move nobody
    # could have acted on.
    bars = frame([100.0] * 10, sessions=[
        "2026-05-28", "2026-05-29", "2026-06-01", "2026-06-02", "2026-06-03",
        "2026-06-04", "2026-06-05", "2026-06-08", "2026-06-09", "2026-06-10",
    ])
    assert O.reference_index(bars, DECISION_DATE) == 2


def test_a_decision_date_that_fell_on_a_weekend_resolves_to_the_next_session():
    bars = frame([100.0] * 8, start="2026-06-01")  # Mon 1st through Wed 10th
    saturday = date(2026, 6, 6)
    assert O.reference_index(bars, saturday) == 5  # Monday the 8th


def test_a_decision_after_every_bar_we_have_resolves_to_nothing():
    bars = frame([100.0] * 5, start="2026-06-01")
    assert O.reference_index(bars, date(2027, 1, 4)) is None


def test_horizons_count_trading_days_not_calendar_days():
    # Ten sessions spanning a fortnight: the 5-day horizon must land on the
    # sixth bar, not on the calendar date five days out.
    closes = [100.0, 101, 102, 103, 104, 105, 106, 107, 108, 109]
    bars = frame(closes, start="2026-06-01")
    snapshot = snapshot_for({"AAA": bars})
    resolution = O.resolve(decision(), bars, snapshot)
    assert resolution.record is not None
    assert resolution.record.horizons["5"]["session"] == "2026-06-08"
    assert resolution.record.horizons["5"]["return_pct"] == pytest.approx(5.0)


def test_a_horizon_that_has_not_matured_is_absent_not_zero():
    bars = frame([100.0] * 8, start="2026-06-01")
    snapshot = snapshot_for({"AAA": bars})
    record = O.resolve(decision(), bars, snapshot).record
    assert record is not None
    # 1 and 5 exist inside eight sessions; 10, 20 and 60 do not.
    assert set(record.horizons) == {"1", "5"}
    assert record.matured == [1, 5]
    assert not O.is_complete(record.to_dict())


def test_a_session_after_the_snapshot_is_never_read():
    # The bars run to the 30th; the snapshot only knows about the 8th. A
    # horizon ending after the snapshot's market date is dropped rather than
    # resolved from data the job is not entitled to see.
    bars = frame([100.0 + i for i in range(20)], start="2026-06-01")
    early = ResearchSnapshot.from_bars(
        {"AAA": bars.iloc[:6]}, DECISION_DATE, name="outcomes"
    )
    record = O.resolve(decision(), bars, early).record
    assert record is not None
    assert set(record.horizons) == {"1", "5"}
    assert record.as_of == "2026-06-08"


def test_beating_the_tape_and_beating_the_market_are_different_questions():
    # +3% in a week SPY rose 4% is a losing call, and only the excess says so.
    stock = frame([100.0, 100.5, 101, 101.5, 102, 103.0], start="2026-06-01")
    spy = frame([400.0, 402, 404, 406, 412, 416.0], start="2026-06-01")
    snapshot = snapshot_for({"AAA": stock, "SPY": spy})
    record = O.resolve(decision(), stock, snapshot, benchmark=spy).record
    assert record is not None
    row = record.horizons["5"]
    assert row["return_pct"] == pytest.approx(3.0)
    assert row["excess_spy_pct"] == pytest.approx(-1.0)
    assert record.excess("5") == pytest.approx(-1.0)


def test_the_sector_line_says_when_a_pick_was_really_a_sector_bet():
    stock = frame([100.0, 102, 104, 106, 108, 110.0], start="2026-06-01")
    xlk = frame([200.0, 204, 208, 212, 216, 220.0], start="2026-06-01")
    snapshot = snapshot_for({"AAA": stock})
    record = O.resolve(decision(), stock, snapshot, sector=xlk).record
    assert record is not None
    assert record.sector_etf == "XLK"
    assert record.horizons["5"]["excess_sector_pct"] == pytest.approx(0.0)


def test_the_benchmark_is_matched_on_sessions_not_on_row_offsets():
    # The stock was halted for two sessions, so its fifth row is a different
    # week from SPY's fifth row. Matching by position would compare the wrong
    # fortnight and produce a plausible, wrong excess.
    stock = frame([100.0, 101, 102, 103, 104, 105.0], sessions=[
        "2026-06-01", "2026-06-02", "2026-06-05", "2026-06-08", "2026-06-09", "2026-06-10",
    ])
    spy = frame([400.0] * 8, start="2026-06-01")
    snapshot = snapshot_for({"AAA": stock, "SPY": spy})
    record = O.resolve(decision(), stock, snapshot, benchmark=spy).record
    assert record is not None
    assert record.horizons["5"]["excess_spy_pct"] == pytest.approx(5.0)


def test_a_missing_benchmark_costs_the_excess_and_not_the_record():
    bars = frame([100.0, 101, 102, 103, 104, 105.0], start="2026-06-01")
    snapshot = snapshot_for({"AAA": bars})
    resolution = O.resolve(decision(), bars, snapshot, benchmark=None)
    assert resolution.record is not None
    assert "excess_spy_pct" not in resolution.record.horizons["5"]
    assert any("SPY" in note for note in resolution.record.notes)


def test_a_profitable_short_is_not_filed_as_a_loss():
    window = frame([100.0, 95, 90.0], highs=[101.0, 96, 91], lows=[99.0, 89, 88.0])
    long_mfe, long_mae = O.excursions(window, 100.0, "long")
    short_mfe, short_mae = O.excursions(window, 100.0, "short")
    assert long_mfe == pytest.approx(1.0) and long_mae == pytest.approx(-12.0)
    assert short_mfe == pytest.approx(12.0) and short_mae == pytest.approx(-1.0)


def test_a_no_trade_is_not_an_entry_that_failed_to_trigger():
    window = frame([100.0, 99, 98.0])
    status = O.entry_status(window, {}, "long")
    assert status["entry_triggered"] is None
    assert status["stop_hit"] is None and status["target_hit"] is None


def test_an_entry_the_market_never_came_back_to_is_recorded_as_untriggered():
    window = frame([100.0, 105, 110.0], lows=[99.5, 104, 109.0])
    status = O.entry_status(window, {"entry": 95.0, "stop": 90.0, "target": 120.0}, "long")
    assert status["entry_triggered"] is False
    assert status["stop_hit"] is False and status["target_hit"] is False


def test_the_stop_and_the_target_are_reported_in_the_order_they_were_hit():
    window = frame(
        [100.0, 98, 96, 105.0],
        highs=[100.5, 99, 97, 106.0],
        lows=[99.0, 97, 93.0, 104.0],
    )
    status = O.entry_status(
        window, {"entry": 99.5, "stop": 94.0, "target": 106.0}, "long"
    )
    assert status["entry_triggered"] is True
    assert status["first_hit"] == "stop"
    assert status["stop_hit"] is True and status["target_hit"] is False


def test_a_bar_that_spans_both_levels_is_scored_as_the_stop():
    # Daily bars cannot say which came first intraday. Assuming the outcome
    # that costs money is the only honest reading of your own research.
    window = frame([100.0, 100.0], highs=[100.5, 130.0], lows=[99.0, 80.0])
    status = O.entry_status(
        window, {"entry": 100.0, "stop": 90.0, "target": 120.0}, "long"
    )
    assert status["first_hit"] == "stop"
    assert status["stop_hit"] is True
    assert status["target_hit"] is False


def test_a_plan_with_no_stop_reports_no_stop_rather_than_a_clean_miss():
    window = frame([100.0, 101.0], highs=[100.5, 121.0], lows=[99.0, 100.0])
    status = O.entry_status(window, {"entry": 100.0, "target": 120.0}, "long")
    assert status["stop_hit"] is None
    assert status["target_hit"] is True


def test_completeness_is_all_five_horizons_and_nothing_less():
    assert not O.is_complete({})
    assert not O.is_complete({"horizons": {"1": {}, "5": {}, "10": {}, "20": {}}})
    assert O.is_complete({"horizons": {str(h): {} for h in O.HORIZONS}})


def test_the_price_download_asks_for_the_benchmarks_the_decisions_need():
    symbols = O.symbols_to_price(
        [
            decision(ticker="AAA", sector="Information Technology"),
            decision(ticker="BBB", sector="Energy"),
            decision(ticker="CCC", sector="Nonsense Sector"),
        ]
    )
    assert set(symbols) == {"AAA", "BBB", "CCC", "SPY", "XLK", "XLE"}


def test_an_unmappable_sector_costs_the_sector_line_and_says_so():
    bars = frame([100.0, 101, 102.0], start="2026-06-01")
    snapshot = snapshot_for({"AAA": bars})
    resolution = O.resolve(decision(sector="Nonsense Sector"), bars, snapshot)
    assert resolution.record is not None
    assert resolution.record.sector_etf == ""
    assert any("no ETF in the map" in note for note in resolution.record.notes)


def test_a_decision_on_a_ticker_with_no_usable_bars_yields_no_record():
    empty = pd.DataFrame()
    snapshot = snapshot_for({"AAA": frame([100.0, 101.0])})
    assert O.resolve(decision(), empty, snapshot).record is None
