"""The handoff between the deep stage and the options stage.

Same pattern, and the same reason, as
:mod:`tradingagent.pipeline.context`: ``--stage all`` passes this in memory,
while a standalone ``--stage options`` reads the JSON the deep stage left in the
report directory. Without it the options stage would have to re-run twelve LLM
calls per ticker to learn a rating it already wrote down.

Only what the overlay needs is persisted: the verdict, the levels the strikes
are anchored to, and the spot price the greeks are solved against. Rerunning the
options stage against yesterday's file therefore reproduces yesterday's strike
selection against today's chain — which is exactly what you want on a Saturday.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path

from .levels import ENTRY, INVALIDATION, TARGET, PriceLevel, classify

log = logging.getLogger(__name__)

CONTEXT_FILENAME = "options-context.json"
# v2 (M6) names the research snapshot every spot and level below was taken
# from, so the overlay can say which moment its strikes are anchored to.
# v3 (M6) types the levels: a chart level a strike may be anchored to is no
# longer the same kind of thing as the equity plan's invalidation or target,
# which are constraints on a strike. See :mod:`.levels`.
SCHEMA_VERSION = 3


@dataclass
class VerdictRow:
    """One ticker's portfolio-manager verdict, plus the levels a strike needs."""

    symbol: str
    name: str = ""
    rating: str = "DEGRADED"
    confidence: str = ""
    price_target: float | None = None
    time_horizon: str | None = None
    executive_summary: str = ""
    invalidation: str = ""
    spot: float | None = None
    #: Typed price levels: chart levels a strike may be anchored to, plus the
    #: equity plan's entry, invalidation and target, which constrain one. Stored
    #: as dicts so the context stays plain JSON; read via :meth:`price_levels`.
    levels: list[dict] = field(default_factory=list)
    #: The rendered price block the deep roles argued over, reused verbatim so
    #: the strategist anchors to the same levels the equity thesis did.
    price_context: str = ""
    earnings_note: str = ""
    #: Percentage points, as yfinance reports it (see data/fundamentals.py).
    dividend_yield_pct: float | None = None
    degraded: bool = False

    def price_levels(self) -> list[PriceLevel]:
        return [PriceLevel.from_dict(raw) for raw in self.levels]


@dataclass
class OptionsContext:
    run_date: str
    data_as_of: str = "unknown"
    verdicts: list[VerdictRow] = field(default_factory=list)
    #: The research snapshot the spots and levels below came from. The option
    #: chain is deliberately fresher than this — see
    #: :meth:`tradingagent.snapshot.ResearchSnapshot.derive` — and the overlay
    #: prints both moments rather than pretending they are one.
    snapshot_id: str = ""
    market_as_of: str = ""
    version: int = SCHEMA_VERSION

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=False)

    @classmethod
    def from_json(cls, text: str) -> OptionsContext:
        raw = json.loads(text)
        version = int(raw.get("version", 0))
        if version != SCHEMA_VERSION:
            raise ValueError(
                f"options context is schema v{version}, this build expects "
                f"v{SCHEMA_VERSION}; re-run the deep stage"
            )
        verdicts = [VerdictRow(**item) for item in raw.get("verdicts", [])]
        return cls(**{**raw, "verdicts": verdicts})

    def write(self, directory: Path) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / CONTEXT_FILENAME
        path.write_text(self.to_json())
        return path

    @classmethod
    def read(cls, directory: Path) -> OptionsContext:
        path = directory / CONTEXT_FILENAME
        if not path.exists():
            raise FileNotFoundError(
                f"No options context at {path}. Run `--stage deep` for this date "
                f"first, or use `--stage all`."
            )
        return cls.from_json(path.read_text())

    @property
    def date(self) -> date:
        return date.fromisoformat(self.run_date)

    def select(self, only: list[str] | None = None) -> list[VerdictRow]:
        if not only:
            return list(self.verdicts)
        wanted = {s.strip().upper() for s in only if s.strip()}
        return [v for v in self.verdicts if v.symbol.upper() in wanted]


# Which indicators become strike anchors, and what to call them in the report.
# Deliberately the same levels the deep pipeline already reasoned over: an
# overlay anchored to a level the equity thesis never mentioned is a second,
# unexplained opinion.
LEVEL_LABELS: list[tuple[str, str]] = [
    ("close_50_sma", "50-day SMA"),
    ("close_200_sma", "200-day SMA"),
    ("boll_lb", "Bollinger lower band"),
    ("boll_ub", "Bollinger upper band"),
]


def levels_from(result) -> list[PriceLevel]:
    """Pull the levels out of a finished :class:`DeepResult`, each with its role.

    Kept here rather than in the deep pipeline because these are the options
    stage's requirements, and the deep stage should not have to know them.

    Chart levels are classified by the side of the market they sit on. The
    equity plan's own levels keep their meaning: an invalidation is not a
    support a strike may be parked on, it is the line assignment must stay
    below (see :mod:`.levels`).
    """
    levels: list[PriceLevel] = []
    evidence = getattr(result, "evidence", None)
    indicators = getattr(evidence, "indicators", None)
    spot = getattr(evidence, "price", None)
    if indicators is not None:
        spot = spot or float(indicators.close)
        for key, label in LEVEL_LABELS:
            value = indicators.get(key)
            if value:
                levels.append(PriceLevel(label, float(value), classify(label, float(value), spot), key))
        atr = indicators.get("atr")
        if atr:
            band = float(indicators.close) - 2 * float(atr)
            levels.append(PriceLevel("2-ATR band", band, classify("2-ATR band", band, spot), "atr"))

    screener = getattr(result.queued, "screener", {}) or {}
    for key, label in (("stop_ref", "screener stop reference"), ("entry_ref", "screener entry reference")):
        try:
            value = float(screener.get(key))
        except (TypeError, ValueError):
            continue
        if value > 0:
            levels.append(PriceLevel(label, value, classify(label, value, spot), f"screener.{key}"))

    # The plan the risk seats argued over, not the prose around it.
    plan = getattr(result, "trade_plan", None)
    for attr, role, label in (
        ("entry", ENTRY, "planned entry"),
        ("stop", INVALIDATION, "planned invalidation"),
        ("target", TARGET, "planned target"),
    ):
        value = getattr(plan, attr, None)
        if value:
            levels.append(PriceLevel(label, float(value), role, "trade plan"))

    decision = getattr(result, "decision", None)
    target = getattr(decision, "price_target", None)
    if target and not any(lv.role == TARGET for lv in levels):
        levels.append(
            PriceLevel("portfolio manager price target", float(target), TARGET, "portfolio manager")
        )
    return levels


def build_options_context(
    results,
    run_date: date,
    data_as_of: str = "unknown",
    snapshot_id: str = "",
    market_as_of: str = "",
) -> OptionsContext:
    """Freeze what the options stage needs out of a finished deep run.

    Tickers whose deep dive produced no verdict are carried with
    ``rating="DEGRADED"`` rather than dropped: the options stage then reports
    "no overlay, the equity analysis degraded" instead of leaving a name
    silently absent from section 6.
    """
    rows: list[VerdictRow] = []
    for result in results:
        decision = getattr(result, "decision", None)
        evidence = getattr(result, "evidence", None)
        fundamentals = getattr(evidence, "fundamentals", None)
        rows.append(
            VerdictRow(
                symbol=result.symbol,
                name=result.queued.name or result.symbol,
                rating=decision.rating if decision else "DEGRADED",
                confidence=decision.confidence if decision else "",
                price_target=decision.price_target if decision else None,
                time_horizon=decision.time_horizon if decision else None,
                executive_summary=decision.executive_summary if decision else "",
                invalidation=decision.invalidation if decision else "",
                spot=getattr(evidence, "price", None),
                levels=[lv.to_dict() for lv in levels_from(result)],
                price_context=evidence.price_context() if evidence else "",
                earnings_note=result.queued.earnings_note,
                dividend_yield_pct=getattr(fundamentals, "dividend_yield", None),
                degraded=bool(getattr(result, "degraded", False)),
            )
        )
    return OptionsContext(
        run_date=run_date.isoformat(),
        data_as_of=data_as_of,
        verdicts=rows,
        snapshot_id=snapshot_id,
        market_as_of=market_as_of,
    )
