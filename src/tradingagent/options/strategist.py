"""The options strategist — one LLM call, after the portfolio manager has ruled.

Position in the pipeline: strictly downstream of the PM verdict. The verdict
chooses the strategy (:func:`~.strategies.strategy_for`), the chain and the
scoring choose the candidate set, and this role chooses among them and says what
the strike assumes. It cannot widen the search, invent a strike, or overrule the
equity call.

**Tier: SMART.** Stated here because CLAUDE.md fixes the budget by role.

- Not FAST: the task is a comparison across six-to-nine numbers per row where
  the wrong pick is plausible-looking rather than obviously broken, and the one
  rule that matters — cite only figures from the table — is exactly the rule a
  Haiku-class model breaks when a row is missing a field.
- Not DEEP: DEEP is reserved for the single portfolio-manager verdict per
  ticker, and this is not a second verdict. The arithmetic is already done in
  Python and checked by tests; what is left is judgement over a table of at most
  three rows, which is the same shape of work the trader and the risk committee
  do on the smart tier.

Cost: one smart call per ticker that has an overlay — i.e. never more than the
deep cap, and zero for Underweight/Sell names, which are skipped without a call.

RESEARCH ONLY. Nothing in this path can place an order.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ..data.validate import DegradedTracker
from ..llm import LLMError, LLMGateway
from ..pipeline.prompts_loader import render
from ..pipeline.schemas import OptionsRecommendation
from .strategies import CC, CSP, OptionCandidate

log = logging.getLogger(__name__)

TIER = "smart"

_STRATEGY_RATIONALE = {
    CSP: (
        "The portfolio manager is constructive and the entry level matters, so the "
        "overlay is a cash-secured put: get paid to bid below the market, and take "
        "assignment as the entry if the level breaks."
    ),
    CC: (
        "The portfolio manager sees no reason to add and no reason to sell, so the "
        "overlay is a covered call against existing shares: sell upside above a level "
        "the analysis does not expect to be exceeded inside the horizon."
    ),
}


@dataclass
class OptionsPlan:
    """One ticker's options view: the screen, the pick, and why."""

    symbol: str
    strategy: str | None
    candidates: list[OptionCandidate] = field(default_factory=list)
    recommendation: OptionsRecommendation | None = None
    chosen: OptionCandidate | None = None
    rejected: list[str] = field(default_factory=list)
    data_notes: list[str] = field(default_factory=list)
    skipped: str | None = None
    error: str | None = None

    @property
    def has_candidates(self) -> bool:
        return bool(self.candidates)

    @property
    def degraded(self) -> bool:
        return self.error is not None or (self.has_candidates and self.recommendation is None)

    def journal_payload(self) -> dict | None:
        """What the journal's ``options`` field records for this ticker.

        The full basis of the recommended contract plus the runners-up it beat:
        grading a recommendation later needs to know what the alternatives were,
        otherwise a good pick from a bad menu is indistinguishable from a good
        pick from a good one.
        """
        if self.strategy is None:
            return {"strategy": None, "skipped": self.skipped} if self.skipped else None
        if not self.candidates:
            return {
                "strategy": self.strategy,
                "recommended": None,
                "reason": "no candidate passed the screen",
                "rejected": self.rejected,
                "data_notes": self.data_notes,
            }
        payload = {
            "strategy": self.strategy,
            "recommended": self.chosen.basis() if self.chosen else None,
            "conviction": self.recommendation.conviction if self.recommendation else None,
            "rationale": self.recommendation.rationale if self.recommendation else None,
            "entry_note": self.recommendation.entry_note if self.recommendation else None,
            "assignment_view": self.recommendation.assignment_view if self.recommendation else None,
            "risk_note": self.recommendation.risk_note if self.recommendation else None,
            "alternatives": [
                c.basis() for c in self.candidates if self.chosen is None or c.symbol != self.chosen.symbol
            ],
            "screen_rejected": self.rejected,
            "data_notes": self.data_notes,
        }
        if self.error:
            payload["error"] = self.error
        return payload


# --------------------------------------------------------------------------
# rendering the model's inputs
# --------------------------------------------------------------------------
TABLE_HEADER = (
    "| Contract | Strike | Expiry | DTE | Delta | IV | Credit | Ann. yield | "
    "OI | Spread | Priced off | Earnings | Score |\n"
    "|---|---|---|---|---|---|---|---|---|---|---|---|---|"
)


def candidate_row(c: OptionCandidate) -> str:
    q = c.quote
    return (
        f"| `{q.symbol}` | ${q.strike:,.2f} | {q.expiry} | {q.dte} | "
        f"{abs(c.delta):.2f} | {c.iv * 100:.0f}% | ${c.credit:.2f} | "
        f"{c.annualized_yield_pct:.1f}% | {q.open_interest if q.open_interest is not None else '—'} | "
        f"{f'{q.spread_pct:.0f}%' if q.spread_pct is not None else 'one-sided'} | "
        f"{q.price_basis}, {q.freshness()} | {c.earnings_flag} | {c.score:.1f} |"
    )


def candidate_table(candidates: list[OptionCandidate]) -> str:
    return "\n".join([TABLE_HEADER] + [candidate_row(c) for c in candidates])


def score_detail(candidates: list[OptionCandidate]) -> str:
    blocks = []
    for c in candidates:
        lines = "\n".join(f"  - {note}" for note in c.notes)
        blocks.append(f"- `{c.symbol}` — score {c.score:.1f}\n{lines}")
    return "\n".join(blocks)


def verdict_block(
    rating: str,
    confidence: str,
    price_target: float | None,
    horizon: str | None,
    summary: str,
    invalidation: str,
) -> str:
    target = f"${price_target:,.2f}" if price_target is not None else "none set"
    return "\n".join(
        [
            f"- Rating: **{rating}** (confidence {confidence})",
            f"- Price target: {target} over {horizon or 'an unstated horizon'}",
            f"- Entry strategy as written: {summary}",
            f"- What would prove the call wrong: {invalidation}",
        ]
    )


def run_options_strategist(
    gateway: LLMGateway,
    plan: OptionsPlan,
    degraded: DegradedTracker,
    *,
    name: str,
    verdict: str,
    price_context: str,
    data_quality: str,
) -> OptionsPlan:
    """One SMART-tier call. Mutates and returns ``plan``; never raises.

    Called only when there is something to choose between: no candidates means
    no call, because there is no question to ask.
    """
    if plan.strategy is None or not plan.candidates:
        return plan

    prompt = render(
        "options_strategist",
        symbol=plan.symbol,
        name=name or plan.symbol,
        strategy=plan.strategy,
        verdict_block=verdict,
        strategy_rationale=_STRATEGY_RATIONALE.get(plan.strategy, ""),
        price_context=price_context,
        candidate_table=candidate_table(plan.candidates),
        score_detail=score_detail(plan.candidates),
        data_quality=data_quality,
    )
    try:
        recommendation = gateway.complete(prompt, tier=TIER, schema=OptionsRecommendation)
    except LLMError as exc:
        plan.error = str(exc)[:200]
        degraded.add(f"Options strategist {plan.symbol}", plan.error)
        return plan

    plan.recommendation = recommendation
    plan.chosen = _resolve(recommendation.recommended_contract, plan.candidates)
    if plan.chosen is None and recommendation.recommended_contract != "NONE":
        # The model named a contract that is not in the table. That is a
        # hallucinated strike, and printing it would be worse than degrading:
        # the reader cannot tell an invented contract from a screened one.
        plan.error = (
            f"strategist named {recommendation.recommended_contract!r}, which is not "
            "one of the screened candidates"
        )
        plan.recommendation = None
        degraded.add(f"Options strategist {plan.symbol}", plan.error)
    return plan


def _resolve(symbol: str, candidates: list[OptionCandidate]) -> OptionCandidate | None:
    wanted = symbol.strip().upper().strip("`")
    return next((c for c in candidates if c.symbol.upper() == wanted), None)
