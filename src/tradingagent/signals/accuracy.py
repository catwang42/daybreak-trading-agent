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

Small samples are the real hazard, and the first version of this module got
the cold start exactly backwards. A source with no record scored weight 1.0 —
full trust — and shrinkage pulled *towards* that, so "we have never checked
this source" and "this source is reliably average" produced the same number.
Four of ten names on a recent shortlist entered on the strength of signals
that had never been graded once.

So the cold start is inverted here. A source starts at weight 0: it is
computed, journaled and shown, but it moves nothing. It earns influence by
resolving observations, on a ladder (:data:`GRADUATION`) that caps how far it
may move a candidate's score — ±1 point at 20 resolved calls, ±3 at 50, ±5 at
100. The old ±8 is reachable only for a source a human has reviewed and marked
``proven``; no code path sets that flag.
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
#: Weight bounds *once a source has graduated*. Below :data:`MIN_OBSERVATIONS`
#: the weight is 0 regardless: an ungraded source has earned nothing, and the
#: floor of 0.5 applies to a source we have measured and found poor.
MIN_WEIGHT, MAX_WEIGHT = 0.5, 1.5

#: Resolved directional calls a source needs before it may move the ranking at
#: all. Below this it is SHADOW: computed, journaled, reported, ignored.
MIN_OBSERVATIONS = 20
#: (resolved observations, points this source may move a candidate's score).
#: Ascending; the highest threshold a source clears is the one that applies.
GRADUATION: tuple[tuple[int, float], ...] = ((20, 1.0), (50, 3.0), (100, 5.0))
#: The original ceiling, now reachable only by a source that has cleared the
#: top rung *and* been marked ``proven`` by a human in the scores file. Nothing
#: in this module writes that flag — that is the point of it.
PROVEN_MAX_ADJUSTMENT = 8.0

SCORES_FILENAME = "source-accuracy.json"
REFRESH_AFTER_DAYS = 7  # "scored weekly" (BUILD_PLAN.md Milestone 3)


@dataclass
class SourceScore:
    source: str
    samples: int = 0
    hits: int = 0
    abstained: int = 0
    #: Human override, read from the scores file and never written by code.
    #: Only a source that has cleared the top rung and been reviewed by a
    #: person may reach :data:`PROVEN_MAX_ADJUSTMENT`.
    proven: bool = False

    @property
    def accuracy(self) -> float | None:
        return self.hits / self.samples if self.samples else None

    @property
    def graduated(self) -> bool:
        """Whether this source has earned any influence over the ranking."""
        return self.samples >= MIN_OBSERVATIONS

    @property
    def weight(self) -> float:
        """Accuracy mapped to a multiplier, or 0 for a source with no record.

        Zero rather than 1.0 below the threshold: an untested source is not a
        neutral one, and treating it as neutral is what let ungraded signals
        pick shortlist names.
        """
        if not self.graduated:
            return 0.0
        raw = MIN_WEIGHT + (MAX_WEIGHT - MIN_WEIGHT) * (self.hits / self.samples)
        confidence = min(1.0, self.samples / FULL_CONFIDENCE_SAMPLES)
        return round(1.0 + (raw - 1.0) * confidence, 3)

    @property
    def max_adjustment(self) -> float:
        """Points this source is allowed to move a candidate's screener score."""
        earned = 0.0
        for needed, points in GRADUATION:
            if self.samples >= needed:
                earned = points
        if self.proven and self.samples >= GRADUATION[-1][0]:
            return PROVEN_MAX_ADJUSTMENT
        return earned

    @property
    def standing(self) -> str:
        """How this source's record is described in the report."""
        if not self.graduated:
            return f"SHADOW ({self.samples}/{MIN_OBSERVATIONS} resolved)"
        cap = f"±{self.max_adjustment:.0f} pts"
        return f"{cap} (PROVEN)" if self.proven else cap

    def line(self) -> str:
        record = (
            "no scored calls yet"
            if not self.samples
            else f"{self.hits}/{self.samples} ({self.accuracy:.0%})"
        )
        return (
            f"| {self.source} | {record} | {self.abstained} abstained | "
            f"{self.weight:.3f} | {self.standing} |"
        )


@dataclass
class AccuracyReport:
    scored_on: date
    window_days: int
    scores: dict[str, SourceScore] = field(default_factory=dict)
    unresolved: int = 0

    def weights(self) -> dict[str, float]:
        return {name: score.weight for name, score in self.scores.items()}

    def caps(self) -> dict[str, float]:
        """Source -> the most it may move one candidate's screener score."""
        return {name: score.max_adjustment for name, score in self.scores.items()}

    @property
    def graduated(self) -> list[str]:
        return sorted(name for name, s in self.scores.items() if s.graduated)

    def markdown(self) -> str:
        header = (
            f"Scored {self.scored_on.isoformat()} over the trailing {self.window_days} days "
            f"({self.unresolved} entries still unresolved)."
            if self.scores
            else "No journal entries carry signal readings with a resolved outcome yet."
        )
        ladder = ", ".join(f"{n} obs → ±{p:.0f}" for n, p in GRADUATION)
        rows = [
            header,
            "",
            f"A source moves the ranking only after {MIN_OBSERVATIONS} resolved directional "
            f"calls, and then only as far as its record allows ({ladder} points; "
            f"±{PROVEN_MAX_ADJUSTMENT:.0f} needs a human to mark it proven). Until then it is "
            "SHADOW — computed, journaled and shown here, but worth zero points.",
            "",
        ]
        if not self.scores:
            return "\n".join(rows)
        rows += [
            "| Source | Directional calls correct | Abstentions | Weight | Ranking influence |",
            "|---|---|---|---:|---|",
        ]
        rows += [self.scores[name].line() for name in sorted(self.scores)]
        return "\n".join(rows)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scored_on": self.scored_on.isoformat(),
            "window_days": self.window_days,
            "unresolved": self.unresolved,
            "scores": {
                name: {
                    "samples": s.samples,
                    "hits": s.hits,
                    "abstained": s.abstained,
                    "proven": s.proven,
                }
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
                    proven=bool(row.get("proven", False)),
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


def _latest_per_decision(entries: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """One entry per (date, ticker, stage), keeping the last one written.

    The journal is append-only and re-running a session is supported (``--date``
    exists precisely for that), so the same decision can appear several times.
    Each copy is the same call about the same day, and counting them as separate
    samples would let a source quadruple its evidence by being re-run — inflating
    a lucky streak into a "proven" record and, through the shrinkage term, an
    unearned weight. The last write wins because it reflects the newest belief.
    """
    latest: dict[tuple[str, str, str], dict[str, Any]] = {}
    for entry in entries:
        key = (
            str(entry.get("date")),
            str(entry.get("ticker")),
            str(entry.get("stage", "")),
        )
        latest[key] = entry
    return list(latest.values())


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
    for entry in _latest_per_decision(entries):
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
            log.info(
                "Source accuracy is stale but no price source was supplied; "
                "every source stays in shadow"
            )
            return report or AccuracyReport(scored_on=run_date, window_days=WINDOW_DAYS)

        from ..journal import read_entries

        fresh = score_entries(read_entries(self.journal_path), realised, run_date)
        # `proven` is the one field a human writes into this file. Rescoring
        # rewrites the file wholesale, so carry the flag forward or the next
        # weekly run silently demotes a source someone deliberately promoted.
        for name, score in fresh.scores.items():
            previous = (report.scores.get(name) if report else None)
            if previous is not None and previous.proven:
                score.proven = True
        self.save(fresh)
        log.info(
            "Rescored signal sources over %d days: %s",
            fresh.window_days,
            ", ".join(f"{n} weight {s.weight:.2f} cap ±{s.max_adjustment:.0f}"
                      for n, s in sorted(fresh.scores.items()))
            or "no samples",
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
