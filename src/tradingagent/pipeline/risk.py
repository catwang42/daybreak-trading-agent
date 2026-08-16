"""The three-voice risk committee — SMART tier, sequential so they can argue.

Ported from `reference/TradingAgents/tradingagents/agents/risk_mgmt/`
(Apache-2.0, commit a33fd4c): `aggressive_debator.py`, `conservative_debator.py`,
`neutral_debator.py`. The three seats, their briefs, and the requirement that
each engage the other two by name are upstream's.

Turn arithmetic follows `graph/conditional_logic.py`, which ends the risk debate
at ``count >= 3 * max_risk_discuss_rounds`` in Aggressive → Conservative →
Neutral order. One round is therefore all three seats speaking once, in that
order, each seeing what came before. We reuse ``DEBATE_ROUNDS`` for this so the
two debates cannot drift apart in cost.

Deviations: schema-enforced output, and no past-mistake memory (M3).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ..data.validate import DegradedTracker
from ..llm import LLMError, LLMGateway
from .analysts import AnalystResult, analyst_digest
from .evidence import Evidence
from .prompts_loader import render
from .schemas import ResearchPlan, RiskTake, TraderProposal
from .trader import render_plan, render_proposal

log = logging.getLogger(__name__)

# Upstream's speaking order. Aggressive opens so the caution has something to bite.
SEATS: tuple[tuple[str, str], ...] = (
    ("Aggressive", "risk_aggressive"),
    ("Conservative", "risk_conservative"),
    ("Neutral", "risk_neutral"),
)


@dataclass
class RiskVoice:
    seat: str
    round_number: int
    take: RiskTake | None
    error: str | None = None

    def transcript_entry(self) -> str:
        if self.take is None:
            return (
                f"**{self.seat} Risk Analyst, round {self.round_number}** — DEGRADED, "
                f"no critique produced ({self.error or 'unknown error'})."
            )
        return (
            f"**{self.seat} Risk Analyst, round {self.round_number}**\n{self.take.argument}\n"
            f"- Wants changed: {self.take.recommended_adjustment}"
        )


@dataclass
class RiskReview:
    voices: list[RiskVoice] = field(default_factory=list)

    def transcript(self) -> str:
        return "\n\n".join(v.transcript_entry() for v in self.voices) or "The risk committee did not sit."

    @property
    def ok(self) -> bool:
        return any(v.take is not None for v in self.voices)

    def adjustments(self) -> str:
        lines = [
            f"- {v.seat}: {v.take.recommended_adjustment}" for v in self.voices if v.take
        ]
        return "\n".join(lines) or "- none on the record"


def run_risk_committee(
    gateway: LLMGateway,
    evidence: Evidence,
    analysts: list[AnalystResult],
    plan: ResearchPlan | None,
    proposal: TraderProposal | None,
    degraded: DegradedTracker,
    rounds: int = 1,
    plan_error: str | None = None,
    proposal_error: str | None = None,
    trade_plan=None,
) -> RiskReview:
    """Aggressive → Conservative → Neutral, each reading the transcript so far."""
    review = RiskReview()
    digest = analyst_digest(analysts)
    plan_md = render_plan(plan, plan_error)
    proposal_md = render_proposal(proposal, proposal_error, plan=trade_plan)

    for round_number in range(1, max(1, rounds) + 1):
        for seat, prompt_name in SEATS:
            prompt = render(
                prompt_name,
                symbol=evidence.symbol,
                name=evidence.queued.name or evidence.symbol,
                proposal=proposal_md,
                plan=plan_md,
                analyst_digest=digest,
                price_context=evidence.price_context(),
                market_context=evidence.market_context,
                debate_so_far=(
                    review.transcript()
                    if review.voices
                    else "You are opening the review; nobody has spoken yet."
                ),
            )
            try:
                take = gateway.complete(prompt, tier="smart", schema=RiskTake)
                review.voices.append(RiskVoice(seat=seat, round_number=round_number, take=take))
            except LLMError as exc:
                reason = str(exc)[:200]
                degraded.add(f"{seat} risk analyst {evidence.symbol}", reason)
                review.voices.append(
                    RiskVoice(seat=seat, round_number=round_number, take=None, error=reason)
                )
    return review
