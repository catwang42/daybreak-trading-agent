"""``--stage outcomes`` and ``--stage evaluate``.

Kept out of :mod:`tradingagent.stages` because these two spend no tokens, place
no order and write no recommendation. They read records the daily run already
wrote and price them against bars, which makes them safe to schedule on their
own, safe to re-run over the same day, and safe to run on a Saturday.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from ..config import Settings
from ..data.market import MarketData
from ..data.validate import DegradedTracker
from ..report.writer import write_report
from ..snapshot import ResearchSnapshot
from . import outcomes as O
from .ledger import DECISIONS, OUTCOMES, ExperimentLedger
from .provenance import build_provenance

log = logging.getLogger(__name__)

#: Enough history to resolve the 60-session horizon of a decision made most of
#: a year ago, which is what a ledger that has been accumulating looks like.
BARS_PERIOD = "2y"


@dataclass
class OutcomesResult:
    resolved: int = 0
    updated: int = 0
    pending: int = 0
    complete: int = 0
    backfilled: int = 0
    records: list = field(default_factory=list)
    snapshot: ResearchSnapshot | None = None
    degraded: DegradedTracker = field(default_factory=DegradedTracker)
    seconds: float = 0.0
    notes: list[str] = field(default_factory=list)

    @property
    def as_of(self) -> str:
        return self.snapshot.market_as_of.isoformat() if self.snapshot else "unknown"


def run_outcomes(
    settings: Settings,
    *,
    degraded: DegradedTracker | None = None,
    ledger: ExperimentLedger | None = None,
    backfill: bool = False,
) -> OutcomesResult:
    """Resolve every ledger decision whose horizons have matured.

    Idempotent by construction: a decision that already has all five horizons
    is skipped, and one that has three gets a fresh row superseding the old.
    Re-running this after the close every day is the intended usage and costs
    one bulk price download.

    ``backfill`` first reconstructs what the pre-ledger journal can support —
    flagged as backfilled, never overwriting a real row. Safe to leave on, but
    off by default: it is a one-time migration, not part of the daily job.
    """
    started = time.monotonic()
    degraded = degraded if degraded is not None else DegradedTracker()
    store = ledger or ExperimentLedger(settings.ledger_root)
    result = OutcomesResult(degraded=degraded)

    if backfill:
        from .backfill import backfill as run_backfill

        recovered = run_backfill(settings.journal_path, store)
        result.backfilled = len(recovered.decisions)
        if recovered.decisions:
            result.notes.append(
                f"backfilled {len(recovered.decisions)} decision(s) from the journal "
                f"across {len(recovered.dates)} date(s); every row is flagged backfilled"
            )

    decisions = list(store.latest(DECISIONS, "decision_id").values())
    if not decisions:
        result.notes.append("the ledger holds no decisions yet; nothing to resolve")
        result.seconds = time.monotonic() - started
        log.info("Outcomes: %s", result.notes[-1])
        return result

    settled = store.latest(OUTCOMES, "decision_id")
    result.complete = sum(1 for row in settled.values() if O.is_complete(row))
    todo = [d for d in decisions if not O.is_complete(settled.get(d.get("decision_id", ""), {}))]
    if not todo:
        result.notes.append(f"all {len(decisions)} decision(s) are fully resolved")
        result.seconds = time.monotonic() - started
        return result

    symbols = O.symbols_to_price(todo)
    market = MarketData(degraded=degraded, period=BARS_PERIOD)
    log.info("Outcomes: pricing %d symbol(s) for %d unresolved decision(s)", len(symbols), len(todo))
    # min_rows=2 because a name only needs the reference session and one more
    # to have a 1-day outcome; the 60-day window fails per horizon, not per
    # ticker.
    bars = market.load_many(symbols, min_rows=2, period=BARS_PERIOD)
    if not bars:
        degraded.add("Outcome resolution", "no price history came back; nothing could be resolved")
        result.seconds = time.monotonic() - started
        return result

    # The one market picture this job is allowed to reason from. Built from the
    # same bars it resolves against, so `market_as_of` is the latest session
    # actually in the data — never the wall clock, which on a Saturday would
    # claim two sessions that do not exist.
    snapshot = ResearchSnapshot.from_bars(
        bars,
        settings.run_date,
        session="outcome resolution (no session state fetched)",
        universe_version="outcomes job",
        name="outcomes",
    )
    result.snapshot = snapshot
    log.info("Outcomes: resolving against %s (%s)", snapshot.snapshot_id, snapshot.market_as_of)

    provenance = build_provenance(settings, snapshot)
    benchmark = bars.get(O.BENCHMARK)
    if benchmark is None:
        degraded.add("Outcome resolution", "no SPY bars: excess-vs-market is missing this run")

    records = []
    for decision in todo:
        ticker = str(decision.get("ticker", "")).upper()
        frame = bars.get(ticker)
        if frame is None:
            result.notes.append(f"{ticker}: no bars")
            result.pending += 1
            continue
        etf = O.sector_etf(str(decision.get("sector", "")))
        resolution = O.resolve(
            decision,
            frame,
            snapshot,
            benchmark=benchmark,
            sector=bars.get(etf) if etf else None,
            provenance=provenance,
        )
        if resolution.record is None:
            result.pending += 1
            result.notes.extend(resolution.notes)
            continue
        records.append(resolution.record)
        if decision.get("decision_id") in settled:
            result.updated += 1
        else:
            result.resolved += 1

    if records:
        store.append(OUTCOMES, records)
    result.records = records
    result.complete += sum(1 for r in records if len(r.horizons) >= len(O.HORIZONS))
    if snapshot.violations:
        degraded.add(
            "Outcome resolution",
            f"{len(snapshot.violations)} look-ahead check(s) failed and those horizons were dropped",
        )
    result.seconds = time.monotonic() - started
    log.info(
        "Outcomes: %d new, %d updated, %d still pending in %.1fs",
        result.resolved,
        result.updated,
        result.pending,
        result.seconds,
    )
    return result


@dataclass
class EvaluateResult:
    path: str = ""
    week: str = ""
    markdown: str = ""
    evidence: str = ""
    resolved: int = 0
    seconds: float = 0.0


def week_label(run_date: date) -> str:
    """``2026-33`` — ISO week, so a Friday and the Monday after it differ."""
    year, week, _ = run_date.isocalendar()
    return f"{year}-{week:02d}"


def run_evaluate(
    settings: Settings, *, ledger: ExperimentLedger | None = None
) -> EvaluateResult:
    """Write ``evaluation/<ISO week>.md`` from the ledger. Spends nothing."""
    from .report import evidence_section, weekly_report

    started = time.monotonic()
    store = ledger or ExperimentLedger(settings.ledger_root)
    label = week_label(settings.run_date)

    report = weekly_report(store, settings.run_date)
    path = Path(settings.evaluation_dir) / f"{label}.md"
    write_report(path, report.markdown, settings.reports_bucket)
    log.info("Evaluation report for %s -> %s", label, path)

    return EvaluateResult(
        path=str(path),
        week=label,
        markdown=report.markdown,
        evidence=evidence_section(report),
        resolved=report.resolved,
        seconds=time.monotonic() - started,
    )
