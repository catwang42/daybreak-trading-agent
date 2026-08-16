"""One re-prompt when the prose and the computed plan describe different trades.

The read-back in :mod:`.trade_plan` catches a figure the models quoted that the
arithmetic does not support, and prints it beneath the paragraph. That is the
right answer for a rounding-sized gap. It is not the right answer for WMB, whose
verdict summary quoted a $71.50 stop over a computed $73.17 and whose thesis
quoted a $75.50 entry over a computed $73.20: the reader was handed a paragraph
and a table describing two different positions, at two different sizes, with two
different invalidations, and a footnote saying so.

So a divergence past :data:`~.trade_plan.MATERIAL_DIVERGENCE_PCT` of the entry
gets CLAUDE.md's rule for malformed output — re-prompt once, then DEGRADED —
applied to output that is well-formed and wrong:

1. The paragraphs' author is shown the computed table, told which figures
   disagree, and asked to restate the same argument against the real numbers.
   One call, SMART tier, and only when a material divergence exists.
2. The restated text is validated against the source schema and read back
   again. What still disagrees is marked DEGRADED where it prints.

The pipeline never edits a paragraph itself. The text that replaces the original
is the model's own, produced with the plan in front of it, and the report says
which paragraphs went through this — an unaudited edit is the thing the whole
read-back exists to prevent.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ..data.validate import DegradedTracker
from ..llm import LLMError, LLMGateway
from .prompts_loader import render
from .schemas import RestatedProse
from .trade_plan import (
    PROSE_FIELDS,
    QuotedFigure,
    TradePlan,
    plan_texts,
    quoted_figure_mismatches,
)

log = logging.getLogger(__name__)

#: Reasoning headroom plus room to return the paragraphs at their original
#: length and half again — a restatement is the same paragraph, not a new one.
_REASONING_TOKENS = 2500
_CHARS_PER_TOKEN = 3.0
_LENGTH_HEADROOM = 1.5


@dataclass
class Restatement:
    """What the pass did, for the plan and the report to record."""

    #: The objects to publish — the originals when nothing was restated.
    proposal: object = None
    decision: object = None
    #: One audit line per paragraph the author rewrote.
    notes: list[str] = field(default_factory=list)
    #: ``label -> reason`` for what still disagrees, or could not be restated.
    degraded: dict[str, str] = field(default_factory=dict)
    #: Whether the LLM call was made at all (for the cost footer's benefit).
    called: bool = False


def restate_quoted_figures(
    gateway: LLMGateway,
    evidence,
    plan: TradePlan,
    proposal,
    decision,
    degraded: DegradedTracker,
) -> Restatement:
    """Re-prompt once for paragraphs that materially contradict ``plan``.

    Returns the objects to publish. Never raises: a failed call leaves the
    original prose in place and marks the affected fields DEGRADED, which is
    what a reader needs to know either way.
    """
    out = Restatement(proposal=proposal, decision=decision)
    sources = {"proposal": proposal, "decision": decision}
    texts = plan_texts(proposal, decision)
    material = [m for m in quoted_figure_mismatches(plan, texts) if m.material]
    if not material:
        return out

    labels = list(dict.fromkeys(m.label for m in material))
    log.info(
        "Restating %s: %s materially disagree(s) with the computed plan",
        evidence.symbol, ", ".join(labels),
    )
    prompt = render(
        "restate_figures",
        symbol=evidence.symbol,
        name=evidence.queued.name or evidence.symbol,
        plan_table=plan.table(),
        disagreements="\n".join(m.disagreement() for m in material),
        paragraphs="\n\n".join(f"### {label}\n\n{texts[label]}" for label in labels),
    )
    budget = _REASONING_TOKENS + int(
        sum(len(texts[label]) for label in labels) * _LENGTH_HEADROOM / _CHARS_PER_TOKEN
    )
    out.called = True
    try:
        reply = gateway.complete(
            prompt, tier="smart", schema=RestatedProse, max_tokens=budget
        )
    except LLMError as exc:
        reason = str(exc)[:200]
        degraded.add(f"Restatement {evidence.symbol}", reason)
        out.degraded = {
            label: _still_wrong(label, material, f"the one re-prompt failed ({reason})")
            for label in labels
        }
        return out

    restated = {p.label: p.text for p in reply.paragraphs if p.label in labels}
    updates: dict[str, dict[str, str]] = {"proposal": {}, "decision": {}}
    for label in labels:
        text = (restated.get(label) or "").strip()
        if not text:
            out.degraded[label] = _still_wrong(
                label, material, "the re-prompt returned nothing for it"
            )
            continue
        owner, attr = PROSE_FIELDS[label]
        updates[owner][attr] = text

    for owner, changes in updates.items():
        if not changes:
            continue
        replacement, error = _revalidate(sources[owner], changes)
        if replacement is None:
            for label in _labels_for(owner, changes):
                out.degraded[label] = _still_wrong(
                    label, material, f"the restated text did not fit its schema ({error})"
                )
            continue
        sources[owner] = replacement

    out.proposal, out.decision = sources["proposal"], sources["decision"]

    # Read the new prose back. A restatement that still argues past the plan is
    # the case CLAUDE.md stops at: one re-prompt, then say so.
    fresh = plan_texts(out.proposal, out.decision)
    remaining = {
        m.label: m for m in quoted_figure_mismatches(plan, fresh) if m.material
    }
    for label in labels:
        if label in out.degraded:
            continue
        if label in remaining:
            out.degraded[label] = _still_wrong(
                label, [remaining[label]], "it still disagrees after one re-prompt"
            )
        else:
            out.notes.append(
                f"{label} was restated once by its author against the computed plan "
                f"({_gaps(label, material)}). The text above is theirs, not an edit by "
                f"the pipeline."
            )
    if out.degraded:
        degraded.add(
            f"Trade plan {evidence.symbol}",
            f"{len(out.degraded)} paragraph(s) contradict the computed plan after a re-prompt",
        )
    return out


def _labels_for(owner: str, changes: dict[str, str]) -> list[str]:
    return [
        label
        for label, (holder, attr) in PROSE_FIELDS.items()
        if holder == owner and attr in changes
    ]


def _revalidate(source, changes: dict[str, str]):
    """Apply ``changes`` through the source's own schema, or refuse them.

    The caps in :mod:`.schemas` exist because each field feeds a later prompt,
    and a restatement is not exempt from them.
    """
    try:
        return type(source).model_validate({**source.model_dump(), **changes}), None
    except Exception as exc:  # noqa: BLE001 - a rejected restatement is a DEGRADED field
        return None, str(exc)[:120]


def _gaps(label: str, material: list[QuotedFigure]) -> str:
    return "; ".join(
        m.disagreement().lstrip("- ").rstrip(".") for m in material if m.label == label
    )


def _still_wrong(label: str, material: list[QuotedFigure], why: str) -> str:
    return (
        f"this paragraph quotes figures the computed plan does not support "
        f"({_gaps(label, material) or 'see the corrections below'}), and {why}. "
        f"Read the table, not the paragraph."
    )
