"""Signal grading v2 and the weekly report.

The three defects in v1 that this layer exists to fix each have a test:

1. discovery + deep on the same name on the same day is **one** observation;
2. grading is against **excess**, so beating the tape while trailing the market
   is a miss;
3. a source is scored against the **pool's own** forward return, because a
   momentum screen's picks drift up and "predicted up" is otherwise free.

And the rule that governs the report: no rate is printed below the minimum
sample. A test asserts that a four-observation week says INSUFFICIENT rather
than "75%".
"""

from datetime import date

import pytest

from tradingagent.evaluation import grading as G
from tradingagent.evaluation.ledger import DECISIONS, ExperimentLedger
from tradingagent.evaluation.report import (
    INSUFFICIENT,
    evidence_section,
    weekly_report,
)

RUN_DATE = date(2026, 8, 16)


def decision(ticker="AAA", *, date_="2026-06-01", stage="deep", readings=None, rating="Buy",
             seat_tiers=None, backfilled=False):
    return {
        "decision_id": f"{date_}:{ticker}:{stage}",
        "ticker": ticker,
        "date": date_,
        "stage": stage,
        "rating": rating,
        "confidence": "medium",
        "signal_readings": readings or {},
        "seat_tiers": seat_tiers or {"portfolio_manager": "deep"},
        "provenance": {"run_id": f"run-{date_}", "backfilled": backfilled},
    }


def outcome(ticker="AAA", *, date_="2026-06-01", stage="deep", excess=None, raw=None,
            sector_excess=None, **extra):
    excess = excess or {}
    horizons = {}
    for horizon, value in excess.items():
        horizons[str(horizon)] = {
            "session": "2026-06-08",
            "return_pct": (raw or {}).get(horizon, value),
            "excess_spy_pct": value,
            "excess_sector_pct": (sector_excess or {}).get(horizon, value),
        }
    row = {
        "decision_id": f"{date_}:{ticker}:{stage}",
        "ticker": ticker,
        "date": date_,
        "stage": stage,
        "horizons": horizons,
    }
    row.update(extra)
    return row


def candidate(ticker="AAA", *, date_="2026-06-01", selected=True, counterfactual=True):
    return {
        "candidate_id": f"run-{date_}:{ticker}",
        "ticker": ticker,
        "date": date_,
        "selected": selected,
        "counterfactual_selected": counterfactual,
    }


# --- clustering ---------------------------------------------------------------


def test_the_quick_take_and_the_deep_dive_are_one_observation_not_two():
    # v1 counted both journal lines, letting a source double its record by
    # being analysed twice — and doubling is exactly how a 20-sample threshold
    # gets cleared on 10 days of data.
    decisions = [
        decision(stage="discovery", readings={"reddit": 1}, rating="Watch"),
        decision(stage="deep", readings={"reddit": 1}, rating="Buy"),
    ]
    observations = G.cluster(decisions, [])
    assert len(observations) == 1
    assert observations[0].stages == ["deep", "discovery"]
    assert observations[0].rating == "Buy"  # the verdict that shipped


def test_the_same_ticker_on_two_days_is_two_observations():
    decisions = [decision(date_="2026-06-01"), decision(date_="2026-06-02")]
    assert len(G.cluster(decisions, [])) == 2


def test_the_options_overlay_is_not_folded_into_the_equity_call():
    decisions = [decision(stage="deep"), decision(stage="options")]
    observations = G.cluster(decisions, [])
    assert len(observations) == 1
    assert observations[0].stages == ["deep"]


def test_readings_from_both_stages_are_merged():
    decisions = [
        decision(stage="discovery", readings={"reddit": 1}),
        decision(stage="deep", readings={"insider": -1}),
    ]
    assert G.cluster(decisions, [])[0].readings == {"reddit": 1, "insider": -1}


# --- grading ------------------------------------------------------------------


def test_beating_the_tape_while_trailing_the_market_scores_as_a_miss():
    decisions = [decision(readings={"reddit": 1})]
    outcomes = [outcome(excess={5: -2.0}, raw={5: 3.0})]
    report = G.grade(decisions, outcomes, horizons=(5,))
    row = report.at("reddit", 5)
    assert row.samples == 1 and row.hits == 0


def test_a_move_inside_the_dead_band_is_neither_right_nor_wrong():
    # Counting sub-1% noise would drag every source towards a meaningless 50%.
    decisions = [decision(ticker="AAA", readings={"reddit": 1})]
    outcomes = [outcome(ticker="AAA", excess={5: 0.4})]
    row = G.grade(decisions, outcomes, horizons=(5,)).at("reddit", 5)
    assert row.samples == 0 and row.noise == 1


def test_a_source_that_abstained_is_not_scored_as_wrong():
    decisions = [decision(readings={"reddit": 0})]
    outcomes = [outcome(excess={5: 5.0})]
    row = G.grade(decisions, outcomes, horizons=(5,)).at("reddit", 5)
    assert row.samples == 0 and row.abstained == 1 and row.hits == 0


def test_an_unresolved_horizon_is_not_a_miss():
    decisions = [decision(readings={"reddit": 1})]
    outcomes = [outcome(excess={1: 4.0})]
    report = G.grade(decisions, outcomes, horizons=(1, 20))
    assert report.at("reddit", 1).samples == 1
    assert report.at("reddit", 20).samples == 0


def test_a_source_is_graded_at_every_horizon_separately():
    # Right at a day and wrong at a month is a real and useful finding that a
    # single 7-day window cannot express.
    decisions = [decision(readings={"news": 1})]
    outcomes = [outcome(excess={1: 3.0, 20: -4.0})]
    report = G.grade(decisions, outcomes, horizons=(1, 20))
    assert report.at("news", 1).hits == 1
    assert report.at("news", 20).hits == 0


def test_a_bearish_reading_is_right_when_the_name_underperforms():
    decisions = [decision(readings={"insider": -1})]
    outcomes = [outcome(excess={5: -6.0})]
    row = G.grade(decisions, outcomes, horizons=(5,)).at("insider", 5)
    assert row.hits == 1
    assert row.mean_excess == pytest.approx(6.0)  # direction × excess


def test_lift_is_measured_against_the_pool_not_against_zero():
    # Every name in this pool ran +5%: the screener found the momentum, not the
    # source. A source that called all three "up" is 100% accurate and adds
    # exactly nothing, and only the lift column says so.
    decisions = [
        decision(ticker=t, readings={"reddit": 1}) for t in ("AAA", "BBB", "CCC")
    ]
    outcomes = [outcome(ticker=t, excess={5: 5.0}) for t in ("AAA", "BBB", "CCC")]
    row = G.grade(decisions, outcomes, horizons=(5,)).at("reddit", 5)
    assert row.accuracy == 1.0
    assert row.mean_excess == pytest.approx(5.0)
    assert row.baseline_excess == pytest.approx(5.0)
    assert row.lift == pytest.approx(0.0)


def test_a_source_that_picks_the_winners_out_of_the_pool_shows_positive_lift():
    decisions = [
        decision(ticker="AAA", readings={"reddit": 1}),
        decision(ticker="BBB", readings={"reddit": 1}),
        decision(ticker="CCC", readings={"reddit": -1}),
    ]
    outcomes = [
        outcome(ticker="AAA", excess={5: 6.0}),
        outcome(ticker="BBB", excess={5: 4.0}),
        outcome(ticker="CCC", excess={5: -8.0}),
    ]
    row = G.grade(decisions, outcomes, horizons=(5,)).at("reddit", 5)
    assert row.baseline_excess == pytest.approx(0.667, abs=0.01)
    assert row.mean_excess == pytest.approx(6.0)
    assert row.lift > 0


def test_no_source_can_graduate_on_a_handful_of_observations():
    decisions = [decision(ticker=f"T{i}", readings={"reddit": 1}) for i in range(5)]
    outcomes = [outcome(ticker=f"T{i}", excess={5: 5.0}) for i in range(5)]
    row = G.grade(decisions, outcomes, horizons=(5,)).at("reddit", 5)
    assert not row.sufficient
    assert row.standing.startswith("SHADOW")
    assert row.max_adjustment == 0.0


def test_twenty_resolved_calls_is_where_the_ladder_starts():
    decisions = [decision(ticker=f"T{i}", readings={"reddit": 1}) for i in range(20)]
    outcomes = [outcome(ticker=f"T{i}", excess={5: 5.0}) for i in range(20)]
    row = G.grade(decisions, outcomes, horizons=(5,)).at("reddit", 5)
    assert row.sufficient
    assert row.standing.startswith("EXPERIMENTAL")
    assert row.max_adjustment == 1.0


# --- ratings ------------------------------------------------------------------


def test_hold_is_not_scored_as_a_directional_call():
    observations = G.cluster(
        [decision(ticker="AAA", rating="Hold"), decision(ticker="BBB", rating="Hold")],
        [outcome(ticker="AAA", excess={5: 9.0}), outcome(ticker="BBB", excess={5: -9.0})],
    )
    records = G.rating_records(observations, horizons=(5,))
    hold = next(r for r in records if r.rating == "Hold")
    assert hold.n == 2 and hold.hits == 0


def test_a_buy_that_beat_the_market_is_a_hit_and_one_that_trailed_is_not():
    observations = G.cluster(
        [decision(ticker="AAA"), decision(ticker="BBB")],
        [outcome(ticker="AAA", excess={5: 3.0}), outcome(ticker="BBB", excess={5: -3.0})],
    )
    buy = next(r for r in G.rating_records(observations, horizons=(5,)) if r.rating == "Buy")
    assert buy.n == 2 and buy.hits == 1
    assert buy.mean_excess == pytest.approx(0.0)


# --- control vs treatment -----------------------------------------------------


def test_the_two_shortlists_are_compared_on_the_names_they_disagree_about():
    candidates = [
        candidate("AAA", selected=True, counterfactual=True),
        candidate("BBB", selected=True, counterfactual=False),
        candidate("CCC", selected=False, counterfactual=True),
    ]
    outcomes = [
        outcome(ticker="AAA", excess={5: 2.0}),
        outcome(ticker="BBB", excess={5: -4.0}),
        outcome(ticker="CCC", excess={5: 6.0}),
    ]
    comparison = G.compare_selection(candidates, outcomes, horizons=(5,))[0]
    assert comparison.control_n == 2 and comparison.treatment_n == 2
    assert comparison.control_excess == pytest.approx(-1.0)
    assert comparison.treatment_excess == pytest.approx(4.0)
    assert comparison.difference == pytest.approx(5.0)
    assert comparison.disputed == ["BBB", "CCC"]


def test_identical_shortlists_leave_nothing_to_compare():
    candidates = [candidate("AAA"), candidate("BBB")]
    outcomes = [outcome(ticker="AAA", excess={5: 2.0}), outcome(ticker="BBB", excess={5: 2.0})]
    comparison = G.compare_selection(candidates, outcomes, horizons=(5,))[0]
    assert comparison.disputed == []
    assert comparison.difference == pytest.approx(0.0)
    assert not comparison.sufficient  # two names is not an experiment


# --- the weekly report --------------------------------------------------------


def ledger_with(tmp_path, decisions=(), outcomes=(), candidates=()):
    store = ExperimentLedger(tmp_path / "ledger")
    store.path(DECISIONS).parent.mkdir(parents=True, exist_ok=True)
    for stream, rows in (
        ("decisions.jsonl", decisions),
        ("outcomes.jsonl", outcomes),
        ("candidates.jsonl", candidates),
    ):
        if rows:
            import json

            with store.path(stream).open("a") as handle:
                for row in rows:
                    handle.write(json.dumps(row) + "\n")
    return store


def test_an_empty_ledger_produces_a_correct_report_rather_than_a_crash(tmp_path):
    report = weekly_report(ExperimentLedger(tmp_path / "ledger"), RUN_DATE)
    assert report.week == "2026-33"
    assert report.resolved == 0
    assert "nothing to evaluate" in report.markdown
    assert INSUFFICIENT in report.markdown
    assert "Not financial advice" in report.markdown
    # Nothing that looks like a performance claim.
    assert "%" not in report.markdown.split("## 1.")[0]


def test_a_thin_week_reports_counts_and_refuses_to_report_rates(tmp_path):
    decisions = [decision(ticker=f"T{i}", readings={"reddit": 1}) for i in range(4)]
    outcomes = [outcome(ticker=f"T{i}", excess={5: 5.0}) for i in range(4)]
    store = ledger_with(tmp_path, decisions=decisions, outcomes=outcomes)
    report = weekly_report(store, RUN_DATE)
    assert report.resolved == 4
    assert not report.sufficient
    assert "INSUFFICIENT DATA" in report.markdown
    # The accuracy is 100% and it must not be printed anywhere.
    assert "100%" not in report.markdown
    assert any("20-observation minimum" in caveat for caveat in report.caveats)


def test_backfilled_rows_are_labelled_as_evidence_of_a_lower_grade(tmp_path):
    store = ledger_with(
        tmp_path,
        decisions=[decision(ticker="AAA", backfilled=True), decision(ticker="BBB")],
    )
    report = weekly_report(store, RUN_DATE)
    assert report.backfilled == 1
    assert any("backfilled" in caveat for caveat in report.caveats)


def test_the_report_counts_which_tier_wrote_each_seat(tmp_path):
    store = ledger_with(
        tmp_path,
        decisions=[
            decision(ticker="AAA", seat_tiers={"portfolio_manager": "deep"}),
            decision(ticker="BBB", seat_tiers={"portfolio_manager": "smart"}),
        ],
    )
    report = weekly_report(store, RUN_DATE)
    assert report.tiers["portfolio_manager"] == {"deep": 1, "smart": 1}


def test_the_friday_email_says_insufficient_before_it_says_anything_else(tmp_path):
    store = ledger_with(tmp_path, decisions=[decision(ticker="AAA")])
    evidence = evidence_section(weekly_report(store, RUN_DATE))
    assert "Evidence so far" in evidence
    assert "INSUFFICIENT DATA" in evidence
    assert "evaluation/2026-33.md" in evidence
    assert len(evidence.splitlines()) < 15  # it is an email, not the report


def test_the_email_carries_the_numbers_once_they_exist(tmp_path):
    decisions = [decision(ticker=f"T{i}", readings={"reddit": 1}) for i in range(3)]
    outcomes = [outcome(ticker=f"T{i}", excess={20: 4.0}) for i in range(3)]
    store = ledger_with(tmp_path, decisions=decisions, outcomes=outcomes)
    evidence = evidence_section(weekly_report(store, RUN_DATE))
    assert "3 resolved observation" in evidence
    assert "reddit" in evidence
    assert INSUFFICIENT in evidence  # three is still not twenty
