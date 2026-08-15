"""The strategist call, the rendered sections, and the journal line.

The screen is tested in tests/test_options_strategies.py; what is checked here
is everything that happens to a plan afterwards — the one guardrail that
matters (a contract the model invented never reaches the page), and the promise
that a skipped ticker is still visibly a decision rather than a gap.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from tradingagent.data.option_chain import OptionQuote
from tradingagent.data.validate import DegradedTracker
from tradingagent.journal import entries_from_options
from tradingagent.llm import LLMError
from tradingagent.options.black_scholes import DAYS_PER_YEAR, bs_price
from tradingagent.options.strategies import (
    CSP,
    LevelAnchor,
    StrategyRules,
    price_candidate,
    score_candidate,
)
from tradingagent.options.strategist import (
    OptionsPlan,
    candidate_table,
    run_options_strategist,
    score_detail,
)
from tradingagent.pipeline.schemas import OptionsRecommendation
from tradingagent.report.options import brief_row, render_options_index, render_options_section

AS_OF = date(2026, 8, 14)
EXPIRY = date(2026, 9, 18)
DTE = (EXPIRY - AS_OF).days
SPOT = 100.0


def candidate(strike: float = 95.0, iv: float = 0.30):
    fair = bs_price(SPOT, strike, DTE / DAYS_PER_YEAR, 0.045, iv, "put")
    quote = OptionQuote(
        symbol=f"XYZ260918P{int(strike * 1000):08d}",
        underlying="XYZ",
        right="put",
        strike=strike,
        expiry=EXPIRY,
        dte=DTE,
        bid=round(fair * 0.98, 2),
        ask=round(fair * 1.02, 2),
        quote_at=datetime(2026, 8, 14, 19, 55, tzinfo=timezone.utc),
        open_interest=500,
        open_interest_date=date(2026, 8, 13),
    )
    scored = price_candidate(
        quote,
        strategy=CSP,
        spot=SPOT,
        risk_free_rate=0.045,
        as_of=AS_OF,
        anchor=LevelAnchor(96.0, "50-day SMA"),
    )
    # The screen scores before anything downstream sees a candidate, and the
    # score notes are what the strategist prompt shows.
    score_candidate(scored, StrategyRules())
    return scored


class FakeGateway:
    """Returns a canned recommendation, or raises, and records the prompt."""

    def __init__(self, contract="XYZ260918P00095000", raises=False):
        self.contract = contract
        self.raises = raises
        self.prompts: list[str] = []

    def complete(self, prompt, *, tier, schema):
        self.prompts.append(prompt)
        assert tier == "smart", "the strategist's tier is fixed by CLAUDE.md's budget"
        if self.raises:
            raise LLMError("upstream refused")
        return OptionsRecommendation(
            recommended_contract=self.contract,
            conviction="M",
            rationale="The 95 strike sits on the 50-day.",
            entry_note="Work the mid.",
            assignment_view="Assignment is the entry.",
            risk_note="A gap through the level.",
        )


def plan_with_candidates():
    return OptionsPlan(symbol="XYZ", strategy=CSP, candidates=[candidate(95.0), candidate(92.0)])


# -- the strategist call ----------------------------------------------------
def test_a_named_contract_from_the_table_becomes_the_pick():
    plan = run_options_strategist(
        FakeGateway(),
        plan_with_candidates(),
        DegradedTracker(),
        name="Example",
        verdict="- Rating: **Overweight**",
        price_context="- Last close: $100.00",
        data_quality="- indicative feed",
    )
    assert plan.chosen is not None and plan.chosen.strike == 95.0
    assert plan.error is None and not plan.degraded


def test_an_invented_contract_is_refused_rather_than_printed():
    degraded = DegradedTracker()
    plan = run_options_strategist(
        FakeGateway(contract="XYZ260918P00093500"),  # never screened
        plan_with_candidates(),
        degraded,
        name="Example",
        verdict="",
        price_context="",
        data_quality="",
    )
    assert plan.chosen is None
    assert plan.recommendation is None, "a hallucinated strike must not carry its rationale through"
    assert "not one of the screened candidates" in plan.error
    assert plan.degraded and degraded.entries


def test_none_is_a_legitimate_answer_and_does_not_degrade():
    plan = run_options_strategist(
        FakeGateway(contract="none"),
        plan_with_candidates(),
        DegradedTracker(),
        name="Example",
        verdict="",
        price_context="",
        data_quality="",
    )
    assert plan.chosen is None and plan.error is None
    assert plan.recommendation is not None


def test_an_llm_failure_degrades_the_ticker_and_keeps_the_screen():
    degraded = DegradedTracker()
    plan = run_options_strategist(
        FakeGateway(raises=True),
        plan_with_candidates(),
        degraded,
        name="Example",
        verdict="",
        price_context="",
        data_quality="",
    )
    assert plan.error and plan.degraded and degraded.entries
    assert len(plan.candidates) == 2, "the screened numbers survive a failed call"


def test_no_candidates_means_no_call_at_all():
    gateway = FakeGateway()
    plan = run_options_strategist(
        gateway,
        OptionsPlan(symbol="XYZ", strategy=CSP),
        DegradedTracker(),
        name="Example",
        verdict="",
        price_context="",
        data_quality="",
    )
    assert gateway.prompts == [], "there is no question to ask when there is nothing to choose"
    assert plan.recommendation is None and plan.error is None


def test_the_prompt_carries_every_number_the_model_may_cite():
    gateway = FakeGateway()
    plan = plan_with_candidates()
    run_options_strategist(
        gateway,
        plan,
        DegradedTracker(),
        name="Example Corp",
        verdict="- Rating: **Overweight** (confidence H)",
        price_context="- Last close: $100.00",
        data_quality="- the free feed supplies no greeks",
    )
    prompt = gateway.prompts[0]
    for fragment in ("XYZ", "Example Corp", "Overweight", "$100.00", "no greeks", "cash-secured put"):
        assert fragment in prompt
    for c in plan.candidates:
        assert c.symbol in prompt


# -- rendering --------------------------------------------------------------
def test_the_candidate_table_shows_a_missing_field_as_a_dash_not_a_zero():
    c = candidate()
    c.quote.open_interest = None
    assert "| — |" in candidate_table([c])


def test_score_detail_explains_every_component():
    detail = score_detail([candidate()])
    assert detail.count("  - ") >= 5


def test_a_skipped_ticker_still_occupies_a_brief_row():
    plan = OptionsPlan(symbol="VZ", strategy=None, skipped="Underweight — no overlay proposed")
    row = brief_row(plan)
    assert row.startswith("| VZ |") and "no overlay proposed" in row


def test_the_brief_index_counts_the_overlays_it_actually_proposes():
    picked = plan_with_candidates()
    picked.chosen = picked.candidates[0]
    index = render_options_index([picked, OptionsPlan(symbol="VZ", strategy=None, skipped="Sell")])
    assert "1 of 2 deep-analysed names carry a proposed overlay" in index
    assert "| VZ |" in index


def test_the_deep_section_prints_the_screen_even_when_the_strategist_failed():
    plan = plan_with_candidates()
    plan.error = "upstream refused"
    body = render_options_section(plan, data_notes=["quotes are from Friday's close"])
    assert "DEGRADED" in body and "upstream refused" in body
    assert "### Screened candidates" in body
    assert "Friday's close" in body


def test_the_deep_section_marks_the_recommended_contract():
    plan = plan_with_candidates()
    plan.chosen = plan.candidates[0]
    plan.recommendation = OptionsRecommendation(
        recommended_contract=plan.chosen.symbol,
        conviction="M",
        rationale="On the 50-day.",
        entry_note="Work the mid.",
        assignment_view="Happy to own it.",
        risk_note="A gap.",
    )
    body = render_options_section(plan)
    assert f"### Recommended: `{plan.chosen.symbol}`" in body
    assert body.count("**recommended** —") == 1


def test_a_rejection_tally_survives_into_the_section():
    plan = OptionsPlan(symbol="XYZ", strategy=CSP, rejected=["5 × credit under the $0.10 floor"])
    body = render_options_section(plan)
    assert "No strike passed the screen" in body
    assert "5 × credit under the $0.10 floor" in body


# -- journal ----------------------------------------------------------------
def test_the_journal_records_the_basis_and_the_alternatives_it_beat():
    plan = plan_with_candidates()
    plan.chosen = plan.candidates[0]
    plan.recommendation = OptionsRecommendation(
        recommended_contract=plan.chosen.symbol,
        conviction="H",
        rationale="On the 50-day.",
        entry_note="Work the mid.",
        assignment_view="Happy to own it.",
        risk_note="A gap.",
    )
    [entry] = entries_from_options([plan], AS_OF, report_dir="reports/2026-08-14/deep")
    row = entry.to_dict()
    assert row["ticker"] == "XYZ" and row["verdict"] == CSP
    assert row["target"] == 95.0 and row["confidence"] == "H"
    assert row["stage"] == "options"
    basis = row["options"]["recommended"]
    for key in ("delta", "implied_vol_pct", "annualized_yield_pct", "breakeven", "anchor_level"):
        assert key in basis, f"{key} is part of why the strike was chosen"
    assert basis["greeks_source"].startswith("computed")
    assert len(row["options"]["alternatives"]) == 1
    assert "finnhub:earnings-calendar" in row["signal_sources"]


def test_a_skipped_ticker_is_journalled_as_a_decision():
    plan = OptionsPlan(symbol="VZ", strategy=None, skipped="Underweight — no overlay proposed")
    [entry] = entries_from_options([plan], AS_OF)
    row = entry.to_dict()
    assert row["verdict"] == "no overlay" and row["target"] is None
    assert row["options"]["skipped"].startswith("Underweight")


def test_an_empty_screen_is_journalled_with_what_was_rejected():
    plan = OptionsPlan(symbol="UNP", strategy=CSP, rejected=["3 × open interest under 20"])
    [entry] = entries_from_options([plan], AS_OF)
    payload = entry.to_dict()["options"]
    assert payload["recommended"] is None
    assert payload["rejected"] == ["3 × open interest under 20"]


@pytest.mark.parametrize("symbol", ["  xyz260918p00095000 ", "`XYZ260918P00095000`"])
def test_a_sloppily_quoted_contract_still_resolves(symbol):
    plan = run_options_strategist(
        FakeGateway(contract=symbol),
        plan_with_candidates(),
        DegradedTracker(),
        name="Example",
        verdict="",
        price_context="",
        data_quality="",
    )
    assert plan.chosen is not None and plan.error is None
