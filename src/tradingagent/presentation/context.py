"""What the decision sheet is allowed to say, frozen when the numbers were live.

The email is now a one-screen decision sheet rather than a rendering of the
brief, and that changes where its numbers may come from. The brief is prose: a
regime line, a wait condition and an entry level all exist in it as sentences.
Reading them back out of the markdown would put a regular expression between the
trade plan and the human — which is the exact defect M6 removed when it stopped
the analysts quoting each other's figures. So the sheet never parses a report.

Instead ``--stage all`` writes ``presentation-context.json`` at the moment every
typed object is still in memory — the :class:`~..discovery.breadth.BreadthResult`,
the :class:`~..discovery.sectors.SectorMap`, each
:class:`~..pipeline.trade_plan.TradePlan`, each
:class:`~..options.strategist.OptionsPlan` — and the sheet is built from that and
nothing else. A standalone ``--stage report`` tomorrow then rebuilds the same
sheet from the same numbers, offline.

Two consequences worth stating:

- Sessions that predate this file cannot get a full sheet, and do not get a
  guessed one. :func:`read_or_none` returns ``None``, the sheet degrades to the
  sections it can build from the older contexts, and it says which ones it
  could not.
- The chart series live here too. Persisting sixty-three SPY closes is cheaper
  than persisting a chart, and it means the picture in the email can be redrawn
  from the record rather than re-downloaded from a market that has moved.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

CONTEXT_FILENAME = "presentation-context.json"
SCHEMA_VERSION = 1

#: Sessions of SPY history the market chart draws. Three months of trading.
SPY_CHART_SESSIONS = 63
#: Sessions of per-ticker history the setup charts draw. Six months.
TICKER_CHART_SESSIONS = 126


@dataclass
class ReadingRow:
    """A :class:`~tradingagent.semantics.Reading`, flattened but not defanged.

    ``described`` is the output of :meth:`Reading.describe`, so the
    ``[UNVALIDATED]`` marker an unvalidated term carries survives into the email
    instead of being reconstructed there from a boolean somebody might forget to
    check.
    """

    term: str = ""
    label: str = ""
    described: str = ""
    validation: str = ""

    @classmethod
    def of(cls, reading) -> ReadingRow:
        return cls(
            term=reading.term.key,
            label=reading.canonical_label,
            described=reading.describe(),
            validation=reading.validation,
        )


@dataclass
class SeriesPoint:
    """One session on a line chart. Lists, not frames — this is JSON."""

    d: str
    close: float
    sma50: float | None = None
    sma200: float | None = None


@dataclass
class SectorBar:
    sector: str
    momentum: float
    status: str = "Neutral"
    etf: str | None = None


@dataclass
class Regime:
    """Section 1 of the sheet, and the three market-level charts."""

    composite: float | None = None
    zone: str = ""
    posture: ReadingRow = field(default_factory=ReadingRow)
    rotation: ReadingRow = field(default_factory=ReadingRow)
    risk_regime: str = ""
    risk_score: float = 0.0
    pct_above_50dma: float | None = None
    universe_size: int = 0
    vix: float | None = None
    leaders: list[str] = field(default_factory=list)
    laggards: list[str] = field(default_factory=list)
    overbought: list[str] = field(default_factory=list)
    oversold: list[str] = field(default_factory=list)
    sectors: list[SectorBar] = field(default_factory=list)
    spy: list[SeriesPoint] = field(default_factory=list)


@dataclass
class Gate:
    """A dated release the sheet may tell a human to wait for.

    Only VERIFIED events are ever put in this list — see
    :func:`tradingagent.pipeline.macro_gate.may_gate`. An INDICATIVE date that
    reaches a "do not act before" line has become an instruction, and it was
    never more than a guess.
    """

    date: str = ""
    name: str = ""
    impact: str = ""
    source: str = ""
    confidence: str = ""


@dataclass
class Consensus:
    """The sell side's number beside ours, so the gap is visible without asking."""

    recommendation: str | None = None
    analysts: int | None = None
    mean: float | None = None
    median: float | None = None
    low: float | None = None
    high: float | None = None
    spread: str | None = None

    @property
    def known(self) -> bool:
        return self.mean is not None or self.recommendation is not None


@dataclass
class Setup:
    """One deep-analysed name, as the sheet shows it.

    Every price here is a :class:`~tradingagent.pipeline.trade_plan.TradePlan`
    field copied verbatim. ``wait_condition`` is *computed* from entry versus
    spot rather than lifted from the executive summary, for the same reason.
    """

    symbol: str
    name: str = ""
    rating: str = "DEGRADED"
    confidence: str = ""
    spot: float | None = None
    price_target: float | None = None
    time_horizon: str = ""
    direction: str = ""
    status: str = ""
    entry: float | None = None
    entry_basis: str = ""
    stop: float | None = None
    stop_basis: str = ""
    target: float | None = None
    target_basis: str = ""
    risk_pct: float | None = None
    reward_risk: float | None = None
    size_pct: float | None = None
    wait_condition: str = ""
    invalidation: str = ""
    consensus: Consensus = field(default_factory=Consensus)
    degraded: bool = False
    #: Six months of daily closes for this ticker's chart.
    series: list[SeriesPoint] = field(default_factory=list)

    @property
    def actionable(self) -> bool:
        """A plan the sheet can draw levels for, as opposed to a view."""
        return bool(self.entry and self.stop and self.target)


@dataclass
class Avoid:
    symbol: str
    name: str = ""
    rating: str = ""
    confidence: str = ""
    reason: str = ""


@dataclass
class Overlay:
    """An options candidate that passed every hard filter, and how it sits.

    ``breakeven_status`` is the one thing a reader of section 6 has to check and
    the one thing prose kept burying: an assignment breakeven below the equity
    plan's invalidation means the overlay buys the stock at a price the equity
    thesis has already called wrong.
    """

    symbol: str
    strategy: str = ""
    strike: float | None = None
    expiry: str = ""
    dte: int | None = None
    delta: float | None = None
    credit: float | None = None
    annualized_yield_pct: float | None = None
    breakeven: float | None = None
    invalidation: float | None = None
    breakeven_status: str = ""
    earnings_flag: str = ""
    conflicts: list[str] = field(default_factory=list)


@dataclass
class OverlaySkip:
    symbol: str
    reason: str = ""


@dataclass
class PresentationContext:
    """Everything the decision sheet is permitted to print."""

    run_date: str
    data_as_of: str = "unknown"
    market_as_of: str = ""
    snapshot_id: str = ""
    session_note: str = ""
    stage: str = ""
    regime: Regime = field(default_factory=Regime)
    gates: list[Gate] = field(default_factory=list)
    setups: list[Setup] = field(default_factory=list)
    avoids: list[Avoid] = field(default_factory=list)
    overlays: list[Overlay] = field(default_factory=list)
    overlay_skips: list[OverlaySkip] = field(default_factory=list)
    degraded: list[str] = field(default_factory=list)
    version: int = SCHEMA_VERSION

    @property
    def date(self) -> date:
        return date.fromisoformat(self.run_date)

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=False)

    @classmethod
    def from_json(cls, text: str) -> PresentationContext:
        raw = json.loads(text)
        version = int(raw.get("version", 0))
        if version != SCHEMA_VERSION:
            raise ValueError(
                f"presentation context is schema v{version}, this build expects "
                f"v{SCHEMA_VERSION}; re-run `--stage all` for this date"
            )
        return cls(**{**raw, **_rehydrate(raw)})

    def write(self, directory: Path) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / CONTEXT_FILENAME
        path.write_text(self.to_json())
        return path

    @classmethod
    def read(cls, directory: Path) -> PresentationContext:
        path = directory / CONTEXT_FILENAME
        if not path.exists():
            raise FileNotFoundError(f"No presentation context at {path}.")
        return cls.from_json(path.read_text())


def read_or_none(directory: Path) -> PresentationContext | None:
    """The sheet's loader: absent or unreadable is a degradation, not a crash.

    Deliberately unlike :meth:`DeepContext.read`, which raises on a stale schema
    because a deep stage reasoning from the wrong market picture is worse than
    no deep stage. Here the downside is one weaker email, and refusing to send
    the morning brief because the presentation artefact is a version behind
    would be the wrong trade.
    """
    try:
        return PresentationContext.read(directory)
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
        log.info("No usable presentation context in %s (%s).", directory, exc)
        return None


def _rehydrate(raw: dict[str, Any]) -> dict[str, Any]:
    """Rebuild the nested dataclasses ``asdict`` flattened on the way out."""
    regime_raw = dict(raw.get("regime") or {})
    regime = Regime(
        **{
            **regime_raw,
            "posture": ReadingRow(**(regime_raw.get("posture") or {})),
            "rotation": ReadingRow(**(regime_raw.get("rotation") or {})),
            "sectors": [SectorBar(**s) for s in regime_raw.get("sectors") or []],
            "spy": [SeriesPoint(**p) for p in regime_raw.get("spy") or []],
        }
    )
    setups = []
    for item in raw.get("setups") or []:
        item = dict(item)
        setups.append(
            Setup(
                **{
                    **item,
                    "consensus": Consensus(**(item.get("consensus") or {})),
                    "series": [SeriesPoint(**p) for p in item.get("series") or []],
                }
            )
        )
    return {
        "regime": regime,
        "gates": [Gate(**g) for g in raw.get("gates") or []],
        "setups": setups,
        "avoids": [Avoid(**a) for a in raw.get("avoids") or []],
        "overlays": [Overlay(**o) for o in raw.get("overlays") or []],
        "overlay_skips": [OverlaySkip(**s) for s in raw.get("overlay_skips") or []],
    }
