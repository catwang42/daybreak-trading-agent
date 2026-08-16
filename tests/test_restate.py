"""One re-prompt when the prose and the table describe different trades.

WMB shipped a verdict summary quoting a $71.50 stop over a computed $73.17 and a
thesis quoting a $75.50 entry over a computed $73.20, and the report's answer was
three bullet points at the bottom of section 4. These tests pin the harder
policy: past 1% of the entry the author restates the paragraph once, and what
still disagrees afterwards is marked DEGRADED where a reader meets it.
"""

from tradingagent.data.validate import DegradedTracker
from tradingagent.llm import LLMError
from tradingagent.pipeline.restate import restate_quoted_figures
from tradingagent.pipeline.schemas import (
    PortfolioDecision,
    RestatedParagraph,
    RestatedProse,
)
from tradingagent.pipeline.trade_plan import (
    INVALIDATION_LINE,
    THESIS,
    VERDICT_SUMMARY,
    build_trade_plan,
    plan_texts,
    quoted_figure_corrections,
)
from tests.test_trade_plan import evidence, proposal


class FakeGateway:
    """Answers the one restatement call, or refuses it."""

    def __init__(self, reply=None, error=None):
        self.reply, self.error = reply, error
        self.calls: list[dict] = []

    def complete(self, prompt, *, tier="fast", schema=None, max_tokens=None, **kwargs):
        self.calls.append({"prompt": prompt, "tier": tier, "schema": schema,
                           "max_tokens": max_tokens})
        if self.error:
            raise LLMError(self.error)
        return self.reply


def decision(**kwargs):
    base = dict(
        rating="Overweight", confidence="M", price_target=112.0, time_horizon="4-8 weeks",
        executive_summary="Enter on the plan's terms.",
        investment_thesis="The base is intact and the trend is confirmed.",
        risk_ruling="Sided with the neutral seat.",
        invalidation="A close below the 50-day ends it.",
    )
    base.update(kwargs)
    return PortfolioDecision(**base)


def plan_for(prop=None, dec=None):
    """A $100 entry, a $96 stop, 4% of risk — the shape every test argues with."""
    return build_trade_plan(evidence(last=100.0), prop or proposal(), "Overweight", target=112.0)


def restate(gateway, prop, dec, tracker=None):
    plan = plan_for(prop, dec)
    return plan, restate_quoted_figures(
        gateway, evidence(last=100.0), plan, prop, dec,
        tracker if tracker is not None else DegradedTracker(),
    )


# --- the re-prompt --------------------------------------------------------


def test_a_material_disagreement_is_re_prompted_once_with_the_computed_plan():
    dec = decision(executive_summary="Buy the pullback with a stop at $92.50 below the base.")
    fixed = "Buy the pullback with a stop below the base, as the plan has it."
    gateway = FakeGateway(RestatedProse(
        paragraphs=[RestatedParagraph(label=VERDICT_SUMMARY, text=fixed)]
    ))

    plan, out = restate(gateway, proposal(), dec)

    assert len(gateway.calls) == 1, "exactly one re-prompt, whatever else is wrong"
    call = gateway.calls[0]
    assert call["tier"] == "smart" and call["schema"] is RestatedProse
    assert plan.table() in call["prompt"]
    assert "quotes a stop of $92.50" in call["prompt"] and "$96.00" in call["prompt"]
    assert dec.executive_summary in call["prompt"]

    assert out.decision.executive_summary == fixed
    assert out.degraded == {}
    assert len(out.notes) == 1
    assert VERDICT_SUMMARY in out.notes[0] and "not an edit by the pipeline" in out.notes[0]
    # The original object is untouched; the published one is a new instance.
    assert dec.executive_summary.endswith("$92.50 below the base.")


def test_every_disagreeing_paragraph_goes_in_the_same_call():
    dec = decision(
        executive_summary="Stop at $92.50.",
        investment_thesis="Entry at $104.00 once the range resolves.",
    )
    gateway = FakeGateway(RestatedProse(paragraphs=[
        RestatedParagraph(label=VERDICT_SUMMARY, text="Stop under the base."),
        RestatedParagraph(label=THESIS, text="Enter once the range resolves."),
    ]))

    _, out = restate(gateway, proposal(), dec)

    assert len(gateway.calls) == 1
    assert out.decision.investment_thesis == "Enter once the range resolves."
    assert len(out.notes) == 2 and out.degraded == {}


def test_a_disagreement_inside_the_tolerance_is_printed_and_never_costs_a_call():
    """0.9% of entry: worth a correction line, not worth a call."""
    dec = decision(executive_summary="Stop at $95.10, just under the shelf.")
    gateway = FakeGateway()

    plan, out = restate(gateway, proposal(), dec)

    assert gateway.calls == [] and out.notes == [] and out.degraded == {}
    assert out.decision is dec
    corrections = quoted_figure_corrections(plan, plan_texts(proposal(), dec))
    assert len(corrections) == 1 and "$95.10" in corrections[0]


# --- then DEGRADED --------------------------------------------------------


def test_a_restatement_that_still_argues_past_the_plan_is_marked_degraded():
    dec = decision(executive_summary="Stop at $92.50 below the base.")
    gateway = FakeGateway(RestatedProse(paragraphs=[
        RestatedParagraph(label=VERDICT_SUMMARY, text="Still stop at $92.50, I mean it."),
    ]))

    _, out = restate(gateway, proposal(), dec)

    assert len(gateway.calls) == 1, "one re-prompt, not two"
    assert out.notes == []
    assert VERDICT_SUMMARY in out.degraded
    reason = out.degraded[VERDICT_SUMMARY]
    assert "still disagrees after one re-prompt" in reason
    assert "Read the table, not the paragraph." in reason


def test_a_failed_re_prompt_leaves_the_paragraph_alone_and_degrades_the_field():
    dec = decision(executive_summary="Stop at $92.50 below the base.")
    tracker = DegradedTracker()
    gateway = FakeGateway(error="provider timeout")

    _, out = restate(gateway, proposal(), dec, tracker)

    assert out.decision is dec, "the prose stands as written when we cannot fix it"
    assert "the one re-prompt failed" in out.degraded[VERDICT_SUMMARY]
    assert any("Restatement" in source for source in tracker.sources)


def test_a_paragraph_the_model_skipped_is_degraded_not_silently_kept():
    dec = decision(executive_summary="Stop at $92.50.", investment_thesis="Entry at $104.00.")
    gateway = FakeGateway(RestatedProse(paragraphs=[
        RestatedParagraph(label=VERDICT_SUMMARY, text="Stop under the base."),
    ]))

    _, out = restate(gateway, proposal(), dec)

    assert out.decision.executive_summary == "Stop under the base."
    assert "the re-prompt returned nothing for it" in out.degraded[THESIS]
    assert out.decision.investment_thesis == "Entry at $104.00."


def test_a_restatement_that_will_not_fit_its_schema_is_refused():
    """The caps exist because each field feeds a later prompt.

    ``invalidation`` is capped at 700 characters by
    :class:`~tradingagent.pipeline.schemas.PortfolioDecision`; a restatement
    that runs past it is not published just because it has the right numbers.
    """
    dec = decision(invalidation="A close below the stop at $92.50 ends it.")
    gateway = FakeGateway(RestatedProse(paragraphs=[
        RestatedParagraph(label=INVALIDATION_LINE, text="A close below the stop ends it. " * 30),
    ]))

    _, out = restate(gateway, proposal(), dec)

    assert out.decision is dec
    assert "did not fit its schema" in out.degraded[INVALIDATION_LINE]


def test_the_ticker_is_degraded_and_the_marker_prints_where_the_paragraph_does():
    from tradingagent.report.deep import render_deep_report
    from tests.test_pipeline import FakeGateway as PipelineGateway, SAMPLES, evidence as pipe_evidence
    from tradingagent.pipeline.deep import analyze_ticker

    stubborn = SAMPLES[PortfolioDecision].model_copy(
        update={"executive_summary": "Half size, stop at $150.00."}
    )

    class Unrepentant(PipelineGateway):
        def complete(self, prompt, *, tier="fast", schema=None, **kwargs):
            if schema is PortfolioDecision:
                self.calls.append((tier, schema))
                return stubborn
            if schema is RestatedProse:
                self.calls.append((tier, schema))
                return RestatedProse(paragraphs=[
                    RestatedParagraph(label=VERDICT_SUMMARY, text="Half size, stop at $150.00.")
                ])
            return super().complete(prompt, tier=tier, schema=schema, **kwargs)

    result = analyze_ticker(Unrepentant(), pipe_evidence(), DegradedTracker(), rounds=1)

    assert VERDICT_SUMMARY in result.trade_plan.degraded_fields
    assert result.degraded
    assert any("verdict summary still contradicts" in r for r in result.degraded_reasons())
    assert result.trade_plan.journal_payload()["degraded_fields"] == [VERDICT_SUMMARY]

    report = render_deep_report(result)
    body = report.split("## 2.")[0]
    assert "Half size, stop at $150.00." in body
    assert "> **DEGRADED** — this paragraph quotes figures" in body
