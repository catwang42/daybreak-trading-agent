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
    prompt = render(
        "portfolio_manager",
        symbol=evidence.symbol,
        name=evidence.queued.name or evidence.symbol,
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
        return gateway.complete(prompt, tier="deep", schema=PortfolioDecision, max_tokens=1600), None
    except LLMError as exc:
        reason = str(exc)[:200]
        degraded.add(f"Portfolio manager {evidence.symbol}", reason)
        return None, reason
