"""Bull vs bear debate and the research manager's ruling — SMART tier.

Ported from `reference/TradingAgents` (Apache-2.0, commit a33fd4c):
- `agents/researchers/bull_researcher.py` / `bear_researcher.py` — the adversarial
  pairing, the instruction to argue conversationally and rebut the other side
  directly, and the running debate history each turn is shown.
- `agents/managers/research_manager.py` — the judge role and, importantly, its
  refusal to default to Hold.
- `graph/conditional_logic.py` — the turn arithmetic. Upstream ends the
  investment debate when ``count >= 2 * max_debate_rounds`` with the bull moving
  first; one "round" is therefore one bull turn plus one bear turn. We keep that
  definition so ``DEBATE_ROUNDS=1`` means the same thing it does upstream.

Deviations:
- Upstream carries a `memory` of past mistakes retrieved by embedding
  similarity. We have no vector store in this milestone; the journal exists but
  is not yet fed back. Noted in PORTING_NOTES as an M3 item.
- Each turn is schema-enforced, so a turn is either a usable argument or an
  explicit DEGRADED marker in the transcript — never silent prose drift.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ..data.validate import DegradedTracker
from ..llm import LLMError, LLMGateway
from .analysts import AnalystResult, analyst_digest
from .evidence import Evidence
from .prompts_loader import render
from .schemas import DebateTurn, ResearchPlan

log = logging.getLogger(__name__)

MAX_ROUNDS = 2  # CLAUDE.md cost discipline: 1 by default, 2 is the ceiling.


@dataclass
class Turn:
    side: str  # "Bull" | "Bear"
    round_number: int
    turn: DebateTurn | None
    error: str | None = None

    def transcript_entry(self) -> str:
        if self.turn is None:
            return (
                f"**{self.side} Researcher, round {self.round_number}** — DEGRADED, "
                f"no argument produced ({self.error or 'unknown error'})."
            )
        return (
            f"**{self.side} Researcher, round {self.round_number}**\n{self.turn.argument}\n"
            f"- Strongest point: {self.turn.strongest_point}\n"
            f"- Concedes: {self.turn.concession}"
        )


@dataclass
class DebateResult:
    turns: list[Turn] = field(default_factory=list)
    plan: ResearchPlan | None = None
    plan_error: str | None = None
    rounds: int = 1

    def transcript(self) -> str:
        return "\n\n".join(t.transcript_entry() for t in self.turns) or "No debate took place."

    def last(self, side: str) -> Turn | None:
        return next((t for t in reversed(self.turns) if t.side == side and t.turn), None)

    def strongest(self, side: str) -> str:
        turn = self.last(side)
        return turn.turn.strongest_point if turn and turn.turn else "no argument on the record"

    def summary(self) -> str:
        """Compact debate view for the trader, the risk team and the manager."""
        parts = [
            f"Bull's strongest point: {self.strongest('Bull')}",
            f"Bear's strongest point: {self.strongest('Bear')}",
        ]
        if self.plan:
            parts += [
                f"Research manager ruled: {self.plan.recommendation}",
                f"Resolution: {self.plan.resolution}",
            ]
        else:
            parts.append(f"Research manager: DEGRADED ({self.plan_error or 'no ruling'}).")
        return "\n".join(parts)


def run_debate(
    gateway: LLMGateway,
    evidence: Evidence,
    analysts: list[AnalystResult],
    degraded: DegradedTracker,
    rounds: int = 1,
) -> DebateResult:
    """Alternate bull and bear for ``rounds`` rounds, then have the manager rule."""
    rounds = max(1, min(rounds, MAX_ROUNDS))
    digest = analyst_digest(analysts)
    result = DebateResult(rounds=rounds)

    for round_number in range(1, rounds + 1):
        for side, prompt_name in (("Bull", "researcher_bull"), ("Bear", "researcher_bear")):
            opponent = "Bear" if side == "Bull" else "Bull"
            last = result.last(opponent)
            prompt = render(
                prompt_name,
                symbol=evidence.symbol,
                name=evidence.queued.name or evidence.symbol,
                analyst_digest=digest,
                market_context=evidence.market_context,
                debate_history=result.transcript() if result.turns else "This is the opening turn.",
                opponent_argument=(
                    last.turn.argument
                    if last and last.turn
                    else f"The {opponent.lower()} has not spoken yet — open the case."
                ),
                round_number=round_number,
                total_rounds=rounds,
            )
            try:
                turn = gateway.complete(prompt, tier="smart", schema=DebateTurn, max_tokens=1400)
                result.turns.append(Turn(side=side, round_number=round_number, turn=turn))
            except LLMError as exc:
                reason = str(exc)[:200]
                degraded.add(f"{side} researcher {evidence.symbol} r{round_number}", reason)
                result.turns.append(
                    Turn(side=side, round_number=round_number, turn=None, error=reason)
                )

    prompt = render(
        "research_manager",
        symbol=evidence.symbol,
        name=evidence.queued.name or evidence.symbol,
        analyst_digest=digest,
        market_context=evidence.market_context,
        debate_transcript=result.transcript(),
    )
    try:
        result.plan = gateway.complete(prompt, tier="smart", schema=ResearchPlan, max_tokens=1400)
    except LLMError as exc:
        result.plan_error = str(exc)[:200]
        degraded.add(f"Research manager {evidence.symbol}", result.plan_error)
    return result
