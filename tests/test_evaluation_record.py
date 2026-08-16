"""Turning a stage's artefacts into ledger rows.

The property under test throughout is coverage, not formatting: the ledger has
to hold the names the screener *rejected* and the overlays the strategist
*declined*, because a record of only the winners cannot grade a ranking or a
screen. Formatting is checked where a later job depends on it — the outcomes
job tests levels against bars, so the entry condition has to be a number.
"""

from datetime import date

from tradingagent.discovery.screener import Candidate
from tradingagent.discovery.shortlist import QuickTake, ShortlistEntry
from tradingagent.evaluation import record as R
from tradingagent.evaluation.provenance import Provenance
from tradingagent.signals.base import Signal
from tradingagent.signals.bundle import SignalBundle, ShadowRanking

RUN_DATE = date(2026, 8, 16)
PROV = Provenance(run_id="run-2026-08-16-025713Z", run_date="2026-08-16")


def candidate(symbol: str, score: int, sector: str = "Technology") -> Candidate:
    return Candidate(
        symbol=symbol, name=f"{symbol} Inc", sector=sector, industry="Software",
        price=100.0, score=score, rating="B", state="extended", primary_trigger="4pct_breakout",
    )


def bundle(symbol: str, *signals: Signal) -> SignalBundle:
    return SignalBundle(
        symbol=symbol, run_date=RUN_DATE, ticker_signals=list(signals),
        weights={}, caps={},  # nothing graduated: every adjustment is shadow only
    )


def signal(source: str, symbol: str, direction: int, strength: float = 1.0) -> Signal:
    return Signal(
        source=source, kind="test", symbol=symbol, direction=direction, strength=strength,
        headline=f"{source} reads {direction:+d}", as_of=RUN_DATE,
    )


def entry(cand: Candidate, rank: int, signals=None, rating: str = "Hold") -> ShortlistEntry:
    return ShortlistEntry(
        candidate=cand,
        take=QuickTake(rating=rating, confidence="M", thesis="t", key_risk="r", deep_dive_priority=5),
        earnings_flag="—", news_headline=None, signals=signals, screen_rank=rank,
    )


class FakeHub:
    def __init__(self, shadow=None, bundles=None):
        self.shadow = shadow
        self._bundles = bundles or {}

    def bundle(self, symbol, run_date):
        return self._bundles.get(symbol) or bundle(symbol)


# --------------------------------------------------------------------------
# candidates — the pool, not the shortlist
# --------------------------------------------------------------------------


def test_every_screened_name_is_recorded_including_the_rejects():
    pool = [candidate(f"T{i}", 90 - i) for i in range(12)]
    shortlist = [entry(pool[0], 1), entry(pool[1], 2), entry(pool[2], 3)]

    rows = R.candidate_records(PROV, RUN_DATE, pool, shortlist=shortlist)

    assert len(rows) == 12
    assert [r.ticker for r in rows if r.selected] == ["T0", "T1", "T2"]
    assert sum(1 for r in rows if not r.selected) == 9


def test_a_name_the_sector_cap_removed_is_recorded_as_ineligible():
    """It was screened and it was excluded; both halves are evidence."""
    pool = [candidate("A", 90), candidate("B", 88), candidate("C", 86)]
    rows = {r.ticker: r for r in R.candidate_records(PROV, RUN_DATE, pool, eligible=pool[:2])}

    assert rows["B"].eligible is True
    assert rows["C"].eligible is False
    assert rows["C"].final_rank == 0, "a name that was never in the running has no final rank"


def test_final_rank_reflects_the_ordering_selection_actually_used():
    pool = [candidate("A", 90), candidate("B", 80), candidate("C", 70)]
    rows = {r.ticker: r for r in R.candidate_records(PROV, RUN_DATE, pool)}
    assert [rows[t].final_rank for t in ("A", "B", "C")] == [1, 2, 3]


def test_the_shadowed_signal_layer_moves_nothing_but_is_recorded_in_full():
    bull = bundle("A", signal("news_tone", "A", 1, 1.0), signal("insider", "A", 1, 0.5))
    pool = [candidate("A", 90)]
    row = R.candidate_records(
        PROV, RUN_DATE, pool, shortlist=[entry(pool[0], 1, signals=bull)]
    )[0]

    assert row.signal_adjustment == 0.0, "no source has graduated, so nothing moved"
    assert row.shadow_adjustment > 0.0, "but the layer had a view and it is on the record"
    assert set(row.per_signal_shadow) == {"news_tone", "insider"}
    assert row.per_signal_shadow["news_tone"] > row.per_signal_shadow["insider"]
    assert row.signal_readings == {"news_tone": 1, "insider": 1}


def test_control_and_treatment_shortlists_are_both_recorded():
    """Item 4: the shipped list is the price-only control, for free."""
    pool = [candidate("A", 90), candidate("B", 88), candidate("C", 86)]
    shortlist = [entry(pool[0], 1), entry(pool[1], 2)]
    hub = FakeHub(shadow=ShadowRanking(size=2, chosen=["A", "B"], shadow=["A", "C"]))

    rows = {r.ticker: r for r in R.candidate_records(PROV, RUN_DATE, pool, shortlist=shortlist, hub=hub)}

    assert (rows["B"].selected, rows["B"].counterfactual_selected) == (True, False)
    assert (rows["C"].selected, rows["C"].counterfactual_selected) == (False, True)


def test_the_queue_is_flagged_so_the_deep_stage_is_gradeable_apart():
    pool = [candidate("A", 90), candidate("B", 88)]
    rows = {r.ticker: r for r in R.candidate_records(PROV, RUN_DATE, pool, queued=["A"])}
    assert rows["A"].queued is True
    assert rows["B"].queued is False


def test_a_hub_that_throws_does_not_lose_the_row():
    class Broken(FakeHub):
        def bundle(self, symbol, run_date):
            raise RuntimeError("source registry exploded")

    rows = R.candidate_records(PROV, RUN_DATE, [candidate("A", 90)], hub=Broken())
    assert len(rows) == 1 and rows[0].per_signal_shadow == {}


# --------------------------------------------------------------------------
# decisions
# --------------------------------------------------------------------------


def test_discovery_decisions_record_the_quick_take_as_a_decision():
    cand = candidate("A", 90)
    rows = R.discovery_decisions(PROV, [entry(cand, 1, rating="Overweight")], RUN_DATE, "reports/x.md")
    assert rows[0].rating == "Overweight"
    assert rows[0].stage == "discovery"
    assert rows[0].seat_tiers == {"quick_take": "fast"}
    assert rows[0].decision_id == "2026-08-16:A:discovery"


def test_a_degraded_quick_take_is_still_a_row():
    cand = candidate("A", 90)
    e = ShortlistEntry(
        candidate=cand, take=None, earnings_flag="—", news_headline=None,
        degraded_reason="LLM disabled",
    )
    row = R.discovery_decisions(PROV, [e], RUN_DATE, "reports/x.md")[0]
    assert row.rating == "DEGRADED" and row.degraded is True
    assert row.degraded_reasons == ["LLM disabled"]


class FakeDecision:
    rating = "Overweight"
    confidence = "M"
    price_target = 78.0
    time_horizon = "4-8 weeks"
    invalidation = "a close below $69"


class FakePlan:
    def __init__(self, payload):
        self._payload = payload

    def journal_payload(self):
        return self._payload


class FakeQueued:
    symbol = "WMB"
    sector = "Energy"
    signal_readings = {"news_tone": 1}


class FakeDeepResult:
    symbol = "WMB"
    queued = FakeQueued()
    seat_tiers = {"portfolio_manager": "deep", "trader": "smart"}
    degraded = False

    def __init__(self, decision=FakeDecision(), plan=None):
        self.decision = decision
        self.trade_plan = plan

    def degraded_reasons(self):
        return []


def test_deep_decisions_carry_the_computed_plan_and_the_tier_that_wrote_them():
    plan = FakePlan({"status": "PLAN", "direction": "long", "entry": 71.5, "stop": 69.0, "target": 78.0})
    row = R.deep_decisions(PROV, [FakeDeepResult(plan=plan)], RUN_DATE, "reports/d")[0]

    assert row.rating == "Overweight"
    assert row.horizon == "4-8 weeks"
    assert row.invalidation == "a close below $69"
    assert row.trade_plan["entry"] == 71.5
    assert row.seat_tiers["portfolio_manager"] == "deep"
    assert row.report == "reports/d/WMB.md"


def test_the_entry_condition_is_a_number_the_outcomes_job_can_test():
    plan = FakePlan({"status": "PLAN", "direction": "long", "entry": 71.5})
    row = R.deep_decisions(PROV, [FakeDeepResult(plan=plan)], RUN_DATE, "reports/d")[0]
    assert row.entry_condition == "long entry at 71.50"


def test_a_no_trade_plan_publishes_no_trigger_rather_than_inventing_one():
    plan = FakePlan({"status": "NO TRADE — the verdict is Hold", "direction": "flat", "entry": None})
    row = R.deep_decisions(PROV, [FakeDeepResult(plan=plan)], RUN_DATE, "reports/d")[0]
    assert row.entry_condition == ""


def test_a_deep_dive_with_no_verdict_is_recorded_as_degraded():
    row = R.deep_decisions(PROV, [FakeDeepResult(decision=None)], RUN_DATE, "reports/d")[0]
    assert row.rating == "DEGRADED"
    assert row.target is None


class FakeOptionsPlan:
    def __init__(self, payload, strategy="cash-secured put", skipped="", error=""):
        self._payload = payload
        self.symbol = "V"
        self.strategy = strategy
        self.recommendation = None
        self.skipped = skipped
        self.error = error

    def journal_payload(self):
        return self._payload


def test_a_declined_overlay_is_a_decision_and_is_recorded():
    plan = FakeOptionsPlan({"recommended": None}, strategy=None, skipped="no strike cleared the screen")
    row = R.options_decisions(PROV, [plan], RUN_DATE, "reports/d")[0]
    assert row.rating == "no overlay"
    assert row.degraded is True
    assert row.degraded_reasons == ["no strike cleared the screen"]


def test_a_recommended_overlay_records_its_strike():
    plan = FakeOptionsPlan({"recommended": {"strike": 275.0}})
    row = R.options_decisions(PROV, [plan], RUN_DATE, "reports/d")[0]
    assert row.target == 275.0
    assert row.stage == "options"
    assert row.decision_id == "2026-08-16:V:options"


def test_a_plan_with_no_payload_is_skipped_not_half_recorded():
    assert R.options_decisions(PROV, [FakeOptionsPlan(None)], RUN_DATE, "reports/d") == []
