"""Rolling per-source accuracy, scored against journal outcomes.

Gate 3 asks whether the signals change decisions or just burn tokens. This is
the machinery that answers it with evidence rather than intuition, and it is
the reason every signal carries a coarse ``direction`` at all: a lexicon score
is reproducible, so it can be checked later, in a way that "the news analyst
felt bullish" cannot.

How a source is scored:

1. Each deep verdict writes the direction every source held at decision time
   into the journal line (``signal_readings``).
2. Some days later the realised move is known. A source that said +1 before a
   rise, or -1 before a fall, scored a hit. Sources that said 0 are not
   scored — abstaining is neither right nor wrong, and counting it would let
   a silent source drift towards 50% and look average.
3. Accuracy over the trailing window becomes a weight in
   :meth:`SignalBundle.score_adjustment`, so influence is earned.

The dead band matters. A ±1% move over a week is noise, and grading a source
against noise teaches it nothing; those samples are dropped rather than
counted as misses.

Small samples are the real hazard: three lucky calls is 100% accuracy and
means nothing. Weights are shrunk towards 1.0 in proportion to how little
evidence there is, so a source needs a sustained record to gain or lose much.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable

log = logging.getLogger(__name__)

#: Realised move smaller than this is noise, not an outcome.
DEAD_BAND_PCT = 1.0
#: Trailing window over which a source is judged.
WINDOW_DAYS = 90
#: Samples needed before a source's record is trusted at full strength.
FULL_CONFIDENCE_SAMPLES = 20
#: Weight bounds. A source can be halved or given half again, never silenced
#: outright — that decision is the human's, by removing it from the registry.
MIN_WEIGHT, MAX_WEIGHT = 0.5, 1.5

SCORES_FILENAME = "source-accuracy.json"
REFRESH_AFTER_DAYS = 7  # "scored weekly" (BUILD_PLAN.md Milestone 3)


@dataclass
class SourceScore:
    source: str
    samples: int = 0
    hits: int = 0
    abstained: int = 0

    @property
    def accuracy(self) -> float | None:
        return self.hits / self.samples if self.samples else None

    @property
    def weight(self) -> float:
        """Accuracy mapped to a multiplier, shrunk towards 1.0 on thin evidence."""
        if not self.samples:
            return 1.0
        raw = MIN_WEIGHT + (MAX_WEIGHT - MIN_WEIGHT) * (self.hits / self.samples)
        confidence = min(1.0, self.samples / FULL_CONFIDENCE_SAMPLES)
        return round(1.0 + (raw - 1.0) * confidence, 3)

    def line(self) -> str:
        if not self.samples:
            return f"| {self.source} | no scored calls yet | {self.abstained} abstained | 1.000 |"
        return (
            f"| {self.source} | {self.hits}/{self.samples} ({self.accuracy:.0%}) | "
            f"{self.abstained} abstained | {self.weight:.3f} |"
        )


@dataclass
class AccuracyReport:
    scored_on: date
    window_days: int
    scores: dict[str, SourceScore] = field(default_factory=dict)
    unresolved: int = 0

    def weights(self) -> dict[str, float]:
        return {name: score.weight for name, score in self.scores.items()}

    def markdown(self) -> str:
        if not self.scores:
            return (
                "No journal entries carry signal readings with a resolved outcome yet. "
                "Every source runs at weight 1.000 until the record exists."
            )
        rows = [
            f"Scored {self.scored_on.isoformat()} over the trailing {self.window_days} days "
            f"({self.unresolved} entries still unresolved).",
            "",
            "| Source | Directional calls correct | Abstentions | Weight |",
            "|---|---|---|---:|",
        ]
        rows += [self.scores[name].line() for name in sorted(self.scores)]
        return "\n".join(rows)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scored_on": self.scored_on.isoformat(),
            "window_days": self.window_days,
            "unresolved": self.unresolved,
            "scores": {
                name: {"samples": s.samples, "hits": s.hits, "abstained": s.abstained}
                for name, s in self.scores.items()
            },
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "AccuracyReport":
        return cls(
            scored_on=date.fromisoformat(payload["scored_on"]),
            window_days=int(payload.get("window_days", WINDOW_DAYS)),
            unresolved=int(payload.get("unresolved", 0)),
            scores={
                name: SourceScore(
                    source=name,
                    samples=int(row.get("samples", 0)),
                    hits=int(row.get("hits", 0)),
                    abstained=int(row.get("abstained", 0)),
                )
                for name, row in (payload.get("scores") or {}).items()
            },
        )


def outcome_direction(return_pct: float | None) -> int | None:
    """+1 / -1 for a realised move, or None when it is inside the dead band."""
    if return_pct is None:
        return None
    if abs(return_pct) < DEAD_BAND_PCT:
        return None
    return 1 if return_pct > 0 else -1


def score_entries(
    entries: Iterable[dict[str, Any]],
    realised: Callable[[str, date], float | None],
    run_date: date,
    window_days: int = WINDOW_DAYS,
) -> AccuracyReport:
    """Grade every source's recorded direction against what the price then did.

    ``realised(ticker, decision_date)`` returns the percent move over the
    holding window, or None when it is not knowable yet. Injecting it keeps
    this function pure and lets the tests state outcomes directly.
    """
    report = AccuracyReport(scored_on=run_date, window_days=window_days)
    cutoff = run_date - timedelta(days=window_days)
    for entry in entries:
        readings = entry.get("signal_readings") or {}
        if not readings:
            continue
        try:
            decided = date.fromisoformat(str(entry.get("date")))
        except (TypeError, ValueError):
            continue
        if decided < cutoff:
            continue

        ticker = str(entry.get("ticker") or "")
        actual = outcome_direction(realised(ticker, decided))
        if actual is None:
            report.unresolved += 1
            continue
        for source, direction in readings.items():
            score = report.scores.setdefault(source, SourceScore(source=source))
            if not direction:
                score.abstained += 1
                continue
            score.samples += 1
            if int(direction) == actual:
                score.hits += 1
    return report


class AccuracyTracker:
    """Persists the scoring and hands the hub its weights.

    Rescored at most weekly: the outcomes only move as fast as the market
    resolves them, and re-downloading a quarter of price history on every run
    to learn the same numbers would be waste.
    """

    def __init__(self, journal_path: Path, scores_path: Path | None = None):
        self.journal_path = journal_path
        self.scores_path = scores_path or journal_path.parent / SCORES_FILENAME

    def load(self) -> AccuracyReport | None:
        if not self.scores_path.exists():
            return None
        try:
            return AccuracyReport.from_dict(json.loads(self.scores_path.read_text()))
        except (ValueError, KeyError) as exc:
            log.warning("Ignoring unreadable %s: %s", self.scores_path, exc)
            return None

    def save(self, report: AccuracyReport) -> None:
        self.scores_path.parent.mkdir(parents=True, exist_ok=True)
        self.scores_path.write_text(json.dumps(report.to_dict(), indent=2) + "\n")

    def stale(self, report: AccuracyReport | None, run_date: date) -> bool:
        return report is None or (run_date - report.scored_on).days >= REFRESH_AFTER_DAYS

    def current(
        self,
        run_date: date,
        realised: Callable[[str, date], float | None] | None = None,
    ) -> AccuracyReport:
        """The report to use today, rescoring only when the cache has aged out."""
        report = self.load()
        if not self.stale(report, run_date) and report is not None:
            return report
        if realised is None:
            log.info("Source accuracy is stale but no price source was supplied; keeping weights at 1.0")
            return report or AccuracyReport(scored_on=run_date, window_days=WINDOW_DAYS)

        from ..journal import read_entries

        fresh = score_entries(read_entries(self.journal_path), realised, run_date)
        self.save(fresh)
        log.info(
            "Rescored signal sources over %d days: %s",
            fresh.window_days,
            ", ".join(f"{n} {s.weight:.2f}" for n, s in sorted(fresh.scores.items())) or "no samples",
        )
        return fresh


def realised_return(market, horizon_days: int = 7) -> Callable[[str, date], float | None]:
    """A ``realised`` callable backed by daily bars.

    Returns the percent change from the decision date's close to the close
    ``horizon_days`` later, or None when the window has not closed yet.
    """
    cache: dict[str, Any] = {}

    def lookup(ticker: str, decided: date) -> float | None:
        if ticker not in cache:
            frames = market.load_many([ticker], min_rows=2, period="1y")
            cache[ticker] = frames.get(ticker)
        frame = cache[ticker]
        if frame is None or frame.empty:
            return None
        import pandas as pd

        index = pd.to_datetime(frame.index).date
        at = [i for i, day in enumerate(index) if day >= decided]
        after = [i for i, day in enumerate(index) if day >= decided + timedelta(days=horizon_days)]
        if not at or not after:
            return None
        start = float(frame["Close"].iloc[at[0]])
        end = float(frame["Close"].iloc[after[0]])
        return None if not start else (end / start - 1.0) * 100.0

    return lookup
