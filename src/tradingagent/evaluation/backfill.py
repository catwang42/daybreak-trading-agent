"""Reconstruct what the ledger can from the pre-ledger journal.

The journal has been running since M1 and holds real decisions with real dates.
Throwing that away because the ledger arrived in M7 would push the first
resolvable outcome three months into the future, so this recovers what is
recoverable — and, more importantly, is honest about what is not.

**Every row it writes carries ``backfilled=True``**, and the weekly report
counts them separately and says so in the caveats. This is not bookkeeping
politeness. A backfilled row cannot support a claim about *why* a call was
made, because the journal never recorded the four things that would answer it:

* **the full pre-selection pool** — the journal only ever wrote the shortlist,
  so the control-vs-treatment comparison has no candidates to compare and no
  backfilled candidate rows are written at all;
* **the config hash, model versions and prompt versions** — nothing in a
  journal line says which models or prompts produced it, so a change in
  behaviour cannot be attributed to a change we made;
* **per-source shadow attribution** — the readings survive, the points each
  source contributed do not, and a fused adjustment cannot be decomposed after
  the fact;
* **the snapshot id** — so the ``run_id`` here is synthesised from the date and
  marked as such rather than borrowed from a snapshot that was never recorded.

What does survive is enough for the outcome job to work on: date, ticker,
stage, rating, target, the trade plan from M6 onward, and the signal readings
from M3 onward. That is the sample the first weeks of grading will run on.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .ledger import DECISIONS, RUNS, DecisionRecord, ExperimentLedger, RunRecord
from .provenance import Provenance

log = logging.getLogger(__name__)

#: Journal stages that map onto ledger decisions. Anything else is skipped
#: loudly rather than guessed at.
KNOWN_STAGES = ("discovery", "deep", "options")

#: What a backfilled row is missing, recorded on the row itself so a reader
#: three months from now does not have to know this module exists.
MISSING = (
    "no config hash, model or prompt versions",
    "no per-source shadow attribution",
    "no snapshot id",
    "no pre-selection pool",
)


def synthetic_run_id(run_date: str) -> str:
    """``backfill-2026-08-14``. Deliberately not a real ``run-`` id.

    A backfilled day is one experiment as far as we can tell, but we cannot
    prove it — a ``--stage deep`` re-run that afternoon would be indis-
    tinguishable in the journal. The prefix says so at a glance.
    """
    return f"backfill-{run_date}"


def provenance_for(run_date: str) -> Provenance:
    return Provenance(
        run_id=synthetic_run_id(run_date),
        run_date=run_date,
        git_commit="unknown",
        universe_version="unknown",
        backfilled=True,
    )


@dataclass
class BackfillResult:
    decisions: list[DecisionRecord] = field(default_factory=list)
    runs: list[RunRecord] = field(default_factory=list)
    read: int = 0
    skipped: int = 0
    already_present: int = 0
    dates: list[str] = field(default_factory=list)

    @property
    def written(self) -> int:
        return len(self.decisions) + len(self.runs)


def read_journal(path: Path) -> list[dict[str, Any]]:
    """Every parseable line. A truncated tail is skipped, never fatal."""
    import json

    if not Path(path).exists():
        return []
    rows: list[dict[str, Any]] = []
    for number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            log.warning("journal line %d is not JSON; skipped", number)
    return rows


def decision_from(row: dict[str, Any]) -> DecisionRecord | None:
    """One journal line as a ledger decision, or None if it is not one."""
    stage = str(row.get("stage") or "")
    date_ = str(row.get("date") or "")
    ticker = str(row.get("ticker") or "")
    if stage not in KNOWN_STAGES or not date_ or not ticker:
        return None

    plan = row.get("trade_plan")
    return DecisionRecord(
        provenance=provenance_for(date_),
        ticker=ticker,
        date=date_,
        stage=stage,
        rating=str(row.get("verdict") or ""),
        confidence=str(row.get("confidence") or ""),
        # The journal never carried these three. Blank is the honest value: a
        # guess reconstructed from the report prose would look like a record.
        horizon="",
        entry_condition="",
        invalidation="",
        target=_number(row.get("target")),
        trade_plan=plan if isinstance(plan, dict) else None,
        sector="",  # unknown, so the outcome job resolves these without a sector line
        report=str(row.get("report") or ""),
        seat_tiers={},
        signal_readings={
            str(k): int(v) for k, v in (row.get("signal_readings") or {}).items()
        },
        degraded=str(row.get("verdict") or "") == "DEGRADED",
        degraded_reasons=list(MISSING),
    )


def run_records(rows: Iterable[dict[str, Any]]) -> list[RunRecord]:
    """One synthetic run per (date, stage), with the counts the journal implies."""
    buckets: dict[tuple[str, str], int] = {}
    for row in rows:
        stage, date_ = str(row.get("stage") or ""), str(row.get("date") or "")
        if stage in KNOWN_STAGES and date_:
            buckets[(date_, stage)] = buckets.get((date_, stage), 0) + 1

    out: list[RunRecord] = []
    for (date_, stage), count in sorted(buckets.items()):
        out.append(
            RunRecord(
                provenance=provenance_for(date_),
                stage=stage,
                started_at="",
                # The journal recorded the shortlist, never the pool it came
                # from, so the universe and candidate counts are genuinely
                # unknown rather than zero.
                universe_size=0,
                candidates=0,
                shortlisted=count if stage == "discovery" else 0,
                queued=count if stage == "deep" else 0,
                notes=[f"backfilled from journal.jsonl: {count} entr(ies)", *MISSING],
            )
        )
    return out


def backfill(journal_path: Path, ledger: ExperimentLedger) -> BackfillResult:
    """Fill the ledger from the journal, skipping anything already recorded.

    Idempotent: a decision the ledger already holds — backfilled or real — is
    left alone. A real row must never be overwritten by a reconstruction that
    knows strictly less than it does.
    """
    rows = read_journal(journal_path)
    result = BackfillResult(read=len(rows))
    if not rows:
        return result

    existing = set(ledger.latest(DECISIONS, "decision_id"))
    for row in rows:
        record = decision_from(row)
        if record is None:
            result.skipped += 1
            continue
        if record.decision_id in existing:
            result.already_present += 1
            continue
        existing.add(record.decision_id)
        result.decisions.append(record)

    seen_runs = {
        (str(r.get("provenance", {}).get("run_id", "")), str(r.get("stage", "")))
        for r in ledger.read(RUNS)
    }
    result.runs = [
        run for run in run_records(rows) if (run.provenance.run_id, run.stage) not in seen_runs
    ]

    if result.decisions:
        ledger.append(DECISIONS, result.decisions)
    if result.runs:
        ledger.append(RUNS, result.runs)
    result.dates = sorted({d.date for d in result.decisions})
    log.info(
        "Backfilled %d decision(s) and %d run(s) from %s (%d already present, %d skipped)",
        len(result.decisions),
        len(result.runs),
        journal_path,
        result.already_present,
        result.skipped,
    )
    return result


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


__all__ = [
    "KNOWN_STAGES",
    "MISSING",
    "BackfillResult",
    "backfill",
    "decision_from",
    "provenance_for",
    "read_journal",
    "run_records",
    "synthetic_run_id",
]
