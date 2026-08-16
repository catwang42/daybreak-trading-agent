"""The experiment ledger — append-only, GCS-persisted, wider than the journal.

The journal answers "what did we recommend". It cannot answer "does the
research work", for three structural reasons, and this module exists to fix
each one:

1. **It only records the winners.** A journal line is written for a name that
   made the shortlist. The 40 candidates the screener ranked below it are gone
   by the time anyone asks whether the ranking was any good — and a ranking can
   only be graded against the names it *rejected*. :class:`CandidateRecord`
   covers the whole pre-selection pool.
2. **It cannot attribute a change.** Two months of entries with no note of
   which prompts, models or code produced them make every improvement
   unexplainable. Each record carries a :class:`~.provenance.Provenance`.
3. **Its outcome fields are a promise, not a measurement.** ``outcome_7d`` has
   been ``null`` on all 128 lines since M1. :class:`OutcomeRecord` is written
   by a separate job at maturity, against bars, with the horizon stated.

Four streams, four files, one JSON object per line::

    journal/ledger/runs.jsonl        one per (run, stage)
    journal/ledger/candidates.jsonl  every name in the pre-selection pool
    journal/ledger/decisions.jsonl   every rating, at every stage
    journal/ledger/outcomes.jsonl    resolution at maturity

Separate files rather than one tagged stream because they are read separately
and they grow at wildly different rates — a day is ~1 run row, ~40 candidate
rows, ~15 decision rows, and outcome rows that arrive weeks later.

Append-only is enforced by having no other write path: :meth:`append` opens in
``"a"``, and the GCS round trip merges rather than overwrites (see
:mod:`tradingagent.storage`). A record that turns out to be wrong is superseded
by a later record with the same :attr:`key`, never edited — readers that want
one row per key call :meth:`latest`, which is last-write-wins.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator

from .provenance import Provenance

log = logging.getLogger(__name__)

LEDGER_DIRNAME = "ledger"

RUNS = "runs.jsonl"
CANDIDATES = "candidates.jsonl"
DECISIONS = "decisions.jsonl"
OUTCOMES = "outcomes.jsonl"

#: Every stream, in the order a reader would want them restored.
STREAMS: tuple[str, ...] = (RUNS, CANDIDATES, DECISIONS, OUTCOMES)

SCHEMA_VERSION = 1


@dataclass
class _Record:
    """Shared shape: a provenance block, a schema version, and a key."""

    provenance: Provenance

    @property
    def key(self) -> tuple[str, ...]:  # pragma: no cover - overridden
        raise NotImplementedError

    def _base(self) -> dict[str, Any]:
        return {"v": SCHEMA_VERSION, "provenance": self.provenance.to_dict()}


@dataclass
class RunRecord(_Record):
    """One row per (run, stage) — the fingerprint, on its own, once.

    Written even when the stage degrades, because "we ran and it produced
    nothing" and "we did not run" are different facts and only the ledger can
    tell them apart afterwards.
    """

    stage: str = ""
    started_at: str = ""
    universe_size: int = 0
    candidates: int = 0
    shortlisted: int = 0
    queued: int = 0
    degraded: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def key(self) -> tuple[str, ...]:
        return (self.provenance.run_id, self.stage)

    def to_dict(self) -> dict[str, Any]:
        return {
            **self._base(),
            "stage": self.stage,
            "started_at": self.started_at,
            "universe_size": self.universe_size,
            "candidates": self.candidates,
            "shortlisted": self.shortlisted,
            "queued": self.queued,
            "degraded": list(self.degraded),
            "notes": list(self.notes),
        }


@dataclass
class CandidateRecord:
    """One name in the pre-selection pool, selected or not.

    ``selected`` is the *control* arm and ``counterfactual_selected`` the
    *treatment* arm of item 4's experiment. They can be compared directly on
    forward returns precisely because the signals are shadowed: the shortlist
    that shipped is the price-only list by construction, so no separate control
    run has to be paid for.
    """

    provenance: Provenance
    ticker: str
    date: str
    screener_score: int = 0
    screen_rank: int = 0
    final_rank: int = 0
    #: What the signal layer was allowed to add. Zero while every source is
    #: shadowed, which is the whole point of recording it separately.
    signal_adjustment: float = 0.0
    #: What it would have added at full trust.
    shadow_adjustment: float = 0.0
    #: source -> its own share of ``shadow_adjustment``, in screener points.
    per_signal_shadow: dict[str, float] = field(default_factory=dict)
    #: source -> -1/0/+1 as read at decision time.
    signal_readings: dict[str, int] = field(default_factory=dict)
    sector: str = ""
    #: Survived the per-sector cap and so was eligible for the shortlist.
    eligible: bool = True
    #: Made the shipped (price-only) shortlist.
    selected: bool = False
    #: Would have made the signals-adjusted shortlist.
    counterfactual_selected: bool = False
    #: Made the deep-dive queue.
    queued: bool = False

    @property
    def candidate_id(self) -> str:
        return f"{self.provenance.run_id}:{self.ticker}"

    @property
    def key(self) -> tuple[str, ...]:
        return (self.candidate_id,)

    def to_dict(self) -> dict[str, Any]:
        return {
            "v": SCHEMA_VERSION,
            "provenance": self.provenance.to_dict(),
            "candidate_id": self.candidate_id,
            "ticker": self.ticker,
            "date": self.date,
            "screener_score": self.screener_score,
            "screen_rank": self.screen_rank,
            "final_rank": self.final_rank,
            "signal_adjustment": round(self.signal_adjustment, 3),
            "shadow_adjustment": round(self.shadow_adjustment, 3),
            "per_signal_shadow": {k: round(v, 3) for k, v in sorted(self.per_signal_shadow.items())},
            "signal_readings": dict(sorted(self.signal_readings.items())),
            "sector": self.sector,
            "eligible": self.eligible,
            "selected": self.selected,
            "counterfactual_selected": self.counterfactual_selected,
            "queued": self.queued,
        }


@dataclass
class DecisionRecord:
    """One rating, at one stage, with the arithmetic that went with it.

    ``seat_tiers`` is the A/B hook (item 5): role -> the tier that produced that
    role's output on this run. It is logged and nothing reads it for a decision
    — the comparison it enables (quick-take vs deep, SMART-PM vs DEEP-PM) is a
    later question, and it is only answerable later if the answer is recorded
    now.
    """

    provenance: Provenance
    ticker: str
    date: str
    stage: str
    rating: str = ""
    confidence: str = ""
    horizon: str = ""
    entry_condition: str = ""
    invalidation: str = ""
    target: float | None = None
    trade_plan: dict[str, Any] | None = None
    sector: str = ""
    report: str = ""
    seat_tiers: dict[str, str] = field(default_factory=dict)
    signal_readings: dict[str, int] = field(default_factory=dict)
    degraded: bool = False
    degraded_reasons: list[str] = field(default_factory=list)

    @property
    def decision_id(self) -> str:
        """Stable across re-runs of the same day, so a replay supersedes.

        Keyed on the *decision*, not the run: re-running 2026-08-16's deep
        stage produces a new run id but the same decision about the same
        ticker on the same date, and counting both would double the sample.
        """
        return f"{self.date}:{self.ticker}:{self.stage}"

    @property
    def key(self) -> tuple[str, ...]:
        return (self.decision_id,)

    def to_dict(self) -> dict[str, Any]:
        return {
            "v": SCHEMA_VERSION,
            "provenance": self.provenance.to_dict(),
            "decision_id": self.decision_id,
            "ticker": self.ticker,
            "date": self.date,
            "stage": self.stage,
            "rating": self.rating,
            "confidence": self.confidence,
            "horizon": self.horizon,
            "entry_condition": self.entry_condition,
            "invalidation": self.invalidation,
            "target": self.target,
            "trade_plan": self.trade_plan,
            "sector": self.sector,
            "report": self.report,
            "seat_tiers": dict(sorted(self.seat_tiers.items())),
            "signal_readings": dict(sorted(self.signal_readings.items())),
            "degraded": self.degraded,
            "degraded_reasons": list(self.degraded_reasons),
        }


@dataclass
class OutcomeRecord:
    """What the market did afterwards, per horizon, with the as-of it used.

    Written by ``--stage outcomes`` rather than by the daily run, so a decision
    accrues outcome rows as its horizons mature. Each row supersedes the last
    for the same decision — the 60-day row contains the 1-day one.
    """

    provenance: Provenance
    decision_id: str
    ticker: str
    date: str
    stage: str
    #: The market date the resolution was computed against. Everything in this
    #: record is knowable from the close of this session and no later.
    as_of: str = ""
    reference_close: float | None = None
    benchmark: str = "SPY"
    sector_etf: str = ""
    #: ``{"5": {"return_pct": .., "excess_spy_pct": .., "excess_sector_pct": ..,
    #: "session": "2026-08-21"}}`` — only horizons that have actually matured.
    horizons: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: Best and worst excursion from the reference close over the longest
    #: matured horizon, in percent. ``mfe`` is signed by the plan's direction:
    #: for a short, a fall is favourable.
    mfe_pct: float | None = None
    mae_pct: float | None = None
    excursion_window: int = 0
    #: Did the published entry ever trade? Then did the stop or the target come
    #: first? ``None`` means the plan carried no such level to test.
    entry_triggered: bool | None = None
    stop_hit: bool | None = None
    target_hit: bool | None = None
    first_hit: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def key(self) -> tuple[str, ...]:
        return (self.decision_id,)

    @property
    def matured(self) -> list[int]:
        return sorted(int(h) for h in self.horizons)

    def excess(self, horizon: int, benchmark: str = "spy") -> float | None:
        row = self.horizons.get(str(horizon))
        if not row:
            return None
        value = row.get(f"excess_{benchmark}_pct")
        return float(value) if value is not None else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "v": SCHEMA_VERSION,
            "provenance": self.provenance.to_dict(),
            "decision_id": self.decision_id,
            "ticker": self.ticker,
            "date": self.date,
            "stage": self.stage,
            "as_of": self.as_of,
            "reference_close": self.reference_close,
            "benchmark": self.benchmark,
            "sector_etf": self.sector_etf,
            "horizons": self.horizons,
            "mfe_pct": self.mfe_pct,
            "mae_pct": self.mae_pct,
            "excursion_window": self.excursion_window,
            "entry_triggered": self.entry_triggered,
            "stop_hit": self.stop_hit,
            "target_hit": self.target_hit,
            "first_hit": self.first_hit,
            "notes": list(self.notes),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "OutcomeRecord":
        return cls(
            provenance=Provenance.from_dict(raw.get("provenance") or {}),
            decision_id=str(raw.get("decision_id", "")),
            ticker=str(raw.get("ticker", "")),
            date=str(raw.get("date", "")),
            stage=str(raw.get("stage", "")),
            as_of=str(raw.get("as_of", "")),
            reference_close=raw.get("reference_close"),
            benchmark=str(raw.get("benchmark", "SPY")),
            sector_etf=str(raw.get("sector_etf", "")),
            horizons=dict(raw.get("horizons") or {}),
            mfe_pct=raw.get("mfe_pct"),
            mae_pct=raw.get("mae_pct"),
            excursion_window=int(raw.get("excursion_window", 0) or 0),
            entry_triggered=raw.get("entry_triggered"),
            stop_hit=raw.get("stop_hit"),
            target_hit=raw.get("target_hit"),
            first_hit=str(raw.get("first_hit", "")),
            notes=list(raw.get("notes") or []),
        )


class ExperimentLedger:
    """Append-only reader/writer over the four streams.

    Never raises on a malformed line: the ledger is mirrored through a bucket
    and merged, and one truncated row must not take the weekly report down. It
    is skipped, counted and logged.
    """

    def __init__(self, root: Path):
        self.root = Path(root)

    @classmethod
    def beside_journal(cls, journal_path: Path) -> "ExperimentLedger":
        return cls(Path(journal_path).parent / LEDGER_DIRNAME)

    def path(self, stream: str) -> Path:
        return self.root / stream

    # -- writing ---------------------------------------------------------
    def append(self, stream: str, records: Iterable[Any]) -> int:
        """Append records (anything with ``to_dict``). Returns rows written."""
        rows = [r.to_dict() if hasattr(r, "to_dict") else dict(r) for r in records]
        if not rows:
            return 0
        path = self.path(stream)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, separators=(",", ":"), default=str) + "\n")
        log.info("Ledger: wrote %d row(s) to %s", len(rows), path)
        return len(rows)

    # -- reading ---------------------------------------------------------
    def read(self, stream: str) -> list[dict[str, Any]]:
        return list(self.iter_rows(stream))

    def iter_rows(self, stream: str) -> Iterator[dict[str, Any]]:
        path = self.path(stream)
        if not path.exists():
            return
        skipped = 0
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                skipped += 1
        if skipped:
            log.warning("Ledger: skipped %d malformed line(s) in %s", skipped, path)

    def latest(self, stream: str, key: str | tuple[str, ...]) -> dict[str, Any]:
        """``{key value: row}``, last write wins. ``key`` is a field name or names."""
        fields = (key,) if isinstance(key, str) else tuple(key)
        out: dict[Any, dict[str, Any]] = {}
        for row in self.iter_rows(stream):
            out[tuple(str(row.get(f, "")) for f in fields)] = row
        return {k[0] if len(k) == 1 else k: v for k, v in out.items()}

    def counts(self) -> dict[str, int]:
        return {stream: sum(1 for _ in self.iter_rows(stream)) for stream in STREAMS}
