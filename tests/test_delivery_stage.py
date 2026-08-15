"""``--stage report``: reconstructing the delivery payload from disk."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pytest

from tradingagent.delivery import stage as S
from tradingagent.options.context import SCHEMA_VERSION

BRIEF_CLEAN = """# Daily Trading Research Brief — 2026-08-14

## 7. Degraded Sources

No sources degraded on this run.
"""

BRIEF_DEGRADED = """# Daily Trading Research Brief — 2026-08-14

## 7. Degraded Sources

**DEGRADED — missing: yfinance OHLCV, Finnhub economic calendar, Economic calendar**

| Source | Detail |
|---|---|
"""


def test_degraded_sources_are_recovered_from_the_rendered_brief():
    assert S.degraded_from_brief(BRIEF_DEGRADED) == [
        "yfinance OHLCV",
        "Finnhub economic calendar",
        "Economic calendar",
    ]


def test_a_clean_run_reports_no_degradation():
    assert S.degraded_from_brief(BRIEF_CLEAN) == []


def test_a_plain_hyphen_is_accepted_as_well_as_an_em_dash():
    """Report rendering uses an em dash; do not let a typographic change lose this."""
    assert S.degraded_from_brief("**DEGRADED - missing: Finnhub**") == ["Finnhub"]


# --- verdicts from disk --------------------------------------------------------


def write_context(directory: Path, rows: list[tuple[str, str, str]]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "run_date": "2026-08-14",
        "data_as_of": "2026-08-13 close",
        "version": SCHEMA_VERSION,
        "verdicts": [
            {"symbol": s, "rating": r, "confidence": c} for s, r, c in rows
        ],
    }
    (directory / "options-context.json").write_text(json.dumps(payload))


def test_verdicts_come_back_in_deep_queue_order(tmp_path):
    write_context(tmp_path, [("FDX", "Hold", "M"), ("NVDA", "Buy", "H")])
    verdicts = S.verdicts_from_context(tmp_path)
    assert [v.symbol for v in verdicts] == ["FDX", "NVDA"]
    assert verdicts[1].rating == "Buy"


def test_a_missing_context_degrades_to_zero_verdicts_rather_than_crashing(tmp_path):
    """A discovery-only day has no deep stage; the email should still go out."""
    assert S.verdicts_from_context(tmp_path) == []


def test_a_context_from_an_older_schema_does_not_break_delivery(tmp_path):
    (tmp_path / "options-context.json").write_text(
        json.dumps({"run_date": "2026-08-14", "version": 999, "verdicts": []})
    )
    assert S.verdicts_from_context(tmp_path) == []


# --- deep report discovery ------------------------------------------------------


def test_deep_reports_are_found_and_sorted(tmp_path):
    deep = tmp_path / "deep"
    deep.mkdir()
    for name in ("NVDA.md", "AMD.md", "notes.txt"):
        (deep / name).write_text("x")
    assert [p.name for p in S.deep_report_paths(tmp_path)] == ["AMD.md", "NVDA.md"]


def test_no_deep_directory_is_not_an_error(tmp_path):
    assert S.deep_report_paths(tmp_path) == []


# --- in-memory adapter -----------------------------------------------------------


@dataclass
class FakeDecision:
    rating: str
    confidence: str


@dataclass
class FakeResult:
    symbol: str
    decision: FakeDecision | None


def test_in_memory_results_convert_to_verdicts():
    verdicts = S.verdicts_from_results(
        [FakeResult("NVDA", FakeDecision("Buy", "H")), FakeResult("FDX", None)]
    )
    assert [(v.symbol, v.rating) for v in verdicts] == [("NVDA", "Buy"), ("FDX", "DEGRADED")]


def test_a_ticker_whose_pipeline_aborted_is_still_listed():
    """Silence about a failed ticker is worse than a DEGRADED row."""
    [verdict] = S.verdicts_from_results([FakeResult("XYZ", None)])
    assert verdict.rating == "DEGRADED"
    assert not verdict.ok


# --- run_report ------------------------------------------------------------------


class Settings:
    """Minimal stand-in: run_report only needs the date and the report dir."""

    def __init__(self, root: Path, run_date: date):
        self._root, self.run_date = root, run_date

    def report_dir(self) -> Path:
        return self._root / self.run_date.isoformat()


@pytest.fixture
def report_dir(tmp_path):
    directory = tmp_path / "2026-08-14"
    (directory / "deep").mkdir(parents=True)
    (directory / "daily-brief.md").write_text(BRIEF_DEGRADED)
    (directory / "deep" / "NVDA.md").write_text("# NVDA")
    write_context(directory, [("NVDA", "Buy", "H")])
    return tmp_path


def test_run_report_assembles_everything_from_disk(report_dir, monkeypatch):
    captured = {}

    def fake_send(run_date, brief_path, deep_paths, verdicts, degraded_sources):
        captured.update(locals())
        return "sent"

    monkeypatch.setattr(S, "send_daily_brief", lambda *a, **k: fake_send(*a, **k))
    settings = Settings(report_dir, date(2026, 8, 14))

    assert S.run_report(settings) == "sent"
    assert captured["brief_path"].name == "daily-brief.md"
    assert [p.name for p in captured["deep_paths"]] == ["NVDA.md"]
    assert [v.symbol for v in captured["verdicts"]] == ["NVDA"]
    assert "Finnhub economic calendar" in captured["degraded_sources"]


def test_in_memory_degradation_wins_over_reparsing_the_brief(report_dir, monkeypatch):
    """--stage all knows the live tracker; it should not re-read its own output."""
    from tradingagent.data.validate import DegradedTracker

    captured = {}
    monkeypatch.setattr(
        S, "send_daily_brief", lambda *a, **k: captured.update(k) or "sent"
    )
    tracker = DegradedTracker()
    tracker.add("Alpaca", "chain unavailable")

    S.run_report(Settings(report_dir, date(2026, 8, 14)), degraded=tracker)
    assert captured["degraded_sources"] == ["Alpaca"]
