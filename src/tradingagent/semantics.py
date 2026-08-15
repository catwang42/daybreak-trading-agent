"""What a derived reading means — and, more usefully, what it does not.

Three of the defects two reviewers found in shipped reports were not reasoning
errors. They were a model reading one of our own labels as if it meant
something else, and nothing in the pipeline could contradict it:

- The breadth composite's *cycle position* component measures where the breadth
  series sits between its own peak and trough. A report called it a valuation
  and wrote that the market "looks expensive on cycle position". It says
  nothing about price relative to earnings, and never did.
- "Estimated cycle phase: Early Cycle Recovery" is the output of matching four
  leading and four lagging sectors against a fixed table. A report turned that
  into a claim about where the US economy is in its business cycle.
- "Suggested equity exposure 60-75%" is a band lifted from the upstream skill's
  health-zone table. It has never been validated against a single outcome here,
  and it was printed next to computed position sizes as though it carried the
  same weight.

A fourth is the same failure in a different place: a 10b5-1 sale is scheduled
months ahead by a plan the seller cannot time, so it says nothing about what
that officer thinks today. Reports read them as conviction draining away.

So the vocabulary is now data. A :class:`Term` carries the label a reading must
be given, what it means, what it may never be called, and whether the mapping
behind it has been validated. :class:`Reading` binds a value to its term, and
anything that prints a reading — report section, prompt, journal entry — takes
the label and the prohibitions from here rather than inventing its own.

This adds no source and no call. It is the same numbers, named once.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: The mapping behind the reading has been checked against outcomes here.
VALIDATED = "VALIDATED"
#: Inherited or assumed, and never checked. Must be labelled wherever it prints.
UNVALIDATED = "UNVALIDATED"


@dataclass(frozen=True)
class Term:
    """One piece of vocabulary the pipeline is allowed to use."""

    key: str
    canonical_label: str
    means: str
    forbidden_interpretations: tuple[str, ...]
    validation: str = VALIDATED

    def render(self, value: Any) -> str:
        """``Sector rotation pattern: early-cycle-like`` — the only phrasing."""
        suffix = f" [{UNVALIDATED}]" if self.validation == UNVALIDATED else ""
        return f"{self.canonical_label}: {value}{suffix}"

    def guard(self) -> str:
        """The line a model is shown next to the reading."""
        forbidden = "; ".join(self.forbidden_interpretations)
        note = (
            " This mapping has never been validated against an outcome here, so it is "
            "context and may not be presented as a rule."
            if self.validation == UNVALIDATED
            else ""
        )
        return f'- **{self.canonical_label}** is {self.means}. It is NOT {forbidden}.{note}'


@dataclass(frozen=True)
class Reading:
    """A value plus the vocabulary that governs how it may be described."""

    term: Term
    value: Any
    basis: str = ""

    @property
    def canonical_label(self) -> str:
        return self.term.canonical_label

    @property
    def forbidden_interpretations(self) -> tuple[str, ...]:
        return self.term.forbidden_interpretations

    @property
    def validation(self) -> str:
        return self.term.validation

    def describe(self) -> str:
        text = self.term.render(self.value)
        return f"{text} ({self.basis})" if self.basis else text

    def to_dict(self) -> dict[str, Any]:
        return {
            "term": self.term.key,
            "canonical_label": self.canonical_label,
            "value": self.value,
            "basis": self.basis,
            "validation": self.validation,
            "forbidden_interpretations": list(self.forbidden_interpretations),
        }


BREADTH_CYCLE_POSITION = Term(
    key="breadth_cycle_position",
    canonical_label="Breadth cycle position",
    means=(
        "where the breadth series — the share of the universe above its 50-day average — "
        "sits between its own recent peak and trough"
    ),
    forbidden_interpretations=(
        "a valuation",
        "a statement about whether the market is expensive or cheap",
        "a stage of the earnings cycle",
        "a forecast of the next move",
    ),
)

BREADTH_POSTURE = Term(
    key="breadth_posture",
    canonical_label="Breadth regime + posture",
    means=(
        "the composite's health zone and the general posture the source skill pairs with it"
    ),
    forbidden_interpretations=(
        "a position-sizing instruction",
        "an allocation this system has tested",
        "a substitute for the computed per-trade size",
    ),
    validation=UNVALIDATED,
)

SECTOR_ROTATION = Term(
    key="sector_rotation",
    canonical_label="Sector rotation pattern",
    means=(
        "which textbook rotation pattern today's sector leaders and laggards most resemble, "
        "from a fixed lookup table"
    ),
    forbidden_interpretations=(
        "a statement about where the economy is in its business cycle",
        "an economic forecast",
        "evidence for or against any individual name",
    ),
)

INSIDER_PLANNED_SALE = Term(
    key="insider_planned_sale",
    canonical_label="10b5-1 planned sale",
    means=(
        "a sale executed by a plan adopted months earlier, on a schedule the seller cannot "
        "time; it is NON-DIRECTIONAL and is reported only because it is on the filing"
    ),
    forbidden_interpretations=(
        "a loss of conviction",
        "confidence eroding",
        "a bearish signal of any strength",
        "insider selling pressure",
    ),
)

GLOSSARY: dict[str, Term] = {
    term.key: term
    for term in (BREADTH_CYCLE_POSITION, BREADTH_POSTURE, SECTOR_ROTATION, INSIDER_PLANNED_SALE)
}


def guard_block(*keys: str) -> str:
    """The prohibitions for ``keys``, as a model reads them in a prompt."""
    lines = [GLOSSARY[key].guard() for key in keys if key in GLOSSARY]
    if not lines:
        return ""
    return "\n".join(
        ["These labels mean exactly what they say and nothing adjacent:", "", *lines]
    )
