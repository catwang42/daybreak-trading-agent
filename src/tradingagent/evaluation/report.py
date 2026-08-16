"""The weekly evaluation report — ``evaluation/YYYY-WW.md``.

One rule governs every table below: **no number the sample cannot support.**
A hit rate over four decisions is not a hit rate, it is four decisions, and
printing "75%" next to it invites exactly the conclusion the data forbids. So
counts are always shown, rates are shown only at or above
:data:`~tradingagent.evaluation.grading.MIN_SAMPLE`, and anything short of that
is labelled ``INSUFFICIENT`` in the row itself rather than in a footnote nobody
reads.

The report spends no tokens and calls no model. It is a rendering of the
ledger, which means two people running it on the same records get the same
file, and a number in it can always be traced back to a row.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Iterable, Sequence

from ..report.render import DISCLAIMER
from .grading import (
    GRADED_HORIZONS,
    MIN_SAMPLE,
    GradingReport,
    RatingRecord,
    SelectionComparison,
    compare_selection,
    grade,
    rating_records,
)
from .ledger import CANDIDATES, DECISIONS, OUTCOMES, RUNS, ExperimentLedger
from .outcomes import HORIZONS

log = logging.getLogger(__name__)

INSUFFICIENT = "INSUFFICIENT"
EMPTY = "—"


@dataclass
class WeeklyReport:
    week: str
    run_date: date
    markdown: str = ""
    runs: int = 0
    candidates: int = 0
    decisions: int = 0
    observations: int = 0
    resolved: int = 0
    matured: int = 0
    backfilled: int = 0
    grading: GradingReport | None = None
    ratings: list[RatingRecord] = field(default_factory=list)
    selection: list[SelectionComparison] = field(default_factory=list)
    triggers: dict[str, int] = field(default_factory=dict)
    tiers: dict[str, dict[str, int]] = field(default_factory=dict)
    caveats: list[str] = field(default_factory=list)

    @property
    def sufficient(self) -> bool:
        return self.resolved >= MIN_SAMPLE


def weekly_report(store: ExperimentLedger, run_date: date) -> WeeklyReport:
    """Read every ledger stream and render the week's evidence."""
    from .stage import week_label

    decisions = list(store.latest(DECISIONS, "decision_id").values())
    outcomes = list(store.latest(OUTCOMES, "decision_id").values())
    candidates = list(store.latest(CANDIDATES, "candidate_id").values())
    runs = store.read(RUNS)

    grading = grade(decisions, outcomes)
    report = WeeklyReport(
        week=week_label(run_date),
        run_date=run_date,
        runs=len({_run_id(r) for r in runs}),
        candidates=len(candidates),
        decisions=len(decisions),
        observations=len(grading.observations),
        resolved=grading.resolved,
        matured=sum(1 for o in outcomes if len(o.get("horizons") or {}) >= len(HORIZONS)),
        backfilled=sum(1 for d in decisions if _backfilled(d)),
        grading=grading,
        ratings=rating_records(grading.observations),
        # Candidates are keyed on (date, ticker) and outcomes carry both, so the
        # two arms join without going through decision_id.
        selection=compare_selection(candidates, outcomes),
        triggers=_trigger_counts(outcomes),
        tiers=_tier_counts(decisions),
    )
    report.caveats = _caveats(report)
    report.markdown = render(report)
    return report


def _run_id(row: dict[str, Any]) -> str:
    return str((row.get("provenance") or {}).get("run_id", ""))


def _backfilled(row: dict[str, Any]) -> bool:
    return bool((row.get("provenance") or {}).get("backfilled"))


def _trigger_counts(outcomes: Iterable[dict[str, Any]]) -> dict[str, int]:
    counts = {"with_plan": 0, "triggered": 0, "stop_first": 0, "target_first": 0, "open": 0}
    for row in outcomes:
        if row.get("entry_triggered") is None:
            continue
        counts["with_plan"] += 1
        if not row.get("entry_triggered"):
            continue
        counts["triggered"] += 1
        first = str(row.get("first_hit") or "")
        if first == "stop":
            counts["stop_first"] += 1
        elif first == "target":
            counts["target_first"] += 1
        else:
            counts["open"] += 1
    return counts


def _tier_counts(decisions: Iterable[dict[str, Any]]) -> dict[str, dict[str, int]]:
    """seat -> tier -> how many decisions that seat was written at that tier.

    Nothing reads this yet. It exists so that the day someone runs a week of
    ``--pm-tier smart``, the comparison is already computable from records that
    were written before anyone thought to ask for it.
    """
    out: dict[str, dict[str, int]] = {}
    for row in decisions:
        for seat, tier in (row.get("seat_tiers") or {}).items():
            out.setdefault(str(seat), {}).setdefault(str(tier), 0)
            out[str(seat)][str(tier)] += 1
    return out


def _caveats(report: WeeklyReport) -> list[str]:
    """What this week's file is not entitled to claim."""
    out: list[str] = []
    if report.resolved < MIN_SAMPLE:
        out.append(
            f"{report.resolved} resolved observation(s) against a {MIN_SAMPLE}-observation "
            "minimum: every rate below is a count, not an estimate, and no source can "
            "graduate on this evidence."
        )
    if report.matured == 0:
        out.append(
            "No decision has reached its 60-session horizon yet, so nothing here speaks "
            "to the horizon the portfolio manager actually writes."
        )
    if report.backfilled:
        out.append(
            f"{report.backfilled} decision(s) were backfilled from the pre-ledger journal. "
            "They carry no config hash, prompt versions or per-source attribution, and "
            "cannot support any claim about *why* a call was made."
        )
    if report.grading and not report.grading.sources:
        out.append("No signal source has a resolved observation yet; the grading table is empty.")
    disputed = {name for c in report.selection for name in c.disputed}
    if not disputed:
        out.append(
            "The signals-adjusted shortlist has not yet differed from the shipped one, so "
            "control and treatment are the same list and the comparison is uninformative."
        )
    return out


# --- rendering ----------------------------------------------------------------


def render(report: WeeklyReport) -> str:
    lines: list[str] = [
        f"# Daybreak evaluation — {report.week}",
        "",
        f"_Week ending {report.run_date.isoformat()}. Rendered from the experiment "
        "ledger; no model was called and no number here is generated text._",
        "",
        f"**Verdict: {_verdict(report)}**",
        "",
    ]
    lines += _sample_section(report)
    lines += _ratings_section(report)
    lines += _signals_section(report)
    lines += _selection_section(report)
    lines += _plans_section(report)
    lines += _tiers_section(report)
    lines += _caveats_section(report)
    lines += ["", f"_{DISCLAIMER}_", ""]
    return "\n".join(lines)


def _verdict(report: WeeklyReport) -> str:
    if not report.decisions:
        return "no decisions in the ledger yet — nothing to evaluate"
    if not report.resolved:
        return (
            f"{report.decisions} decision(s) logged, none resolved yet — "
            "INSUFFICIENT DATA for any claim about performance"
        )
    if not report.sufficient:
        return (
            f"{report.resolved} of {MIN_SAMPLE} observations needed — "
            "INSUFFICIENT DATA; counts only below"
        )
    return f"{report.resolved} resolved observations — rates below are reportable"


def _sample_section(report: WeeklyReport) -> list[str]:
    return [
        "## 1. Sample",
        "",
        "| | |",
        "| --- | ---: |",
        f"| Runs logged | {report.runs} |",
        f"| Candidates screened (full pool) | {report.candidates} |",
        f"| Decisions recorded | {report.decisions} |",
        f"| Observations (ticker × date) | {report.observations} |",
        f"| ...with at least one horizon resolved | {report.resolved} |",
        f"| ...fully matured (all {len(HORIZONS)} horizons) | {report.matured} |",
        f"| Backfilled from the pre-ledger journal | {report.backfilled} |",
        "",
        "One observation is one ticker on one decision date. The discovery quick take "
        "and the deep dive on the same name are the same observation, counted once.",
        "",
    ]


def _ratings_section(report: WeeklyReport) -> list[str]:
    lines = [
        "## 2. Decisions by rating",
        "",
        "Directionally right *against SPY*: a bullish rating that beat the market or a "
        "bearish one that trailed it. Hold is not a directional call and is reported "
        "without a hit rate.",
        "",
        "| Rating | Horizon | n | Hit rate | Mean excess |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    if not report.ratings:
        lines.append(f"| {EMPTY} | {EMPTY} | 0 | {INSUFFICIENT} | {EMPTY} |")
    for record in report.ratings:
        rate = (
            _pct(record.hit_rate)
            if record.sufficient and record.rating not in {"Hold"}
            else INSUFFICIENT
        )
        lines.append(
            f"| {record.rating} | {record.horizon}d | {record.n} | {rate} "
            f"| {_signed(record.mean_excess)} |"
        )
    lines.append("")
    return lines


def _signals_section(report: WeeklyReport) -> list[str]:
    grading = report.grading
    lines = [
        "## 3. Signal sources (grading v2)",
        "",
        "Graded on **excess** return, not raw. *Lift* is the source's mean directional "
        "excess minus the pool's own mean excess — what it added to a price-only screen. "
        "A source can be accurate and have zero lift if it is agreeing with momentum the "
        "screener had already found.",
        "",
        "| Source | Horizon | n | Accuracy | Mean excess | Baseline | Lift | Standing |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    grades = grading.grades if grading else []
    if not grades:
        lines.append(
            f"| {EMPTY} | {EMPTY} | 0 | {INSUFFICIENT} | {EMPTY} | {EMPTY} | {EMPTY} "
            "| no resolved observations |"
        )
    for row in sorted(grades, key=lambda g: (g.source, g.horizon)):
        reportable = row.sufficient
        lines.append(
            f"| {row.source} | {row.horizon}d | {row.samples} "
            f"| {_pct(row.accuracy) if reportable else INSUFFICIENT} "
            f"| {_signed(row.mean_excess) if reportable else INSUFFICIENT} "
            f"| {_signed(row.baseline_excess)} "
            f"| {_signed(row.lift) if reportable else INSUFFICIENT} "
            f"| {row.standing} |"
        )
    lines += [
        "",
        "Every source is still SHADOW: nothing in this pipeline grants a source influence "
        "over the shortlist without a human editing the weights.",
        "",
    ]
    return lines


def _selection_section(report: WeeklyReport) -> list[str]:
    lines = [
        "## 4. Control vs treatment (which shortlist would have been better)",
        "",
        "The shipped shortlist is price-only, so it *is* the control. The treatment is the "
        "counterfactual shortlist the same run would have produced had the signal "
        "adjustments been live. Both are logged every day; neither costs an extra run.",
        "",
        "| Horizon | Control n | Control excess | Treatment n | Treatment excess | Difference |",
        "| ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in report.selection:
        difference = _signed(row.difference) if row.sufficient else INSUFFICIENT
        lines.append(
            f"| {row.horizon}d | {row.control_n} | {_signed(row.control_excess)} "
            f"| {row.treatment_n} | {_signed(row.treatment_excess)} | {difference} |"
        )
    disputed = sorted({name for c in report.selection for name in c.disputed})
    lines += [
        "",
        (
            f"Names the two lists disagreed on: {', '.join(disputed)}."
            if disputed
            else "The two lists have not yet disagreed on a single name."
        ),
        "",
    ]
    return lines


def _plans_section(report: WeeklyReport) -> list[str]:
    counts = report.triggers
    with_plan = counts.get("with_plan", 0)
    triggered = counts.get("triggered", 0)
    return [
        "## 5. Trade plans",
        "",
        "| | |",
        "| --- | ---: |",
        f"| Decisions that published an entry level | {with_plan} |",
        f"| Entry triggered | {triggered} |",
        f"| ...stop hit first | {counts.get('stop_first', 0)} |",
        f"| ...target hit first | {counts.get('target_first', 0)} |",
        f"| ...still open at the end of the window | {counts.get('open', 0)} |",
        "",
        "A bar spanning both the stop and the target is scored as the stop: daily bars "
        "cannot resolve the order, and the pessimistic reading is the honest one.",
        "",
    ]


def _tiers_section(report: WeeklyReport) -> list[str]:
    lines = [
        "## 6. Model tiers (logged, not yet tested)",
        "",
        "Recorded so a future A/B — quick take vs deep dive, or a week of "
        "`--pm-tier smart` — is computable from records that already exist.",
        "",
        "| Seat | Tier | Decisions |",
        "| --- | --- | ---: |",
    ]
    if not report.tiers:
        lines.append(f"| {EMPTY} | {EMPTY} | 0 |")
    for seat in sorted(report.tiers):
        for tier in sorted(report.tiers[seat]):
            lines.append(f"| {seat} | {tier} | {report.tiers[seat][tier]} |")
    lines.append("")
    return lines


def _caveats_section(report: WeeklyReport) -> list[str]:
    lines = ["## 7. What this week cannot tell you", ""]
    if not report.caveats:
        lines.append("Nothing material — the sample supports every rate printed above.")
    for caveat in report.caveats:
        lines.append(f"- {caveat}")
    lines.append("")
    return lines


def _pct(value: float | None) -> str:
    return f"{value * 100:.0f}%" if value is not None else EMPTY


def _signed(value: float | None) -> str:
    return f"{value:+.2f}%" if value is not None else EMPTY


# --- the Friday email ---------------------------------------------------------

#: How many source rows the email carries before it stops being an email.
EVIDENCE_SOURCE_ROWS = 6


def evidence_section(report: WeeklyReport) -> str:
    """The "Evidence so far" block appended to the Friday brief.

    Short by design. The email says how much evidence exists and where the full
    file is; it does not try to be the file. If the sample cannot support a
    rate, the email says that in its first line rather than burying it.
    """
    lines = [
        "## Evidence so far",
        "",
        f"_Week {report.week} · full report: `evaluation/{report.week}.md`_",
        "",
    ]
    if not report.resolved:
        lines += [
            f"{report.decisions} decision(s) are in the ledger and none has resolved yet. "
            "**INSUFFICIENT DATA** — no claim about whether this research works is "
            "available, and none will be until decisions mature.",
            "",
        ]
        return "\n".join(lines)

    lines.append(
        f"{report.resolved} resolved observation(s) of {report.observations} logged; "
        f"{report.matured} fully matured."
        + ("" if report.sufficient else f" Below the {MIN_SAMPLE}-observation minimum — "
           "**counts only, no rates**.")
    )
    lines.append("")

    rated = [r for r in report.ratings if r.horizon == 20] or report.ratings
    if rated:
        lines += ["| Rating | Horizon | n | Hit rate | Mean excess |",
                  "| --- | ---: | ---: | ---: | ---: |"]
        for record in rated[:EVIDENCE_SOURCE_ROWS]:
            rate = _pct(record.hit_rate) if record.sufficient else INSUFFICIENT
            lines.append(
                f"| {record.rating} | {record.horizon}d | {record.n} | {rate} "
                f"| {_signed(record.mean_excess)} |"
            )
        lines.append("")

    grades = _headline_grades(report)
    if grades:
        lines += ["| Source | n | Lift vs price-only | Standing |", "| --- | ---: | ---: | --- |"]
        for row in grades:
            lift = _signed(row.lift) if row.sufficient else INSUFFICIENT
            lines.append(f"| {row.source} | {row.samples} | {lift} | {row.standing} |")
        lines.append("")

    if report.caveats:
        lines.append(f"_{report.caveats[0]}_")
        lines.append("")
    return "\n".join(lines)


def _headline_grades(report: WeeklyReport) -> Sequence[Any]:
    """One row per source — the 20-day horizon, or the longest it has."""
    if not report.grading:
        return []
    out = []
    for source in report.grading.sources:
        rows = report.grading.for_source(source)
        preferred = next((r for r in rows if r.horizon == 20), None)
        out.append(preferred or max(rows, key=lambda r: r.horizon))
    return out[:EVIDENCE_SOURCE_ROWS]


__all__ = [
    "EVIDENCE_SOURCE_ROWS",
    "INSUFFICIENT",
    "MIN_SAMPLE",
    "GRADED_HORIZONS",
    "WeeklyReport",
    "evidence_section",
    "render",
    "weekly_report",
]
