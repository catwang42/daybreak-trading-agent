"""The decision sheet: eight sections, in the order a decision gets made.

Section order is load-bearing and matches how the question is actually answered
on a phone at 07:00 — what kind of market is this, is there a reason not to act
at all, what would I act on, what am I skipping, is there an overlay worth
having, what changed overnight, how much should I trust today's run, and what
has the record said so far.

This module builds the *content*. :mod:`.html` turns it into markup and
:mod:`.charts` draws the pictures; keeping the three apart is what lets the
sheet be tested without rendering and rendered without a mail server.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .context import PresentationContext, read_or_none

log = logging.getLogger(__name__)

#: Setups shown in full on the sheet. Beyond this the reader is scrolling, and
#: the deep-analysis cap is 3-5 anyway — the limit is a guard, not a policy.
MAX_SETUPS = 5

#: Verdict badge colours. Deliberately not a red/green pair: Overweight and Buy
#: are different sizes of the same opinion and a reader skimming should be able
#: to tell them apart without reading the word.
BADGE_COLOURS = {
    "Buy": ("#065f46", "#d1fae5"),
    "Overweight": ("#047857", "#ecfdf5"),
    "Hold": ("#374151", "#f3f4f6"),
    "Neutral": ("#374151", "#f3f4f6"),
    "Underweight": ("#b45309", "#fffbeb"),
    "Sell": ("#991b1b", "#fee2e2"),
    "DEGRADED": ("#6b7280", "#f9fafb"),
}
DEFAULT_BADGE = ("#374151", "#f3f4f6")


def badge_colours(rating: str) -> tuple[str, str]:
    return BADGE_COLOURS.get(rating, DEFAULT_BADGE)


@dataclass
class Change:
    """One line of "what moved since the last session that produced verdicts"."""

    symbol: str
    kind: str  # new | dropped | upgraded | downgraded | unchanged
    detail: str


@dataclass
class DecisionSheet:
    """Everything the email body says, and nothing it cannot prove."""

    run_date: date
    context: PresentationContext | None = None
    changes: list[Change] = field(default_factory=list)
    compared_with: str = ""
    evidence: str = ""
    unavailable: list[str] = field(default_factory=list)

    # -- section 1 ---------------------------------------------------------
    @property
    def regime_line(self) -> str:
        """Breadth posture and rotation pattern, in the words they are allowed.

        Both come through :meth:`Reading.describe`, so an unvalidated reading
        arrives carrying its ``[UNVALIDATED]`` marker rather than depending on
        this layer to remember to add one.
        """
        if self.context is None:
            return ""
        regime = self.context.regime
        parts = [p for p in (regime.posture.described, regime.rotation.described) if p]
        return " · ".join(parts)

    @property
    def posture_extras(self) -> list[tuple[str, str]]:
        if self.context is None:
            return []
        regime = self.context.regime
        rows: list[tuple[str, str]] = []
        if regime.risk_regime:
            rows.append(("Risk regime", f"{regime.risk_regime} ({regime.risk_score:+.2f} spread)"))
        if regime.vix is not None:
            rows.append(("VIX", f"{regime.vix:.2f}"))
        if regime.leaders:
            rows.append(("Leading", ", ".join(regime.leaders)))
        if regime.laggards:
            rows.append(("Lagging", ", ".join(regime.laggards)))
        return rows

    # -- section 3 ---------------------------------------------------------
    @property
    def setups(self):
        return (self.context.setups[:MAX_SETUPS] if self.context else [])

    @property
    def setups_hidden(self) -> int:
        return max(0, len(self.context.setups) - MAX_SETUPS) if self.context else 0

    # -- section 7 ---------------------------------------------------------
    @property
    def confidence(self) -> str:
        """One word, earned by counting what failed rather than by asserting."""
        degraded = len(self.context.degraded) if self.context else 0
        if self.context is None:
            return "LOW"
        if degraded == 0 and not self.unavailable:
            return "HIGH"
        if degraded <= 2:
            return "MODERATE"
        return "LOW"

    @property
    def confidence_reasons(self) -> list[str]:
        reasons: list[str] = []
        if self.context is not None:
            reasons += [f"degraded source: {name}" for name in self.context.degraded]
        reasons += self.unavailable
        return reasons


def _rating_of(context: PresentationContext, symbol: str) -> str:
    for setup in context.setups:
        if setup.symbol == symbol:
            return setup.rating
    for avoid in context.avoids:
        if avoid.symbol == symbol:
            return avoid.rating
    return ""


_CONVICTION = ["Sell", "Underweight", "Neutral", "Hold", "Overweight", "Buy"]


def diff_verdicts(today: PresentationContext, prior: PresentationContext) -> list[Change]:
    """What changed between two sessions' verdicts.

    Compares ratings by their place on the conviction scale, so "Overweight ->
    Buy" reads as an upgrade rather than as an unexplained string change. A name
    that appears or disappears is reported as such: a dropped name is a decision
    too, and it is the one a reader holding the position most needs to see.
    """
    now = {s.symbol: s.rating for s in today.setups}
    now.update({a.symbol: a.rating for a in today.avoids})
    was = {s.symbol: s.rating for s in prior.setups}
    was.update({a.symbol: a.rating for a in prior.avoids})

    changes: list[Change] = []
    for symbol in sorted(set(now) | set(was)):
        before, after = was.get(symbol), now.get(symbol)
        if before is None:
            changes.append(Change(symbol, "new", f"new — {after}"))
        elif after is None:
            changes.append(Change(symbol, "dropped", f"dropped from the queue (was {before})"))
        elif before != after:
            kind = (
                "upgraded"
                if _rank(after) > _rank(before)
                else "downgraded"
                if _rank(after) < _rank(before)
                else "unchanged"
            )
            changes.append(Change(symbol, kind, f"{before} → {after}"))
    return changes


def _rank(rating: str) -> int:
    return _CONVICTION.index(rating) if rating in _CONVICTION else -1


def previous_context(reports_dir: Path, run_date: date) -> tuple[PresentationContext | None, str]:
    """The most recent earlier session that produced a presentation context.

    Walks backwards through the report directories rather than assuming
    yesterday: the job does not run at weekends, and a Monday email comparing
    itself against a Sunday that never happened would report every name as new.
    """
    if not reports_dir.is_dir():
        return None, ""
    candidates = sorted(
        (p for p in reports_dir.iterdir() if p.is_dir() and p.name < run_date.isoformat()),
        reverse=True,
    )
    for directory in candidates:
        context = read_or_none(directory)
        if context is not None and (context.setups or context.avoids):
            return context, directory.name
    return None, ""


def build_sheet(
    run_date: date,
    report_dir: Path,
    *,
    reports_dir: Path | None = None,
    evidence: str = "",
) -> DecisionSheet:
    """Assemble the sheet for one session from what is on disk."""
    context = read_or_none(report_dir)
    unavailable: list[str] = []
    if context is None:
        unavailable.append(
            "the decision sheet's data file is missing for this session — it is written "
            "by `--stage all`, so a session that predates it, or one whose run did not "
            "finish, can only be delivered as the attached research"
        )
        return DecisionSheet(
            run_date=run_date, context=None, evidence=evidence, unavailable=unavailable
        )

    if not context.regime.spy:
        unavailable.append("no SPY history in the record — the index chart is omitted")
    if context.regime.composite is None:
        unavailable.append("breadth did not compute — the gauge and the posture line are omitted")

    prior, compared_with = (
        previous_context(reports_dir, run_date) if reports_dir is not None else (None, "")
    )
    changes = diff_verdicts(context, prior) if prior is not None else []

    return DecisionSheet(
        run_date=run_date,
        context=context,
        changes=changes,
        compared_with=compared_with,
        evidence=evidence,
        unavailable=unavailable,
    )
