"""The arithmetic the models are no longer allowed to do.

Every number in section 4 is computed in :mod:`tradingagent.pipeline.trade_plan`
from the run's snapshot. These tests pin the assertions that decide whether a
plan is publishable at all, and the regression that started it: a report quoting
"2.5% risk" over a plan that risked 3.6%.
"""

from datetime import date, datetime, timezone

import pandas as pd

from tradingagent.data.indicators import compute_indicators
from tradingagent.pipeline.context import QueuedTicker
from tradingagent.pipeline.evidence import Evidence
from tradingagent.pipeline.schemas import TraderProposal
from tradingagent.pipeline.trade_plan import (
    FLAT,
    MAX_POSITION_PCT,
    MAX_RISK_PCT,
    NO_TRADE,
    PLAN,
    UNPRICED,
    build_trade_plan,
    direction_for,
    plan_texts,
    quoted_figure_corrections,
    quoted_figure_mismatches,
)
from tradingagent.snapshot import Observation

MARKET = date(2026, 8, 14)
SNAPSHOT = "snap-2026-08-14-abc123"


def evidence(last=100.0, priced=True, bars=True, symbol="TST", snapshot_id=SNAPSHOT, atr=None):
    """An evidence pack carrying one snapshot-stamped close."""
    ev = Evidence(
        queued=QueuedTicker(symbol=symbol, name="Test Co", sector="Utilities", priority=7),
        run_date=MARKET,
        market_context="- Breadth 65/100 (Healthy).",
        macro_note="- none scheduled",
        snapshot_id=snapshot_id,
        market_as_of=MARKET,
    )
    if bars:
        # A flat-ish series so ATR(14) is small and predictable next to `last`.
        closes = [last - 5 + i * 0.02 for i in range(260)]
        closes[-1] = last
        frame = pd.DataFrame(
            {
                "Open": closes,
                "High": [c * 1.01 for c in closes],
                "Low": [c * 0.99 for c in closes],
                "Close": closes,
                "Volume": [2_000_000] * len(closes),
            },
            index=pd.date_range("2024-01-01", periods=len(closes), freq="B"),
        )
        ev.indicators = compute_indicators(symbol, frame)
    if atr is not None:
        # Pinned so the noise floor in the failure message is a fixed number.
        for indicator in ev.indicators.indicators:
            if indicator.key == "atr":
                indicator.value = atr
    if priced:
        ev.price_observation = Observation(
            value=last,
            source="snapshot bars",
            observed_at=datetime(2026, 8, 14, 21, 5, tzinfo=timezone.utc),
            effective_at=MARKET,
            snapshot_id=snapshot_id,
        )
    return ev


def proposal(**kwargs):
    base = dict(
        action="Buy",
        reasoning="The plan says Overweight and the base is intact.",
        entry_condition="Only on a close back above the 50-day.",
        entry_type="market",
        entry_level=None,
        invalidation_type="level",
        invalidation_level=96.0,
    )
    base.update(kwargs)
    return TraderProposal(**base)


class _Decision:
    """The prose fields the corrections pass reads back."""

    def __init__(self, **kwargs):
        self.executive_summary = kwargs.get("executive_summary", "")
        self.investment_thesis = kwargs.get("investment_thesis", "")
        self.risk_ruling = kwargs.get("risk_ruling", "")
        self.invalidation = kwargs.get("invalidation", "")


# --- the happy path -------------------------------------------------------


def test_the_plan_is_arithmetic_on_the_snapshot_close_not_a_model_s_numbers():
    plan = build_trade_plan(evidence(last=100.0), proposal(), "Overweight", target=112.0)

    assert plan.status == PLAN and plan.actionable
    assert plan.entry == 100.0 and plan.stop == 96.0
    assert plan.risk_per_share == 4.0
    assert round(plan.risk_pct, 2) == 4.0
    assert round(plan.reward_risk, 2) == 3.0
    # 0.5% of the portfolio at risk over a 4% stop distance is 12.5%, capped.
    assert plan.size_pct == MAX_POSITION_PCT and "capped" in plan.size_basis
    assert plan.snapshot_id == SNAPSHOT and plan.market_as_of == MARKET
    assert "2026-08-14" in plan.entry_basis
    assert SNAPSHOT in plan.note()


def test_the_table_shows_every_computed_figure_with_the_rule_that_produced_it():
    table = build_trade_plan(evidence(), proposal(), "Buy", target=112.0).table()
    for row in ("Entry", "Stop", "Risk / share", "Risk", "Target", "Reward : risk", "Size cap"):
        assert row in table
    assert "entry − stop" in table and "risk per share ÷ entry" in table


def test_a_tighter_stop_earns_a_bigger_size_until_the_position_cap_bites():
    wide = build_trade_plan(evidence(last=100.0), proposal(invalidation_level=90.0), "Buy",
                            target=140.0)
    tight = build_trade_plan(evidence(last=100.0), proposal(invalidation_level=98.0), "Buy",
                             target=112.0)
    assert wide.size_pct < tight.size_pct
    assert tight.size_pct == MAX_POSITION_PCT and "capped" in tight.size_basis


def test_direction_comes_from_the_rating_and_hold_is_not_a_trade():
    assert direction_for("Overweight") == "long" and direction_for("Buy") == "long"
    assert direction_for("Underweight") == "short" and direction_for("Sell") == "short"
    assert direction_for("Hold") == "flat" and direction_for(None) == "flat"

    plan = build_trade_plan(evidence(), proposal(action="Hold"), "Hold", target=112.0)
    assert plan.status == FLAT and not plan.actionable
    assert plan.stop is None and plan.size_pct is None


def test_a_short_prices_its_stop_above_the_entry_and_its_target_below():
    plan = build_trade_plan(
        evidence(last=100.0), proposal(action="Sell", invalidation_level=104.0),
        "Underweight", target=88.0,
    )
    assert plan.status == PLAN
    assert plan.stop == 104.0 and round(plan.risk_pct, 1) == 4.0
    assert round(plan.reward_risk, 1) == 3.0


# --- the assertions -------------------------------------------------------


def test_a_stop_on_the_winning_side_of_the_entry_is_not_published_as_a_plan():
    plan = build_trade_plan(evidence(last=100.0), proposal(invalidation_level=104.0), "Buy",
                            target=112.0)
    assert plan.status == NO_TRADE and not plan.actionable
    assert any("wrong side of the entry" in f for f in plan.failures)


def test_risk_past_the_cap_is_a_no_trade_however_good_the_thesis():
    plan = build_trade_plan(evidence(last=100.0), proposal(invalidation_level=88.0), "Buy",
                            target=160.0)
    assert plan.status == NO_TRADE
    assert any("past the 8% cap" in f for f in plan.failures)


def test_the_wmb_regression_a_three_cent_stop_is_not_an_invalidation_level():
    """WMB, 2026-08-16: a $73.17 stop under a $73.20 entry.

    Verbatim from the shipped report. Every assertion in place at the time was
    satisfied — the stop was on the losing side, 0.04% of risk sat well inside
    the 8% cap, and the $82 target came out at 293× reward:risk — so the plan
    published, and because size is the risk budget divided by the stop distance,
    it published at the maximum position. All three risk seats then spent their
    turn arguing that the stop was an artifact. The arithmetic should have said
    so first.
    """
    plan = build_trade_plan(
        evidence(last=75.20, atr=1.85),
        proposal(entry_type="pullback", entry_level=73.20,
                 invalidation_type="moving_average", invalidation_level=73.17),
        "Overweight",
        target=82.00,
    )

    assert plan.entry == 73.20 and plan.stop == 73.17
    assert round(plan.risk_per_share, 2) == 0.03
    assert plan.status == NO_TRADE and not plan.actionable
    floor = [f for f in plan.failures if "minimum" in f]
    assert len(floor) == 1, plan.failures
    assert "$0.03 from the $73.20 entry (0.04%)" in floor[0]
    assert "inside the $0.93 minimum (0.5 × ATR(14) $1.85)" in floor[0]
    # And nothing else objected: this is the whole point of the regression.
    assert plan.failures == floor
    assert plan.risk_pct < MAX_RISK_PCT and plan.reward_risk > 200


def test_the_noise_floor_falls_back_to_a_flat_fraction_when_there_is_no_atr():
    plan = build_trade_plan(
        evidence(last=100.0, bars=False), proposal(invalidation_level=99.8), "Buy", target=140.0
    )
    assert plan.status == NO_TRADE
    assert any("0.3% of entry, no ATR available" in f for f in plan.failures)


def test_a_stop_outside_the_noise_floor_is_published_as_written():
    plan = build_trade_plan(
        evidence(last=100.0, atr=2.0), proposal(invalidation_level=98.9), "Buy", target=112.0
    )
    assert plan.status == PLAN and round(plan.risk_per_share, 2) == 1.10
    assert plan.size_pct == MAX_POSITION_PCT  # tight, but tight for a real reason


def test_the_table_states_the_floor_next_to_the_cap():
    table = build_trade_plan(evidence(), proposal(), "Buy", target=112.0).table()
    assert "cap 8%, floor 0.3% or 0.5 × ATR(14), whichever is wider" in table


def test_a_target_that_does_not_pay_for_the_stop_is_a_no_trade():
    plan = build_trade_plan(evidence(last=100.0), proposal(invalidation_level=96.0), "Buy",
                            target=104.0)
    assert plan.status == NO_TRADE
    assert any("below the 1.5× floor" in f for f in plan.failures)


def test_a_target_on_the_wrong_side_of_the_entry_says_so_in_the_failure():
    plan = build_trade_plan(evidence(last=100.0), proposal(invalidation_level=96.0), "Buy",
                            target=94.0)
    assert plan.status == NO_TRADE
    assert any("wrong side of the entry" in f for f in plan.failures)


def test_no_target_is_a_warning_not_a_rejection_because_targets_are_soft():
    plan = build_trade_plan(evidence(), proposal(), "Buy", target=None)
    assert plan.status == PLAN
    assert any("reward:risk floor could not be checked" in w for w in plan.warnings)


def test_pricing_evidence_from_one_snapshot_against_another_is_refused():
    class _OtherSnapshot:
        snapshot_id = "snap-2026-08-15-def456"

    plan = build_trade_plan(evidence(), proposal(), "Buy", target=112.0,
                            snapshot=_OtherSnapshot())
    assert plan.status == NO_TRADE
    assert any("mixed snapshots" in f for f in plan.failures)


def test_a_ticker_with_no_usable_close_gets_no_arithmetic_at_all():
    plan = build_trade_plan(evidence(priced=False, bars=False), proposal(), "Buy", target=112.0)
    assert plan.status == UNPRICED
    assert plan.entry is None and plan.risk_pct is None


def test_without_an_invalidation_level_the_stop_falls_back_to_two_atr():
    plan = build_trade_plan(
        evidence(last=100.0), proposal(invalidation_type="atr", invalidation_level=None),
        "Buy", target=140.0,
    )
    assert plan.status == PLAN
    assert plan.stop is not None and plan.stop < plan.entry
    assert "2 × ATR(14)" in plan.stop_basis


def test_a_level_from_another_month_is_ignored_in_favour_of_the_close():
    entry_drift = build_trade_plan(
        evidence(last=100.0), proposal(entry_type="pullback", entry_level=60.0), "Buy",
        target=112.0,
    )
    assert entry_drift.entry == 100.0
    assert any("from the close" in w for w in entry_drift.warnings)

    stop_drift = build_trade_plan(evidence(last=100.0), proposal(invalidation_level=40.0), "Buy",
                                  target=140.0)
    assert "2 × ATR(14)" in stop_drift.stop_basis
    assert any("an ATR stop was used instead" in w for w in stop_drift.warnings)


def test_a_pullback_entry_above_the_close_is_a_different_trade_and_is_not_used():
    plan = build_trade_plan(
        evidence(last=100.0), proposal(entry_type="pullback", entry_level=103.0), "Buy",
        target=112.0,
    )
    assert plan.entry == 100.0
    assert any("wrong side of the" in w for w in plan.warnings)


def test_a_breakout_entry_above_the_close_is_honoured_as_written():
    plan = build_trade_plan(
        evidence(last=100.0), proposal(entry_type="breakout", entry_level=103.0,
                                       invalidation_level=99.0),
        "Buy", target=120.0,
    )
    assert plan.entry == 103.0 and "breakout entry proposed by the trader" in plan.entry_basis
    assert round(plan.risk_pct, 2) == round(4 / 103 * 100, 2)


# --- reading the prose back ----------------------------------------------


def test_the_stz_regression_a_quoted_risk_figure_that_the_plan_does_not_support():
    """STZ: the prose said 2.5%; entry-to-stop was 3.6%.

    A human sizing off "2.5%" would have taken 44% more risk than they thought.
    The paragraph is left as the model wrote it and the disagreement is printed.
    """
    plan = build_trade_plan(
        evidence(last=136.35), proposal(invalidation_level=131.4), "Buy", target=152.0
    )
    assert round(plan.risk_pct, 1) == 3.6

    corrections = quoted_figure_corrections(
        plan,
        plan_texts(
            proposal(reasoning="Enter here, risking 2.5% to the invalidation."),
            _Decision(executive_summary="A 2.5% risk entry against the August base."),
        ),
    )
    assert corrections, "the 2.5% claim should have been caught"
    assert all("3.6%" in c for c in corrections)
    assert any("The trader's reasoning says 2.5% risk" in c for c in corrections)
    assert any("The verdict summary" in c for c in corrections)


def test_a_quoted_figure_that_matches_the_computed_plan_is_left_alone():
    plan = build_trade_plan(evidence(last=100.0), proposal(), "Buy", target=112.0)
    corrections = quoted_figure_corrections(
        plan,
        plan_texts(
            proposal(reasoning="Risking 4.0% to a stop at $96.00 from an entry of $100.00."),
            _Decision(investment_thesis="Stop $96.00; 4% of risk for a 3R setup."),
        ),
    )
    assert corrections == []


def test_a_quoted_stop_price_that_is_not_the_computed_stop_is_flagged():
    plan = build_trade_plan(evidence(last=100.0), proposal(), "Buy", target=112.0)
    corrections = quoted_figure_corrections(
        plan, {"The thesis": "A close below the stop at $92.50 ends the trade."}
    )
    assert len(corrections) == 1
    assert "quotes a stop of $92.50" in corrections[0] and "$96.00" in corrections[0]


def test_the_same_disagreement_reaching_two_roles_is_reported_once():
    plan = build_trade_plan(evidence(last=100.0), proposal(), "Buy", target=112.0)
    line = "Risking 2.0% here."
    corrections = quoted_figure_corrections(
        plan, {"The trader's reasoning": line, "The thesis": line}
    )
    assert len(corrections) == 2  # one per role, but each said once
    assert len(set(corrections)) == 2


def test_an_unpriced_plan_makes_no_claims_about_the_prose():
    plan = build_trade_plan(evidence(priced=False, bars=False), proposal(), "Buy")
    assert quoted_figure_corrections(plan, {"The thesis": "Risking 2.5%."}) == []


def test_a_number_that_is_not_a_level_for_this_ticker_is_not_read_as_one():
    """WMB's risk ruling: "the $73.17 stop … manufactures a $0.03 risk-per-share".

    The reader-back flagged $0.03 as a quoted stop against a computed $73.17 —
    right regex, wrong noun. Three cents is not a price level for a $73 stock,
    and under the tightened policy that false positive would have cost a
    re-prompt and marked a correct paragraph DEGRADED.
    """
    plan = build_trade_plan(
        evidence(last=75.20, atr=1.85),
        proposal(entry_type="pullback", entry_level=73.20, invalidation_level=71.50),
        "Overweight", target=82.00,
    )
    assert plan.status == PLAN
    text = ("The stop at $73.17 is an artifact — a $0.03 risk/share off a rounding error.")
    quoted = [m.quoted for m in quoted_figure_mismatches(plan, {"The risk ruling": text})]
    assert quoted == [73.17], "the level is the disagreement; the three cents is not a level"


def test_a_gap_wider_than_one_percent_of_entry_is_material_whatever_it_measures():
    plan = build_trade_plan(evidence(last=100.0), proposal(), "Buy", target=112.0)
    quoted = quoted_figure_mismatches(
        plan,
        {
            "The verdict summary": "Stop at $94.50.",       # 1.5% of entry — material
            "The thesis": "Stop at $95.10.",                # 0.9% — a correction only
            "The risk ruling": "Risking 2.0% to the stop.",  # 2.0 points of entry — material
            "The invalidation line": "Risking 4.4% to the stop.",  # 0.4 points — a correction
        },
    )
    material = {m.label for m in quoted if m.material}
    assert material == {"The verdict summary", "The risk ruling"}
    assert len(quoted) == 4, "everything is still reported; only the response differs"


def test_the_journal_records_the_computed_plan_not_the_narrative():
    plan = build_trade_plan(evidence(last=100.0), proposal(), "Buy", target=112.0)
    payload = plan.journal_payload()
    assert payload["status"] == PLAN and payload["direction"] == "long"
    assert payload["entry"] == 100.0 and payload["stop"] == 96.0
    assert payload["risk_pct"] == 4.0 and payload["reward_risk"] == 3.0
    assert payload["snapshot_id"] == SNAPSHOT and payload["failures"] == []
