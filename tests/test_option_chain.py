"""OptionQuote arithmetic — the part of the chain reader that decides a number.

The transport is covered by the live smoke run recorded in PORTING_NOTES.md;
what is tested here is the pricing fallback chain, because that is where a
one-sided or stale book turns into a recommendation. Every fixture below is a
shape Alpaca's indicative feed actually returned for KMI/V on 2026-08-14.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from tradingagent.data.option_chain import ChainSlice, OptionQuote

EXPIRY = date(2026, 9, 18)
NOW = datetime(2026, 8, 14, 20, 0, tzinfo=timezone.utc)


def quote(**kwargs) -> OptionQuote:
    base = dict(
        symbol="KMI260918P00031000",
        underlying="KMI",
        right="put",
        strike=31.0,
        expiry=EXPIRY,
        dte=35,
    )
    base.update(kwargs)
    return OptionQuote(**base)


def test_two_sided_book_prices_off_the_mid():
    q = quote(bid=0.40, ask=0.50, last=0.44)
    assert q.mid == 0.45
    assert q.price_basis == "mid"
    assert q.reference_price == 0.45


def test_seller_collects_the_bid_not_the_mid():
    """You sell into the bid. Marking the credit at the mid overstates every yield."""
    q = quote(bid=0.40, ask=0.50)
    assert q.credit == 0.40
    assert q.reference_price == 0.45, "IV still solves off the mid — that is the fair mark"


def test_one_sided_book_has_no_mid():
    """bid 0.00 / ask 0.06 is the far-OTM norm on the free feed; (0+0.06)/2 is fiction."""
    q = quote(bid=0.0, ask=0.06, last=0.08)
    assert q.mid is None
    assert q.spread_pct is None
    assert q.credit == 0.08
    assert q.price_basis == "last trade"


def test_falls_back_to_prior_close_when_nothing_traded():
    q = quote(bid=0.0, ask=0.03, prev_close=0.07)
    assert q.credit == 0.07
    assert q.price_basis == "prior close"


def test_a_contract_with_no_price_at_all_is_marked_unusable():
    q = quote(bid=0.0, ask=0.05)
    assert q.credit is None
    assert q.reference_price is None
    assert q.price_basis == "none"


def test_spread_pct_measures_the_round_trip_cost():
    q = quote(bid=0.90, ask=1.10)
    assert q.spread_pct == pytest.approx(20.0)


def test_freshness_follows_the_price_that_was_actually_used():
    """A fresh quote on a stale last-trade contract must not inherit the quote's age."""
    q = quote(
        bid=0.0,
        ask=0.03,
        last=0.08,
        quote_at=NOW - timedelta(minutes=5),
        last_trade_at=NOW - timedelta(days=3),
    )
    assert q.price_basis == "last trade"
    assert q.age_minutes(NOW) == 3 * 24 * 60
    assert q.freshness(NOW) == "3.0d old"


def test_freshness_of_a_live_quote():
    q = quote(bid=0.40, ask=0.50, quote_at=NOW - timedelta(minutes=12))
    assert q.freshness(NOW) == "12m old"


def test_naive_timestamps_are_treated_as_utc():
    q = quote(bid=0.40, ask=0.50, quote_at=datetime(2026, 8, 14, 19, 0))
    assert q.age_minutes(NOW) == 60


def test_nearest_expiry_prefers_the_longer_date_on_a_tie():
    """A tie means equal distance from target; the extra week is more premium."""
    quotes = [
        quote(expiry=date(2026, 9, 4), strike=31.0),
        quote(expiry=date(2026, 9, 18), strike=31.0),
    ]
    sl = ChainSlice("KMI", "put", quotes, fetched_at=NOW)
    # From 2026-08-14 those expiries are 21 and 35 DTE; 28 is equidistant.
    assert sl.nearest_expiry(28) == date(2026, 9, 18)
    assert sl.nearest_expiry(24) == date(2026, 9, 4)


def test_priced_drops_the_contracts_with_no_usable_mark():
    quotes = [quote(bid=0.40, ask=0.50), quote(strike=20.0, bid=0.0, ask=0.05)]
    sl = ChainSlice("KMI", "put", quotes, fetched_at=NOW)
    assert [q.strike for q in sl.priced()] == [31.0]
