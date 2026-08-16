"""Which company a headline is about — the adversarial cases and the regressions.

The regressions are all real: every headline in the second block was published
in one of our own reports under the wrong ticker.
"""

from __future__ import annotations

from datetime import date

import pytest

from tradingagent.data.entity import (
    MEDIUM,
    MIN_TONE_RELEVANCE,
    NONE,
    STRONG,
    IssuerIndex,
    aliases_for,
    issuer_index,
)
from tradingagent.data.finnhub_client import NewsItem, resolve_item
from tradingagent.pipeline.evidence import Evidence
from tradingagent.signals import news as N

# The letter tickers and word tickers that make a bare-token match useless.
NAMES = {
    "V": "Visa",
    "A": "Agilent Technologies",
    "C": "Citigroup",
    "ON": "ON Semiconductor",
    "IT": "Gartner",
    "ALL": "Allstate",
    "CAT": "Caterpillar",
    "AON": "Aon plc",
    "BRO": "Brown & Brown",
    "STZ": "Constellation Brands",
    "UNP": "Union Pacific",
    "NFLX": "Netflix",
    "TGT": "Target",
    "GPS": "Gap",
    "KMI": "Kinder Morgan",
}
INDEX = IssuerIndex(NAMES)


def resolve(headline: str, symbol: str, feed_tagged: bool = False):
    return INDEX.resolve(headline, symbol, feed_tagged=feed_tagged)


# --- adversarial: the bare ticker token -----------------------------------


@pytest.mark.parametrize(
    "symbol,headline",
    [
        ("V", "SanDisk's V-NAND Roadmap Puts Layers Center Stage"),
        ("A", "A Deep Dive Into Chevron's Refining Margins"),
        ("C", "Vitamin C Maker Announces Recall"),
        ("ON", "The Pressure Is ON For The Fed To Cut"),
        ("IT", "IT Spending Is Set To Slow, Says Survey"),
        ("ALL", "ALL Eyes On Nvidia Earnings Tonight"),
        ("CAT", "CAT Scan Volumes Rebound At Hospital Chains"),
        ("KMI", "Analysts Debate KMI-Style Toll Models Across Midstream"),
    ],
)
def test_a_bare_ticker_token_is_never_an_attribution(symbol, headline):
    match = resolve(headline, symbol)
    assert match.confidence == NONE
    assert not match.attributable
    assert match.relevance < MIN_TONE_RELEVANCE


@pytest.mark.parametrize(
    "symbol,headline,basis",
    [
        ("V", "Why $V Is Still The Best Network Toll", "cashtag"),
        ("UNP", "Union Pacific (UNP) On Q2 Earnings Beat", "in the headline"),
        ("C", "Citigroup (NYSE: C) Raises Its Dividend", "in the headline"),
        ("A", "Agilent Technologies Beats On Q3 Revenue", "issuer name"),
        ("ON", "ON Semiconductor Guides Below Consensus", "issuer name"),
        ("AON", "Aon Wins A Large Reinsurance Mandate", "issuer name"),
    ],
)
def test_a_cashtag_a_parenthetical_or_the_issuer_name_attaches(symbol, headline, basis):
    match = resolve(headline, symbol)
    assert match.confidence == STRONG and match.attributable
    assert basis in match.basis


# --- adversarial: issuer names that are ordinary words --------------------


def test_a_price_target_is_not_news_about_target():
    match = resolve("Jefferies Maintains Hold on Paychex, Raises Price Target to $120", "TGT")
    assert match.confidence == MEDIUM and not match.attributable


def test_the_same_word_used_as_a_company_does_attach():
    assert resolve("Target Q2 Earnings Beat Estimates", "TGT").attributable
    assert resolve("Shares of Gap Jump On Guidance", "GPS").attributable
    assert not resolve("The gap between bid and ask widened", "GPS").attributable


# --- regressions: headlines we actually published under the wrong ticker ---


def test_sandisks_investor_day_is_not_news_about_v_or_nflx():
    headline = (
        "SanDisk's Investor Day Puts NAND Center Stage as the 30-Year Quietly "
        "Prices In Fiscal Reality"
    )
    for symbol in ("V", "NFLX"):
        match = resolve(headline, symbol, feed_tagged=True)
        assert match.confidence == MEDIUM, "the feed's tag is a lead, not a fact"
        assert not match.attributable


def test_a_bro_story_is_not_aons_latest_headline():
    match = resolve("BRO Stock Trading at a Discount to Industry at 15.04X: Time to Hold?",
                    "AON", feed_tagged=True)
    assert not match.attributable
    assert INDEX.other_issuers(
        "Brown & Brown Trades at a Discount to Industry", exclude="AON"
    ) == ["BRO"]


def test_a_berkshire_13f_preview_is_not_news_about_stz():
    match = resolve(
        "Berkshire Hathaway 13F Preview: What Stock Moves Did Buffett Successor Greg Abel Make?",
        "STZ", feed_tagged=True,
    )
    assert not match.attributable


def test_berkshire_no_longer_carries_unps_news_tone():
    """The published defect: this headline scored +0.70 and took UNP's tone to
    +0.68, worth +5.4 ranking points, without ever mentioning Union Pacific."""
    items = [
        _item("UNP", "Union Pacific (UNP) On Q2 Earnings Beat And Higher Outlook"),
        _item("UNP", "Berkshire Hathaway Stock Nears Record. Wall Street Liked Its Earnings."),
    ]
    source = N.NewsToneSource(finnhub=_Finnhub(items), as_of=date(2026, 8, 14))
    signals = source.collect(["UNP"], date(2026, 8, 14))

    assert len(signals) == 1
    assert "1 of 1 headlines that name the company" in signals[0].headline
    assert "1 feed-tagged headline(s) excluded" in signals[0].headline
    assert "Berkshire" not in signals[0].detail


def test_a_ticker_whose_news_is_all_feed_tagged_scores_nothing():
    items = [_item("V", "SanDisk's Investor Day Puts NAND Center Stage")]
    source = N.NewsToneSource(finnhub=_Finnhub(items), as_of=date(2026, 8, 14))
    assert source.collect(["V"], date(2026, 8, 14)) == []


# --- the market-wide RSS leg ----------------------------------------------


def test_a_market_headline_needs_to_name_the_company_itself():
    """No feed tag exists here at all, so the headline is the only evidence."""
    index = issuer_index()
    assert not index.resolve("Dow Jones Futures: Sandisk Soars, Nvidia In Buy Area", "V").attributable
    assert index.resolve("Visa Stock Edges Higher As Volumes Hold", "V").attributable


# --- how it is shown ------------------------------------------------------


def test_the_evidence_pack_separates_tagged_headlines_from_the_companys_own():
    from tests.test_pipeline import _full_evidence  # noqa: PLC0415 - shared fixture

    evidence: Evidence = _full_evidence()
    evidence.queued.symbol = "UNP"
    evidence.news = [
        _item("UNP", "Union Pacific Beats On Q2 Revenue"),
        _item("UNP", "Berkshire Hathaway Stock Nears Record"),
    ]
    block = evidence.news_block()

    assert "Berkshire" in block, "a peer story is still worth reading"
    assert "but not about it on their face" in block
    own, tagged = block.split("#### Tagged to UNP")
    assert "Union Pacific Beats" in own and "Berkshire" not in own
    assert "Berkshire" in tagged and "Union Pacific Beats" not in tagged
    assert "do not build a thesis on them" in tagged


def test_the_shortlists_latest_headline_skips_the_unattributed_ones():
    items = [
        _item("STZ", "Berkshire Hathaway 13F Preview"),
        _item("STZ", "Constellation Brands Names A New Beer President"),
    ]
    assert next((n.headline for n in items if n.attributable), None) == (
        "Constellation Brands Names A New Beer President"
    )


# --- plumbing -------------------------------------------------------------


def test_aliases_drop_legal_forms_but_keep_the_name():
    assert "Aon" in aliases_for("Aon plc")
    assert "Brown and Brown" in aliases_for("Brown & Brown")
    assert "Union Pacific" in aliases_for("Union Pacific Corporation")


def test_relevance_survives_a_round_trip_through_the_snapshot():
    item = _item("V", "SanDisk's Investor Day Puts NAND Center Stage")
    back = NewsItem.from_dict(item.to_dict())
    assert back.relevance == item.relevance and back.match_basis == item.match_basis
    assert not back.attributable


def test_an_unresolved_item_is_left_alone_rather_than_dropped():
    """Old frozen snapshots predate the field; they must not vanish."""
    item = NewsItem(symbol="V", headline="whatever", source="s", url="", datetime_utc=0)
    assert item.relevance is None and item.attributable


def test_the_real_universe_index_knows_the_names_we_trade():
    index = issuer_index()
    assert index.resolve("Visa Stock Edges Higher", "V").attributable
    assert index.resolve("Kinder Morgan Lifts Its Dividend", "KMI").attributable
    assert not index.resolve("Oil Is Up 86% in 2026. The 4% Toll-Collector Fund", "KMI",
                             feed_tagged=True).attributable


def _item(symbol: str, headline: str) -> NewsItem:
    return resolve_item(
        NewsItem(symbol=symbol, headline=headline, source="Test", url="", datetime_utc=1_755_100_000)
    )


class _Finnhub:
    enabled = True

    def __init__(self, items: list[NewsItem]):
        self.items = items

    def company_news(self, symbol, start_date, end_date, limit=5):
        return [n for n in self.items if n.symbol == symbol][:limit]
