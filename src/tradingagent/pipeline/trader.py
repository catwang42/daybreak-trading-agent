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


def render_proposal(
    proposal: TraderProposal | None, error: str | None = None, plan=None
) -> str:
    """The proposal as the risk committee and the manager see it.

    ``plan`` is the computed arithmetic (:mod:`.trade_plan`). Passing it means
    the risk seats critique the numbers that will be published rather than the
    trader's description of them — the two used to be able to differ.
    """
    if proposal is None:
        return f"The trader produced no proposal this run (DEGRADED: {error or 'unknown'})."
    level = (
        f"${proposal.invalidation_level:,.2f}"
        if proposal.invalidation_level is not None
        else "no level given"
    )
    lines = [
        f"Proposed action: **{proposal.action}**",
        "",
        proposal.reasoning,
        "",
        f"- Entry: {proposal.entry_type}"
        + (f" at ${proposal.entry_level:,.2f}" if proposal.entry_level is not None else ""),
        f"- Entry condition: {proposal.entry_condition or 'none stated'}",
        f"- Invalidation ({proposal.invalidation_type}): {level}",
    ]
    if plan is not None:
        lines += [
            "",
            "Computed from those levels against the run's snapshot "
            "(this is the arithmetic that will be published):",
            "",
            plan.table(),
        ]
        if plan.failures:
            lines += ["", f"**{plan.status}** — {'; '.join(plan.failures)}"]
    return "\n".join(lines)


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
        return gateway.complete(prompt, tier="smart", schema=TraderProposal), None
    except LLMError as exc:
        reason = str(exc)[:200]
        degraded.add(f"Trader {evidence.symbol}", reason)
        return None, reason
