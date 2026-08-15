"""Black-Scholes core — checked against closed-form identities, not fixtures.

The port dropped scipy for ``math.erf``; these tests exist so that swap stays
honest. Put-call parity and the round-trip price -> IV -> price are the two
properties that break loudly if the CDF is wrong.
"""

from __future__ import annotations

import math

import pytest

from tradingagent.options.black_scholes import (
    bs_delta,
    bs_price,
    bs_theta,
    bs_vega,
    implied_volatility,
    norm_cdf,
    norm_pdf,
)


def test_normal_cdf_matches_known_values():
    assert norm_cdf(0.0) == pytest.approx(0.5)
    assert norm_cdf(1.0) == pytest.approx(0.8413447461, abs=1e-9)
    assert norm_cdf(-1.96) == pytest.approx(0.0249978952, abs=1e-9)
    assert norm_cdf(8.0) == pytest.approx(1.0, abs=1e-12)


def test_normal_pdf_matches_known_values():
    assert norm_pdf(0.0) == pytest.approx(1 / math.sqrt(2 * math.pi))
    assert norm_pdf(1.0) == pytest.approx(0.2419707245, abs=1e-9)


def test_put_call_parity_holds_with_a_dividend_yield():
    S, K, T, r, sigma, q = 100.0, 95.0, 0.5, 0.045, 0.32, 0.015
    call = bs_price(S, K, T, r, sigma, "call", q)
    put = bs_price(S, K, T, r, sigma, "put", q)
    parity = S * math.exp(-q * T) - K * math.exp(-r * T)
    assert call - put == pytest.approx(parity, abs=1e-9)


def test_delta_signs_and_bounds():
    S, K, T, r, sigma = 100.0, 110.0, 30 / 365, 0.045, 0.30
    call_delta = bs_delta(S, K, T, r, sigma, "call")
    put_delta = bs_delta(S, 90.0, T, r, sigma, "put")
    assert 0.0 < call_delta < 0.5, "an OTM call is under 50 delta"
    assert -0.5 < put_delta < 0.0, "a put delta is negative"


def test_expired_option_collapses_to_intrinsic():
    assert bs_price(100.0, 90.0, 0.0, 0.045, 0.3, "call") == pytest.approx(10.0)
    assert bs_price(100.0, 90.0, 0.0, 0.045, 0.3, "put") == pytest.approx(0.0)
    assert bs_delta(100.0, 90.0, 0.0, 0.045, 0.3, "call") == 1.0
    assert bs_delta(80.0, 90.0, 0.0, 0.045, 0.3, "put") == -1.0


def test_vega_and_theta_have_the_expected_sign():
    args = (100.0, 100.0, 45 / 365, 0.045, 0.30)
    assert bs_vega(*args) > 0, "more vol is always worth more to the holder"
    assert bs_theta(*args, "call") < 0, "a long call bleeds time value"


def test_implied_volatility_round_trips():
    S, K, T, r, sigma, q = 340.0, 320.0, 35 / 365, 0.045, 0.284, 0.008
    price = bs_price(S, K, T, r, sigma, "put", q)
    recovered = implied_volatility(price, S, K, T, r, "put", q)
    assert recovered is not None
    assert recovered == pytest.approx(sigma, abs=1e-4)


def test_implied_volatility_round_trips_far_out_of_the_money():
    """The 0.20-delta strikes we actually sell sit here, where vega is small."""
    S, K, T, r, sigma = 78.0, 65.0, 28 / 365, 0.045, 0.55
    price = bs_price(S, K, T, r, sigma, "put")
    assert implied_volatility(price, S, K, T, r, "put") == pytest.approx(sigma, abs=1e-3)


def test_price_below_intrinsic_returns_none_rather_than_a_floor_iv():
    """The indicative feed does produce sub-intrinsic quotes on a stale book."""
    assert implied_volatility(1.0, 100.0, 120.0, 30 / 365, 0.045, "put") is None


def test_unreachable_price_returns_none():
    """A put cannot be worth more than its strike; upstream returned 2.5 here."""
    assert implied_volatility(500.0, 100.0, 90.0, 30 / 365, 0.045, "put") is None


def test_zero_or_negative_inputs_are_refused():
    assert implied_volatility(0.0, 100.0, 90.0, 0.1, 0.045, "put") is None
    assert implied_volatility(2.0, 100.0, 90.0, 0.0, 0.045, "put") is None
