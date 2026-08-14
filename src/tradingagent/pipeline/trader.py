"""The trader — turns the research manager's plan into a concrete proposal.

Ported from `reference/TradingAgents/tradingagents/agents/trader/trader.py`
(Apache-2.0, commit a33fd4c): the role's position between the research manager
and the risk committee, and its brief to commit to a specific action rather than
re-litigate the plan.

RESEARCH ONLY. Upstream's trader emits a decision a downstream harness can act
on; ours emits a *proposal in a markdown report*. There is no order path in this
codebase — Alpaca is paper-only and read-only here — and the prompt says so, so
the model never writes as if it were placing a trade.

Deviations: schema-enforced output (:class:`~.schemas.TraderProposal`), no
past-mistake memory (M3), and price levels come from our evidence pack rather
than from a tool call.
"""

from __future__ import annotations

import logging

from ..data.validate import DegradedTracker
from ..llm import LLMError, LLMGateway
from .analysts import AnalystResult, analyst_digest
from .evidence import Evidence
from .prompts_loader import render
from .schemas import ResearchPlan, TraderProposal

log = logging.getLogger(__name__)


def render_plan(plan: ResearchPlan | None, error: str | None = None) -> str:
    if plan is None:
        return f"The research manager produced no plan this run (DEGRADED: {error or 'unknown'})."
    return (
        f"Recommendation: **{plan.recommendation}**\n\n"
        f"How the debate resolved: {plan.resolution}\n\n"
        f"Instructions to the trader: {plan.strategic_actions}"
    )


def render_proposal(proposal: TraderProposal | None, error: str | None = None) -> str:
    if proposal is None:
        return f"The trader produced no proposal this run (DEGRADED: {error or 'unknown'})."
    entry = f"${proposal.entry_price:,.2f}" if proposal.entry_price is not None else "not specified"
    stop = f"${proposal.stop_loss:,.2f}" if proposal.stop_loss is not None else "not specified"
    return (
        f"Proposed action: **{proposal.action}**\n\n"
        f"{proposal.reasoning}\n\n"
        f"- Entry reference: {entry}\n"
        f"- Stop loss: {stop}\n"
        f"- Sizing: {proposal.position_sizing or 'not specified'}"
    )


def run_trader(
    gateway: LLMGateway,
    evidence: Evidence,
    analysts: list[AnalystResult],
    plan: ResearchPlan | None,
    degraded: DegradedTracker,
    plan_error: str | None = None,
) -> tuple[TraderProposal | None, str | None]:
    """One SMART-tier call. Returns ``(proposal, error)``; never raises."""
    prompt = render(
        "trader",
        symbol=evidence.symbol,
        name=evidence.queued.name or evidence.symbol,
        plan=render_plan(plan, plan_error),
        analyst_digest=analyst_digest(analysts),
        price_context=evidence.price_context(),
        market_context=evidence.market_context,
    )
    try:
        return gateway.complete(prompt, tier="smart", schema=TraderProposal, max_tokens=1000), None
    except LLMError as exc:
        reason = str(exc)[:200]
        degraded.add(f"Trader {evidence.symbol}", reason)
        return None, reason
