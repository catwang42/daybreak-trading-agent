"""Append-only journal — the only benchmark that counts (BUILD_PLAN.md).

One JSON line per recommendation, in the exact shape declared by
``config/report-schema.md``::

    {"date":"","ticker":"","verdict":"","target":null,"confidence":"",
     "options":null,"signal_sources":[],"report":"reports/<date>/deep/<ticker>.md",
     "outcome_7d":null,"outcome_30d":null}

Milestone 1 writes the shortlist's quick ratings as the verdict; M2 overwrites
nothing — deep verdicts are appended as their own lines with the deep report
path, so the journal records what was believed at each stage.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger(__name__)

FIELD_ORDER = [
    "date",
    "ticker",
    "verdict",
    "target",
    "confidence",
    "options",
    "signal_sources",
    "report",
    "outcome_7d",
    "outcome_30d",
]


@dataclass
class JournalEntry:
    date: str
    ticker: str
    verdict: str
    confidence: str
    report: str
    target: float | None = None
    options: Any | None = None
    signal_sources: list[str] = field(default_factory=list)
    outcome_7d: Any | None = None
    outcome_30d: Any | None = None
    # Extra M1 context, kept after the schema fields so the declared shape is intact.
    stage: str = "discovery"
    screener_score: int | None = None
    deep_dive_priority: int | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = {key: getattr(self, key) for key in FIELD_ORDER}
        payload["stage"] = self.stage
        if self.screener_score is not None:
            payload["screener_score"] = self.screener_score
        if self.deep_dive_priority is not None:
            payload["deep_dive_priority"] = self.deep_dive_priority
        return payload


def append_entries(path: Path, entries: Iterable[JournalEntry]) -> int:
    """Append entries as JSON lines. Returns the number written."""
    rows = list(entries)
    if not rows:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for entry in rows:
            handle.write(json.dumps(entry.to_dict(), separators=(",", ":")) + "\n")
    log.info("Wrote %d journal entries to %s", len(rows), path)
    return len(rows)


def entries_from_shortlist(shortlist, run_date: date, report_path: str, stage: str = "discovery"):
    """Build one journal entry per shortlisted ticker that produced a rating."""
    out: list[JournalEntry] = []
    for item in shortlist:
        take = item.take
        out.append(
            JournalEntry(
                date=run_date.isoformat(),
                ticker=item.symbol,
                verdict=take.rating if take else "DEGRADED",
                confidence=take.confidence if take else "",
                report=report_path,
                target=None,  # M1 quick takes carry no price target; M2's PM sets one
                signal_sources=["yfinance", "finnhub", "screener:momentum-burst"],
                stage=stage,
                screener_score=item.candidate.score,
                deep_dive_priority=take.deep_dive_priority if take else None,
            )
        )
    return out


def read_entries(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            log.warning("Skipping malformed journal line: %s", line[:120])
    return rows
