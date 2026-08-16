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
  influence from the journal or has none.
- The total is clamped to what the *sources that fired* have earned. A source
  with no resolved record is worth zero points, so with today's journal every
  bundle adjusts by 0.0 and the shortlist is the price screener's alone.
  What the layer *would* have done is still computed and reported, as
  :meth:`SignalBundle.shadow_adjustment`.

The shadow half is not decoration. Gate 3 asks whether these sources change
decisions or just burn tokens, and that question can only be answered by
recording what they wanted to do while they were not allowed to do it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date

from ..data.validate import DegradedTracker
from .accuracy import PROVEN_MAX_ADJUSTMENT
from .base import DIRECTION_WORD, Signal, SignalSource, SourceResult

log = logging.getLogger(__name__)

#: Ceiling on any fused signal set, in screener points, reachable only by a
#: source a human has marked proven. The screener's own range is 0-100 and its
#: spread across a day's pool is typically 30-40 points, so even this can
#: promote a name a few places, not to the top.
MAX_SCORE_ADJUSTMENT = PROVEN_MAX_ADJUSTMENT


@dataclass
class SignalBundle:
    """Everything the signal layer knows about one ticker, plus today's backdrop."""

    symbol: str
    run_date: date
    ticker_signals: list[Signal] = field(default_factory=list)
    market_signals: list[Signal] = field(default_factory=list)
    weights: dict[str, float] = field(default_factory=dict)
    #: Source -> points it has earned the right to move a score by. Absent
    #: means zero: a source nobody has graded moves nothing.
    caps: dict[str, float] = field(default_factory=dict)
    skipped: dict[str, str] = field(default_factory=dict)

    def weight_for(self, source: str) -> float:
        """A source's multiplier. Unknown means untested, and untested is 0.

        Defaulting to 1.0 here was the cold-start bug: it made "never checked"
        indistinguishable from "checked and average", and every new source
        arrived with full authority over the shortlist.
        """
        return self.weights.get(source, 0.0)

    def cap_for(self, source: str) -> float:
        return self.caps.get(source, 0.0)

    @property
    def sources_present(self) -> list[str]:
        return sorted({s.source for s in self.ticker_signals + self.market_signals})

    @property
    def max_adjustment(self) -> float:
        """The furthest today's firing sources may move this candidate.

        The most-graduated source that fired sets the ceiling, so one earned
        source is not held back by an untested one firing beside it.
        """
        return max((self.cap_for(s.source) for s in self.ticker_signals), default=0.0)

    @property
    def is_shadow(self) -> bool:
        """True when the layer had a view but has not earned the right to act."""
        return bool(self.ticker_signals) and self.max_adjustment == 0.0

    def _fuse(self, cap: float, weight_of) -> float:
        raw = sum(s.signed_strength * weight_of(s.source) for s in self.ticker_signals)
        scaled = raw * cap / max(1.0, len(self.ticker_signals))
        clamped = max(-cap, min(cap, scaled))
        # `+ 0.0` collapses -0.0, which formats as "-0.0" and reads in a report
        # as a bearish nudge rather than as nothing at all.
        return clamped + 0.0

    def score_adjustment(self) -> float:
        """Points to add to this candidate's screener score, clamped.

        0.0 for every source that has not graduated, which is currently all of
        them — see :mod:`tradingagent.signals.accuracy`.
        """
        return self._fuse(self.max_adjustment, self.weight_for)

    def shadow_adjustment(self) -> float:
        """What :meth:`score_adjustment` would return under full trust.

        The counterfactual the old code shipped as fact: every source at
        weight 1.0 against the proven ceiling. Reported, never applied.
        """
        return self._fuse(MAX_SCORE_ADJUSTMENT, lambda _source: 1.0)

    def net_direction(self) -> int:
        """The layer's directional read, shadow or not.

        Deliberately taken from the shadow figure: this feeds the journal and
        the prompts, and a source whose weight is zero still held a view worth
        grading later. It does not move the ranking — only
        :meth:`score_adjustment` does that.
        """
        adjustment = self.shadow_adjustment()
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

    def effect_note(self) -> str:
        """How this bundle's arithmetic ended up, in the report's words."""
        if not self.ticker_signals:
            return "no ticker-level signals"
        if self.is_shadow:
            return f"SHADOW — would have changed: {self.shadow_adjustment():+.1f} pts"
        return f"{self.score_adjustment():+.1f} pts (shadow {self.shadow_adjustment():+.1f})"

    def summary(self) -> str:
        """One line for the shortlist table."""
        if not self.ticker_signals:
            return "no ticker-level signals"
        parts = [
            f"{s.source} {DIRECTION_WORD[s.direction]} {s.strength:.2f}" for s in self.ticker_signals
        ]
        return f"{'; '.join(parts)} → {self.effect_note()}"

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
            if self.is_shadow:
                out += [
                    "",
                    "**SHADOW — would have changed: "
                    f"{self.shadow_adjustment():+.1f} points.** No source above has resolved "
                    "enough journal outcomes to earn ranking influence, so the actual effect on "
                    "today's ranking is 0.0 points and this name is here on price action alone. "
                    "Read the readings as one more opinion, not as evidence that has been "
                    "checked against anything.",
                ]
            else:
                out += [
                    "",
                    f"Net effect on today's ranking: {self.score_adjustment():+.1f} points "
                    f"(capped at ±{self.max_adjustment:.0f} by what these sources have earned; "
                    f"under full trust it would be {self.shadow_adjustment():+.1f}).",
                ]
        else:
            out.append("- No ticker-level signals fired for this name today.")
        if self.skipped:
            out += ["", *_skipped_lines(self.skipped)]
        return "\n".join(out)

    def prompt_block(self) -> str:
        """Ticker signals and the market backdrop together, for a standalone prompt."""
        return "\n".join([self.ticker_block(), "", market_block(self.market_signals)])


@dataclass
class ShadowRanking:
    """The shortlist the signal layer wanted, beside the one it got.

    Gate 3 needs a number, not an assurance. Recording which names the layer
    would have promoted — while it promoted none — is what turns "the signals
    are shadowed" into a claim someone can check in a month, against outcomes,
    before deciding whether to graduate a source.
    """

    size: int
    #: Symbols actually shortlisted, in screener order.
    chosen: list[str] = field(default_factory=list)
    #: Symbols the layer would have shortlisted, in its own order.
    shadow: list[str] = field(default_factory=list)
    #: Symbol -> the shadow adjustment that produced that order.
    adjustments: dict[str, float] = field(default_factory=dict)

    @property
    def would_promote(self) -> list[str]:
        return [s for s in self.shadow if s not in self.chosen]

    @property
    def would_drop(self) -> list[str]:
        return [s for s in self.chosen if s not in self.shadow]

    @property
    def would_reorder(self) -> bool:
        return self.chosen != self.shadow

    def note(self) -> str:
        """One sentence for the report and the log."""
        if not self.would_reorder:
            return "SHADOW — would have changed: nothing; the layer agreed with the screener."
        swaps = (
            f"in {', '.join(self.would_promote)} / out {', '.join(self.would_drop)}"
            if self.would_promote
            else "the order only"
        )
        return f"SHADOW — would have changed: {swaps}."


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
        caps: dict[str, float] | None = None,
    ):
        self.sources = sources
        self.degraded = degraded if degraded is not None else DegradedTracker()
        self.weights = weights or {}
        self.caps = caps or {}
        self.results: list[SourceResult] = []
        #: Filled in by the shortlist selector: what the layer would have done
        #: to today's ranking if its sources had earned the right to act.
        self.shadow: "ShadowRanking | None" = None

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
            caps=self.caps,
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
    caps: dict[str, float] | None = None,
    as_of: date | None = None,
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
            NewsToneSource(finnhub=finnhub, degraded=tracker, as_of=as_of),
            InsiderSource(degraded=tracker),
            MacroSource(degraded=tracker),
            PredictionMarketSource(degraded=tracker),
        ],
        degraded=tracker,
        weights=weights,
        caps=caps,
    )
