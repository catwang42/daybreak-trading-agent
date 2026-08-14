"""Tests for the Milestone 3 signal layer.

The bar these hold is not "does the parser run" but "does the source say
something honest". Most of the bugs found while building this milestone were
of the second kind — a five-way Polymarket event read as if it were binary, a
settled market printing maximal conviction, the same Fed question counted
twice — and each of those has a test here so it cannot come back silently.
"""

from datetime import date, timedelta

import pytest

from tradingagent.signals import accuracy as A
from tradingagent.signals import insiders as I
from tradingagent.signals import macro as M
from tradingagent.signals import news as N
from tradingagent.signals import prediction as P
from tradingagent.signals.base import Signal, SignalSource, SourceResult
from tradingagent.signals.bundle import MAX_SCORE_ADJUSTMENT, SignalBundle, SignalHub

RUN = date(2026, 8, 14)


def sig(source="src", direction=1, strength=0.5, symbol="TST", kind="k", headline="h"):
    return Signal(
        source=source, kind=kind, direction=direction, strength=strength,
        headline=headline, as_of=RUN, symbol=symbol,
    )


# --- base ---------------------------------------------------------------


def test_strength_is_clamped_into_the_unit_range():
    assert sig(strength=4.0).strength == 1.0
    assert sig(strength=-2.0).strength == 0.0


def test_a_raising_source_degrades_instead_of_ending_the_run():
    """No source may raise: the whole daily pipeline hangs off these calls."""

    class Broken(SignalSource):
        name, scope = "broken", "market"

        def collect(self, symbols, run_date):
            raise RuntimeError("endpoint on fire")

    source = Broken()
    result = source.fetch(["TST"], RUN)
    assert result.signals == [] and not result.ok
    assert "endpoint on fire" in result.error
    assert source.degraded.entries, "a failed source must surface in the DEGRADED section"


def test_an_unavailable_source_is_skipped_without_a_degraded_line():
    """An unset key is a configuration decision, not a failure."""

    class Keyless(SignalSource):
        name, scope = "keyless", "ticker"

        def available(self):
            return False, "NO_KEY not set"

        def collect(self, symbols, run_date):
            raise AssertionError("must not be called")

    source = Keyless()
    assert source.fetch(["TST"], RUN).error == "NO_KEY not set"
    assert not source.degraded.entries


# --- news tone ----------------------------------------------------------


def test_lexicon_reads_direction_off_a_headline():
    assert N.score_headline("Acme beats on earnings, raises guidance")[0] > 0
    assert N.score_headline("Acme misses, warns on demand")[0] < 0


def test_headline_with_no_lexicon_term_abstains_rather_than_guessing():
    tone, hits = N.score_headline("Acme to present at an investor conference")
    assert (tone, hits) == (0.0, [])


def test_negation_flips_the_term_it_precedes():
    assert N.score_headline("Acme denies fraud allegations")[0] > 0
    assert N.score_headline("Acme wins contract")[0] > 0


def test_substring_matches_do_not_count():
    """'cut' must not fire on 'executed' or 'acute'."""
    assert N.score_headline("Acme executed its acute care rollout")[1] == []


def test_synonym_stuffing_does_not_beat_a_clear_single_signal():
    stuffed = N.score_headline("Shares surge, jump, soar and rally on the news")[0]
    clean = N.score_headline("Acme beats estimates")[0]
    assert stuffed <= clean


# --- insiders -----------------------------------------------------------

FORM4 = """<?xml version="1.0"?>
<ownershipDocument>
  <reportingOwner>
    <reportingOwnerId><rptOwnerName>DOE JANE</rptOwnerName></reportingOwnerId>
    <reportingOwnerRelationship><isOfficer>1</isOfficer>
      <officerTitle>Chief Executive Officer</officerTitle></reportingOwnerRelationship>
  </reportingOwner>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <transactionDate><value>2026-08-01</value></transactionDate>
      <transactionCoding><transactionCode>P</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>1000</value></transactionShares>
        <transactionPricePerShare><value>50.00</value></transactionPricePerShare>
      </transactionAmounts>
    </nonDerivativeTransaction>
    <nonDerivativeTransaction>
      <transactionDate><value>2026-08-02</value></transactionDate>
      <transactionCoding><transactionCode>A</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>500</value></transactionShares>
      </transactionAmounts>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
</ownershipDocument>
"""


def test_parse_form4_reads_owner_transactions_and_prices():
    trades = I.parse_form4(FORM4, symbol="TST", filed=RUN)
    assert [t.code for t in trades] == ["P", "A"]
    buy = trades[0]
    assert buy.insider == "DOE JANE" and "Officer" in buy.role
    assert buy.shares == 1000 and buy.price == 50.0 and buy.value == 50_000
    assert buy.filed == date(2026, 8, 1), "the transaction date beats the filing date"


def test_10b5_1_is_detected_from_a_footnote_not_just_the_flag():
    """Filer agents often state the plan in prose; missing it would call a
    scheduled sale a bearish decision."""
    plain = I.parse_form4(FORM4, symbol="TST", filed=RUN)
    assert not plain[0].planned
    footnoted = FORM4.replace(
        "</nonDerivativeTable>",
        "</nonDerivativeTable><footnotes><footnote>Sale under a Rule 10b5-1 "
        "trading plan adopted 2026-01-05.</footnote></footnotes>",
    )
    assert all(t.planned for t in I.parse_form4(footnoted, symbol="TST", filed=RUN))


def trade(code, shares=100, price=10.0, planned=False, insider="A", when=RUN):
    return I.InsiderTrade(
        symbol="TST", filed=when, insider=insider, role="Officer",
        code=code, shares=shares, price=price, planned=planned,
    )


def test_only_open_market_trades_produce_a_signal():
    """Grants and tax withholding happen on a schedule regardless of price."""
    assert I.summarize("TST", [trade("A"), trade("M"), trade("F")], RUN) is None
    assert I.summarize("TST", [], RUN) is None


def test_cluster_buying_is_the_strongest_bullish_read():
    one = I.summarize("TST", [trade("P")], RUN)
    three = I.summarize(
        "TST", [trade("P", insider="A"), trade("P", insider="B"), trade("P", insider="C")], RUN
    )
    assert one.direction == three.direction == 1
    assert three.strength > one.strength


def test_sales_entirely_on_scheduled_plans_are_reported_but_not_directional():
    signal = I.summarize("TST", [trade("S", planned=True), trade("S", planned=True)], RUN)
    assert signal.direction == 0, "a 10b5-1 sale is not a decision about the price"
    assert "10b5-1" in signal.headline


def test_a_discretionary_sale_is_bearish_even_alongside_a_planned_one():
    signal = I.summarize("TST", [trade("S", planned=True), trade("S", planned=False)], RUN)
    assert signal.direction == -1
    assert "1 of the sales were on 10b5-1 plans" in signal.headline


# --- macro --------------------------------------------------------------


def obs(series, latest, prior):
    return M.Observation(series=series, latest=latest, prior=prior, latest_date=RUN)


HY = M.SERIES[0]   # high-yield spread, a rise is bearish
VIX = M.SERIES[1]  # VIX, a rise is bearish
CURVE = M.SERIES[2]  # 10y-2y, a rise is bullish


def test_a_rise_is_read_through_the_series_own_direction_rule():
    assert obs(HY, 4.0, 3.0).direction == -1     # spreads widened
    assert obs(CURVE, 0.5, 0.1).direction == 1   # curve steepened
    assert obs(VIX, 14.0, 25.0).direction == 1   # volatility collapsed


def test_a_move_inside_the_noise_band_is_not_a_move():
    assert obs(HY, 3.01, 3.0).direction == 0
    assert not obs(HY, 3.01, 3.0).material


def test_regime_signal_weights_the_series_and_ignores_the_still_ones():
    quiet = M.regime_signal([obs(HY, 3.0, 3.0), obs(VIX, 20.0, 20.0)], RUN)
    assert quiet.direction == 0 and quiet.strength == 0.0
    assert "0 of 2 macro series moved" in quiet.headline

    stressed = M.regime_signal([obs(HY, 4.0, 3.0), obs(VIX, 30.0, 20.0)], RUN)
    assert stressed.direction == -1 and stressed.strength > 0.9
    assert "High-yield credit spread" in stressed.headline


def test_the_heavier_series_wins_a_disagreement():
    """Credit is weighted above the curve deliberately; if that inverts, the
    regime read flips sign without anyone noticing."""
    split = M.regime_signal([obs(HY, 4.0, 3.0), obs(CURVE, 0.5, 0.1)], RUN)
    assert split.direction == -1


def test_macro_is_market_wide_and_carries_no_symbol():
    assert M.regime_signal([obs(HY, 4.0, 3.0)], RUN).symbol is None


# --- prediction markets -------------------------------------------------


def market(price, group="", change=0.0):
    return {
        "outcomePrices": f'["{price}", "{1 - price:.4f}"]',
        "groupItemTitle": group,
        "oneWeekPriceChange": change,
    }


def event(title, markets, liquidity=100_000.0, end="2026-12-09"):
    return {"title": title, "markets": markets, "liquidity": liquidity, "endDate": end}


FED = "Fed Decision in December?"


def test_a_multi_outcome_event_sums_the_legs_that_mean_the_topic_happened():
    """P(cut) is 25bp + 50bp, not whichever leg Gamma listed first."""
    payload = [event(FED, [
        market(0.10, "25 bps decrease"),
        market(0.04, "50+ bps decrease"),
        market(0.86, "no change"),
    ])]
    odds = P.parse_events(payload, min_liquidity=1.0)
    assert len(odds) == 1
    assert odds[0].probability == pytest.approx(0.14)


def test_an_event_with_no_matching_leg_is_skipped_rather_than_guessed_at():
    payload = [event(FED, [market(0.3, "hike"), market(0.7, "no change")])]
    assert P.parse_events(payload, min_liquidity=1.0) == []


def test_a_multi_outcome_leg_price_is_never_read_as_a_probability():
    three_way = {"outcomePrices": '["0.5", "0.3", "0.2"]'}
    assert P._yes_price(three_way) is None


def test_settled_markets_are_dropped():
    """At 99% the market is a fact everyone has priced, not information."""
    payload = [event("Recession in 2026?", [market(0.995)])]
    assert P.parse_events(payload, min_liquidity=1.0) == []


def test_illiquid_markets_are_dropped():
    payload = [event("Recession in 2026?", [market(0.3)], liquidity=500.0)]
    assert P.parse_events(payload, min_liquidity=P.MIN_LIQUIDITY_USD) == []


def test_one_topic_asked_twice_is_counted_once_and_the_deeper_book_wins():
    payload = [
        event("Fed Decision in October?", [market(0.06, "25 bps decrease")], liquidity=50_000.0),
        event(FED, [market(0.09, "25 bps decrease")], liquidity=900_000.0),
    ]
    odds = P.parse_events(payload, min_liquidity=1.0)
    assert [o.question for o in odds] == [FED]


def test_foreign_elections_are_not_classified_as_us_macro():
    assert P.classify("Brazil Presidential Election") is None
    assert P.classify("US Senate control after the midterms").label == "US election"


def test_the_signal_reads_the_weekly_move_not_the_level():
    """A probability parked at 6% would otherwise print maximal risk-off every
    single day until the meeting — a constant is not information."""
    parked = P.parse_events(
        [event(FED, [market(0.06, "25 bps decrease", change=0.0)])], min_liquidity=1.0
    )
    signal = P.odds_signal(parked, RUN)
    assert signal.direction == 0 and signal.strength == 0.0
    assert "No tracked topic repriced" in signal.headline

    repriced = P.parse_events(
        [event(FED, [market(0.06, "25 bps decrease", change=-0.12)])], min_liquidity=1.0
    )
    moved = P.odds_signal(repriced, RUN)
    assert moved.direction == -1, "a collapsing cut probability is risk-off"
    assert moved.strength == pytest.approx(1.0)


def test_a_topic_with_a_contested_equity_mapping_supplies_context_only():
    payload = [event("US CPI inflation above 3%?", [market(0.4, change=0.2)])]
    odds = P.parse_events(payload, min_liquidity=1.0)
    assert odds[0].equity_direction is None and odds[0].move == 0.0
    assert P.odds_signal(odds, RUN).direction == 0


def test_a_partially_reported_weekly_change_is_dropped_not_half_summed():
    legs = [market(0.10, "25 bps decrease", change=0.05), market(0.04, "50+ bps decrease")]
    legs[1].pop("oneWeekPriceChange")
    assert P.change_of(event(FED, legs), P.TOPICS[0]) is None


# --- bundle -------------------------------------------------------------


def test_market_wide_signals_cannot_reorder_the_shortlist():
    """They are identical for every candidate, so a score they all get is no
    score at all — they belong in the shared context instead."""
    bundle = SignalBundle(
        symbol="TST", run_date=RUN,
        market_signals=[sig(direction=-1, strength=1.0, symbol=None)],
    )
    assert bundle.score_adjustment() == 0.0


def test_the_adjustment_is_clamped_so_signals_cannot_override_the_price_screen():
    unanimous = [sig(source=f"s{i}", direction=1, strength=1.0) for i in range(6)]
    bundle = SignalBundle(symbol="TST", run_date=RUN, ticker_signals=unanimous)
    assert bundle.score_adjustment() == pytest.approx(MAX_SCORE_ADJUSTMENT)


def test_disagreeing_sources_cancel():
    bundle = SignalBundle(symbol="TST", run_date=RUN, ticker_signals=[
        sig(source="a", direction=1, strength=0.8),
        sig(source="b", direction=-1, strength=0.8),
    ])
    assert bundle.score_adjustment() == pytest.approx(0.0)
    assert bundle.net_direction() == 0


def test_accuracy_weights_scale_a_sources_influence():
    signals = [sig(source="trusted", direction=1, strength=1.0)]
    plain = SignalBundle(symbol="TST", run_date=RUN, ticker_signals=signals)
    demoted = SignalBundle(
        symbol="TST", run_date=RUN, ticker_signals=signals, weights={"trusted": 0.5}
    )
    assert demoted.score_adjustment() == pytest.approx(plain.score_adjustment() * 0.5)


def test_readings_record_market_signals_too_so_the_macro_call_can_be_graded():
    bundle = SignalBundle(
        symbol="TST", run_date=RUN,
        ticker_signals=[sig(source="news_tone", direction=1)],
        market_signals=[sig(source="macro_fred", direction=-1, symbol=None)],
    )
    assert bundle.readings() == {"news_tone": 1, "macro_fred": -1}


def test_a_source_firing_twice_is_reduced_to_the_view_it_actually_expressed():
    bundle = SignalBundle(symbol="TST", run_date=RUN, ticker_signals=[
        sig(source="news_tone", direction=1, strength=0.9),
        sig(source="news_tone", direction=-1, strength=0.2),
    ])
    assert bundle.readings() == {"news_tone": 1}


def test_skipped_sources_are_named_in_the_prompt_as_a_gap_not_a_neutral_read():
    bundle = SignalBundle(
        symbol="TST", run_date=RUN, skipped={"insider_form4": "SEC_USER_AGENT not set"}
    )
    block = bundle.prompt_block()
    assert "SEC_USER_AGENT not set" in block
    assert "absence here is a gap in our data" in block


def test_the_ticker_block_omits_the_market_backdrop_it_would_otherwise_duplicate():
    bundle = SignalBundle(
        symbol="TST", run_date=RUN,
        ticker_signals=[sig(headline="ticker news")],
        market_signals=[sig(symbol=None, headline="macro backdrop")],
    )
    assert "macro backdrop" not in bundle.ticker_block()
    assert "macro backdrop" in bundle.prompt_block()


# --- hub ----------------------------------------------------------------


class FakeSource(SignalSource):
    scope = "ticker"

    def __init__(self, name, signals=(), error=None):
        self.name = name
        super().__init__()
        self._signals = list(signals)
        self._error = error
        self.calls = 0

    def available(self):
        return (False, self._error) if self._error else (True, "")

    def collect(self, symbols, run_date):
        self.calls += 1
        return self._signals


def test_the_hub_fetches_each_source_once_per_run_not_once_per_ticker():
    source = FakeSource("cheap", [sig(symbol="AAA")])
    hub = SignalHub([source])
    hub.collect(["AAA", "BBB", "CCC"], RUN)
    assert source.calls == 1


def test_the_hub_routes_signals_to_the_right_ticker_and_shares_the_market_half():
    hub = SignalHub([
        FakeSource("per_ticker", [sig(source="per_ticker", symbol="AAA")]),
        FakeSource("wide", [sig(source="wide", symbol=None)]),
        FakeSource("dark", error="key not set"),
    ])
    hub.collect(["AAA", "BBB"], RUN)

    aaa, bbb = hub.bundle("AAA", RUN), hub.bundle("BBB", RUN)
    assert [s.source for s in aaa.ticker_signals] == ["per_ticker"]
    assert bbb.ticker_signals == []
    assert [s.source for s in bbb.market_signals] == ["wide"]
    assert hub.skipped == {"dark": "key not set"}
    assert aaa.skipped == {"dark": "key not set"}


# --- accuracy -----------------------------------------------------------


def entry(ticker, when, readings):
    return {"date": when.isoformat(), "ticker": ticker, "signal_readings": readings}


def fixed(pct):
    return lambda ticker, decided: pct


def test_a_correct_call_is_a_hit_and_a_wrong_one_is_not():
    report = A.score_entries([entry("AAA", RUN, {"good": 1, "bad": -1})], fixed(6.0), RUN)
    assert (report.scores["good"].hits, report.scores["good"].samples) == (1, 1)
    assert (report.scores["bad"].hits, report.scores["bad"].samples) == (0, 1)


def test_abstentions_are_not_scored_either_way():
    """Counting them would let a silent source drift towards 50% and look average."""
    report = A.score_entries([entry("AAA", RUN, {"quiet": 0})], fixed(6.0), RUN)
    score = report.scores["quiet"]
    assert (score.samples, score.hits, score.abstained) == (0, 0, 1)
    assert score.accuracy is None and score.weight == 1.0


def test_moves_inside_the_dead_band_are_dropped_rather_than_graded_as_misses():
    assert A.outcome_direction(0.4) is None
    assert A.outcome_direction(None) is None
    assert A.outcome_direction(-3.0) == -1
    report = A.score_entries([entry("AAA", RUN, {"src": 1})], fixed(0.4), RUN)
    assert report.scores == {} and report.unresolved == 1


def test_entries_outside_the_window_are_ignored():
    old = RUN - timedelta(days=A.WINDOW_DAYS + 5)
    report = A.score_entries([entry("AAA", old, {"src": 1})], fixed(6.0), RUN)
    assert report.scores == {}


def test_entries_without_signal_readings_are_skipped():
    report = A.score_entries([{"date": RUN.isoformat(), "ticker": "AAA"}], fixed(6.0), RUN)
    assert report.scores == {} and report.unresolved == 0


def test_a_short_lucky_streak_barely_moves_the_weight():
    """Three correct calls is 100% accuracy and means nothing."""
    lucky = A.SourceScore("src", samples=3, hits=3)
    proven = A.SourceScore("src", samples=A.FULL_CONFIDENCE_SAMPLES, hits=A.FULL_CONFIDENCE_SAMPLES)
    assert lucky.accuracy == proven.accuracy == 1.0
    assert 1.0 < lucky.weight < 1.15
    assert proven.weight == pytest.approx(A.MAX_WEIGHT)


def test_a_proven_wrong_source_is_halved_but_never_silenced():
    hopeless = A.SourceScore("src", samples=100, hits=0)
    assert hopeless.weight == pytest.approx(A.MIN_WEIGHT)


def test_an_unscored_source_runs_at_neutral_weight():
    assert A.SourceScore("src").weight == 1.0
    assert A.AccuracyReport(scored_on=RUN, window_days=90).weights() == {}


def test_the_tracker_rescores_only_after_a_week(tmp_path):
    tracker = A.AccuracyTracker(journal_path=tmp_path / "journal.jsonl")
    tracker.save(A.AccuracyReport(scored_on=RUN, window_days=90))
    assert not tracker.stale(tracker.load(), RUN + timedelta(days=A.REFRESH_AFTER_DAYS - 1))
    assert tracker.stale(tracker.load(), RUN + timedelta(days=A.REFRESH_AFTER_DAYS))


def test_the_tracker_survives_a_corrupt_scores_file(tmp_path):
    tracker = A.AccuracyTracker(journal_path=tmp_path / "journal.jsonl")
    tracker.scores_path.write_text("{not json")
    assert tracker.load() is None
    assert tracker.current(RUN).weights() == {}


def test_a_saved_report_round_trips():
    original = A.AccuracyReport(
        scored_on=RUN, window_days=90, unresolved=4,
        scores={"news_tone": A.SourceScore("news_tone", samples=10, hits=7, abstained=2)},
    )
    restored = A.AccuracyReport.from_dict(original.to_dict())
    assert restored.weights() == original.weights()
    assert restored.scored_on == RUN and restored.unresolved == 4
