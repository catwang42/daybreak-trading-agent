"""Recovering the pre-ledger journal — and being honest about the gaps.

The value of a backfill is that grading has a sample to work on months before
the ledger would have produced one. The risk is that a reconstruction which
knows less than a real row starts being counted as if it knew the same. Every
test here is about keeping those two apart.
"""

import json
from pathlib import Path

import pytest

from tradingagent.evaluation import backfill as B
from tradingagent.evaluation.ledger import DECISIONS, RUNS, ExperimentLedger


def journal(tmp_path: Path, rows) -> Path:
    path = tmp_path / "journal.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return path


def line(**overrides):
    row = {
        "date": "2026-08-14",
        "ticker": "V",
        "verdict": "Overweight",
        "target": None,
        "confidence": "M",
        "options": None,
        "signal_sources": ["yfinance", "finnhub"],
        "report": "reports/2026-08-14/daily-brief.md",
        "outcome_7d": None,
        "outcome_30d": None,
        "stage": "discovery",
        "screener_score": 95,
    }
    row.update(overrides)
    return row


def test_every_backfilled_row_says_so(tmp_path):
    store = ExperimentLedger(tmp_path / "ledger")
    B.backfill(journal(tmp_path, [line()]), store)
    rows = store.read(DECISIONS)
    assert rows and all(row["provenance"]["backfilled"] for row in rows)
    assert all(row["provenance"]["run_id"].startswith("backfill-") for row in rows)


def test_the_run_id_is_synthetic_because_no_snapshot_was_ever_recorded(tmp_path):
    # A real run id is derived from the snapshot the run took. There is no such
    # id in the journal, and inventing one that looked real would let a
    # backfilled day masquerade as a reproducible experiment.
    store = ExperimentLedger(tmp_path / "ledger")
    B.backfill(journal(tmp_path, [line()]), store)
    provenance = store.read(DECISIONS)[0]["provenance"]
    assert provenance["run_id"] == "backfill-2026-08-14"
    assert provenance["snapshot_id"] == ""
    assert provenance["config_hash"] == ""


def test_what_the_journal_did_record_survives(tmp_path):
    store = ExperimentLedger(tmp_path / "ledger")
    B.backfill(
        journal(
            tmp_path,
            [
                line(
                    stage="deep",
                    ticker="WMB",
                    verdict="Overweight",
                    target=82.0,
                    signal_readings={"news_tone": 1, "macro_fred": 1},
                    trade_plan={"status": "PLAN", "entry": 75.0, "stop": 71.0},
                )
            ],
        ),
        store,
    )
    row = store.read(DECISIONS)[0]
    assert row["ticker"] == "WMB" and row["stage"] == "deep"
    assert row["rating"] == "Overweight" and row["target"] == 82.0
    assert row["signal_readings"] == {"news_tone": 1, "macro_fred": 1}
    assert row["trade_plan"]["entry"] == 75.0


def test_what_it_never_recorded_is_blank_rather_than_reconstructed(tmp_path):
    # The horizon and the invalidation exist in the report prose. Parsing them
    # back out would produce something that looks like a record of what was
    # decided and is actually a guess made two months later.
    store = ExperimentLedger(tmp_path / "ledger")
    B.backfill(journal(tmp_path, [line(stage="deep")]), store)
    row = store.read(DECISIONS)[0]
    assert row["horizon"] == "" and row["invalidation"] == "" and row["entry_condition"] == ""
    assert row["seat_tiers"] == {}
    assert row["sector"] == ""
    assert "no per-source shadow attribution" in row["degraded_reasons"]


def test_no_candidate_rows_are_invented(tmp_path):
    # The journal only ever held the shortlist. Writing the shortlist as if it
    # were the pool would make the control-vs-treatment comparison compare a
    # list against itself and report a difference of zero as a finding.
    store = ExperimentLedger(tmp_path / "ledger")
    B.backfill(journal(tmp_path, [line(), line(ticker="NVDA")]), store)
    assert store.counts()["candidates.jsonl"] == 0


def test_running_it_twice_does_not_double_the_sample(tmp_path):
    store = ExperimentLedger(tmp_path / "ledger")
    path = journal(tmp_path, [line(), line(ticker="NVDA")])
    first = B.backfill(path, store)
    second = B.backfill(path, store)
    assert len(first.decisions) == 2
    assert second.decisions == [] and second.already_present == 2
    assert len(store.latest(DECISIONS, "decision_id")) == 2


def test_a_real_row_is_never_overwritten_by_a_reconstruction(tmp_path):
    from tradingagent.evaluation.ledger import DecisionRecord
    from tradingagent.evaluation.provenance import Provenance

    store = ExperimentLedger(tmp_path / "ledger")
    store.append(
        DECISIONS,
        [
            DecisionRecord(
                provenance=Provenance(run_id="run-real", run_date="2026-08-14"),
                ticker="V",
                date="2026-08-14",
                stage="discovery",
                rating="Overweight",
                horizon="4-8 weeks",
            )
        ],
    )
    result = B.backfill(journal(tmp_path, [line()]), store)
    assert result.decisions == []
    surviving = store.latest(DECISIONS, "decision_id")["2026-08-14:V:discovery"]
    assert surviving["horizon"] == "4-8 weeks"
    assert not surviving["provenance"]["backfilled"]


def test_the_same_ticker_at_two_stages_stays_two_rows(tmp_path):
    store = ExperimentLedger(tmp_path / "ledger")
    B.backfill(journal(tmp_path, [line(stage="discovery"), line(stage="deep")]), store)
    assert len(store.latest(DECISIONS, "decision_id")) == 2


def test_an_unrecognised_stage_is_skipped_rather_than_guessed_at(tmp_path):
    store = ExperimentLedger(tmp_path / "ledger")
    result = B.backfill(journal(tmp_path, [line(stage="experiment"), line()]), store)
    assert result.skipped == 1 and len(result.decisions) == 1


def test_a_line_with_no_date_or_ticker_is_not_a_decision(tmp_path):
    store = ExperimentLedger(tmp_path / "ledger")
    result = B.backfill(journal(tmp_path, [line(date=""), line(ticker="")]), store)
    assert result.decisions == [] and result.skipped == 2


def test_a_truncated_journal_tail_costs_one_line_not_the_migration(tmp_path):
    path = tmp_path / "journal.jsonl"
    path.write_text(json.dumps(line()) + "\n" + '{"date":"2026-08-15","tick')
    store = ExperimentLedger(tmp_path / "ledger")
    result = B.backfill(path, store)
    assert len(result.decisions) == 1


def test_a_missing_journal_is_not_an_error(tmp_path):
    store = ExperimentLedger(tmp_path / "ledger")
    result = B.backfill(tmp_path / "nope.jsonl", store)
    assert result.read == 0 and result.written == 0


def test_one_synthetic_run_per_date_and_stage(tmp_path):
    store = ExperimentLedger(tmp_path / "ledger")
    B.backfill(
        journal(
            tmp_path,
            [
                line(date="2026-08-14", stage="discovery"),
                line(date="2026-08-14", stage="discovery", ticker="NVDA"),
                line(date="2026-08-14", stage="deep"),
                line(date="2026-08-15", stage="discovery"),
            ],
        ),
        store,
    )
    runs = store.read(RUNS)
    assert len(runs) == 3
    discovery = next(r for r in runs if r["stage"] == "deep")
    assert discovery["universe_size"] == 0  # genuinely unknown, not zero candidates
    assert any("backfilled from journal" in note for note in discovery["notes"])


def test_a_degraded_journal_line_stays_degraded(tmp_path):
    store = ExperimentLedger(tmp_path / "ledger")
    B.backfill(journal(tmp_path, [line(stage="deep", verdict="DEGRADED")]), store)
    assert store.read(DECISIONS)[0]["degraded"] is True


def test_the_weekly_report_counts_and_flags_the_backfilled_sample(tmp_path):
    from datetime import date

    from tradingagent.evaluation.report import weekly_report

    store = ExperimentLedger(tmp_path / "ledger")
    B.backfill(journal(tmp_path, [line(), line(ticker="NVDA")]), store)
    report = weekly_report(store, date(2026, 8, 16))
    assert report.backfilled == 2
    assert any("backfilled" in caveat for caveat in report.caveats)
    assert "backfilled" in report.markdown
