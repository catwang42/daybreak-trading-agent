"""The portfolio manager — the final verdict. The ONLY DEEP-tier call.

Ported from `reference/TradingAgents/tradingagents/agents/managers/portfolio_manager.py`
and `agents/utils/rating.py` (Apache-2.0, commit a33fd4c): the role as judge of
the risk debate, the five-tier rating vocabulary, and the instruction to rule
rather than summarise.

Model tiering (CLAUDE.md cost discipline): every other role runs on FAST or
SMART. This one call per ticker gets ``LLM_DEEP_MODEL`` because it is the only
output that reaches the journal as a decision, and the only place where a
subtler read of conflicting evidence changes what the human does.

Deviations: schema-enforced output with an added L/M/H ``confidence`` required
by `config/report-schema.md`, plus an explicit evidence-quality input so a
degraded run cannot produce a high-confidence verdict without saying why.
"""

from __future__ import annotations

import logging

from ..data.validate import DegradedTracker
from ..llm import LLMError, LLMGateway
from .analysts import AnalystResult, analyst_digest
from .debate import DebateResult
from .evidence import Evidence
from .prompts_loader import render
from .risk import RiskReview
from .schemas import PortfolioDecision, TraderProposal
from .trader import render_plan, render_proposal

log = logging.getLogger(__name__)


_BULLISH = {"Bullish", "Mildly Bullish"}
_BEARISH = {"Bearish", "Mildly Bearish"}
_RATING_DIRECTION = {
    "Buy": "long", "Overweight": "long",
    "Hold": "flat",
    "Underweight": "short", "Sell": "short",
}
_ACTION_DIRECTION = {"Buy": "long", "Hold": "flat", "Sell": "short"}


def confidence_checklist(
    evidence: Evidence,
    analysts: list[AnalystResult],
    debate: DebateResult,
    proposal: TraderProposal | None,
    risk: RiskReview,
) -> tuple[list[str], int, int]:
    """Six countable conditions behind the verdict's L/M/H.

    The Gate 2 run returned confidence M on all three tickers, and the rubric is
    why: H was reserved for "the analysts, the debate and the risk committee
    point the same way on complete evidence". The debate is adversarial by
    construction, and the evidence pack always reports at least one missing
    source (no social sentiment until this milestone), so H could never be
    earned and L — "the inputs conflict" — was true simultaneously. M was the
    only stable answer. These conditions are things the pipeline already knows,
    so the model counts instead of judging its own mood.
    """
    stances = [a.report.stance for a in analysts if a.report]
    bulls = sum(s in _BULLISH for s in stances)
    bears = sum(s in _BEARISH for s in stances)
    plan_dir = _RATING_DIRECTION.get(debate.plan.recommendation) if debate.plan else None
    trade_dir = _ACTION_DIRECTION.get(proposal.action) if proposal else None
    seated = sum(1 for v in risk.voices if v.take is not None)
    lows = [a.label for a in analysts if a.report and a.report.confidence == "L"]

    checks: list[tuple[bool, str]] = [
        (max(bulls, bears) >= 3, f"at least 3 of 4 analysts on the same side of neutral ({bulls} bullish, {bears} bearish)"),
        (not lows, "no analyst reported low confidence" + (f" (L from {', '.join(lows)})" if lows else "")),
        (len(stances) == 4, f"all four analysts reported ({len(stances)}/4)"),
        (
            plan_dir is not None and plan_dir == trade_dir,
            f"research manager and trader agree in direction (plan {plan_dir or 'n/a'}, trade {trade_dir or 'n/a'})",
        ),
        (seated == 3, f"all three risk seats reported ({seated}/3)"),
        (not evidence.blocking_gaps(), "no blocking data gaps in the evidence pack"),
    ]
    lines = [f"- [{'x' if held else ' '}] {label}" for held, label in checks]
    return lines, sum(held for held, _ in checks), len(checks)


def evidence_quality_note(evidence: Evidence, analysts: list[AnalystResult]) -> str:
    """What the verdict must not pretend it had."""
    failed = [a.label for a in analysts if not a.ok]
    lines = []
    if failed:
        lines.append(f"- These analysts did not report: {', '.join(failed)}.")
    else:
        lines.append("- All four analysts reported.")
    if evidence.missing:
        lines.append(f"- Missing or partial data: {'; '.join(evidence.missing)}.")
    suspect = (evidence.fundamentals.suspect_fields() if evidence.fundamentals else []) + (
        evidence.positioning.suspect_fields() if evidence.positioning else []
    )
    if suspect:
        lines.append(
            f"- Marked SUSPECT (outside the plausible range for the field, treat as "
            f"unavailable): {', '.join(suspect)}."
        )
    lines.append(
        "- No social-media sentiment and no options-market data were available in this run."
    )
    return "\n".join(lines)


def run_portfolio_manager(
    gateway: LLMGateway,
    evidence: Evidence,
    analysts: list[AnalystResult],
    debate: DebateResult,
    proposal: TraderProposal | None,
    risk: RiskReview,
    degraded: DegradedTracker,
    proposal_error: str | None = None,
) -> tuple[PortfolioDecision | None, str | None]:
    """One DEEP-tier call. Returns ``(decision, error)``; never raises."""
    checklist, confirmed, total = confidence_checklist(evidence, analysts, debate, proposal, risk)
    prompt = render(
        "portfolio_manager",
        symbol=evidence.symbol,
        name=evidence.queued.name or evidence.symbol,
        confidence_checklist="\n".join(checklist),
        confirmed=confirmed,
        total_conditions=total,
        analyst_digest=analyst_digest(analysts),
        debate_summary=debate.summary(),
        plan=render_plan(debate.plan, debate.plan_error),
        proposal=render_proposal(proposal, proposal_error),
        risk_debate=f"{risk.transcript()}\n\nAdjustments on the table:\n{risk.adjustments()}",
        price_context=evidence.price_context(),
        market_context=evidence.market_context,
        degraded_note=evidence_quality_note(evidence, analysts),
    )
    try:
        return gateway.complete(prompt, tier="deep", schema=PortfolioDecision), None
    except LLMError as exc:
        reason = str(exc)[:200]
        degraded.add(f"Portfolio manager {evidence.symbol}", reason)
        return None, reason
