"""The decision sheet's inputs: frozen from objects, never parsed from prose."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pandas as pd
import pytest

from tradingagent.discovery.breadth import BreadthResult
from tradingagent.discovery.release_schedule import MacroEvent
from tradingagent.discovery.sectors import SectorMap, SectorRow
from tradingagent.presentation import build
from tradingagent.presentation.context import (
    PresentationContext,
    Regime,
    SeriesPoint,
    Setup,
    read_or_none,
)


# --- series -----------------------------------------------------------------


def _frame(closes: list[float], start: str = "2026-01-01") -> pd.DataFrame:
    index = pd.bdate_range(start=start, periods=len(closes))
    return pd.DataFrame({"Close": closes}, index=index)


def test_the_moving_average_is_computed_before_the_window_is_cut():
    # 300 rising sessions; the last 63 are what the chart shows. A 50-day mean
    # computed after slicing would average the tail and sit far too high.
    frame = _frame([100.0 + i for i in range(300)])
    points = build.series_from(frame, 63, with_mas=True)
    assert len(points) == 63
    last = points[-1]
    assert last.close == pytest.approx(399.0)
    # The true 50-day mean of 350..399 is 374.5.
    assert last.sma50 == pytest.approx(374.5)
    assert last.sma200 == pytest.approx(299.5)


def test_a_short_history_leaves_the_long_average_empty_rather_than_wrong():
    points = build.series_from(_frame([10.0] * 60), 63, with_mas=True)
    assert points[-1].sma50 == pytest.approx(10.0)
    assert points[-1].sma200 is None


def test_no_frame_is_no_series():
    assert build.series_from(None, 63) == []
    assert build.series_from(pd.DataFrame(), 63) == []


# --- wait condition ---------------------------------------------------------


@dataclass
class _Plan:
    entry: float | None = None
    entry_basis: str = ""
    stop: float | None = None
    target: float | None = None
    status: str = ""
    direction: str = "long"
    failures: list = None
    actionable: bool = True

    def __post_init__(self):
        self.failures = self.failures or []


def test_an_entry_below_the_close_is_a_pullback_not_a_breakout():
    line = build.wait_condition(_Plan(entry=73.20, entry_basis="50-day SMA"), spot=75.20)
    assert "pullback" in line and "$73.20" in line and "50-day SMA" in line


def test_an_entry_above_the_close_is_a_breakout():
    line = build.wait_condition(_Plan(entry=80.0), spot=75.0)
    assert "break above" in line and "+6.7%" in line


def test_an_entry_at_the_close_says_so_instead_of_inventing_a_wait():
    line = build.wait_condition(_Plan(entry=75.10), spot=75.20)
    assert line.startswith("At the market")


def test_no_entry_is_no_wait_condition():
    assert build.wait_condition(_Plan(entry=None), spot=75.0) == ""
    assert build.wait_condition(None, spot=75.0) == ""


# --- gates ------------------------------------------------------------------


def test_only_a_verified_future_release_becomes_a_do_not_act_before_line():
    as_of = date(2026, 8, 14)
    events = [
        MacroEvent(date(2026, 8, 20), "Initial Jobless Claims", "Medium", "FRED", "VERIFIED"),
        MacroEvent(date(2026, 8, 21), "Guessed CPI", "High", "heuristic", "INDICATIVE"),
        MacroEvent(date(2026, 8, 10), "Retail Sales", "Medium", "FRED", "VERIFIED"),
        MacroEvent(None, "Unknown release", "High", "FRED", "MISSING"),
    ]
    gates = build.build_gates(events, as_of)
    assert [g.name for g in gates] == ["Initial Jobless Claims"]
    assert gates[0].confidence == "VERIFIED"


# --- regime -----------------------------------------------------------------


@dataclass
class _ReportContext:
    breadth: BreadthResult
    sector_map: SectorMap
    vix: float | None = 14.25
    data_as_of: str = "2026-08-14 close"
    session_note: str = "market CLOSED"
    degraded: object = None


def _regime_inputs():
    breadth = BreadthResult(
        composite=74.5,
        zone="Healthy",
        exposure="75-90%",
        guidance="Normal operations.",
        breadth_pct_above_50dma=70.0,
        universe_size=501,
    )
    sectors = SectorMap(
        rows=[
            SectorRow("Energy", "XLE", 0.7, 20, ret_5d=2.0, ret_1mo=4.0, ret_3mo=6.0),
            SectorRow("Utilities", "XLU", 0.3, 15, ret_5d=-1.0, ret_1mo=-2.0, ret_3mo=-1.0),
        ],
        risk_regime="Neutral",
        risk_score=0.44,
        cycle_phase="Early",
        cycle_confidence="Medium",
        overbought=["Energy"],
    )
    return _ReportContext(breadth=breadth, sector_map=sectors)


def test_the_unvalidated_marker_survives_into_the_sheet():
    # The exposure band has never been graded against an outcome. If the email
    # can drop that marker, the email can quietly promote a heuristic.
    regime = build.build_regime(_regime_inputs(), spy_frame=None)
    assert "[UNVALIDATED]" in regime.posture.described
    assert regime.posture.validation == "UNVALIDATED"
    assert "[UNVALIDATED]" not in regime.rotation.described


def test_the_sector_bars_come_out_ranked():
    regime = build.build_regime(_regime_inputs(), spy_frame=None)
    assert [s.sector for s in regime.sectors] == ["Energy", "Utilities"]
    assert regime.leaders[0] == "Energy"


# --- overlays ---------------------------------------------------------------


@dataclass
class _Quote:
    expiry: str = "2026-09-18"
    dte: int = 33


@dataclass
class _Candidate:
    strike: float = 110.0
    delta: float = -0.36
    credit: float = 1.68
    annualized_yield_pct: float = 17.4
    breakeven: float = 108.32
    earnings_flag: str = "clear"
    conflicts: list = None
    quote: _Quote = None

    def __post_init__(self):
        self.conflicts = self.conflicts or []
        self.quote = self.quote or _Quote()


@dataclass
class _OptionsPlan:
    symbol: str
    strategy: str | None = "covered call"
    chosen: object = None
    candidates: list = None
    rejected: list = None
    error: str | None = None
    skipped: str | None = None

    def __post_init__(self):
        self.candidates = self.candidates if self.candidates is not None else []
        self.rejected = self.rejected or []


def test_a_breakeven_under_the_invalidation_is_called_out_on_the_row():
    status = build.breakeven_status(_Candidate(breakeven=95.0), invalidation=100.0)
    assert "BELOW" in status and "$5.00" in status


def test_a_breakeven_above_the_invalidation_states_the_clearance():
    status = build.breakeven_status(_Candidate(breakeven=108.32), invalidation=100.0)
    assert "clears" in status and "$8.32" in status


def test_a_skipped_overlay_is_a_row_with_a_reason_not_an_absence():
    overlays, skips = build.build_overlays(
        [
            _OptionsPlan("DIS", chosen=_Candidate()),
            _OptionsPlan("WMB", candidates=[]),
            _OptionsPlan("PYPL", strategy=None, skipped="Underweight — no overlay proposed."),
        ],
        invalidations={"DIS": 100.0},
    )
    assert [o.symbol for o in overlays] == ["DIS"]
    assert {s.symbol: s.reason for s in skips} == {
        "WMB": "no strike passed the hard filters",
        "PYPL": "Underweight — no overlay proposed.",
    }
    assert overlays[0].invalidation == 100.0


# --- round trip -------------------------------------------------------------


def test_the_artefact_survives_a_round_trip_through_disk(tmp_path):
    original = PresentationContext(
        run_date="2026-08-16",
        regime=Regime(composite=74.5, spy=[SeriesPoint("2026-08-14", 660.1, 640.0, 600.0)]),
        setups=[Setup(symbol="WMB", entry=73.2, stop=71.5, target=82.0)],
    )
    original.write(tmp_path)
    back = PresentationContext.read(tmp_path)
    assert back.regime.spy[0].sma200 == 600.0
    assert back.setups[0].actionable
    assert back.setups[0].consensus.known is False


def test_a_missing_artefact_degrades_the_email_rather_than_failing_it(tmp_path):
    assert read_or_none(tmp_path) is None


def test_a_future_schema_degrades_rather_than_raising(tmp_path):
    (tmp_path / "presentation-context.json").write_text('{"run_date": "2026-08-16", "version": 99}')
    assert read_or_none(tmp_path) is None
