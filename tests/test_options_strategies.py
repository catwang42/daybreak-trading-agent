"""CSP and covered-call selection — the decisions, not the plumbing.

Every fixture is built from a Black-Scholes price at a known IV, so the
delta the module recovers is checkable against the delta that priced it. The
live-chain shapes (one-sided books, missing OI) are covered in
tests/test_option_chain.py; what matters here is which strike wins and why.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from tradingagent.data.option_chain import ChainSlice, OptionQuote
from tradingagent.options.black_scholes import DAYS_PER_YEAR, bs_price
from tradingagent.options.strategies import (
    CC,
    CSP,
    LevelAnchor,
    StrategyRules,
    build_candidates,
    hard_filters,
    price_candidate,
    resistance_anchor,
    score_candidate,
    skip_reason,
    strategy_for,
    support_anchor,
)

AS_OF = date(2026, 8, 14)
EXPIRY = date(2026, 9, 18)
DTE = (EXPIRY - AS_OF).days  # 35
RATE = 0.045
SPOT = 100.0


def synthetic(strike: float, right: str, iv: float = 0.30, *, oi: int = 500, spread: float = 0.04):
    """A quote priced at a known IV, with a tight two-sided book around it."""
    fair = bs_price(SPOT, strike, DTE / DAYS_PER_YEAR, RATE, iv, right)
    half = fair * spread / 2
    return OptionQuote(
        symbol=f"XYZ{EXPIRY:%y%m%d}{'P' if right == 'put' else 'C'}{int(strike * 1000):08d}",
        underlying="XYZ",
        right=right,
        strike=strike,
        expiry=EXPIRY,
        dte=DTE,
        bid=round(fair - half, 2),
        ask=round(fair + half, 2),
        quote_at=datetime(2026, 8, 14, 19, 55, tzinfo=timezone.utc),
        open_interest=oi,
        open_interest_date=date(2026, 8, 13),
    )


def priced(strike: float, right: str, **kw):
    strategy = CSP if right == "put" else CC
    anchor = kw.pop("anchor", LevelAnchor(None, "none"))
    return price_candidate(
        synthetic(strike, right, **kw),
        strategy=strategy,
        spot=SPOT,
        risk_free_rate=RATE,
        as_of=AS_OF,
        anchor=anchor,
    )


# -- strategy selection -----------------------------------------------------
@pytest.mark.parametrize("rating", ["Buy", "Overweight"])
def test_bullish_verdicts_get_a_cash_secured_put(rating):
    assert strategy_for(rating) == CSP


def test_hold_gets_a_covered_call():
    assert strategy_for("Hold") == CC


@pytest.mark.parametrize("rating", ["Underweight", "Sell"])
def test_negative_verdicts_get_no_overlay_and_a_stated_reason(rating):
    assert strategy_for(rating) is None
    assert "no overlay proposed" in skip_reason(rating)


# -- level anchoring --------------------------------------------------------
def test_support_anchor_takes_the_nearest_level_below_spot():
    levels = {"50-day SMA": 96.0, "Bollinger lower band": 91.0, "200-day SMA": 104.0}
    anchor = support_anchor(levels, SPOT)
    assert (anchor.price, anchor.label) == (96.0, "50-day SMA")


def test_resistance_anchor_takes_the_nearest_level_above_spot():
    levels = {"price target": 112.0, "Bollinger upper band": 106.0, "50-day SMA": 96.0}
    anchor = resistance_anchor(levels, SPOT)
    assert (anchor.price, anchor.label) == (106.0, "Bollinger upper band")


def test_anchors_report_honestly_when_there_is_no_level():
    assert support_anchor({"50-day SMA": 104.0}, SPOT).price is None
    assert resistance_anchor({"50-day SMA": 96.0}, SPOT).price is None


# -- pricing ----------------------------------------------------------------
def test_recovered_delta_matches_the_iv_that_priced_the_contract():
    """The whole selection rests on a delta we compute ourselves; this is that check."""
    candidate = priced(95.0, "put", iv=0.30, spread=0.0)
    assert candidate is not None
    assert candidate.iv == pytest.approx(0.30, abs=0.02)
    # 5% out of the money, 35 days, 30 vol — the 0.25-delta region we target.
    assert candidate.delta == pytest.approx(-0.246, abs=0.02)


def test_csp_yield_is_measured_against_the_cash_actually_posted():
    candidate = priced(90.0, "put", spread=0.0)
    credit = candidate.credit
    assert candidate.collateral == pytest.approx((90.0 - credit) * 100)
    assert candidate.breakeven == pytest.approx(90.0 - credit)
    expected = credit * 100 / candidate.collateral * 100 * (DAYS_PER_YEAR / DTE)
    assert candidate.annualized_yield_pct == pytest.approx(expected)


def test_covered_call_yield_is_measured_against_the_stock_and_reports_the_called_away_case():
    candidate = priced(110.0, "call", spread=0.0)
    assert candidate.collateral == pytest.approx(SPOT * 100)
    # Called away at 110 from 100 is 10 points of stock plus the premium.
    assert candidate.if_called_return_pct == pytest.approx(
        (110.0 - SPOT + candidate.credit) / SPOT * 100
    )
    assert candidate.if_called_return_pct > 10.0


def test_a_stale_last_trade_is_solved_at_the_time_it_printed():
    """Solving a 4-day-old print against today's shorter T overstates IV."""
    fair = bs_price(SPOT, 90.0, (DTE + 4) / DAYS_PER_YEAR, RATE, 0.30, "put")
    quote = OptionQuote(
        symbol="XYZ260918P00090000",
        underlying="XYZ",
        right="put",
        strike=90.0,
        expiry=EXPIRY,
        dte=DTE,
        bid=0.0,
        ask=fair * 1.5,
        last=fair,
        last_trade_at=datetime(2026, 8, 10, 15, 0, tzinfo=timezone.utc),
    )
    candidate = price_candidate(
        quote, strategy=CSP, spot=SPOT, risk_free_rate=RATE, as_of=AS_OF,
        anchor=LevelAnchor(None, "none"),
    )
    assert candidate.iv == pytest.approx(0.30, abs=0.01)


def test_a_contract_with_no_collectable_credit_produces_no_candidate():
    quote = OptionQuote(
        symbol="XYZ260918P00050000", underlying="XYZ", right="put",
        strike=50.0, expiry=EXPIRY, dte=DTE, bid=0.0, ask=0.05,
    )
    assert price_candidate(
        quote, strategy=CSP, spot=SPOT, risk_free_rate=RATE, as_of=AS_OF,
        anchor=LevelAnchor(None, "none"),
    ) is None


# -- scoring ----------------------------------------------------------------
def test_the_target_delta_band_outscores_a_strike_that_is_too_far_out():
    rules = StrategyRules()
    in_band = score_candidate(priced(95.0, "put"), rules)
    far_out = score_candidate(priced(88.0, "put"), rules)
    assert rules.delta_low <= abs(in_band.delta) <= rules.delta_high
    assert abs(far_out.delta) < rules.delta_low
    assert in_band.score > far_out.score


def test_an_earnings_print_before_expiry_is_penalised():
    rules = StrategyRules()
    clear = score_candidate(priced(92.0, "put"), rules)
    exposed = price_candidate(
        synthetic(92.0, "put"), strategy=CSP, spot=SPOT, risk_free_rate=RATE, as_of=AS_OF,
        anchor=LevelAnchor(None, "none"), earnings_dates=[date(2026, 9, 3)],
    )
    score_candidate(exposed, rules)
    assert exposed.earnings_flag == "⚠ 2026-09-03"
    assert clear.score - exposed.score == pytest.approx(3.0), "+1.0 clear becomes -2.0 exposed"


def test_an_unchecked_earnings_calendar_scores_zero_rather_than_clear():
    """Absent data must not read as good news."""
    unchecked = price_candidate(
        synthetic(92.0, "put"), strategy=CSP, spot=SPOT, risk_free_rate=RATE, as_of=AS_OF,
        anchor=LevelAnchor(None, "none"), earnings_checked=False,
    )
    score_candidate(unchecked, StrategyRules())
    assert unchecked.earnings_flag == "unchecked"
    assert "earnings calendar unavailable" in " ".join(unchecked.notes)


def test_a_strike_on_the_wrong_side_of_support_loses_a_point():
    rules = StrategyRules()
    anchor = LevelAnchor(94.0, "50-day SMA")
    below = score_candidate(priced(92.0, "put", anchor=anchor), rules)
    above = score_candidate(priced(96.0, "put", anchor=anchor), rules)
    assert "below 50-day SMA" in " ".join(below.notes)
    assert "above 50-day SMA" in " ".join(above.notes)


def test_illiquid_open_interest_is_rejected_outright():
    rules = StrategyRules()
    candidate = priced(92.0, "put", oi=3)
    assert hard_filters(candidate, rules) == "open interest under 20"


def test_missing_open_interest_is_not_treated_as_illiquid():
    """The contracts endpoint reports no OI on strikes that never traded."""
    quote = synthetic(92.0, "put")
    quote.open_interest = None
    candidate = price_candidate(
        quote, strategy=CSP, spot=SPOT, risk_free_rate=RATE, as_of=AS_OF,
        anchor=LevelAnchor(None, "none"),
    )
    assert hard_filters(candidate, StrategyRules()) is None
    score_candidate(candidate, StrategyRules())
    assert "open interest unreported" in " ".join(candidate.notes)


def test_an_in_the_money_strike_is_refused_for_both_strategies():
    rules = StrategyRules()
    assert hard_filters(priced(105.0, "put"), rules) == "in the money"
    assert hard_filters(priced(95.0, "call"), rules) == "in the money"


# -- the screen end to end --------------------------------------------------
def test_build_candidates_returns_the_best_strikes_and_a_rejection_tally():
    quotes = [synthetic(k, "put") for k in (70.0, 85.0, 92.0, 94.0, 105.0)]
    chain = ChainSlice("XYZ", "put", quotes, fetched_at=datetime(2026, 8, 14, 20, tzinfo=timezone.utc))
    candidates, tally = build_candidates(
        chain,
        strategy=CSP,
        spot=SPOT,
        levels={"50-day SMA": 95.0},
        risk_free_rate=RATE,
        as_of=AS_OF,
        rules=StrategyRules(max_candidates=2),
    )
    assert len(candidates) == 2
    assert candidates[0].score >= candidates[1].score
    assert all(c.strike < SPOT for c in candidates)
    assert "1 × in the money" in tally
    assert any("delta too low" in row for row in tally), "the 70 strike is far too far out"


def test_an_empty_window_says_so_instead_of_returning_silence():
    chain = ChainSlice("XYZ", "put", [], fetched_at=datetime(2026, 8, 14, 20, tzinfo=timezone.utc))
    candidates, tally = build_candidates(
        chain, strategy=CSP, spot=SPOT, levels={}, risk_free_rate=RATE, as_of=AS_OF
    )
    assert candidates == []
    assert tally == ["no expiry listed in the requested window"]
