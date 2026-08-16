"""``--stage outcomes`` and ``--stage evaluate`` as jobs.

Both are meant to be safe to schedule on their own and safe to re-run over the
same day as often as you like, which is a stronger claim than "it works once".
The tests here hold it to that: a second run must not double a sample, a
Saturday run must not invent Saturday's session, and neither stage may spend a
token.
"""

import json
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from tradingagent.evaluation import stage as S
from tradingagent.evaluation.ledger import DECISIONS, OUTCOMES, ExperimentLedger
from tests.test_evaluation_ledger import make_settings

DECISION_DATE = date(2026, 6, 1)


def bars(closes, start="2026-06-01"):
    return pd.DataFrame(
        {
            "Open": closes,
            "High": [c * 1.02 for c in closes],
            "Low": [c * 0.98 for c in closes],
            "Close": closes,
            "Volume": [2_000_000] * len(closes),
        },
        index=pd.bdate_range(start=start, periods=len(closes)),
    )


class FakeMarket:
    """Stands in for yfinance. Records what was asked for."""

    def __init__(self, frames):
        self.frames = frames
        self.asked: list[str] = []

    def __call__(self, *args, **kwargs):
        return self

    def load_many(self, symbols, **kwargs):
        self.asked = list(symbols)
        return {s: self.frames[s] for s in symbols if s in self.frames}


def settings_for(tmp_path, run_date=DECISION_DATE):
    # Both stages take an explicit ledger, so nothing here touches the repo's
    # own journal directory.
    return make_settings(run_date=run_date, reports_bucket="")


def seed(store, rows, stream=DECISIONS):
    store.path(stream).parent.mkdir(parents=True, exist_ok=True)
    with store.path(stream).open("a") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")


def decision_row(ticker="AAA", stage="deep", **extra):
    row = {
        "decision_id": f"2026-06-01:{ticker}:{stage}",
        "ticker": ticker,
        "date": DECISION_DATE.isoformat(),
        "stage": stage,
        "rating": "Buy",
        "sector": "Information Technology",
        "provenance": {"run_id": "run-1"},
    }
    row.update(extra)
    return row


@pytest.fixture
def market(monkeypatch):
    frames = {
        "AAA": bars([100.0 + i for i in range(30)]),
        "SPY": bars([400.0] * 30),
        "XLK": bars([200.0] * 30),
    }
    fake = FakeMarket(frames)
    monkeypatch.setattr(S, "MarketData", fake)
    return fake


def test_an_empty_ledger_is_a_no_op_and_not_a_download(tmp_path, market):
    store = ExperimentLedger(tmp_path / "ledger")
    result = S.run_outcomes(settings_for(tmp_path), ledger=store)
    assert result.resolved == 0
    assert market.asked == []
    assert "no decisions" in result.notes[0]


def test_a_decision_resolves_and_lands_in_the_outcomes_stream(tmp_path, market):
    store = ExperimentLedger(tmp_path / "ledger")
    seed(store, [decision_row()])
    result = S.run_outcomes(settings_for(tmp_path), ledger=store)
    assert result.resolved == 1
    rows = store.read(OUTCOMES)
    assert len(rows) == 1
    assert rows[0]["ticker"] == "AAA"
    assert rows[0]["horizons"]["5"]["excess_spy_pct"] == pytest.approx(5.0)


def test_the_job_prices_the_benchmarks_the_decisions_need_and_nothing_else(tmp_path, market):
    store = ExperimentLedger(tmp_path / "ledger")
    seed(store, [decision_row()])
    S.run_outcomes(settings_for(tmp_path), ledger=store)
    assert market.asked == ["AAA", "SPY", "XLK"]


def test_running_it_twice_supersedes_rather_than_doubling_the_sample(tmp_path, market):
    # The job is meant to run every day after the close. If a re-run added a
    # second row for the same decision, every horizon would be counted twice
    # and the graduation thresholds would clear on half the real evidence.
    store = ExperimentLedger(tmp_path / "ledger")
    seed(store, [decision_row()])
    settings = settings_for(tmp_path)
    S.run_outcomes(settings, ledger=store)
    second = S.run_outcomes(settings, ledger=store)
    assert second.updated == 1 and second.resolved == 0
    assert len(store.latest(OUTCOMES, "decision_id")) == 1


def test_a_fully_matured_decision_is_not_re_resolved(tmp_path, market):
    store = ExperimentLedger(tmp_path / "ledger")
    seed(store, [decision_row()])
    seed(
        store,
        [
            {
                "decision_id": "2026-06-01:AAA:deep",
                "ticker": "AAA",
                "horizons": {"1": {}, "5": {}, "10": {}, "20": {}, "60": {}},
            }
        ],
        stream=OUTCOMES,
    )
    result = S.run_outcomes(settings_for(tmp_path), ledger=store)
    assert result.complete == 1
    assert market.asked == []  # nothing left to price


def test_the_as_of_date_comes_from_the_bars_not_from_the_calendar(tmp_path, market):
    # Run "on" a Saturday. The wall clock would happily claim two sessions that
    # never happened; the snapshot reports the last bar it actually has.
    store = ExperimentLedger(tmp_path / "ledger")
    seed(store, [decision_row()])
    saturday = date(2026, 7, 18)
    result = S.run_outcomes(settings_for(tmp_path, run_date=saturday), ledger=store)
    assert result.as_of == "2026-07-10"  # the 30th business day from 1 June
    assert result.snapshot is not None
    assert result.snapshot.market_as_of.isoformat() == result.as_of


def test_a_ticker_with_no_price_history_is_pending_not_lost(tmp_path, market):
    store = ExperimentLedger(tmp_path / "ledger")
    seed(store, [decision_row(ticker="ZZZ")])
    result = S.run_outcomes(settings_for(tmp_path), ledger=store)
    assert result.pending == 1 and result.resolved == 0
    assert any("ZZZ" in note for note in result.notes)


def test_a_total_price_failure_degrades_rather_than_raising(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "MarketData", FakeMarket({}))
    store = ExperimentLedger(tmp_path / "ledger")
    seed(store, [decision_row()])
    result = S.run_outcomes(settings_for(tmp_path), ledger=store)
    assert result.resolved == 0
    assert "Outcome resolution" in result.degraded.sources


def test_the_discovery_and_deep_rows_both_resolve_and_cluster_later(tmp_path, market):
    # Two ledger decisions, two outcome rows — the de-duplication to one
    # observation is grading's job, not resolution's, because the two rows can
    # carry different trade plans.
    store = ExperimentLedger(tmp_path / "ledger")
    seed(store, [decision_row(stage="discovery"), decision_row(stage="deep")])
    result = S.run_outcomes(settings_for(tmp_path), ledger=store)
    assert result.resolved == 2

    from tradingagent.evaluation.grading import cluster

    observations = cluster(store.read(DECISIONS), store.read(OUTCOMES))
    assert len(observations) == 1


def test_the_weekly_file_is_written_where_the_report_writer_puts_it(tmp_path, market):
    store = ExperimentLedger(tmp_path / "ledger")
    seed(store, [decision_row()])
    settings = settings_for(tmp_path)
    S.run_outcomes(settings, ledger=store)

    written: dict[str, str] = {}
    import tradingagent.evaluation.stage as stage_module

    def fake_write(path: Path, content: str, bucket: str = ""):
        written["path"] = str(path)
        written["content"] = content
        return path

    original = stage_module.write_report
    stage_module.write_report = fake_write
    try:
        result = S.run_evaluate(settings, ledger=store)
    finally:
        stage_module.write_report = original

    assert result.week == "2026-23"
    assert written["path"].endswith("evaluation/2026-23.md")
    assert "Daybreak evaluation" in written["content"]
    assert result.evidence.startswith("## Evidence so far")


def test_the_iso_week_distinguishes_a_friday_from_the_monday_after():
    assert S.week_label(date(2026, 8, 14)) == "2026-33"
    assert S.week_label(date(2026, 8, 17)) == "2026-34"


def test_neither_stage_holds_a_reference_to_the_llm_gateway():
    # A stage that could call a model would eventually be asked to summarise
    # the evidence, and the report's whole claim is that no number in it was
    # generated.
    source = Path(S.__file__).read_text() + Path(
        S.__file__.replace("stage.py", "report.py")
    ).read_text()
    assert "gateway" not in source.lower()
    assert "llm" not in source.lower()
