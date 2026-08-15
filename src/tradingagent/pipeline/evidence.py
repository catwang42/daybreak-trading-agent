"""Assemble the per-ticker evidence pack the deep pipeline reasons over.

This module is the load-bearing deviation from upstream. In
`reference/TradingAgents` each analyst is a LangGraph node holding a toolbox
(`get_stockstats_indicators_report`, `get_YFin_data`, `get_finnhub_news`, ...)
and decides for itself what to fetch over several tool-calling turns. Our
runtime has no graph and no tool loop — CLAUDE.md keeps the LLM layer to a
single provider-agnostic `complete()` call — so we fetch everything once, up
front, and hand each analyst a pre-rendered slice of it.

What that costs: the analysts cannot chase a follow-up question, so the evidence
menu is fixed by us rather than by them.
What it buys: a bounded, predictable number of LLM calls per ticker, no
provider-specific tool-calling API, and one place where every figure a report
cites can be traced to a source and a timestamp (see :meth:`Evidence.sources`).

Each block is rendered here, not in the prompt, so a missing source produces a
visible "unavailable" line in the analyst's evidence rather than an absent
section it cannot notice.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

import pandas as pd

from ..data.finnhub_client import FinnhubFree, NewsItem, news_window
from ..data.fundamentals import Fundamentals, FundamentalsClient, Positioning
from ..data.indicators import IndicatorSet, compute_indicators
from ..data.market import MarketData
from ..data.validate import DegradedTracker
from ..snapshot import Observation, ResearchSnapshot
from .context import DeepContext, QueuedTicker

log = logging.getLogger(__name__)

# Enough history for a 200-day SMA plus a year of context for the percentile reads.
DEEP_HISTORY = "2y"
MIN_BARS = 60
#: Headlines an analyst prompt gets when this stage has to fetch its own.
NEWS_LIMIT = 8


@dataclass
class Evidence:
    """Everything one ticker's analysts are allowed to see."""

    queued: QueuedTicker
    run_date: date
    market_context: str
    macro_note: str
    indicators: IndicatorSet | None = None
    fundamentals: Fundamentals | None = None
    positioning: Positioning | None = None
    news: list[NewsItem] = field(default_factory=list)
    #: The window those headlines were asked for, rendered for the prompt so an
    #: analyst reads "2026-08-07..2026-08-14", not an unfalsifiable "recent".
    news_window_note: str = ""
    missing: list[str] = field(default_factory=list)
    source_notes: list[tuple[str, str]] = field(default_factory=list)
    #: Which research snapshot every price figure below belongs to, and the
    #: session it describes. Empty only if the deep stage ran without one.
    snapshot_id: str = ""
    market_as_of: date | None = None
    #: The last close, with its lineage — the same object discovery ranked on.
    price_observation: Observation | None = None
    #: Anything that had to be fetched outside the snapshot, named so a reader
    #: can see which figures are not covered by the run's one picture.
    off_snapshot: list[str] = field(default_factory=list)

    @property
    def symbol(self) -> str:
        return self.queued.symbol

    @property
    def price(self) -> float | None:
        return self.indicators.close if self.indicators else None

    @property
    def usable(self) -> bool:
        """Below this bar there is nothing honest to analyse."""
        return self.indicators is not None

    def blocking_gaps(self) -> list[str]:
        """Sources that failed *this run*, as opposed to known milestone limits.

        ``missing`` is the reader-facing list and always contains the social
        sentiment line, because we genuinely never fetch it. Confidence rubrics
        must not count that: a permanent entry in a "was anything missing?"
        test makes high confidence permanently unreachable, which is exactly
        how the Gate 2 verdicts all came back M. A couple of absent valuation
        fields are not blocking either — the threshold matches the one
        :class:`~tradingagent.data.fundamentals.FundamentalsClient` already
        uses to call a snapshot sparse.
        """
        gaps = []
        if self.indicators is None:
            gaps.append("price history")
        if (
            self.fundamentals is None
            or len(self.fundamentals.missing) >= 4
            or len(self.fundamentals.suspect_fields()) >= 3
        ):
            # A SUSPECT field is worse than an absent one: it can be believed.
            gaps.append("company fundamentals")
        if self.positioning is None:
            gaps.append("positioning data")
        if not self.news:
            gaps.append("company news")
        return gaps

    # -- rendered slices, one per analyst --------------------------------
    def technical_block(self) -> str:
        parts = ["### Indicator set (daily bars)", ""]
        parts.append(
            self.indicators.markdown() if self.indicators else "Price history unavailable."
        )
        parts += ["", "### What the screener saw this morning", "", self.queued.screener_markdown()]
        return "\n".join(parts)

    def fundamentals_block(self) -> str:
        parts = ["### Company financials (free tier, may be trailing)", ""]
        parts.append(
            self.fundamentals.markdown() if self.fundamentals else "Fundamentals unavailable."
        )
        if self.price is not None:
            parts += ["", f"Last close: ${self.price:,.2f}."]
        return "\n".join(parts)

    def news_block(self) -> str:
        window = self.news_window_note or "last 7 days"
        parts = [f"### Company headlines ({window})", ""]
        if self.news:
            for item in self.news:
                stamp = _stamp(item.datetime_utc)
                parts.append(f"- [{stamp}] {item.headline} ({item.source})")
        else:
            parts.append("- No headlines retrieved for this ticker in the window.")
        parts += [
            "",
            "### Scheduled events",
            "",
            f"- Earnings: {self.queued.earnings_note}",
            f"- Macro releases in the window:\n{_indent(self.macro_note)}",
            "",
            self.signal_block(),
        ]
        return "\n".join(parts)

    def signal_block(self) -> str:
        """The M3 per-ticker signal layer, as handed to the analysts.

        Only the ticker-level half: the market-wide backdrop is already in
        ``market_context``, which every role is shown, so repeating it here
        would pay for the same tokens twice and invite a role to read a
        market-wide reading as something specific to this name.
        """
        return self.queued.signal_block or (
            "### Signal layer\n\n- No signal source ran for this ticker "
            "(see the market context for the run-wide backdrop)."
        )

    def sentiment_block(self) -> str:
        parts = ["### Sell-side posture and float positioning", ""]
        parts.append(
            self.positioning.markdown(self.price)
            if self.positioning
            else "Positioning data unavailable."
        )
        parts += ["", "### Crowding in the tape", ""]
        if self.indicators:
            mfi = self.indicators.get("mfi")
            rsi = self.indicators.get("rsi")
            parts += [
                f"- Money Flow Index (14): {mfi:,.1f}" if mfi is not None else "- Money Flow Index: unavailable",
                f"- RSI (14): {rsi:,.1f}" if rsi is not None else "- RSI: unavailable",
            ]
        parts += [
            f"- Volume vs 20-day average: {self.queued.screener.get('volume_ratio_20d', 'unavailable')}",
            f"- Close location in the day's range: {self.queued.screener.get('close_location_pct', 'unavailable')}%",
            f"- Headlines retrieved in the last 7 days: {len(self.news)}",
            "",
            self.signal_block(),
            "",
            "### Coverage limit",
            "",
            "- No social-media or retail-forum data, and none is coming from Reddit: the API "
            "application was rejected. Insider filings are what corporate officers did with "
            "their own money and headline tone is what the press said — neither is a proxy for "
            "retail positioning, so do not treat them as one.",
        ]
        return "\n".join(parts)

    def price_context(self) -> str:
        """Levels the trader, the risk committee and the manager all price against."""
        if not self.indicators:
            return "Price and level data unavailable for this ticker."
        ind = self.indicators
        s = self.queued.screener
        atr = ind.get("atr")
        lines = [
            f"- Last close: ${ind.close:,.2f}",
            f"- Screener entry reference: ${s.get('entry_ref', 'n/a')}, "
            f"stop reference: ${s.get('stop_ref', 'n/a')} (risk {s.get('risk_pct', 'n/a')}%)",
            f"- 50-day SMA ${_lvl(ind.get('close_50_sma'))}, 200-day SMA ${_lvl(ind.get('close_200_sma'))}",
            f"- Bollinger band ${_lvl(ind.get('boll_lb'))} – ${_lvl(ind.get('boll_ub'))} "
            f"(mid ${_lvl(ind.get('boll'))})",
        ]
        if atr:
            lines.append(
                f"- ATR(14) ${atr:,.2f} = {atr / ind.close * 100:.1f}% of price; "
                f"a 2-ATR stop sits near ${ind.close - 2 * atr:,.2f}"
            )
        lines.append(f"- Earnings: {self.queued.earnings_note}")
        return "\n".join(lines)

    def provenance(self) -> str:
        """One line naming the picture every price below was taken from.

        Before M6 the deep stage downloaded its own bars, so a reader comparing
        the brief with a deep report found two different closes for the same
        ticker and no way to tell which was which. Naming the snapshot is what
        makes that checkable rather than a matter of trust.
        """
        if not self.snapshot_id:
            return (
                "**Provenance unavailable** — this ticker was analysed without a research "
                "snapshot, so its prices cannot be tied to the discovery run."
            )
        line = (
            f"**Snapshot:** `{self.snapshot_id}` · market as of "
            f"{self.market_as_of.isoformat() if self.market_as_of else 'unknown'} close"
        )
        if self.price_observation:
            line += f" · last close {self.price_observation.cite()}"
        if self.off_snapshot:
            line += (
                f"\n\n**Fetched outside the snapshot:** {', '.join(self.off_snapshot)} — "
                "these are not covered by the closes above."
            )
        return line

    def sources(self) -> str:
        """Section 7 of the deep report: what was read, and when."""
        rows = [self.provenance(), "", "| Source | Coverage | As of |", "|---|---|---|"]
        rows += [f"| {name} | {what} | {when} |" for name, what, when in self._source_rows()]
        if self.missing:
            rows += ["", f"**Missing this run:** {', '.join(self.missing)}."]
        return "\n".join(rows)

    def _source_rows(self) -> list[tuple[str, str, str]]:
        rows: list[tuple[str, str, str]] = []
        if self.indicators:
            origin = "discovery snapshot" if self.snapshot_id else "re-downloaded by this stage"
            rows.append(
                (
                    f"yfinance OHLCV ({origin})",
                    f"{self.indicators.sessions} daily bars, {len(self.indicators.indicators)} indicators",
                    dict(self.source_notes).get("bars", "—"),
                )
            )
        if self.fundamentals:
            rows.append(("yfinance fundamentals", "valuation, margins, growth, 4 quarters", "latest filing"))
        if self.positioning:
            rows.append(("yfinance positioning", "analyst targets, short interest, holders", "vendor snapshot"))
        rows.append(
            (
                "Finnhub company news",
                f"{len(self.news)} headline(s), published on or before the close",
                dict(self.source_notes).get("news", self.news_window_note or "—"),
            )
        )
        rows.append(("Screener (this run)", "momentum-burst metrics", self.run_date.isoformat()))
        shadowed = self.queued.signal_adjustment == 0.0
        for source in sorted(self.queued.signal_readings):
            rows.append(
                (
                    f"Signal: {source}" + (" (SHADOW)" if shadowed else ""),
                    f"direction {self.queued.signal_readings[source]:+d} at decision time"
                    + (
                        " — ungraded source, contributed 0 ranking points"
                        if shadowed
                        else ""
                    ),
                    self.run_date.isoformat(),
                )
            )
        rows.append(
            ("Social / retail sentiment", "not collected — no source wired up", "—")
        )
        return rows


class EvidenceBuilder:
    """Assembles everything the deep stage needs for a set of tickers.

    Bars come from the run's :class:`~tradingagent.snapshot.ResearchSnapshot`,
    not from a fresh download. That is the M6 fix: this class used to construct
    its own :class:`MarketData` and re-fetch the queue minutes after discovery
    had screened it, which is how one run reported V at 365.45 in the brief and
    364.15 in the deep report. A symbol the snapshot does not carry is still
    fetched — a queue entry from ``--tickers`` was never in it — but it is
    named in the report as off-snapshot rather than blended in silently.
    """

    def __init__(
        self,
        context: DeepContext,
        finnhub: FinnhubFree,
        degraded: DegradedTracker,
        market: MarketData | None = None,
        fundamentals: FundamentalsClient | None = None,
        snapshot: ResearchSnapshot | None = None,
    ):
        self.context = context
        self.finnhub = finnhub
        self.degraded = degraded
        self.market = market or MarketData(degraded=degraded, period=DEEP_HISTORY)
        self.fundamentals = fundamentals or FundamentalsClient(degraded=degraded)
        self.snapshot = snapshot
        self._bars: dict[str, pd.DataFrame] = {}
        #: Symbols whose bars the snapshot could not supply.
        self._refetched: set[str] = set()

    def prefetch(self, symbols: list[str]) -> None:
        """Take the queue's bars from the snapshot; download only what it lacks."""
        if not symbols:
            return
        wanted = [s.upper() for s in symbols]
        if self.snapshot is not None:
            self._bars = {
                s: frame
                for s in wanted
                if (frame := self.snapshot.frame(s)) is not None and len(frame.index) >= MIN_BARS
            }
            log.info(
                "Deep stage: %d/%d queued tickers served from snapshot %s",
                len(self._bars),
                len(wanted),
                self.snapshot.snapshot_id,
            )
        gaps = [s for s in wanted if s not in self._bars]
        if not gaps:
            return
        if self.snapshot is not None:
            log.warning("Deep stage: not in the snapshot, downloading: %s", ", ".join(gaps))
        fetched = self.market.load_many(gaps, min_rows=MIN_BARS, period=DEEP_HISTORY)
        self._refetched.update(fetched)
        self._bars.update(fetched)
        log.info("Deep stage: usable price history for %d/%d queued tickers", len(self._bars), len(wanted))

    def build(self, queued: QueuedTicker) -> Evidence:
        symbol = queued.symbol
        evidence = Evidence(
            queued=queued,
            run_date=self.context.date,
            market_context=self.context.market_context,
            macro_note=self.context.macro_note,
            snapshot_id=self.snapshot.snapshot_id if self.snapshot else self.context.snapshot_id,
            market_as_of=self.snapshot.market_as_of if self.snapshot else None,
        )

        frame = self._bars.get(symbol)
        if frame is None:
            frame = self.snapshot.frame(symbol) if self.snapshot else None
        if frame is None:
            single = self.market.load_many([symbol], min_rows=MIN_BARS, period=DEEP_HISTORY)
            frame = single.get(symbol)
            if frame is not None:
                self._refetched.add(symbol)
        if frame is None:
            evidence.missing.append("price history")
            self.degraded.add(f"Deep {symbol}", "no usable OHLCV history; ticker cannot be analysed")
            return evidence

        evidence.indicators = compute_indicators(symbol, frame)
        bar_date = pd.Timestamp(frame.index[-1]).date()
        evidence.source_notes.append(("bars", f"{bar_date.isoformat()} close"))

        if self.snapshot is not None:
            # A bar dated past the snapshot's market date is a look-ahead
            # wherever it came from: from a fresh download it is tomorrow's
            # data in today's report, and from the snapshot file it means
            # something merged newer bars into a frozen picture.
            self.snapshot.check(f"{symbol} bars", bar_date)
            if symbol in self._refetched:
                evidence.off_snapshot.append(f"{symbol} daily bars (not in the snapshot)")
            else:
                evidence.price_observation = self.snapshot.price(symbol)

        evidence.fundamentals = self.fundamentals.fundamentals(symbol)
        if evidence.fundamentals.missing:
            evidence.missing.append(f"fundamentals fields ({len(evidence.fundamentals.missing)})")
        evidence.positioning = self.fundamentals.positioning(symbol)

        evidence.news = self._news(symbol, evidence)
        if not evidence.news:
            evidence.missing.append("company news")
        evidence.missing.append("social/retail sentiment (not collected in this milestone)")
        return evidence

    def _news(self, symbol: str, evidence: Evidence) -> list:
        """Discovery's frozen headlines, or a window-bounded fetch if it has none.

        The old call was ``company_news(symbol, days=7)``, which meant seven
        days back from wall-clock now: a ``--date`` re-run read the right
        prices against this week's headlines, and even a same-day run gave the
        deep stage stories the shortlist had never seen. The window is the
        snapshot's; anything filed after its close is dropped by the client.
        """
        frozen = self.snapshot.headlines(symbol) if self.snapshot else None
        if frozen is not None:
            window = self.snapshot.news_window
            note = (
                f"{window[0].isoformat()}..{window[1].isoformat()}, frozen at discovery"
                if window
                else "frozen at discovery"
            )
            evidence.news_window_note = note
            evidence.source_notes.append(("news", note))
            return list(frozen)

        as_of = (
            self.snapshot.market_as_of
            if self.snapshot
            else (evidence.market_as_of or self.context.date)
        )
        start, end = news_window(as_of)
        items = self.finnhub.company_news(symbol, start, end, limit=NEWS_LIMIT)
        evidence.news_window_note = f"{start.isoformat()}..{end.isoformat()}"
        evidence.source_notes.append(("news", f"{start.isoformat()}..{end.isoformat()}"))
        if self.snapshot is not None:
            # Not in discovery's freeze — a --tickers run, or a name added
            # after the shortlist. Fetched to the same window, but say so.
            evidence.off_snapshot.append(f"{symbol} company news (fetched by this stage)")
        return items


def _lvl(value: float | None) -> str:
    return "n/a" if value is None else f"{value:,.2f}"


def _indent(text: str) -> str:
    return "\n".join(f"  {line}" for line in str(text).splitlines()) or "  none"


def _stamp(epoch: int) -> str:
    if not epoch:
        return "undated"
    try:
        return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d")
    except (OverflowError, OSError, ValueError):
        return "undated"
