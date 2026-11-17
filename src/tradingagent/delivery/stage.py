"""``--stage report`` — deliver the brief for a date, by email.

Same in-memory-or-from-disk split as the deep and options stages: ``--stage
all`` hands the verdicts and the degradation over directly, while a standalone
``--stage report`` reconstructs both from what the earlier stages wrote into
``reports/<date>/``. That makes re-delivery cheap and side-effect free — no
market data, no LLM calls, no journal writes — which is what you want when the
morning email bounced, or when you are testing SMTP credentials.
"""

from __future__ import annotations

import logging
import re
from datetime import date
from pathlib import Path

from ..config import Settings
from ..data.validate import DegradedTracker
from ..options.context import OptionsContext
from ..presentation.sheet import build_sheet
from .email import DeliveryResult, Verdict, send_daily_brief

log = logging.getLogger(__name__)

#: Report section 7 opens with this line whenever anything degraded. It is the
#: only machine-readable record of the run's degradation once the process has
#: exited, so ``--stage report`` reads it back rather than claiming a clean run.
_DEGRADED_LINE = re.compile(r"^\*\*DEGRADED\s*[—-]\s*missing:\s*(.+?)\*\*\s*$", re.MULTILINE)


def degraded_from_brief(markdown: str) -> list[str]:
    """Recover the degraded source names from a rendered brief."""
    match = _DEGRADED_LINE.search(markdown)
    if not match:
        return []
    return [part.strip() for part in match.group(1).split(",") if part.strip()]


def verdicts_from_context(report_dir: Path) -> list[Verdict]:
    """Read the deep stage's verdicts back out of ``options-context.json``.

    That file exists because the options stage needed the same thing, and it
    preserves deep-queue order — which is what the subject line's tie-break
    depends on.
    """
    try:
        context = OptionsContext.read(report_dir)
    except (FileNotFoundError, ValueError) as exc:
        log.warning("No usable verdict context in %s (%s); subject will say 0 verdicts.", report_dir, exc)
        return []
    return [
        Verdict(symbol=row.symbol, rating=row.rating, confidence=row.confidence)
        for row in context.verdicts
    ]


def deep_report_paths(report_dir: Path) -> list[Path]:
    """The deep reports as markdown. The email renders each one to PDF."""
    deep_dir = report_dir / "deep"
    return sorted(deep_dir.glob("*.md")) if deep_dir.is_dir() else []


#: Only Friday's brief carries the evidence block. Daily would be noise — a
#: single session moves almost nothing in a rolling sample — and it is the same
#: cadence as the weekly file the block summarises.
EVIDENCE_WEEKDAY = 4


def weekly_evidence(settings: Settings, run_date: date) -> str:
    """Render Friday's "Evidence so far" block, and write the weekly file.

    Best effort in the strongest sense: the evaluation lab reads records and
    spends nothing, but it is downstream of the research and must never be the
    reason the morning email does not arrive.
    """
    if run_date.weekday() != EVIDENCE_WEEKDAY:
        return ""
    try:
        from ..evaluation.stage import run_evaluate

        result = run_evaluate(settings)
        log.info("Evidence block for week %s (%d resolved)", result.week, result.resolved)
        return result.evidence
    except Exception as exc:  # noqa: BLE001 - the brief ships regardless
        log.warning("Weekly evidence unavailable (%s); sending the brief without it.", exc)
        return ""


def run_report(
    settings: Settings,
    *,
    verdicts: list[Verdict] | None = None,
    degraded: DegradedTracker | None = None,
    run_date: date | None = None,
    evidence: str | None = None,
) -> DeliveryResult:
    """Email the brief for ``run_date``, attaching it and every deep report."""
    run_date = run_date or settings.run_date
    report_dir = settings.report_dir()
    brief_path = report_dir / "daily-brief.md"

    if verdicts is None:
        verdicts = verdicts_from_context(report_dir)

    if degraded is not None:
        sources = list(degraded.sources)
    elif brief_path.exists():
        sources = degraded_from_brief(brief_path.read_text(encoding="utf-8"))
    else:
        sources = []

    if evidence is None:
        evidence = weekly_evidence(settings, run_date)

    # The sheet is read from disk even during `--stage all`, which has all the
    # objects in memory: the same code path then serves the re-delivery case,
    # and a context that failed to write shows up here as a degraded email
    # rather than as a difference between today's send and tomorrow's resend.
    sheet = build_sheet(
        run_date, report_dir, reports_dir=report_dir.parent, evidence=evidence or ""
    )

    return send_daily_brief(
        run_date,
        brief_path,
        deep_paths=deep_report_paths(report_dir),
        verdicts=verdicts,
        degraded_sources=sources,
        evidence=evidence,
        sheet=sheet,
        bucket=settings.reports_bucket,
    )


def verdicts_from_results(results) -> list[Verdict]:
    """Adapt in-memory :class:`~tradingagent.pipeline.deep.DeepResult` objects."""
    out: list[Verdict] = []
    for result in results:
        decision = result.decision
        out.append(
            Verdict(
                symbol=result.symbol,
                rating=decision.rating if decision else "DEGRADED",
                confidence=decision.confidence if decision else "",
            )
        )
    return out
