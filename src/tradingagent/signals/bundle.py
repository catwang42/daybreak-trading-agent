"""Fuse the sources into one per-ticker bundle, and the registry that owns them.

This is the layer the rest of the app talks to. The discovery stage asks a
bundle for a ranking adjustment; the deep stage asks it for a prompt block.
Neither knows how many sources exist or which ones ran, which is what makes
adding the fifth (social sentiment) a registry edit and dropping a noisy one
at Gate 3 a one-line change.

Fusion rule, stated plainly because it decides what the human sees:

- Per-ticker signals move that ticker's rank. Market-wide signals do not —
  they are the same for every candidate, so they would shift all scores
  equally and change no ordering. They go into the prompts as context instead.
- Each source's contribution is ``direction × strength × weight``, where the
  weight comes from :mod:`tradingagent.signals.accuracy` — a source earns its
  influence from the journal or loses it.
- The total is clamped. A signal layer that can override the price screen is
  a different product; this one is allowed to reorder a shortlist, not
  rewrite it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date

from ..data.validate import DegradedTracker
from .base import DIRECTION_WORD, Signal, SignalSource, SourceResult

log = logging.getLogger(__name__)

#: Most a fused signal set may move one candidate's screener score, in points.
#: The screener's own range is 0-100 and its spread across a day's pool is
#: typically 30-40 points, so this can promote a name a few places, not to the
#: top. Deliberately conservative until the accuracy tracker has a verdict.
MAX_SCORE_ADJUSTMENT = 8.0


@dataclass
class SignalBundle:
    """Everything the signal layer knows about one ticker, plus today's backdrop."""

    symbol: str
    run_date: date
    ticker_signals: list[Signal] = field(default_factory=list)
    market_signals: list[Signal] = field(default_factory=list)
    weights: dict[str, float] = field(default_factory=dict)
    skipped: dict[str, str] = field(default_factory=dict)

    def weight_for(self, source: str) -> float:
        return self.weights.get(source, 1.0)

    @property
    def sources_present(self) -> list[str]:
        return sorted({s.source for s in self.ticker_signals + self.market_signals})

    def score_adjustment(self) -> float:
        """Points to add to this candidate's screener score, clamped."""
        raw = sum(s.signed_strength * self.weight_for(s.source) for s in self.ticker_signals)
        scaled = raw * MAX_SCORE_ADJUSTMENT / max(1.0, len(self.ticker_signals))
        return max(-MAX_SCORE_ADJUSTMENT, min(MAX_SCORE_ADJUSTMENT, scaled))

    def net_direction(self) -> int:
        adjustment = self.score_adjustment()
        return 1 if adjustment > 0.5 else -1 if adjustment < -0.5 else 0

    def readings(self) -> dict[str, int]:
        """Source name -> direction held at decision time, for the journal.

        Market-wide signals are included even though they do not move the
        ranking: the accuracy tracker still wants to know whether the macro
        read was right, and it can only learn that from a recorded direction.
        A source firing more than once for one ticker is reduced to the sign of
        its net, because that is the view it actually expressed.
        """
        totals: dict[str, float] = {}
        for signal in self.ticker_signals + self.market_signals:
            totals[signal.source] = totals.get(signal.source, 0.0) + signal.signed_strength
        return {
            source: 1 if total > 0 else -1 if total < 0 else 0 for source, total in totals.items()
        }

    def summary(self) -> str:
        """One line for the shortlist table."""
        if not self.ticker_signals:
            return "no ticker-level signals"
        adjustment = self.score_adjustment()
        parts = [
            f"{s.source} {DIRECTION_WORD[s.direction]} {s.strength:.2f}" for s in self.ticker_signals
        ]
        return f"{'; '.join(parts)} → {adjustment:+.1f} pts"

    def ticker_block(self) -> str:
        """The per-ticker half of the signal layer, plus what failed to report.

        Separate from :func:`market_block` because the market-wide half is
        identical for every name and is rendered once into the shared market
        context; repeating it per ticker would pay for the same tokens five
        times and invite a role to read it as ticker-specific.
        """
        out = [f"### Signal layer for {self.symbol}", ""]
        if self.ticker_signals:
            for signal in self.ticker_signals:
                out.append(signal.line())
                if signal.detail:
                    out.append(signal.detail)
            out += ["", f"Net effect on today's ranking: {self.score_adjustment():+.1f} points."]
        else:
            out.append("- No ticker-level signals fired for this name today.")
        if self.skipped:
            out += ["", *_skipped_lines(self.skipped)]
        return "\n".join(out)

    def prompt_block(self) -> str:
        """Ticker signals and the market backdrop together, for a standalone prompt."""
        return "\n".join([self.ticker_block(), "", market_block(self.market_signals)])


def market_block(signals: list[Signal]) -> str:
    """The market-wide backdrop, rendered once per run for every role to share."""
    out = ["### Signal layer — market-wide backdrop", ""]
    if not signals:
        out.append("- No market-wide signal source reported this run.")
        return "\n".join(out)
    for signal in signals:
        out.append(signal.line())
        if signal.detail:
            out.append(signal.detail)
    out += [
        "",
        "These readings are identical for every candidate today, so they do not change the "
        "ranking. Use them to judge whether a setup is running with the tape or against it.",
    ]
    return "\n".join(out)


def _skipped_lines(skipped: dict[str, str]) -> list[str]:
    return [
        "### Signal sources that did not report",
        "",
        *[f"- {name}: {reason}" for name, reason in sorted(skipped.items())],
        "",
        "Do not infer anything from a source that did not run — absence here is a gap in "
        "our data, not a neutral reading.",
    ]


class SignalHub:
    """Owns the source registry, runs them once, and hands out bundles.

    One fetch per source per run, not per ticker: every client here is either
    rate-limited (EDGAR), keyed with a quota (Finnhub, FRED), or returns the
    same market-wide answer to everyone (FRED, Polymarket).
    """

    def __init__(
        self,
        sources: list[SignalSource],
        degraded: DegradedTracker | None = None,
        weights: dict[str, float] | None = None,
    ):
        self.sources = sources
        self.degraded = degraded if degraded is not None else DegradedTracker()
        self.weights = weights or {}
        self.results: list[SourceResult] = []

    def collect(self, symbols: list[str], run_date: date) -> None:
        self.results = [source.fetch(symbols, run_date) for source in self.sources]
        fired = sum(len(r.signals) for r in self.results)
        log.info(
            "Signal layer: %d signal(s) from %d/%d source(s) over %d ticker(s)",
            fired,
            sum(1 for r in self.results if r.ok and r.signals),
            len(self.sources),
            len(symbols),
        )

    @property
    def skipped(self) -> dict[str, str]:
        return {r.source: r.error for r in self.results if r.error}

    def bundle(self, symbol: str, run_date: date) -> SignalBundle:
        signals = [s for result in self.results for s in result.signals]
        return SignalBundle(
            symbol=symbol,
            run_date=run_date,
            ticker_signals=[s for s in signals if s.symbol == symbol],
            market_signals=[s for s in signals if s.symbol is None],
            weights=self.weights,
            skipped=self.skipped,
        )

    @property
    def market_signals(self) -> list[Signal]:
        return [s for r in self.results for s in r.signals if s.symbol is None]

    def market_block(self) -> str:
        """The shared backdrop, plus this run's skipped sources."""
        block = market_block(self.market_signals)
        if self.skipped:
            block += "\n\n" + "\n".join(_skipped_lines(self.skipped))
        return block

    def source_rows(self) -> list[tuple[str, str, str]]:
        """Rows for the report's source table: (name, coverage, status)."""
        by_name = {source.name: source for source in self.sources}
        rows = []
        for result in self.results:
            source = by_name.get(result.source)
            coverage = source.describes if source else ""
            status = result.error or f"{len(result.signals)} signal(s)"
            rows.append((result.source, coverage, status))
        return rows


def build_default_hub(
    finnhub,
    degraded: DegradedTracker | None = None,
    weights: dict[str, float] | None = None,
) -> SignalHub:
    """The four Milestone 3 sources.

    A fifth social-sentiment source belongs in this list and nowhere else. That
    slot is empty: Reddit was the intended occupant and the API application was
    rejected, so there is no retail-chatter input and no pending one. Adding a
    replacement (StockTwits, Bluesky, a licensed feed) means writing one
    :class:`~tradingagent.signals.base.SignalSource` and appending it here.
    """
    from .insiders import InsiderSource
    from .macro import MacroSource
    from .news import NewsToneSource
    from .prediction import PredictionMarketSource

    tracker = degraded if degraded is not None else DegradedTracker()
    return SignalHub(
        sources=[
            NewsToneSource(finnhub=finnhub, degraded=tracker),
            InsiderSource(degraded=tracker),
            MacroSource(degraded=tracker),
            PredictionMarketSource(degraded=tracker),
        ],
        degraded=tracker,
        weights=weights,
    )
