"""Price levels that know what they are for.

The options stage used to receive ``{"50-day SMA": 31.84, "screener stop
reference": 31.58, "portfolio manager price target": 36.00}`` — a bag of
numbers with human labels and no meaning attached. Everything in the bag was
treated as a possible strike anchor, and nothing in it could act as a
constraint. Two defects followed directly:

- KMI's recommended cash-secured put had an assignment breakeven of $31.58
  against a stop reference of $31.58 — the same number. The overlay offered to
  buy the stock at exactly the price at which the plan says to be out of it,
  and nothing in the pipeline could see that the two numbers were the same
  number, because one was "screener stop reference" and the other was arithmetic
  on a strike.
- A covered call can be written below the base-case target the equity thesis
  is built on, capping the position under the number the report is arguing for.

So a level now carries a role, and the role decides what it may do:

``SUPPORT`` / ``RESISTANCE``
    Chart levels. These, and only these, anchor a strike.
``ENTRY`` / ``INVALIDATION`` / ``TARGET``
    The equity plan's own levels (see
    :mod:`tradingagent.pipeline.trade_plan`). They are constraints, never
    anchors: a strike is checked against them, not placed on them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

SUPPORT = "SUPPORT"
RESISTANCE = "RESISTANCE"
ENTRY = "ENTRY"
INVALIDATION = "INVALIDATION"
TARGET = "TARGET"

ROLES = (SUPPORT, RESISTANCE, ENTRY, INVALIDATION, TARGET)
#: The roles a strike may be anchored to. The plan's own levels are not here.
ANCHOR_ROLES = (SUPPORT, RESISTANCE)


@dataclass(frozen=True)
class PriceLevel:
    """One price, what it means, and where it came from."""

    label: str
    value: float
    role: str
    source: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"label": self.label, "value": self.value, "role": self.role, "source": self.source}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "PriceLevel":
        return cls(
            label=str(raw.get("label", "")),
            value=float(raw.get("value") or 0.0),
            role=str(raw.get("role", SUPPORT)).upper(),
            source=str(raw.get("source", "")),
        )

    def describe(self) -> str:
        return f"{self.label} ${self.value:,.2f} ({self.role.lower()})"


def chart_levels(levels: Iterable[PriceLevel]) -> dict[str, float]:
    """The anchor candidates, in the ``{label: price}`` shape anchoring wants."""
    return {lv.label: lv.value for lv in levels if lv.role in ANCHOR_ROLES and lv.value > 0}


def of_role(levels: Iterable[PriceLevel], role: str) -> PriceLevel | None:
    """The plan's level for ``role``, if the equity stage produced one."""
    for level in levels:
        if level.role == role and level.value > 0:
            return level
    return None


def classify(label: str, value: float, spot: float | None) -> str:
    """Role for a chart level: which side of the market it sits on."""
    if spot and value > spot:
        return RESISTANCE
    return SUPPORT


# --------------------------------------------------------------------------
# cross-strategy consistency
# --------------------------------------------------------------------------
#: What a candidate that assigns above the equity invalidation has to be called
#: if it is going to be recommended at all.
ACQUIRE_AFTER_FAILURE = "acquire-after-setup-failure"


def assignment_conflict(breakeven: float, invalidation: PriceLevel | None) -> str | None:
    """Would assignment hand us shares the equity plan has already stopped out of?

    A cash-secured put is sold as "get paid to bid for the entry the thesis
    wants". That holds only while the price we would be assigned at is *above*
    the level the plan calls the thesis dead: assignment then lands inside a
    live setup. At or below it, the put can only be assigned after the plan has
    already said to be out, so the same trade is a different trade — buying a
    setup that has failed — and it has to be labelled
    :data:`ACQUIRE_AFTER_FAILURE` rather than presented as an entry.

    KMI's own strategist made this argument in prose and then ignored it: it
    ruled out the $31 strike because "it would only assign after price has
    already broken the $31.40 stop", and recommended one whose breakeven sat
    exactly on the $31.58 stop reference.
    """
    if invalidation is None:
        return None
    if breakeven <= invalidation.value:
        return (
            f"assignment breakeven ${breakeven:,.2f} is at or below the equity "
            f"invalidation ${invalidation.value:,.2f} ({invalidation.label}) — the put "
            f"can only be assigned after the plan has already stopped out, so assignment "
            f"buys a setup that has failed rather than the entry the thesis wants"
        )
    return None


def upside_conflict(strike: float, target: PriceLevel | None) -> str | None:
    """Does this call cap the position below the case the equity thesis makes?"""
    if target is None or strike >= target.value:
        return None
    forgone = target.value - strike
    return (
        f"strike ${strike:,.2f} caps the position ${forgone:,.2f}/share below the "
        f"base-case target ${target.value:,.2f} ({target.label}) — the overlay sells "
        f"the upside the equity thesis is built on"
    )
