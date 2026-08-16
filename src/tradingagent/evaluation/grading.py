"""Signal grading v2 — against excess returns, at several horizons.

v1 (:mod:`tradingagent.signals.accuracy`) grades each source's recorded
direction against the raw 7-day move of one journal line. It was the right
instrument for M3 and it has three defects the ledger now lets us fix:

1. **It double-counts.** The journal writes a discovery line and a deep line
   for the same name on the same day, carrying the same readings. Those are one
   observation of the signal layer, not two, and counting both lets a source
   inflate its record by being analysed twice. Here they cluster by
   ``(date, ticker)``.
2. **It grades against the market's move, not ours.** A source that said "up"
   before a 3% rise in a week SPY rose 4% was wrong about the *stock* and v1
   scores it a hit. Everything here grades against excess.
3. **One horizon.** A news-tone signal that is right at 1 day and noise at 20
   is a real and useful finding, and a single 7-day window cannot express it.

What it does *not* change is the graduation ladder. The thresholds in
:mod:`tradingagent.signals.accuracy` (20 resolved calls to move anything, 50
for ±3, 100 for ±5) stay exactly as they are — this module reports standing
against them on better evidence, and no code path here grants weight. That
remains a human decision, as it was in M3.

The baseline is the point of the exercise. A source's accuracy in isolation is
close to meaningless when the screener has already selected for momentum: a
pool of breakout names drifts up, so "predicted up" is right most of the time
for reasons that have nothing to do with the source. **Lift** — the source's
conditional mean excess minus the pool's mean excess — is the number that says
whether the source added anything to a price-only screen.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from statistics import fmean
from typing import Any, Iterable

from ..signals.accuracy import (
    DEAD_BAND_PCT,
    GRADUATION,
    MIN_OBSERVATIONS,
    PROVEN_MAX_ADJUSTMENT,
)
from .outcomes import HORIZONS

log = logging.getLogger(__name__)

#: Horizons the weekly table reports. A subset of :data:`.outcomes.HORIZONS`:
#: 1 day grades the entry timing, 5 and 20 the thesis. 60 is resolved and
#: stored but too slow to say anything after a few weeks of running.
GRADED_HORIZONS: tuple[int, ...] = (1, 5, 20)

#: Below this many resolved observations a number is printed as a count and
#: labelled insufficient, never as a rate. Deliberately the same threshold the
#: graduation ladder uses, so "we can quote a hit rate" and "this source could
#: earn influence" become true on the same day rather than a fortnight apart.
MIN_SAMPLE = MIN_OBSERVATIONS

#: Stages that are the same view of the same name on the same day.
CLUSTERED_STAGES = ("discovery", "deep")


@dataclass
class Observation:
    """One ticker, one decision date — however many stages looked at it."""

    date: str
    ticker: str
    stages: list[str] = field(default_factory=list)
    readings: dict[str, int] = field(default_factory=dict)
    rating: str = ""
    confidence: str = ""
    #: horizon (as int) -> excess return vs SPY, in percent.
    excess: dict[int, float] = field(default_factory=dict)
    #: horizon -> excess vs the sector ETF.
    sector_excess: dict[int, float] = field(default_factory=dict)
    raw: dict[int, float] = field(default_factory=dict)

    @property
    def key(self) -> tuple[str, str]:
        return (self.date, self.ticker)


def cluster(
    decisions: Iterable[dict[str, Any]], outcomes: Iterable[dict[str, Any]]
) -> list[Observation]:
    """Fold decisions and their outcomes into one row per ticker × decision date.

    The deep stage's rating wins where both stages produced one: it is the
    verdict that shipped in the brief and the one the human read. The readings
    are the same object in both rows — the signal layer runs once per day — so
    merging them is a union, not a reconciliation.
    """
    by_decision = {str(o.get("decision_id", "")): o for o in outcomes}
    merged: dict[tuple[str, str], Observation] = {}

    for row in decisions:
        stage = str(row.get("stage", ""))
        if stage not in CLUSTERED_STAGES:
            continue  # the options overlay is a different instrument, graded apart
        key = (str(row.get("date", "")), str(row.get("ticker", "")))
        observation = merged.setdefault(key, Observation(date=key[0], ticker=key[1]))
        observation.stages.append(stage)
        observation.readings.update(
            {k: int(v) for k, v in (row.get("signal_readings") or {}).items()}
        )
        if stage == "deep" or not observation.rating:
            observation.rating = str(row.get("rating", ""))
            observation.confidence = str(row.get("confidence", ""))

        outcome = by_decision.get(str(row.get("decision_id", "")))
        for horizon, values in ((outcome or {}).get("horizons") or {}).items():
            h = int(horizon)
            # Deep and discovery resolve to the same prices; whichever arrives
            # is the same number, so first-wins avoids a pointless overwrite.
            observation.excess.setdefault(h, _f(values.get("excess_spy_pct")))
            observation.sector_excess.setdefault(h, _f(values.get("excess_sector_pct")))
            observation.raw.setdefault(h, _f(values.get("return_pct")))

    for observation in merged.values():
        observation.stages = sorted(set(observation.stages))
        observation.excess = {k: v for k, v in observation.excess.items() if v is not None}
        observation.sector_excess = {
            k: v for k, v in observation.sector_excess.items() if v is not None
        }
        observation.raw = {k: v for k, v in observation.raw.items() if v is not None}
    return sorted(merged.values(), key=lambda o: (o.date, o.ticker))


def _f(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


@dataclass
class SourceGrade:
    """One source at one horizon."""

    source: str
    horizon: int
    samples: int = 0
    hits: int = 0
    abstained: int = 0
    #: Dropped because the move was inside the dead band — neither right nor
    #: wrong, and counting them would drag every source towards 50%.
    noise: int = 0
    #: Mean of ``direction × excess`` over the scored samples: what following
    #: this source would have earned above SPY, in percent per observation.
    mean_excess: float | None = None
    #: The same statistic for the whole pool, direction-free — the price-only
    #: screen's own payoff, which is what the source has to beat.
    baseline_excess: float | None = None

    @property
    def accuracy(self) -> float | None:
        return self.hits / self.samples if self.samples else None

    @property
    def lift(self) -> float | None:
        if self.mean_excess is None or self.baseline_excess is None:
            return None
        return round(self.mean_excess - self.baseline_excess, 3)

    @property
    def sufficient(self) -> bool:
        return self.samples >= MIN_SAMPLE

    @property
    def max_adjustment(self) -> float:
        earned = 0.0
        for needed, points in GRADUATION:
            if self.samples >= needed:
                earned = points
        return earned

    @property
    def standing(self) -> str:
        """Where this source sits on the M3 ladder, on M7's evidence.

        No code path here promotes anything: this is a report of what the
        record would justify, and the graduation itself stays a human call.
        """
        if self.samples < MIN_OBSERVATIONS:
            return f"SHADOW ({self.samples}/{MIN_OBSERVATIONS} resolved)"
        if self.samples < GRADUATION[-1][0]:
            return f"EXPERIMENTAL (would earn ±{self.max_adjustment:.0f} pts)"
        return (
            f"ACTIVE (would earn ±{self.max_adjustment:.0f} pts; "
            f"±{PROVEN_MAX_ADJUSTMENT:.0f} still needs a human)"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "horizon": self.horizon,
            "samples": self.samples,
            "hits": self.hits,
            "abstained": self.abstained,
            "noise": self.noise,
            "accuracy": self.accuracy,
            "mean_excess": self.mean_excess,
            "baseline_excess": self.baseline_excess,
            "lift": self.lift,
            "standing": self.standing,
            "sufficient": self.sufficient,
        }


@dataclass
class GradingReport:
    observations: list[Observation] = field(default_factory=list)
    grades: list[SourceGrade] = field(default_factory=list)
    horizons: tuple[int, ...] = GRADED_HORIZONS
    #: horizon -> mean excess over every resolved observation.
    baseline: dict[int, float] = field(default_factory=dict)

    @property
    def resolved(self) -> int:
        return sum(1 for o in self.observations if o.excess)

    @property
    def sources(self) -> list[str]:
        return sorted({g.source for g in self.grades})

    def for_source(self, source: str) -> list[SourceGrade]:
        return [g for g in self.grades if g.source == source]

    def at(self, source: str, horizon: int) -> SourceGrade | None:
        return next(
            (g for g in self.grades if g.source == source and g.horizon == horizon), None
        )


def grade(
    decisions: Iterable[dict[str, Any]],
    outcomes: Iterable[dict[str, Any]],
    *,
    horizons: tuple[int, ...] = GRADED_HORIZONS,
) -> GradingReport:
    """Grade every source at every horizon against excess returns."""
    observations = cluster(decisions, outcomes)
    report = GradingReport(observations=observations, horizons=horizons)

    for horizon in horizons:
        pool = [o.excess[horizon] for o in observations if horizon in o.excess]
        if pool:
            report.baseline[horizon] = round(fmean(pool), 3)

    sources = sorted({s for o in observations for s in o.readings})
    for source in sources:
        for horizon in horizons:
            report.grades.append(
                _grade_one(source, horizon, observations, report.baseline.get(horizon))
            )
    return report


def _grade_one(
    source: str, horizon: int, observations: list[Observation], baseline: float | None
) -> SourceGrade:
    grade_row = SourceGrade(source=source, horizon=horizon, baseline_excess=baseline)
    payoffs: list[float] = []
    for observation in observations:
        if source not in observation.readings:
            continue
        excess = observation.excess.get(horizon)
        if excess is None:
            continue  # not matured; not a miss
        direction = observation.readings[source]
        if direction == 0:
            grade_row.abstained += 1
            continue
        if abs(excess) < DEAD_BAND_PCT:
            grade_row.noise += 1
            continue
        grade_row.samples += 1
        if (direction > 0) == (excess > 0):
            grade_row.hits += 1
        payoffs.append(direction * excess)
    if payoffs:
        grade_row.mean_excess = round(fmean(payoffs), 3)
    return grade_row


# --- selection experiment (item 4) --------------------------------------------


@dataclass
class SelectionComparison:
    """The shipped, price-only shortlist against the signals-adjusted one.

    Both arms come out of the same run because the signals are shadowed, so
    this costs nothing to compute and is the only measurement in the lab that
    directly answers "would turning the signal layer on have helped".
    """

    horizon: int
    control_n: int = 0
    treatment_n: int = 0
    control_excess: float | None = None
    treatment_excess: float | None = None
    #: Names in one arm and not the other — the whole of the difference.
    disputed: list[str] = field(default_factory=list)

    @property
    def difference(self) -> float | None:
        if self.control_excess is None or self.treatment_excess is None:
            return None
        return round(self.treatment_excess - self.control_excess, 3)

    @property
    def sufficient(self) -> bool:
        return min(self.control_n, self.treatment_n) >= MIN_SAMPLE


def compare_selection(
    candidates: Iterable[dict[str, Any]],
    outcomes: Iterable[dict[str, Any]],
    *,
    horizons: tuple[int, ...] = GRADED_HORIZONS,
) -> list[SelectionComparison]:
    """Forward excess of the two shortlists, per horizon."""
    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for row in outcomes:
        by_key[(str(row.get("date", "")), str(row.get("ticker", "")))] = row

    out: list[SelectionComparison] = []
    for horizon in horizons:
        comparison = SelectionComparison(horizon=horizon)
        control: list[float] = []
        treatment: list[float] = []
        disputed: set[str] = set()
        for row in candidates:
            key = (str(row.get("date", "")), str(row.get("ticker", "")))
            excess = _excess_at(by_key.get(key), horizon)
            selected, counterfactual = bool(row.get("selected")), bool(
                row.get("counterfactual_selected")
            )
            if selected != counterfactual and (selected or counterfactual):
                disputed.add(key[1])
            if excess is None:
                continue
            if selected:
                control.append(excess)
            if counterfactual:
                treatment.append(excess)
        comparison.control_n, comparison.treatment_n = len(control), len(treatment)
        if control:
            comparison.control_excess = round(fmean(control), 3)
        if treatment:
            comparison.treatment_excess = round(fmean(treatment), 3)
        comparison.disputed = sorted(disputed)
        out.append(comparison)
    return out


def _excess_at(outcome: dict[str, Any] | None, horizon: int) -> float | None:
    if not outcome:
        return None
    row = (outcome.get("horizons") or {}).get(str(horizon)) or {}
    return _f(row.get("excess_spy_pct"))


# --- ratings ------------------------------------------------------------------


@dataclass
class RatingRecord:
    rating: str
    horizon: int
    n: int = 0
    #: A "hit" is directionally right *against SPY*: a bullish rating that beat
    #: the market, or a bearish one that trailed it.
    hits: int = 0
    mean_excess: float | None = None

    @property
    def hit_rate(self) -> float | None:
        return self.hits / self.n if self.n else None

    @property
    def sufficient(self) -> bool:
        return self.n >= MIN_SAMPLE


BULLISH = {"Buy", "Overweight"}
BEARISH = {"Sell", "Underweight"}


def rating_records(
    observations: Iterable[Observation], *, horizons: tuple[int, ...] = GRADED_HORIZONS
) -> list[RatingRecord]:
    """Hit rate and mean excess per rating, per horizon."""
    buckets: dict[tuple[str, int], list[float]] = {}
    for observation in observations:
        rating = observation.rating
        if not rating:
            continue
        for horizon in horizons:
            excess = observation.excess.get(horizon)
            if excess is not None:
                buckets.setdefault((rating, horizon), []).append(excess)

    out: list[RatingRecord] = []
    for (rating, horizon), values in sorted(buckets.items()):
        record = RatingRecord(rating=rating, horizon=horizon, n=len(values))
        if rating in BULLISH:
            record.hits = sum(1 for v in values if v > 0)
        elif rating in BEARISH:
            record.hits = sum(1 for v in values if v < 0)
        else:
            # Hold is not a directional call. Counting it as one would make the
            # aggregate hit rate a function of how many Holds we happened to
            # write, which is a fact about the pipeline's caution, not its skill.
            record.hits = 0
        record.mean_excess = round(fmean(values), 3)
        out.append(record)
    return out


__all__ = [
    "GRADED_HORIZONS",
    "HORIZONS",
    "MIN_SAMPLE",
    "GradingReport",
    "Observation",
    "RatingRecord",
    "SelectionComparison",
    "SourceGrade",
    "cluster",
    "compare_selection",
    "grade",
    "rating_records",
]
