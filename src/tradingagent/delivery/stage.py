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
    deep_dir = report_dir / "deep"
    return sorted(deep_dir.glob("*.md")) if deep_dir.is_dir() else []


def run_report(
    settings: Settings,
    *,
    verdicts: list[Verdict] | None = None,
    degraded: DegradedTracker | None = None,
    run_date: date | None = None,
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

    return send_daily_brief(
        run_date,
        brief_path,
        deep_paths=deep_report_paths(report_dir),
        verdicts=verdicts,
        degraded_sources=sources,
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
