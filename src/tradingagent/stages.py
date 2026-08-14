"""Stage orchestration — the daily scan, wired end to end.

Milestone 1 implements ``discovery`` (and ``report``, which renders whatever the
discovery stage produced). ``deep`` and ``options`` are added in M2/M4.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import date

import pandas as pd

from .config import Settings
from .data.alpaca_client import AlpacaPaper
from .data.finnhub_client import FinnhubFree
from .data.market import MarketData
from .data.universe import Constituent, load_universe, normalize_sector
from .data.validate import DegradedTracker
from .discovery.breadth import BreadthResult, analyze_breadth
from .discovery.calendar import CalendarView, build_calendar
from .discovery.screener import Candidate, market_gate_from_breadth, paid_gaps, screen_universe
from .discovery.sectors import SectorMap, build_sector_map
from .discovery.shortlist import ShortlistEntry, build_shortlist, market_commentary
from .journal import append_entries, entries_from_shortlist
from .llm import LLMGateway, TokenLedger, reset_ledger
from .report.render import ReportContext, render_daily_brief
from .report.writer import write_report

log = logging.getLogger(__name__)


@dataclass
class DiscoveryResult:
    context: ReportContext
    report_path: str
    journal_written: int


def _session_note(alpaca: AlpacaPaper, run_date: date) -> str:
    session = alpaca.market_session()
    if session is None:
        return "session state unavailable"
    if session.is_open:
        return f"market OPEN, closes {session.next_close:%H:%M %Z}" if session.next_close else "market OPEN"
    prev = session.previous_close_date
    opens = f", next open {session.next_open:%Y-%m-%d %H:%M %Z}" if session.next_open else ""
    return f"market CLOSED (last completed session {prev}){opens}"


def run_discovery(
    settings: Settings,
    *,
    refresh_universe: bool = False,
    universe_limit: int | None = None,
    shortlist_size: int | None = None,
    skip_llm: bool = False,
) -> DiscoveryResult:
    started = time.monotonic()
    ledger: TokenLedger = reset_ledger()
    degraded = DegradedTracker()
    prefs = settings.preferences
    run_date = settings.run_date

    # --- universe + prices ------------------------------------------------
    constituents: list[Constituent] = load_universe(refresh=refresh_universe)
    if universe_limit:
        constituents = constituents[:universe_limit]
    preferred = {normalize_sector(s) for s in prefs.target_sectors}

    market = MarketData(degraded=degraded, period="2y")
    log.info("Downloading OHLCV for %d symbols...", len(constituents))
    bars = market.load_many([c.symbol for c in constituents], min_rows=60)
    log.info("Usable price histories: %d", len(bars))

    indices, vix = market.index_snapshot()
    sector_quotes = market.sector_etf_quotes()

    spy_close: pd.Series | None = None
    spy_bars = market.load_many(["SPY"], min_rows=250)
    if "SPY" in spy_bars:
        spy_close = spy_bars["SPY"]["Close"]
    else:
        degraded.add("SPY benchmark", "relative-strength and divergence comparisons unavailable")

    # --- analysis ---------------------------------------------------------
    breadth: BreadthResult = analyze_breadth(bars, spx_close=spy_close)
    sector_map: SectorMap = build_sector_map(constituents, bars, sector_quotes, prefs.target_sectors)
    gate = market_gate_from_breadth(breadth.composite)
    log.info("Breadth %.1f (%s) -> market gate '%s'", breadth.composite, breadth.zone, gate)

    candidates: list[Candidate] = screen_universe(
        bars,
        constituents,
        gate=gate,
        spy_close=spy_close,
        preferred_sectors=preferred,
        min_avg_share_volume=prefs.min_avg_volume,
    )
    log.info("Screener produced %d candidates", len(candidates))
    if not candidates:
        degraded.add(
            "Screener",
            "no candidate passed the momentum-burst triggers today (a quiet tape, not a failure)",
        )

    # --- calendars + external context -------------------------------------
    alpaca = AlpacaPaper(settings, degraded=degraded)
    finnhub = FinnhubFree(settings, degraded=degraded)
    calendar_view: CalendarView = build_calendar(
        finnhub, run_date, {c.symbol for c in constituents}, degraded
    )

    if indices and alpaca.enabled:
        alpaca.quote_crosscheck("SPY", next((q.price for q in indices if q.symbol == "SPY"), 0.0))

    # --- LLM (FAST tier only in M1) ---------------------------------------
    size = shortlist_size or prefs.shortlist_max
    shortlist: list[ShortlistEntry] = []
    commentary = "_LLM disabled for this run (--skip-llm)._"
    if skip_llm:
        degraded.add("LLM", "--skip-llm was set: no quick takes or commentary this run")
        shortlist = [
            ShortlistEntry(candidate=c, take=None, earnings_flag="—", news_headline=None,
                           degraded_reason="LLM disabled")
            for c in candidates[:size]
        ]
    else:
        gateway = LLMGateway(settings, ledger)
        shortlist = build_shortlist(
            candidates, breadth, sector_map, calendar_view, finnhub, gateway, run_date, degraded, size=size
        )
        commentary = market_commentary(
            gateway,
            degraded,
            run_date=run_date.isoformat(),
            session_note=_session_note(alpaca, run_date),
            index_table="\n".join(
                f"- {q.label} ({q.symbol}): 1d {q.ret('1d'):+.2f}%, 5d {q.ret('5d'):+.2f}%, "
                f"1mo {q.ret('1mo'):+.2f}%"
                for q in indices
                if q.ret("1d") is not None and q.ret("5d") is not None and q.ret("1mo") is not None
            )
            or "unavailable",
            vix_note=f"{vix:.2f}" if vix is not None else "unavailable",
            breadth_composite=breadth.composite,
            breadth_zone=breadth.zone,
            breadth_pct_50dma=(
                f"{breadth.breadth_pct_above_50dma:.0f}%"
                if breadth.breadth_pct_above_50dma is not None
                else "an unknown share"
            ),
            breadth_exposure=breadth.exposure,
            breadth_strongest=(
                f"{breadth.strongest.signal}" if breadth.strongest else "unavailable"
            ),
            breadth_weakest=(f"{breadth.weakest.signal}" if breadth.weakest else "unavailable"),
            risk_regime=sector_map.risk_regime,
            risk_score=f"{sector_map.risk_score:+.2f}",
            cycle_phase=sector_map.cycle_phase,
            cycle_confidence=sector_map.cycle_confidence,
            sector_leaders=", ".join(
                f"{r.sector} ({r.momentum:+.1f})" for r in sector_map.leaders()
            )
            or "unavailable",
            sector_laggards=", ".join(
                f"{r.sector} ({r.momentum:+.1f})" for r in sector_map.laggards()
            )
            or "unavailable",
            overbought=", ".join(sector_map.overbought) or "none",
            oversold=", ".join(sector_map.oversold) or "none",
            macro_note="\n".join(f"- {e.date} {e.name} ({e.impact})" for e in calendar_view.macro[:8])
            or "- none scheduled",
        )

    # --- render + persist -------------------------------------------------
    data_as_of = "unknown"
    if bars:
        latest = max(frame.index[-1] for frame in bars.values())
        data_as_of = pd.Timestamp(latest).strftime("%Y-%m-%d close")

    report_rel = f"reports/{run_date.isoformat()}/daily-brief.md"
    context = ReportContext(
        run_date=run_date,
        commentary=commentary,
        indices=indices,
        vix=vix,
        breadth=breadth,
        sector_map=sector_map,
        calendar=calendar_view,
        shortlist=shortlist,
        degraded=degraded,
        ledger=ledger,
        universe_size=len(constituents),
        screened=len(bars),
        candidates=candidates,
        session_note=_session_note(alpaca, run_date),
        data_as_of=data_as_of,
        paid_gaps=paid_gaps(),
        runtime_seconds=time.monotonic() - started,
        stage="discovery",
    )
    markdown = render_daily_brief(context)
    path = write_report(settings.report_dir() / "daily-brief.md", markdown, settings.reports_bucket)

    written = append_entries(
        settings.journal_path, entries_from_shortlist(shortlist, run_date, report_rel)
    )
    return DiscoveryResult(context=context, report_path=str(path), journal_written=written)
