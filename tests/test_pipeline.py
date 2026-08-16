"""Deep-pipeline tests: schema enforcement, orchestration, tiering, degradation."""

from datetime import date

import pandas as pd
import pytest
from pydantic import ValidationError

from tradingagent.data.validate import DegradedTracker
from tradingagent.llm import LLMError, TokenLedger
from tradingagent.pipeline.analysts import analyst_digest, run_analysts, stance_spread
from tradingagent.pipeline.context import CONTEXT_FILENAME, DeepContext, QueuedTicker
from tradingagent.pipeline.deep import analyze_ticker
from tradingagent.pipeline.evidence import Evidence
from tradingagent.pipeline.schemas import (
    AnalystReport,
    DebateTurn,
    PortfolioDecision,
    ResearchPlan,
    RiskTake,
    TraderProposal,
)
from tradingagent.report.deep import render_deep_index, render_deep_report
from tradingagent.report.writer import replace_section

# --- fixtures -----------------------------------------------------------


def queued(symbol="TST"):
    return QueuedTicker(
        symbol=symbol,
        name="Test Co",
        sector="Information Technology",
        industry="Software",
        priority=7,
        quick_rating="Overweight (M)",
        screener={"score": 85, "rating": "A-", "state": "ACTIONABLE_DAY1", "entry_ref": 101.0,
                  "stop_ref": 98.0, "risk_pct": 3.0, "volume_ratio_20d": 3.0,
                  "close_location_pct": 95.0, "triggers": "4pct_breakout"},
    )


def evidence(usable=True):
    from tradingagent.data.indicators import compute_indicators

    ev = Evidence(
        queued=queued(),
        run_date=date(2026, 8, 14),
        market_context="- Breadth 65/100 (Healthy).",
        macro_note="- none scheduled",
    )
    if usable:
        closes = [100.0 + i * 0.4 for i in range(260)]
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
        ev.indicators = compute_indicators("TST", frame)
    return ev


SAMPLES = {
    AnalystReport: AnalystReport(
        stance="Bullish", confidence="M", summary="RSI 68 with price 18% over the 50DMA.",
        key_points=["RSI 68", "volume 3x"], evidence_gaps="none",
    ),
    DebateTurn: DebateTurn(argument="The trend is intact.", strongest_point="Volume confirms.",
                           concession="Valuation is rich."),
    ResearchPlan: ResearchPlan(recommendation="Overweight", resolution="Bull carried it.",
                               strategic_actions="Enter half size above 101."),
    TraderProposal: TraderProposal(
        action="Buy", reasoning="Plan says Overweight.",
        entry_condition="Only on a close back above the 50-day.",
        entry_type="pullback", entry_level=101.0,
        invalidation_type="level", invalidation_level=98.0,
    ),
    RiskTake: RiskTake(argument="Stop is too tight.", recommended_adjustment="Widen to 2 ATR."),
    PortfolioDecision: PortfolioDecision(
        rating="Overweight", confidence="M", price_target=125.0, time_horizon="4-8 weeks",
        executive_summary="Half size above 101.", investment_thesis="Momentum with earnings support.",
        risk_ruling="Sided with the neutral seat.", invalidation="A close below the 50DMA.",
    ),
}


class FakeGateway:
    """Records the tier of every call and replies with a valid schema instance."""

    def __init__(self, fail_schemas=(), fail_tiers=()):
        self.ledger = TokenLedger()
        self.calls: list[tuple[str, type]] = []
        self.fail_schemas = set(fail_schemas)
        self.fail_tiers = set(fail_tiers)

    def complete(self, prompt, *, tier="fast", schema=None, max_tokens=1024, **kwargs):
        self.calls.append((tier, schema))
        if schema in self.fail_schemas or tier in self.fail_tiers:
            self.ledger.record_failure(tier)
            raise LLMError(f"forced failure for {getattr(schema, '__name__', schema)}")
        # Charge something so the per-ticker cost delta is observable.
        self.ledger.record(tier, f"provider/{tier}", 1000, 200, cost_usd=0.01)
        return SAMPLES[schema]

    def tiers(self):
        return [tier for tier, _ in self.calls]


# --- schemas ------------------------------------------------------------


def test_optional_numeric_fields_accept_the_placeholder_strings_models_emit():
    """Models write 'N/A' into a nullable float instead of omitting it."""
    proposal = TraderProposal.model_validate(
        {"action": "Hold", "reasoning": "Wait.", "entry_level": "N/A", "invalidation_level": ""}
    )
    assert proposal.entry_level is None and proposal.invalidation_level is None
    decision = PortfolioDecision.model_validate(
        {**SAMPLES[PortfolioDecision].model_dump(), "price_target": "none"}
    )
    assert decision.price_target is None


def _prose_caps():
    """Every ``max_length``-capped string field across the pipeline schemas."""
    for model in (AnalystReport, DebateTurn, ResearchPlan, TraderProposal, RiskTake,
                  PortfolioDecision):
        for name, info in model.model_fields.items():
            if info.annotation is not str:
                continue
            cap = next((m.max_length for m in info.metadata if hasattr(m, "max_length")), None)
            if cap:
                yield model, name, cap, info.description or ""


def test_prose_fields_are_length_capped_so_one_role_cannot_inflate_the_next_prompt():
    cap = next(c for m, n, c, _ in _prose_caps() if m is AnalystReport and n == "summary")
    with pytest.raises(ValidationError):
        AnalystReport(stance="Bullish", confidence="M", summary="x" * (cap + 1),
                      key_points=["a", "b"], evidence_gaps="none")


def test_every_capped_field_states_its_budget_in_the_unit_pydantic_enforces():
    """A cap the model is never told about costs a re-prompt, then the verdict.

    CRM 2026-08-14: `risk_ruling` carried a 900-character cap and a description
    with no budget at all, overran it twice, and the run ended DEGRADED with no
    rating. The number in the description must be the character cap itself.
    """
    for model, name, cap, description in _prose_caps():
        assert str(cap) in description, f"{model.__name__}.{name} does not state its {cap}-char cap"


def test_an_analyst_must_produce_at_least_two_key_points():
    with pytest.raises(ValidationError):
        AnalystReport(stance="Neutral", confidence="L", summary="thin",
                      key_points=["only one"], evidence_gaps="none")


def test_ratings_outside_the_five_tier_scale_are_rejected():
    with pytest.raises(ValidationError):
        ResearchPlan(recommendation="Strong Buy", resolution="x", strategic_actions="y")


# --- context handoff ----------------------------------------------------


def test_deep_context_round_trips_through_json(tmp_path):
    ctx = DeepContext(run_date="2026-08-14", market_context="- Breadth 65.", queue=[queued("V")])
    ctx.write(tmp_path)
    assert (tmp_path / CONTEXT_FILENAME).exists()

    back = DeepContext.read(tmp_path)
    assert back.date == date(2026, 8, 14)
    assert [q.symbol for q in back.queue] == ["V"]
    assert back.queue[0].screener["score"] == 85


def test_a_stale_context_schema_is_refused_rather_than_half_read(tmp_path):
    (tmp_path / CONTEXT_FILENAME).write_text('{"run_date":"2026-08-14","version":0,"queue":[]}')
    with pytest.raises(ValueError, match="re-run the discovery stage"):
        DeepContext.read(tmp_path)


def test_missing_context_names_the_command_that_produces_it(tmp_path):
    with pytest.raises(FileNotFoundError, match="--stage discovery"):
        DeepContext.read(tmp_path)


def test_the_cap_trims_the_queue_and_an_override_can_name_an_unqueued_ticker():
    ctx = DeepContext(run_date="2026-08-14", queue=[queued("A"), queued("B"), queued("C")])
    assert [q.symbol for q in ctx.limit(2)] == ["A", "B"]
    # An override still respects the cap, and an unqueued name gets a stub.
    picked = ctx.limit(3, only=["c", "ZZZ"])
    assert [q.symbol for q in picked] == ["C", "ZZZ"]
    assert picked[1].screener == {}
    assert "Screener detail unavailable" in picked[1].screener_markdown()


# --- orchestration ------------------------------------------------------


def test_the_happy_path_spends_each_tier_exactly_where_the_cost_policy_says():
    """4 fast analysts, 7 smart (2 debate + manager + trader + 3 risk), 1 deep verdict."""
    gw = FakeGateway()
    result = analyze_ticker(gw, evidence(), DegradedTracker(), rounds=1)

    assert gw.tiers().count("fast") == 4
    assert gw.tiers().count("smart") == 7
    assert gw.tiers().count("deep") == 1
    # The one deep call is the portfolio manager's, and it is the last thing to run.
    assert gw.calls[-1] == ("deep", PortfolioDecision)
    assert result.verdict == "Overweight (M)"
    assert result.total_calls == 12


def test_the_pm_tier_is_an_ab_arm_that_moves_the_spend_and_nothing_else():
    """--pm-tier smart is the cheaper arm of item 5's comparison."""
    gw = FakeGateway()
    result = analyze_ticker(gw, evidence(), DegradedTracker(), rounds=1, pm_tier="smart")

    assert gw.tiers().count("deep") == 0
    assert gw.tiers().count("smart") == 8  # the seven, plus the manager's verdict
    assert gw.calls[-1] == ("smart", PortfolioDecision)
    assert result.seat_tiers["portfolio_manager"] == "smart"
    assert result.seat_tiers["trader"] == "smart", "no other seat moves"


def test_every_result_names_the_tier_that_wrote_each_seat():
    result = analyze_ticker(FakeGateway(), evidence(), DegradedTracker(), rounds=1)
    assert result.seat_tiers["portfolio_manager"] == "deep"
    assert result.seat_tiers["analyst_technical"] == "fast"


def test_the_seat_tier_map_matches_the_call_sites_it_claims_to_mirror():
    """A record of the wrong tier is worse than none: it would settle the A/B wrongly."""
    import inspect
    import re
    from pathlib import Path

    from tradingagent.pipeline import deep as deep_module
    from tradingagent.pipeline.deep import SEAT_TIERS
    from tradingagent.pipeline.portfolio_manager import run_portfolio_manager

    pipeline_dir = Path(deep_module.__file__).parent
    seen: set[str] = set()
    for path in pipeline_dir.glob("*.py"):
        seen.update(re.findall(r'tier="(fast|smart|deep)"', path.read_text()))
    # The portfolio manager's is parameterised, so its literal lives in the
    # signature default rather than at the call site.
    seen.add(inspect.signature(run_portfolio_manager).parameters["tier"].default)

    assert set(SEAT_TIERS.values()) == seen
    assert SEAT_TIERS["portfolio_manager"] == "deep"


def test_a_second_debate_round_adds_exactly_two_more_smart_calls():
    one = FakeGateway()
    analyze_ticker(one, evidence(), DegradedTracker(), rounds=1)
    two = FakeGateway()
    analyze_ticker(two, evidence(), DegradedTracker(), rounds=2)
    assert two.tiers().count("smart") - one.tiers().count("smart") == 2


def test_rounds_are_clamped_to_the_two_round_ceiling():
    gw = FakeGateway()
    analyze_ticker(gw, evidence(), DegradedTracker(), rounds=9)
    assert gw.tiers().count("smart") == 9  # 4 debate turns + manager + trader + 3 risk


def test_per_ticker_cost_is_attributed_by_tier():
    gw = FakeGateway()
    result = analyze_ticker(gw, evidence(), DegradedTracker(), rounds=1)
    assert result.cost_by_tier["fast"].calls == 4
    assert result.cost_by_tier["deep"].calls == 1
    assert result.total_cost_usd == pytest.approx(0.12)
    assert result.total_tokens == 12 * 1200


def test_a_ticker_with_no_price_history_is_abandoned_before_any_token_is_spent():
    gw = FakeGateway()
    degraded = DegradedTracker()
    result = analyze_ticker(gw, evidence(usable=False), degraded, rounds=1)
    assert gw.calls == []
    assert result.verdict == "DEGRADED"
    assert "no usable price history" in (result.aborted or "")


def test_when_every_analyst_fails_the_debate_is_not_attempted():
    gw = FakeGateway(fail_schemas={AnalystReport})
    degraded = DegradedTracker()
    result = analyze_ticker(gw, evidence(), degraded, rounds=1)
    assert gw.tiers() == ["fast"] * 4
    assert "nothing to debate" in (result.aborted or "")
    assert degraded.entries


def test_one_failed_analyst_degrades_the_verdict_without_stopping_the_pipeline():
    class OneDown(FakeGateway):
        """Fails the fourth analyst — the sentiment seat, which runs last."""

        def complete(self, prompt, *, tier="fast", schema=None, **kwargs):
            if schema is AnalystReport and sum(s is AnalystReport for _, s in self.calls) == 3:
                self.calls.append((tier, schema))
                raise LLMError("provider timeout")
            return super().complete(prompt, tier=tier, schema=schema, **kwargs)

    gw = OneDown()
    result = analyze_ticker(gw, evidence(), DegradedTracker(), rounds=1)
    assert result.decision is not None  # still reached a verdict
    assert result.degraded
    assert any("Sentiment" in reason for reason in result.degraded_reasons())
    assert "DEGRADED" in render_deep_report(result)


def test_a_failed_portfolio_manager_leaves_a_report_that_states_it_has_no_verdict():
    gw = FakeGateway(fail_tiers={"deep"})
    result = analyze_ticker(gw, evidence(), DegradedTracker(), rounds=1)
    assert result.verdict == "DEGRADED"
    report = render_deep_report(result)
    assert "no verdict this run" in report
    assert "Nothing below should be read as a recommendation" in report


# --- rendering ----------------------------------------------------------


def test_the_deep_report_carries_every_schema_section_and_the_verbatim_disclaimer():
    result = analyze_ticker(FakeGateway(), evidence(), DegradedTracker(), rounds=1)
    report = render_deep_report(result)
    for heading in (
        "## 1. Verdict",
        "## 2. Analyst Summaries",
        "## 3. Bull vs Bear",
        "## 4. Trade Proposal",
        "## 5. Risk Review",
        "## 6. Options View",
        "## 7. Data Sources",
    ):
        assert heading in report
    assert report.index("## 1. Verdict") < report.index("## 7. Data Sources")
    assert (
        "Automated research output for personal study. Not financial advice. "
        "Paper trading only. Verify all data before acting." in report
    )


def test_section_four_publishes_the_computed_table_not_the_model_s_own_numbers():
    """The trader states intent; the arithmetic under it is the pipeline's.

    The sample verdict targets 125 on a ~200 close, so the plan is also a live
    check that an incoherent reward:risk is refused rather than printed.
    """
    result = analyze_ticker(FakeGateway(), evidence(), DegradedTracker(), rounds=1)
    report = render_deep_report(result)
    section = report[report.index("## 4. Trade Proposal"):report.index("## 5. Risk Review")]

    assert "| Entry |" in section and "| Reward : risk |" in section
    assert "Computed by the pipeline" in section
    assert "NO TRADE — inconsistent plan" in section
    assert result.trade_plan is not None and not result.trade_plan.actionable
    # The seats critique the published arithmetic, not a paraphrase of it.
    assert "no order path" in section


def test_the_verdict_states_where_the_sell_side_stands(monkeypatch):
    """Coverage was already in the evidence pack; it never reached the reader.

    The gap is printed as a percentage of *their* number because that is the
    question a reader has: how far from the crowd is this verdict standing.
    """
    from tradingagent.data.fundamentals import Positioning

    ev = evidence()
    ev.positioning = Positioning(
        symbol="TST",
        recommendation_key="buy",
        recommendation_spread="12 buy / 4 hold / 1 sell",
        analyst_count=17,
        target_mean=200.0,
        target_median=198.0,
    )
    result = analyze_ticker(FakeGateway(), ev, DegradedTracker(), rounds=1)
    section = render_deep_report(result)
    section = section[: section.index("## 2. Analyst Summaries")]

    assert "Analyst consensus" in section
    assert "17 analyst(s)" in section and "12 buy / 4 hold / 1 sell" in section
    assert "$200.00" in section and "$198.00" in section
    # The sample verdict targets 125 against a $200 mean.
    assert "-37.5%" in section


def test_a_name_with_no_coverage_says_so_rather_than_printing_a_blank_row():
    from tradingagent.data.fundamentals import Positioning

    ev = evidence()
    ev.positioning = Positioning(symbol="TST")
    result = analyze_ticker(FakeGateway(), ev, DegradedTracker(), rounds=1)
    section = render_deep_report(result)

    assert "no coverage reported" in section
    assert "analyst count unavailable" in section


def test_no_positioning_at_all_omits_the_row_entirely():
    result = analyze_ticker(FakeGateway(), evidence(), DegradedTracker(), rounds=1)
    assert "Analyst consensus" not in render_deep_report(result)


def test_the_consensus_gap_needs_both_numbers_to_mean_anything():
    from tradingagent.report.deep import consensus_gap

    assert consensus_gap(112.0, 100.0) == "+12.0%"
    assert consensus_gap(88.0, 100.0) == "-12.0%"
    assert consensus_gap(None, 100.0) == "—"
    assert consensus_gap(100.0, None) == "—"
    assert consensus_gap(100.0, 0.0) == "—"  # no dividing by an absent target


def test_the_index_carries_the_consensus_gap_as_its_own_column():
    from tradingagent.data.fundamentals import Positioning

    ev = evidence()
    ev.positioning = Positioning(symbol="TST", analyst_count=17, target_mean=200.0)
    result = analyze_ticker(FakeGateway(), ev, DegradedTracker(), rounds=1)
    index = render_deep_index([result])

    assert "| vs consensus |" in index
    assert "-37.5% (17 an.)" in index


def test_a_rejected_plan_says_so_in_the_brief_where_the_verdict_is_skimmed():
    result = analyze_ticker(FakeGateway(), evidence(), DegradedTracker(), rounds=1)
    assert "NO TRADE — inconsistent plan" in render_deep_index([result])


def test_the_brief_index_links_each_ticker_to_its_deep_report():
    results = [analyze_ticker(FakeGateway(), evidence(), DegradedTracker(), rounds=1)]
    index = render_deep_index(results)
    assert "[deep/TST.md](deep/TST.md)" in index
    assert "per ticker" in index


def test_patching_section_five_leaves_the_neighbouring_sections_intact():
    brief = (
        "# Brief\n\n## 4. Shortlist\n\nrows here\n\n"
        "## 5. Deep Analysis\n\n_pending_\n\n## 6. Options Candidates\n\nnot yet\n"
    )
    patched = replace_section(brief, "## 5. Deep Analysis", "| TST | Buy |")
    assert "| TST | Buy |" in patched
    assert "_pending_" not in patched
    assert "rows here" in patched and "not yet" in patched
    assert patched.index("## 4.") < patched.index("## 5.") < patched.index("## 6.")


def test_patching_a_missing_heading_returns_the_brief_unchanged():
    brief = "# Brief\n\n## 4. Shortlist\n\nrows\n"
    assert replace_section(brief, "## 5. Deep Analysis", "new") == brief


# --- digests ------------------------------------------------------------


def test_the_analyst_digest_names_the_ones_that_did_not_report():
    gw = FakeGateway(fail_schemas={AnalystReport})
    results = run_analysts(gw, evidence(), DegradedTracker())
    digest = analyst_digest(results)
    assert digest.count("DEGRADED") == 4
    assert stance_spread(results) == "no analyst reported"


def test_the_stance_spread_flags_agreement_between_the_analysts():
    results = run_analysts(FakeGateway(), evidence(), DegradedTracker())
    assert "unanimous" in stance_spread(results)


# --- countable confidence and blocking gaps (Gate 2 pre-work) -------------


def _analysts(stances, confidences=None):
    from tradingagent.pipeline.analysts import AnalystResult

    confidences = confidences or ["M"] * len(stances)
    return [
        AnalystResult(
            key=f"a{i}",
            label=f"Analyst {i}",
            report=AnalystReport(
                stance=stance, confidence=conf, summary="s",
                key_points=["p1", "p2"], evidence_gaps="none",
            ),
        )
        for i, (stance, conf) in enumerate(zip(stances, confidences))
    ]


def _risk(seats=3):
    from tradingagent.pipeline.risk import RiskReview, RiskVoice

    return RiskReview(voices=[
        RiskVoice(seat=f"Seat{i}", round_number=1, take=SAMPLES[RiskTake]) for i in range(seats)
    ])


def _full_evidence():
    from tradingagent.data.fundamentals import Fundamentals, Positioning
    from tradingagent.data.finnhub_client import NewsItem

    ev = evidence()
    ev.fundamentals = Fundamentals(symbol="TST", missing=[])
    ev.positioning = Positioning(symbol="TST")
    ev.news = [NewsItem(symbol="TST", headline="h", source="src", url="u", datetime_utc=0)]
    return ev


def test_blocking_gaps_ignores_the_permanent_social_sentiment_limit():
    """`missing` always names social sentiment; a confidence rubric must not count it."""
    from tradingagent.pipeline.debate import DebateResult
    from tradingagent.pipeline.portfolio_manager import confidence_checklist

    ev = _full_evidence()
    ev.missing.append("social/retail sentiment (not collected in this milestone)")
    assert ev.blocking_gaps() == []

    ev.news = []
    assert ev.blocking_gaps() == ["company news naming the company"]

    ev.indicators = None
    ev.fundamentals = None
    ev.positioning = None
    assert set(ev.blocking_gaps()) == {
        "price history", "company fundamentals", "positioning data",
        "company news naming the company",
    }

    # And the checklist condition tracks it, not `missing`.
    debate = DebateResult(plan=SAMPLES[ResearchPlan])
    lines, _, _ = confidence_checklist(
        _full_evidence(), _analysts(["Bullish"] * 4), debate, SAMPLES[TraderProposal], _risk()
    )
    assert "- [x] no blocking data gaps in the evidence pack" in lines


def test_confidence_checklist_reaches_the_high_band_on_a_clean_run():
    from tradingagent.pipeline.debate import DebateResult
    from tradingagent.pipeline.portfolio_manager import confidence_checklist

    lines, held, total = confidence_checklist(
        _full_evidence(),
        _analysts(["Bullish", "Bullish", "Mildly Bullish", "Neutral"]),
        DebateResult(plan=SAMPLES[ResearchPlan]),   # Overweight -> long
        SAMPLES[TraderProposal],                    # Buy -> long
        _risk(),
    )
    assert (held, total) == (6, 6)
    assert all(line.startswith("- [x]") for line in lines)


def test_confidence_checklist_reaches_the_low_band_on_a_thin_run():
    from tradingagent.pipeline.debate import DebateResult
    from tradingagent.pipeline.portfolio_manager import confidence_checklist

    ev = evidence()  # no fundamentals, no positioning, no news
    lines, held, total = confidence_checklist(
        ev,
        _analysts(["Bullish", "Bearish"], ["L", "M"]),
        DebateResult(plan=None),
        None,
        _risk(seats=1),
    )
    assert (held, total) == (0, 6)
    assert "L from Analyst 0" in "\n".join(lines)


def test_confidence_checklist_counts_a_plan_trade_disagreement():
    """The band has to be able to move; a direction split is what should move it."""
    from tradingagent.pipeline.debate import DebateResult
    from tradingagent.pipeline.portfolio_manager import confidence_checklist

    plan = ResearchPlan(recommendation="Underweight", resolution="Bear carried it.",
                        strategic_actions="Stand aside.")
    lines, held, _ = confidence_checklist(
        _full_evidence(),
        _analysts(["Bullish", "Bullish", "Bullish", "Neutral"]),
        DebateResult(plan=plan),          # short
        SAMPLES[TraderProposal],          # Buy -> long
        _risk(),
    )
    assert held == 5
    assert "- [ ] research manager and trader agree in direction (plan short, trade long)" in lines
