"""Pydantic contracts for every role in the deep-analysis pipeline.

Ported from `reference/TradingAgents/tradingagents/agents/schemas.py`
(Apache-2.0, commit a33fd4c). Upstream's insight is kept intact: prose is still
the artifact the human reads, so each role's reasoning lives in a text field
inside a typed envelope. Field descriptions double as output instructions —
:func:`tradingagent.llm._schema_instruction` ships the JSON Schema, descriptions
and all, to the model.

Deviations from upstream:
- Upstream leaves the four analysts free-text and types only the three
  decision-making roles. CLAUDE.md requires schema enforcement everywhere
  (re-prompt once, then DEGRADED), so every role is typed here.
- ``max_length`` on the prose fields is a cost control, not a style preference:
  each field feeds the next role's prompt, so an unbounded analyst report
  inflates every downstream call. Every capped field states its budget in
  *characters* in its own description, because that is the unit pydantic
  enforces — an early run asked the portfolio manager for a ruling with no
  stated budget, blew a 900-character cap twice, and lost the verdict to
  DEGRADED. Word counts stay in the descriptions as style guidance; the
  character number is the contract. Gate 2 then showed the models overrunning
  even a stated cap seven times in 36 calls (a 16% re-prompt surcharge), so the
  four schemas that fired — DebateTurn, ResearchPlan, RiskTake,
  PortfolioDecision — carry ~40% more headroom than the word target implies.
  The caps exist to stop one role inflating the next role's prompt, not to
  enforce brevity; the word count does that.
- ``confidence`` (L/M/H) is added to the portfolio decision because
  ``config/report-schema.md`` requires it in the per-ticker verdict line.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

# Canonical 5-tier scale, shared by the research manager, the portfolio manager,
# the shortlist quick take, and the journal. Upstream centralises the same list
# in agents/utils/rating.py for the same reason: drift between call sites.
Rating = Literal["Buy", "Overweight", "Hold", "Underweight", "Sell"]
RATINGS_5_TIER: tuple[str, ...] = ("Buy", "Overweight", "Hold", "Underweight", "Sell")
RATING_ORDER: dict[str, int] = {"Buy": 5, "Overweight": 4, "Hold": 3, "Underweight": 2, "Sell": 1}

Confidence = Literal["L", "M", "H"]
Stance = Literal["Bullish", "Mildly Bullish", "Neutral", "Mixed", "Mildly Bearish", "Bearish"]

# Models sometimes write a placeholder string into an optional numeric field
# instead of omitting it (upstream hit the same thing, issue #1058).
_NULLISH = {"", "none", "n/a", "na", "null", "nil", "-", "tbd", "unknown"}


def _nullish_to_none(value: object) -> object:
    if isinstance(value, str) and value.strip().lower() in _NULLISH:
        return None
    return value


class AnalystReport(BaseModel):
    """One analyst's read of its own evidence slice (FAST tier)."""

    stance: Stance = Field(
        description=(
            "Directional read of YOUR evidence only. Exactly one of: Bullish / "
            "Mildly Bullish / Neutral / Mixed / Mildly Bearish / Bearish. Use "
            "Mixed when your own evidence points both ways; use Neutral only "
            "when the evidence is genuinely silent."
        )
    )
    confidence: Confidence = Field(
        description=(
            "H when your evidence is complete and internally consistent; M when "
            "it is present but partial; L when key inputs were missing or stale."
        )
    )
    summary: str = Field(
        max_length=1100,
        description=(
            "One paragraph of at most 1100 characters (roughly 120 words), citing "
            "specific numbers from the evidence given to you. No preamble, no "
            "restating the task."
        ),
    )
    key_points: list[str] = Field(
        min_length=2,
        max_length=5,
        description="2-5 one-line findings, each naming the figure it rests on.",
    )
    evidence_gaps: str = Field(
        max_length=400,
        description=(
            "What you could not see that a professional would want, in one "
            "sentence of at most 400 characters. Write 'none' if your evidence "
            "was sufficient."
        ),
    )


class DebateTurn(BaseModel):
    """One bull or bear turn (SMART tier)."""

    argument: str = Field(
        max_length=3500,
        description=(
            "Your case, engaging directly with the other side's last turn rather "
            "than listing data. Conversational; aim for 250 words, hard limit "
            "3500 characters."
        ),
    )
    strongest_point: str = Field(
        max_length=700,
        description=(
            "The single strongest point in your case, in one sentence. Hard limit "
            "700 characters."
        ),
    )
    concession: str = Field(
        max_length=700,
        description=(
            "The one thing the other side is right about; hard limit 700 "
            "characters. Never write 'nothing' — if you cannot find a concession "
            "you have not read their argument."
        ),
    )


class ResearchPlan(BaseModel):
    """The research manager's ruling on the bull/bear debate (SMART tier)."""

    recommendation: Rating = Field(
        description=(
            "Exactly one of Buy / Overweight / Hold / Underweight / Sell. Reserve "
            "Hold for when the two sides are genuinely balanced; otherwise commit "
            "to the side with the stronger argument."
        )
    )
    resolution: str = Field(
        max_length=2100,
        description=(
            "Which arguments carried the debate and why, naming the bull and bear "
            "points you are ruling on. Aim for 150 words, hard limit 2100 characters."
        ),
    )
    strategic_actions: str = Field(
        max_length=1700,
        description=(
            "Concrete instructions for the trader, including position-sizing "
            "guidance consistent with the rating. Hard limit 1700 characters."
        ),
    )


class TraderProposal(BaseModel):
    """The trader's transaction proposal (SMART tier)."""

    action: Literal["Buy", "Hold", "Sell"] = Field(
        description="Exactly one of Buy / Hold / Sell. Sizing nuance is the portfolio manager's job."
    )
    reasoning: str = Field(
        max_length=1100,
        description=(
            "Why this action, anchored in the plan and the analysts. 2-4 "
            "sentences, at most 1100 characters."
        ),
    )
    entry_price: float | None = Field(default=None, description="Entry reference price, or null.")
    stop_loss: float | None = Field(default=None, description="Stop-loss price, or null.")
    position_sizing: str | None = Field(
        default=None, description="Sizing guidance, e.g. '3% of portfolio, half now'."
    )

    @field_validator("entry_price", "stop_loss", mode="before")
    @classmethod
    def _coerce(cls, v: object) -> object:
        return _nullish_to_none(v)


class RiskTake(BaseModel):
    """One risk analyst's critique of the trader's proposal (SMART tier)."""

    argument: str = Field(
        max_length=2800,
        description=(
            "Your critique, answering the other risk analysts directly. "
            "Conversational, no headings; aim for 180 words, hard limit 2800 "
            "characters."
        ),
    )
    recommended_adjustment: str = Field(
        max_length=700,
        description=(
            "The one concrete change you want made to the trade — size, stop, "
            "timing, or 'no change' with a reason. Hard limit 700 characters."
        ),
    )


class PortfolioDecision(BaseModel):
    """The final verdict (DEEP tier). This is what the journal records."""

    rating: Rating = Field(
        description="Final position rating. Exactly one of Buy / Overweight / Hold / Underweight / Sell."
    )
    confidence: Confidence = Field(
        description=(
            "Read off the confidence conditions supplied in the prompt: H when 5 "
            "or 6 of the six hold, M when 3 or 4 hold, L when 2 or fewer hold. "
            "Move one step off that band only if you name the condition you are "
            "overriding."
        )
    )
    price_target: float | None = Field(
        default=None,
        description=(
            "Soft price target for the stated horizon, or null if the evidence "
            "does not support one. A number, not a range."
        ),
    )
    time_horizon: str | None = Field(
        default=None, description="Holding period the rating applies to, e.g. '4-8 weeks'."
    )
    executive_summary: str = Field(
        max_length=1500,
        description=(
            "Entry strategy, sizing, key levels, horizon. 2-4 sentences, hard "
            "limit 1500 characters."
        ),
    )
    investment_thesis: str = Field(
        max_length=3100,
        description=(
            "The reasoning, anchored in specific evidence from the debate and the "
            "risk review. Aim for 200 words, hard limit 3100 characters."
        ),
    )
    risk_ruling: str = Field(
        max_length=2200,
        description=(
            "Your ruling on the risk debate: which of the aggressive, "
            "conservative, and neutral analysts you sided with, and what you "
            "changed in the trade as a result. Hard limit 2200 characters."
        ),
    )
    invalidation: str = Field(
        max_length=700,
        description=(
            "The specific observable that would prove this call wrong. One "
            "sentence, hard limit 700 characters."
        ),
    )

    @field_validator("price_target", mode="before")
    @classmethod
    def _coerce(cls, v: object) -> object:
        return _nullish_to_none(v)
