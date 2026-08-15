"""Stage orchestration — the daily scan, wired end to end.

``discovery`` screens the universe and publishes the queue; ``deep`` runs the
ported multi-agent pipeline over that queue; ``options`` screens the Alpaca
paper option chain against each deep verdict; ``all`` runs the three in one
process.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

import pandas as pd

from .config import Settings
from .data.alpaca_client import AlpacaPaper
from .data.finnhub_client import FinnhubFree
from .data.market import MarketData, Quote
from .data.universe import Constituent, load_universe, normalize_sector
from .data.validate import DegradedTracker
from .discovery.breadth import BreadthResult, analyze_breadth
from .discovery.calendar import CalendarView, build_calendar
from .discovery.screener import (
    MAX_PER_SECTOR,
    Candidate,
    cap_per_sector,
    market_gate_from_breadth,
    paid_gaps,
    screen_universe,
)
from .discovery.sectors import SectorMap, build_sector_map
from .discovery.shortlist import (
    ShortlistEntry,
    _earnings_note,
    _sector_note,
    build_shortlist,
    deep_dive_queue,
    market_commentary,
    select_with_signals,
)
from .journal import (
    append_entries,
    entries_from_deep,
    entries_from_options,
    entries_from_shortlist,
)
from .llm import LLMGateway, TokenLedger, ledger as current_ledger, reset_ledger
from .options.context import OptionsContext, build_options_context
from .options.stage import run_plans
from .options.strategist import OptionsPlan
from .pipeline.context import DeepContext, QueuedTicker
from .pipeline.deep import DeepResult, TierCost, run_queue
from .report.deep import OPTIONS_HEADING as DEEP_OPTIONS_HEADING, render_deep_index, render_deep_report
from .report.options import render_options_index, render_options_section
from .report.render import (
    DEEP_HEADING,
    OPTIONS_HEADING as BRIEF_OPTIONS_HEADING,
    ReportContext,
    render_daily_brief,
)
from .report.writer import replace_section, write_report
from .signals import SignalHub, build_default_hub
from .signals.accuracy import AccuracyReport, AccuracyTracker, realised_return

log = logging.getLogger(__name__)

# How many names the queue persists. The deep stage still honours
# DEEP_TICKER_CAP; the extra rows are metadata for a `--tickers` override.
QUEUE_DEPTH = 10


@dataclass
class DiscoveryResult:
    context: ReportContext
    report_path: str
    journal_written: int
    deep_context: DeepContext | None = None


def build_signal_hub(
    settings: Settings,
    finnhub: FinnhubFree,
    degraded: DegradedTracker,
    market: MarketData,
) -> tuple[SignalHub, AccuracyReport]:
    """The four M3 sources, weighted by their record in the journal.

    Rescoring is weekly (see :mod:`tradingagent.signals.accuracy`), so most runs
    load the cached weights and download nothing. A run that cannot score — no
    journal history yet — leaves every source at weight 1.0 rather than
    guessing.
    """
    tracker = AccuracyTracker(settings.journal_path)
    report = tracker.current(settings.run_date, realised=realised_return(market))
    hub = build_default_hub(finnhub, degraded=degraded, weights=report.weights())
    return hub, report


@dataclass
class DeepStageResult:
    results: list[DeepResult] = field(default_factory=list)
    report_paths: list[str] = field(default_factory=list)
    brief_path: str = ""
    journal_written: int = 0
    seconds: float = 0.0
    degraded: DegradedTracker = field(default_factory=DegradedTracker)
    ledger: TokenLedger | None = None
    options_context: OptionsContext | None = None


@dataclass
class OptionsStageResult:
    plans: list[OptionsPlan] = field(default_factory=list)
    report_paths: list[str] = field(default_factory=list)
    brief_path: str = ""
    journal_written: int = 0
    seconds: float = 0.0
    degraded: DegradedTracker = field(default_factory=DegradedTracker)
    ledger: TokenLedger | None = None
    #: One line on which feed answered — printed under the brief's table.
    feed_note: str = ""
    #: This stage's own spend, isolated from whatever the ledger already held —
    #: the milestone question is "what does the overlay add", not "what did the
    #: whole run cost".
    cost_by_tier: dict[str, TierCost] = field(default_factory=dict)

    @property
    def cost_usd(self) -> float:
        return sum(t.cost_usd for t in self.cost_by_tier.values())

    @property
    def calls(self) -> int:
        return sum(t.calls for t in self.cost_by_tier.values())

    @property
    def tokens(self) -> int:
        return sum(t.total_tokens for t in self.cost_by_tier.values())

    @property
    def proposed(self) -> int:
        return sum(1 for p in self.plans if p.chosen is not None)


def _session_note(alpaca: AlpacaPaper, run_date: date) -> str:
    session = alpaca.market_session()
    if session is None:
        return "session state unavailable"
    if session.is_open:
        return f"market OPEN, closes {session.next_close:%H:%M %Z}" if session.next_close else "market OPEN"
    prev = session.previous_close_date
    opens = f", next open {session.next_open:%Y-%m-%d %H:%M %Z}" if session.next_open else ""
    return f"market CLOSED (last completed session {prev}){opens}"


def _market_context_block(
    run_date: date,
    session_note: str,
    indices: list[Quote],
    vix: float | None,
    breadth: BreadthResult,
    sector_map: SectorMap,
    macro_note: str,
    signal_backdrop: str = "",
) -> str:
    """The shared market picture every deep-pipeline role is shown.

    Rendered once here rather than in each prompt so all twelve roles argue
    from the same numbers — a disagreement between them is then a disagreement
    about the evidence, not about which snapshot they happened to see.

    ``signal_backdrop`` is the market-wide half of the M3 signal layer (FRED
    macro regime, Polymarket odds, market news tone). It belongs here rather
    than in each ticker's evidence because it is the same for every candidate.
    """
    index_lines = [
        f"  - {q.label} ({q.symbol}): 1d {q.ret('1d'):+.2f}%, 5d {q.ret('5d'):+.2f}%, "
        f"1mo {q.ret('1mo'):+.2f}%"
        for q in indices
        if q.ret("1d") is not None and q.ret("5d") is not None and q.ret("1mo") is not None
    ]
    pct50 = (
        f"{breadth.breadth_pct_above_50dma:.0f}%"
        if breadth.breadth_pct_above_50dma is not None
        else "an unknown share"
    )
    return "\n".join(
        [
            f"- Date: {run_date.isoformat()} ({session_note}).",
            f"- Index moves:\n" + ("\n".join(index_lines) or "  - unavailable"),
            f"- VIX: {vix:.2f}" if vix is not None else "- VIX: unavailable",
            f"- Breadth composite {breadth.composite}/100 ({breadth.zone}) — {breadth.guidance} "
            f"Suggested equity exposure {breadth.exposure}.",
            f"- {pct50} of the {breadth.universe_size}-name universe is above its 50-day MA.",
            f"- Risk regime: {sector_map.risk_regime} (cyclical−defensive momentum spread "
            f"{sector_map.risk_score:+.2f}); estimated cycle phase {sector_map.cycle_phase} "
            f"(confidence {sector_map.cycle_confidence}).",
            f"- Leading sectors: {', '.join(r.sector for r in sector_map.leaders()) or 'n/a'}. "
            f"Lagging: {', '.join(r.sector for r in sector_map.laggards()) or 'n/a'}.",
            f"- Overbought sectors: {', '.join(sector_map.overbought) or 'none'}. "
            f"Oversold: {', '.join(sector_map.oversold) or 'none'}.",
            "- Scheduled macro releases:\n"
            + "\n".join(f"  {line}" for line in macro_note.splitlines()),
        ]
        + (["", signal_backdrop] if signal_backdrop else [])
    )


def build_deep_context(
    run_date: date,
    shortlist: list[ShortlistEntry],
    sector_map: SectorMap,
    calendar_view: CalendarView,
    market_context: str,
    macro_note: str,
    data_as_of: str,
    degraded: DegradedTracker,
) -> DeepContext:
    """Freeze what the deep stage needs so it never re-derives a different picture."""
    queue: list[QueuedTicker] = []
    for entry in deep_dive_queue(shortlist, sector_map, cap=QUEUE_DEPTH):
        c = entry.candidate
        earnings_note, earnings_flag = _earnings_note(calendar_view, c.symbol, run_date)
        queue.append(
            QueuedTicker(
                symbol=c.symbol,
                name=c.name,
                sector=c.sector,
                industry=c.industry,
                priority=entry.priority,
                quick_rating=entry.rating_label,
                quick_thesis=entry.take.thesis if entry.take else "",
                quick_risk=entry.take.key_risk if entry.take else "",
                earnings_flag=earnings_flag,
                earnings_note=earnings_note,
                sector_note=_sector_note(sector_map, c.sector),
                news_headline=entry.news_headline,
                signal_block=entry.signals.ticker_block() if entry.signals else "",
                signal_readings=entry.signals.readings() if entry.signals else {},
                signal_adjustment=round(entry.score_adjustment, 2),
                screener={
                    "score": c.score,
                    "rating": c.rating,
                    "state": c.state,
                    "triggers": ", ".join(c.triggers),
                    "day_gain_pct": c.day_gain_pct,
                    "volume_ratio_20d": c.volume_ratio_20d,
                    "close_location_pct": c.close_location_pct,
                    "prior_base_days": c.prior_base_days,
                    "base_width_pct": c.base_width_pct,
                    "entry_ref": c.entry_ref,
                    "stop_ref": c.stop_ref,
                    "risk_pct": c.risk_pct,
                    "dist_52w_high_pct": c.dist_52w_high_pct,
                    "rs_vs_spy_3mo": c.rs_vs_spy_3mo,
                    "reject_reasons": ", ".join(c.reject_reasons),
                },
            )
        )
    return DeepContext(
        run_date=run_date.isoformat(),
        market_context=market_context,
        macro_note=macro_note,
        data_as_of=data_as_of,
        queue=queue,
        discovery_degraded=list(degraded.sources),
    )


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

    macro_note = (
        "\n".join(f"- {e.date} {e.name} ({e.impact})" for e in calendar_view.macro[:8])
        or "- none scheduled"
    )
    session_note = _session_note(alpaca, run_date)

    if indices and alpaca.enabled:
        alpaca.quote_crosscheck("SPY", next((q.price for q in indices if q.symbol == "SPY"), 0.0))

    # --- LLM (FAST tier only in M1) ---------------------------------------
    size = shortlist_size or prefs.shortlist_max
    # Cap sector concentration before spending tokens: a hot sector triggers most
    # of its members at once and would otherwise fill the whole shortlist.
    eligible = cap_per_sector(candidates, MAX_PER_SECTOR)
    log.info(
        "Shortlist pool after the %d-per-sector cap: %d of %d candidates",
        MAX_PER_SECTOR,
        len(eligible),
        len(candidates),
    )
    hub, accuracy = build_signal_hub(settings, finnhub, degraded, market)

    shortlist: list[ShortlistEntry] = []
    commentary = "_LLM disabled for this run (--skip-llm)._"
    if skip_llm:
        degraded.add("LLM", "--skip-llm was set: no quick takes or commentary this run")
        selected = select_with_signals(eligible, hub, run_date, size)
        shortlist = [
            ShortlistEntry(candidate=c, take=None, earnings_flag="—", news_headline=None,
                           degraded_reason="LLM disabled", signals=bundle, screen_rank=rank)
            for c, bundle, rank in selected
        ]
    else:
        gateway = LLMGateway(settings, ledger)
        shortlist = build_shortlist(
            eligible, breadth, sector_map, calendar_view, finnhub, gateway, run_date, degraded,
            size=size, hub=hub,
        )
        commentary = market_commentary(
            gateway,
            degraded,
            run_date=run_date.isoformat(),
            session_note=session_note,
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
            macro_note=macro_note,
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
        session_note=session_note,
        data_as_of=data_as_of,
        paid_gaps=paid_gaps(),
        runtime_seconds=time.monotonic() - started,
        stage="discovery",
        max_per_sector=MAX_PER_SECTOR,
        deep_cap=settings.deep_ticker_cap,
        signal_rows=hub.source_rows(),
        signal_backdrop=hub.market_block(),
        signal_accuracy=accuracy.markdown(),
    )
    markdown = render_daily_brief(context)
    path = write_report(settings.report_dir() / "daily-brief.md", markdown, settings.reports_bucket)

    deep_context = build_deep_context(
        run_date=run_date,
        shortlist=shortlist,
        sector_map=sector_map,
        calendar_view=calendar_view,
        market_context=_market_context_block(
            run_date, session_note, indices, vix, breadth, sector_map, macro_note,
            signal_backdrop=hub.market_block(),
        ),
        macro_note=macro_note,
        data_as_of=data_as_of,
        degraded=degraded,
    )
    deep_context.write(settings.report_dir())

    written = append_entries(
        settings.journal_path, entries_from_shortlist(shortlist, run_date, report_rel)
    )
    return DiscoveryResult(
        context=context,
        report_path=str(path),
        journal_written=written,
        deep_context=deep_context,
    )


def run_deep(
    settings: Settings,
    *,
    context: DeepContext | None = None,
    only: list[str] | None = None,
    degraded: DegradedTracker | None = None,
    ledger: TokenLedger | None = None,
    patch_brief: bool = True,
) -> DeepStageResult:
    """Run the multi-agent pipeline over the discovery queue.

    ``context`` is passed in memory by ``--stage all``; a standalone
    ``--stage deep`` reads the JSON discovery left behind, so the two produce
    the same analysis from the same market picture.
    """
    started = time.monotonic()
    degraded = degraded if degraded is not None else DegradedTracker()
    ledger = ledger or current_ledger()
    report_dir = settings.report_dir()
    context = context or DeepContext.read(report_dir)

    gateway = LLMGateway(settings, ledger)
    finnhub = FinnhubFree(settings, degraded=degraded)
    results = run_queue(settings, context, gateway, finnhub, degraded, only=only)

    paths: list[str] = []
    for result in results:
        markdown = render_deep_report(result)
        path = write_report(
            report_dir / "deep" / f"{result.symbol}.md", markdown, settings.reports_bucket
        )
        paths.append(str(path))

    brief_path = report_dir / "daily-brief.md"
    if patch_brief and brief_path.exists():
        patched = replace_section(
            brief_path.read_text(), DEEP_HEADING, render_deep_index(results)
        )
        write_report(brief_path, patched, settings.reports_bucket)
    elif patch_brief:
        degraded.add("Daily brief", f"no brief at {brief_path} to link the deep reports into")

    written = append_entries(
        settings.journal_path,
        entries_from_deep(results, context.date, f"reports/{context.run_date}/deep"),
    )

    # The options stage needs the verdicts and the levels, not the transcripts.
    # Written here so `--stage options` can run tomorrow, or on a Saturday,
    # against exactly the picture this run produced.
    options_context = build_options_context(results, context.date, context.data_as_of)
    options_context.write(report_dir)

    return DeepStageResult(
        results=results,
        report_paths=paths,
        brief_path=str(brief_path),
        journal_written=written,
        seconds=time.monotonic() - started,
        degraded=degraded,
        ledger=ledger,
        options_context=options_context,
    )


def run_options(
    settings: Settings,
    *,
    context: OptionsContext | None = None,
    only: list[str] | None = None,
    degraded: DegradedTracker | None = None,
    ledger: TokenLedger | None = None,
    patch_reports: bool = True,
    patch_brief: bool = True,
) -> OptionsStageResult:
    """Screen the paper option chain against each deep verdict.

    Section 6 is patched into each ``deep/<ticker>.md`` in place rather than
    re-rendered, because the overlay depends on a verdict those files already
    contain and re-rendering would mean re-running the deep stage. ``--stage
    all`` leaves ``patch_brief`` off and rebuilds the brief once at the end, so
    its footer counts this stage's tokens too.
    """
    started = time.monotonic()
    degraded = degraded if degraded is not None else DegradedTracker()
    ledger = ledger or current_ledger()
    report_dir = settings.report_dir()
    context = context or OptionsContext.read(report_dir)

    before = {tier: TierCost(u.calls, u.prompt_tokens, u.completion_tokens, u.cost_usd)
              for tier, u in ledger.by_tier.items()}
    gateway = LLMGateway(settings, ledger)
    plans, chains = run_plans(settings, context, gateway, degraded, only=only)
    cost = {}
    for tier, usage in ledger.by_tier.items():
        was = before.get(tier, TierCost())
        delta = TierCost(
            calls=usage.calls - was.calls,
            prompt_tokens=usage.prompt_tokens - was.prompt_tokens,
            completion_tokens=usage.completion_tokens - was.completion_tokens,
            cost_usd=usage.cost_usd - was.cost_usd,
        )
        if delta.calls:
            cost[tier] = delta

    paths: list[str] = []
    brief_path = report_dir / "daily-brief.md"
    if patch_reports:
        for plan in plans:
            deep_path = report_dir / "deep" / f"{plan.symbol}.md"
            if not deep_path.exists():
                degraded.add(
                    f"Options {plan.symbol}",
                    f"no deep report at {deep_path} to write the options view into",
                )
                continue
            patched = replace_section(
                deep_path.read_text(), DEEP_OPTIONS_HEADING, render_options_section(plan)
            )
            paths.append(str(write_report(deep_path, patched, settings.reports_bucket)))

    if patch_brief and brief_path.exists():
        # The footer belongs to the run that wrote the brief and cannot know
        # about this one, so the overlay states its own spend inline — the same
        # thing section 5 already does for the deep stage.
        calls = sum(c.calls for c in cost.values())
        tokens = sum(c.prompt_tokens + c.completion_tokens for c in cost.values())
        spend = sum(c.cost_usd for c in cost.values())
        note = (
            f"Overlay patched in by `--stage options` on "
            f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC} · {calls} "
            f"{', '.join(cost) or 'no'}-tier call(s) · {tokens:,} tok · est. ${spend:.4f}, "
            "over and above the footer total below."
        )
        patched = replace_section(
            brief_path.read_text(),
            BRIEF_OPTIONS_HEADING,
            render_options_index(plans, chains.feed_note, note),
        )
        write_report(brief_path, patched, settings.reports_bucket)
    elif patch_brief:
        degraded.add("Daily brief", f"no brief at {brief_path} to publish the overlay into")

    written = append_entries(
        settings.journal_path,
        entries_from_options(plans, context.date, f"reports/{context.run_date}/deep"),
    )
    return OptionsStageResult(
        plans=plans,
        report_paths=paths,
        brief_path=str(brief_path),
        journal_written=written,
        seconds=time.monotonic() - started,
        degraded=degraded,
        ledger=ledger,
        feed_note=chains.feed_note,
        cost_by_tier=cost,
    )


def run_all(
    settings: Settings,
    *,
    refresh_universe: bool = False,
    universe_limit: int | None = None,
    shortlist_size: int | None = None,
    only: list[str] | None = None,
) -> tuple[DiscoveryResult, DeepStageResult, OptionsStageResult]:
    """Discovery, deep, then options in one process, sharing the ledger and brief."""
    discovery = run_discovery(
        settings,
        refresh_universe=refresh_universe,
        universe_limit=universe_limit,
        shortlist_size=shortlist_size,
    )
    ctx = discovery.context
    deep = run_deep(
        settings,
        context=discovery.deep_context,
        only=only,
        degraded=ctx.degraded,
        ledger=ctx.ledger,
        patch_brief=False,  # re-rendered below so the footer counts the later spend too
    )
    options = run_options(
        settings,
        context=deep.options_context,
        only=only,
        degraded=ctx.degraded,
        ledger=ctx.ledger,
        patch_brief=False,
    )

    ctx.deep_index = render_deep_index(deep.results)
    ctx.options_index = render_options_index(options.plans, options.feed_note)
    ctx.stage = "all"
    ctx.runtime_seconds = ctx.runtime_seconds + deep.seconds + options.seconds
    write_report(
        settings.report_dir() / "daily-brief.md",
        render_daily_brief(ctx),
        settings.reports_bucket,
    )
    return discovery, deep, options
