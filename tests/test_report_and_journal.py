import json
from datetime import date

import numpy as np
import pandas as pd
import pytest

from tradingagent.data.market import Quote, pct_change
from tradingagent.data.validate import DataUnavailable, DegradedTracker, validate_bars
from tradingagent.discovery.breadth import analyze_breadth
from tradingagent.discovery.calendar import CalendarView, static_release_calendar
from tradingagent.discovery.screener import Candidate
from tradingagent.discovery.sectors import SectorMap, SectorRow
from tradingagent.discovery.shortlist import QuickTake, ShortlistEntry
from tradingagent.journal import JournalEntry, append_entries, entries_from_shortlist, read_entries
from tradingagent.llm import TokenLedger
from tradingagent.report.render import DISCLAIMER, ReportContext, render_daily_brief


def candidate(symbol="TST", score=85):
    return Candidate(
        symbol=symbol, name="Test Co", sector="Information Technology", industry="Software",
        price=100.0, score=score, rating="A-", state="ACTIONABLE_DAY1",
        primary_trigger="4pct_breakout", triggers=["4pct_breakout"], day_gain_pct=5.0,
        volume_ratio_20d=3.0, avg_share_volume=2_000_000, close_location_pct=95.0,
        prior_base_days=12, base_width_pct=6.0, entry_ref=101.0, stop_ref=98.0, risk_pct=3.0,
        dist_52w_high_pct=-2.0, above_50dma=True, above_200dma=True, rs_vs_spy_3mo=12.0,
    )


def context(shortlist=None, degraded=None, ledger=None):
    bars = {f"T{i}": _frame() for i in range(6)}
    return ReportContext(
        run_date=date(2026, 8, 14),
        commentary="Breadth is holding up while the index grinds higher.",
        indices=[Quote("SPY", "S&P 500", 500.0, {"1d": 0.5, "5d": 1.0, "1mo": 2.0, "3mo": 3.0})],
        vix=14.6,
        breadth=analyze_breadth(bars),
        sector_map=SectorMap(
            rows=[SectorRow("Information Technology", "XLK", 0.7, 60, 0.5, 1.0, 2.0, 3.0, "Cyclical", "Neutral", True)],
            risk_regime="Risk-On", risk_score=3.1, cycle_phase="Mid Cycle Expansion", cycle_confidence="Medium",
            overbought=[], oversold=[],
        ),
        calendar=CalendarView(
            macro=static_release_calendar(date(2026, 8, 14), date(2026, 8, 21)),
            earnings_today=[], earnings_week=[], macro_is_live=False,
        ),
        shortlist=shortlist if shortlist is not None else [],
        degraded=degraded or DegradedTracker(),
        ledger=ledger or TokenLedger(),
        universe_size=503, screened=498, candidates=[candidate()],
        session_note="market CLOSED", data_as_of="2026-08-13 close",
        paid_gaps=["FMP is needed for the full US universe."], runtime_seconds=42.3,
    )


def _frame():
    closes = np.linspace(100, 180, 400)
    return pd.DataFrame(
        {"Open": closes, "High": closes * 1.01, "Low": closes * 0.99, "Close": closes,
         "Volume": np.full(400, 1_000_000.0)},
        index=pd.date_range("2024-01-01", periods=400, freq="B"),
    )


def entry(take=True):
    return ShortlistEntry(
        candidate=candidate(),
        take=QuickTake(rating="Overweight", confidence="M", thesis="Clean base breakout.",
                       key_risk="A failed follow-through day.", deep_dive_priority=8) if take else None,
        earnings_flag="—", news_headline="Test Co wins a contract",
        degraded_reason=None if take else "provider timeout",
    )


# --- report -------------------------------------------------------------


def test_sections_appear_in_schema_order():
    md = render_daily_brief(context(shortlist=[entry()]))
    headings = [
        "## 1. Market Overview", "## 2. Macro & Events Today", "## 3. Sector Opportunity Map",
        "## 4. Shortlist", "## 5. Deep Analysis", "## 6. Options Candidates",
        "## 7. Degraded Sources",
    ]
    positions = [md.index(h) for h in headings]
    assert positions == sorted(positions)


def test_disclaimer_is_verbatim_and_last():
    md = render_daily_brief(context())
    assert DISCLAIMER in md
    assert md.rstrip().endswith(f"_{DISCLAIMER}_")


def test_degraded_section_says_none_when_clean():
    md = render_daily_brief(context())
    section = md.split("## 7. Degraded Sources")[1]
    assert "\nnone\n" in section
    assert "DEGRADED — missing" not in section


def test_degraded_section_names_every_failed_source():
    tracker = DegradedTracker()
    tracker.add("Finnhub news", "429 rate limited")
    tracker.add("Alpaca clock", "connection refused")
    md = render_daily_brief(context(degraded=tracker))
    assert "**DEGRADED — missing: Finnhub news, Alpaca clock**" in md
    assert "429 rate limited" in md


def test_shortlist_marks_a_missing_quick_take_as_degraded():
    md = render_daily_brief(context(shortlist=[entry(take=False)]))
    assert "| **TST** |" in md
    assert "DEGRADED" in md
    assert "provider timeout" in md


def test_footer_reports_tokens_per_tier_and_total():
    ledger = TokenLedger()
    ledger.record("fast", "vertex_ai/haiku", 1000, 300, cost_usd=0.0012)
    ledger.record("smart", "vertex_ai/sonnet", 500, 200, cost_usd=0.006)
    md = render_daily_brief(context(ledger=ledger))
    assert "| fast | `vertex_ai/haiku` | 1 | 1,000 | 300 | 1,300 | $0.0012 |" in md
    assert "**2,000**" in md and "$0.0072" in md


def test_report_survives_a_completely_empty_shortlist():
    md = render_daily_brief(context(shortlist=[]))
    assert "_no candidates passed today's filters_" in md
    assert DISCLAIMER in md


# --- journal ------------------------------------------------------------


def test_journal_line_matches_the_declared_schema(tmp_path):
    path = tmp_path / "journal.jsonl"
    written = append_entries(path, entries_from_shortlist([entry()], date(2026, 8, 14),
                                                          "reports/2026-08-14/daily-brief.md"))
    assert written == 1
    row = json.loads(path.read_text().strip())
    assert list(row)[:10] == [
        "date", "ticker", "verdict", "target", "confidence", "options",
        "signal_sources", "report", "outcome_7d", "outcome_30d",
    ]
    assert row["ticker"] == "TST"
    assert row["verdict"] == "Overweight"
    assert row["confidence"] == "M"
    assert row["target"] is None and row["outcome_7d"] is None and row["outcome_30d"] is None


def test_journal_records_degraded_verdict_without_confidence(tmp_path):
    path = tmp_path / "journal.jsonl"
    append_entries(path, entries_from_shortlist([entry(take=False)], date(2026, 8, 14), "r.md"))
    row = json.loads(path.read_text().strip())
    assert row["verdict"] == "DEGRADED" and row["confidence"] == ""


def test_journal_appends_rather_than_truncates(tmp_path):
    path = tmp_path / "journal.jsonl"
    for _ in range(3):
        append_entries(path, [JournalEntry("2026-08-14", "TST", "Hold", "L", "r.md")])
    assert len(read_entries(path)) == 3


def test_read_entries_skips_malformed_lines(tmp_path):
    path = tmp_path / "journal.jsonl"
    path.write_text('{"ticker":"OK"}\nnot json\n\n{"ticker":"ALSO_OK"}\n')
    assert [r["ticker"] for r in read_entries(path)] == ["OK", "ALSO_OK"]


def test_append_nothing_creates_no_file(tmp_path):
    path = tmp_path / "journal.jsonl"
    assert append_entries(path, []) == 0
    assert not path.exists()


# --- validation ---------------------------------------------------------


def test_validate_bars_rejects_empty_short_and_volumeless():
    good = _frame()
    validate_bars(good, "T", min_rows=20)

    with pytest.raises(DataUnavailable, match="empty"):
        validate_bars(good.iloc[:0], "T")
    with pytest.raises(DataUnavailable, match="only 5 bars"):
        validate_bars(good.head(5), "T", min_rows=20)

    zero = good.copy()
    zero["Volume"] = 0.0
    with pytest.raises(DataUnavailable, match="zero volume"):
        validate_bars(zero, "T", min_rows=20)
    validate_bars(zero, "^VIX", min_rows=20, require_volume=False)  # indices are exempt


def test_pct_change_handles_short_and_zero_series():
    assert pct_change(pd.Series([100.0, 110.0]), 1) == pytest.approx(10.0)
    assert pct_change(pd.Series([100.0]), 5) is None
    assert pct_change(pd.Series([0.0, 10.0]), 1) is None
