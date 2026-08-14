"""The one shape every signal source produces, and the contract they implement.

Milestone 3 fuses four independent sources — company news tone, insider Form 4
filings, macro series, and prediction-market odds — into a per-ticker bundle
that re-ranks the shortlist and feeds the analyst and debate prompts. They
share nothing but this module: each client owns its own transport, its own
rate limits and its own failure mode, and hands back :class:`Signal` objects.

That indirection is the point. A fifth source (Reddit or StockTwits social
sentiment, blocked on manual API approval at the time of writing) has to
implement :class:`SignalSource` and be added to the registry; nothing in the
bundle, the ranking, the prompts or the accuracy tracker needs to change. The
same is true in the other direction: Gate 3 asks whether each source changes
decisions or just burns tokens, and dropping one is a registry edit.

No source may raise. A dead endpoint is a DEGRADED line in the report, never a
lost run — the whole daily pipeline hangs off these calls.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date
from typing import ClassVar, Literal

from ..data.validate import DegradedTracker

log = logging.getLogger(__name__)

Scope = Literal["ticker", "market"]

# Directional vocabulary. Deliberately coarse: these are inputs to a judgement,
# not a judgement. A source that claims more precision than "which way, and how
# hard" is over-fitting a free data tier.
Direction = Literal[-1, 0, 1]

DIRECTION_WORD = {1: "bullish", 0: "neutral", -1: "bearish"}


@dataclass(frozen=True)
class Signal:
    """One observation from one source.

    ``strength`` is 0.0–1.0 within the source's own scale — it says how firmly
    *this* source holds *this* view, not how it compares to another source.
    Cross-source weighting is the bundle's job, informed by the accuracy
    tracker, because only the journal can say which source has earned weight.
    """

    source: str
    kind: str
    direction: Direction
    strength: float
    headline: str
    as_of: date
    symbol: str | None = None  # None means market-wide
    detail: str = ""
    url: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "strength", max(0.0, min(1.0, self.strength)))

    @property
    def signed_strength(self) -> float:
        return self.direction * self.strength

    def line(self) -> str:
        """One rendered line for a prompt or a report table."""
        stamp = self.as_of.isoformat()
        return (
            f"- [{stamp}] **{self.source}** ({DIRECTION_WORD[self.direction]}, "
            f"{self.strength:.2f}): {self.headline}"
        )


@dataclass
class SourceResult:
    """What one source returned this run, including the fact that it returned nothing."""

    source: str
    signals: list[Signal] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


class SignalSource(ABC):
    """Base class for every signal client.

    Subclasses implement :meth:`collect` and may assume it is allowed to raise;
    :meth:`fetch` is what the pipeline calls, and it converts any failure into
    a DEGRADED note plus an empty result.
    """

    name: ClassVar[str]
    scope: ClassVar[Scope]
    #: Human-readable note for the report's source table.
    describes: ClassVar[str] = ""

    def __init__(self, degraded: DegradedTracker | None = None):
        self.degraded = degraded if degraded is not None else DegradedTracker()

    @abstractmethod
    def collect(self, symbols: list[str], run_date: date) -> list[Signal]:
        """Fetch and score. May raise; :meth:`fetch` contains the failure."""

    def available(self) -> tuple[bool, str]:
        """Whether this source can run at all — e.g. a missing API key.

        Returns ``(ok, reason)``. An unavailable source is skipped without a
        DEGRADED line the first time, because a key the operator chose not to
        set is a configuration decision, not a failure.
        """
        return True, ""

    def fetch(self, symbols: list[str], run_date: date) -> SourceResult:
        ok, reason = self.available()
        if not ok:
            log.info("Signal source %s skipped: %s", self.name, reason)
            return SourceResult(source=self.name, error=reason)
        try:
            signals = self.collect(symbols, run_date)
        except Exception as exc:  # noqa: BLE001 - a third-party outage cannot end the run
            reason = f"{type(exc).__name__}: {exc}"[:200]
            self.degraded.add(f"signals:{self.name}", reason)
            log.warning("Signal source %s failed: %s", self.name, reason)
            return SourceResult(source=self.name, error=reason)
        log.info("Signal source %s: %d signal(s)", self.name, len(signals))
        return SourceResult(source=self.name, signals=signals)
