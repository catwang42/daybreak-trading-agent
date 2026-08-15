"""Render ``reports/<date>/daily-brief.md`` in the exact order of
``config/report-schema.md``.

Section order is fixed and load-bearing: 1 Market Overview, 2 Macro & Events,
3 Sector Opportunity Map, 4 Shortlist, 5 Deep Analysis (M2+), 6 Options (M4+),
7 Degraded Sources, 8 Disclaimer footer (verbatim).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from ..data.market import Quote
from ..data.validate import DegradedTracker
from ..discovery.breadth import BreadthResult
from ..discovery.calendar import CalendarView
from ..discovery.screener import Candidate
from ..discovery.sectors import SectorMap
from ..discovery.shortlist import SIGNAL_POOL_MULTIPLE, ShortlistEntry
from ..llm import TokenLedger
from ..signals.bundle import MAX_SCORE_ADJUSTMENT

DISCLAIMER = (
    "Automated research output for personal study. Not financial advice. "
    "Paper trading only. Verify all data before acting."
)


@dataclass
class ReportContext:
    run_date: date
    commentary: str
    indices: list[Quote]
    vix: float | None
    breadth: BreadthResult
    sector_map: SectorMap
    calendar: CalendarView
    shortlist: list[ShortlistEntry]
    degraded: DegradedTracker
    ledger: TokenLedger
    universe_size: int
    screened: int
    candidates: list[Candidate]
    session_note: str
    data_as_of: str
    paid_gaps: list[str] = field(default_factory=list)
    runtime_seconds: float = 0.0
    stage: str = "discovery"
    max_per_sector: int = 3
    deep_cap: int = 3
    # M3 signal layer: (source, coverage, status) per source, the shared
    # market-wide backdrop, and the rolling accuracy table.
    signal_rows: list[tuple[str, str, str]] = field(default_factory=list)
    signal_backdrop: str = ""
    signal_accuracy: str = ""
    # Set once the deep stage has run in the same process (``--stage all``);
    # a standalone ``--stage deep`` patches this section on disk instead.
    deep_index: str | None = None
    #: Same arrangement for section 6, set by the options stage.
    options_index: str | None = None


def _pct(value: float | None, digits: int = 2) -> str:
    return f"{value:+.{digits}f}%" if value is not None else "n/a"


def _num(value: float | None, digits: int = 2) -> str:
    return f"{value:,.{digits}f}" if value is not None else "n/a"


def _section_market_overview(ctx: ReportContext) -> str:
    lines = ["## 1. Market Overview", "", ctx.commentary, ""]
    lines += [
        "| Index (ETF proxy) | Last | 1d | 5d | 1mo | 3mo |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for q in ctx.indices:
        lines.append(
            f"| {q.label} ({q.symbol}) | {_num(q.price)} | {_pct(q.ret('1d'))} | "
            f"{_pct(q.ret('5d'))} | {_pct(q.ret('1mo'))} | {_pct(q.ret('3mo'))} |"
        )
    if not ctx.indices:
        lines.append("| _no index data_ | | | | | |")

    b = ctx.breadth
    lines += [
        "",
        f"**VIX** {_num(ctx.vix)} · **Breadth composite** {b.composite}/100 "
        f"({b.zone}) · **Suggested equity exposure** {b.exposure}",
        "",
        f"{_num(b.breadth_pct_above_50dma, 1)}% of the {b.universe_size}-name universe is above its "
        f"50-day MA; {_num(b.breadth_pct_above_200dma, 1)}% above its 200-day MA. "
        f"Data quality: {b.data_quality}.",
        "",
        "| Breadth component | Score | Signal |",
        "|---|---:|---|",
    ]
    for c in b.components:
        score = f"{c.score:.0f}" if c.available else "—"
        lines.append(f"| {_component_label(c.key)} | {score} | {c.signal} |")
    lines += ["", f"> {b.history_note}", ""]
    return "\n".join(lines)


def _component_label(key: str) -> str:
    from ..discovery.breadth import COMPONENT_LABELS

    return COMPONENT_LABELS.get(key, key)


def _section_macro(ctx: ReportContext) -> str:
    lines = ["## 2. Macro & Events Today", ""]
    cal = ctx.calendar
    if not cal.macro_is_live:
        lines.append(
            "> Economic calendar source is an **indicative static release schedule**, not a live "
            "feed (no free live source available — see Degraded Sources). Confirm exact dates and "
            "times before acting."
        )
        lines.append("")

    todays = [e for e in cal.macro if e.date == ctx.run_date]
    lines.append(
        "**Today:** "
        + (", ".join(f"{e.name} ({e.impact})" for e in todays) if todays else "no scheduled US macro release.")
    )
    lines.append("")
    upcoming = [e for e in cal.macro if e.date > ctx.run_date][:8]
    if upcoming:
        lines += ["| Date | Event | Impact | Source |", "|---|---|---|---|"]
        lines += [f"| {e.date} | {e.name} | {e.impact} | {e.source} |" for e in upcoming]
        lines.append("")

    if cal.earnings_today:
        names = ", ".join(f"{e.symbol} ({e.timing})" for e in cal.earnings_today[:20])
        lines.append(f"**Earnings today (universe):** {names}")
    else:
        lines.append("**Earnings today (universe):** none.")
    lines.append("")

    week = [e for e in cal.earnings_week if e.date > ctx.run_date][:15]
    if week:
        lines += ["| Date | Ticker | Timing | EPS est. |", "|---|---|---|---:|"]
        lines += [
            f"| {e.date} | {e.symbol} | {e.timing} | {_num(e.eps_estimate)} |" for e in week
        ]
        lines.append("")
    return "\n".join(lines)


def _section_sectors(ctx: ReportContext) -> str:
    s = ctx.sector_map
    lines = [
        "## 3. Sector Opportunity Map",
        "",
        f"**Risk regime:** {s.risk_regime} (cyclical−defensive momentum spread {s.risk_score:+.2f}) · "
        f"**Estimated cycle phase:** {s.cycle_phase} (confidence {s.cycle_confidence})",
        "",
        "| Sector | ETF | Bucket | % above 50DMA | 1d | 5d | 1mo | 3mo | Status |",
        "|---|---|---|---:|---:|---:|---:|---:|---|",
    ]
    for row in s.rows:
        star = " ⭑" if row.preferred else ""
        lines.append(
            f"| {row.sector}{star} | {row.etf or '—'} | {row.bucket} | {row.uptrend_ratio:.0%} | "
            f"{_pct(row.ret_1d)} | {_pct(row.ret_5d)} | {_pct(row.ret_1mo)} | {_pct(row.ret_3mo)} | "
            f"{row.status} |"
        )
    if not s.rows:
        lines.append("| _no sector data_ | | | | | | | | |")

    lines += [
        "",
        "⭑ = a target sector from `config/preferences.md`.",
        "",
        f"**Leading:** {', '.join(r.sector for r in s.leaders()) or 'n/a'} · "
        f"**Lagging:** {', '.join(r.sector for r in s.laggards()) or 'n/a'}",
        "",
        f"**Overbought:** {', '.join(s.overbought) or 'none'} · "
        f"**Oversold:** {', '.join(s.oversold) or 'none'}",
        "",
    ]
    return "\n".join(lines)


def _signal_layer(ctx: ReportContext) -> str:
    """The 4.x subsections: what each source said, what it moved, what it has earned.

    Kept inside section 4 rather than given a number of its own because
    ``config/report-schema.md`` fixes the eight top-level sections, and the
    signal layer's whole job is to inform the shortlist above it.
    """
    if not ctx.signal_rows:
        return "### Signal layer\n\n_No signal source ran this run._\n"

    lines = ["### Signal layer", "", "| Source | Coverage | This run |", "|---|---|---|"]
    lines += [f"| {name} | {coverage} | {status} |" for name, coverage, status in ctx.signal_rows]
    lines.append("")

    moved = [e for e in ctx.shortlist if abs(e.score_adjustment) >= 0.05]
    if moved:
        size = len(ctx.shortlist)
        promoted = [e for e in moved if e.screen_rank > size]
        lines += [
            "**What the signals moved.** The screener ranks the whole pool on price action "
            f"alone; the shortlist is then picked from its top {size * SIGNAL_POOL_MULTIPLE} by "
            f"screener score plus the signal adjustment, which is capped at "
            f"±{MAX_SCORE_ADJUSTMENT:.0f} points. So the layer decides *who makes this list*, "
            "and never overrides the price screen.",
            "",
            "| Ticker | Screener score | Screener rank | Signal adj. | Adjusted | Made the list because of signals |",
            "|---|---:|---:|---:|---:|---|",
        ]
        for e in moved:
            gained = e.screen_rank > size
            note = f"**yes** — screener had it {e.screen_rank}th, outside the top {size}" if gained else "no"
            lines.append(
                f"| **{e.symbol}** | {e.candidate.score} | {e.screen_rank or '—'} | "
                f"{e.score_adjustment:+.1f} | {e.adjusted_score:.1f} | {note} |"
            )
        lines += [
            "",
            f"{len(promoted)} of {size} shortlisted names were promoted past the screener's own "
            "cut by the signal layer."
            if promoted
            else "No name was promoted past the screener's own cut this run; the adjustments "
            "only reordered names the screener had already selected.",
            "",
            "_Ordering within the shortlist below is set first by the quick take's priority and "
            "rating and then interleaved by sector momentum, so a name's position there is not "
            "a signal-layer effect. Membership and tie-breaking are._",
            "",
        ]
    else:
        lines += ["No ticker-level signal was strong enough to move a score this run.", ""]

    if ctx.signal_backdrop:
        lines += [ctx.signal_backdrop, ""]
    if ctx.signal_accuracy:
        lines += ["#### Source accuracy (rolling, scored against the journal)", "",
                  ctx.signal_accuracy, ""]
    return "\n".join(lines)


def _section_shortlist(ctx: ReportContext) -> str:
    lines = [
        "## 4. Shortlist",
        "",
        f"Screened {ctx.screened} of {ctx.universe_size} universe names; "
        f"{len(ctx.candidates)} passed the momentum-burst filter; top {len(ctx.shortlist)} shown "
        f"(at most {ctx.max_per_sector} per sector).",
        "",
        "| Ticker | Sector | Why it surfaced | Signal bundle | Quick rating | Priority | Earnings |",
        "|---|---|---|---|---|---:|---|",
    ]
    for e in ctx.shortlist:
        c = e.candidate
        lines.append(
            f"| **{c.symbol}** | {c.sector} | {c.why} | {e.signal_note()} | {e.rating_label} | "
            f"{e.priority or '—'} | {e.earnings_flag} |"
        )
    if not ctx.shortlist:
        lines.append("| _no candidates passed today's filters_ | | | | | | |")
    lines.append("")
    lines.append(_signal_layer(ctx))

    for e in ctx.shortlist:
        c = e.candidate
        lines.append(f"### {c.symbol} — {c.name}")
        lines.append(
            f"Screener {c.score}/100 ({c.rating}, {c.state}) · last ${c.price:,.2f} "
            f"({c.day_gain_pct:+.2f}%) · volume {c.volume_ratio_20d:.2f}× 20d avg · "
            f"entry ref ${c.entry_ref:,.2f} / stop ref ${c.stop_ref:,.2f} (risk {c.risk_pct:.1f}%)"
        )
        lines.append("")
        if e.take is None:
            lines.append(
                f"> **DEGRADED** — quick take unavailable: {e.degraded_reason or 'LLM call failed'}"
            )
        else:
            lines.append(f"- **Rating:** {e.take.rating} (confidence {e.take.confidence})")
            lines.append(f"- **Thesis:** {e.take.thesis}")
            lines.append(f"- **Key risk:** {e.take.key_risk}")
            lines.append(f"- **Deep-dive priority:** {e.take.deep_dive_priority}/10")
        if e.news_headline:
            lines.append(f"- **Latest headline:** {e.news_headline}")
        lines.append("")
    return "\n".join(lines)


DEEP_HEADING = "## 5. Deep Analysis"


def _section_deep(ctx: ReportContext) -> str:
    from ..discovery.shortlist import deep_dive_queue

    if ctx.deep_index is not None:
        return f"{DEEP_HEADING}\n\n{ctx.deep_index}\n"

    queued = deep_dive_queue(ctx.shortlist, ctx.sector_map, cap=ctx.deep_cap)
    queue = (
        ", ".join(f"{e.symbol} ({e.candidate.sector}, priority {e.priority}/10)" for e in queued)
        or "none"
    )
    return (
        f"{DEEP_HEADING}\n\n"
        "_Not run in this stage — `--stage deep` executes the ported TradingAgents pipeline and "
        "writes `deep/<ticker>.md` per ticker._\n\n"
        f"Deep-analysis queue (round-robin across leading sectors): {queue}\n"
    )


OPTIONS_HEADING = "## 6. Options Candidates"


def _section_options(ctx: ReportContext) -> str:
    if ctx.options_index is not None:
        return f"{OPTIONS_HEADING}\n\n{ctx.options_index}\n"
    return (
        f"{OPTIONS_HEADING}\n\n"
        "_Not run in this stage — `--stage options` screens the Alpaca paper option "
        "chain against each deep verdict and patches this section in place._\n"
    )


def _section_degraded(ctx: ReportContext) -> str:
    lines = ["## 7. Degraded Sources", ""]
    if ctx.degraded.entries:
        lines.append(f"**DEGRADED — missing: {', '.join(ctx.degraded.sources)}**")
        lines.append("")
        lines += ["| Source | Detail |", "|---|---|"]
        lines += [f"| {source} | {reason} |" for source, reason in ctx.degraded.entries]
    else:
        lines.append("none")
    lines.append("")

    if ctx.paid_gaps:
        lines += [
            "### Paid-data bottlenecks (not purchased — your call)",
            "",
        ]
        lines += [f"- {gap}" for gap in ctx.paid_gaps]
        lines.append("")
    return "\n".join(lines)


def _footer(ctx: ReportContext) -> str:
    ledger = ctx.ledger
    lines = [
        "---",
        "",
        "### Run footer",
        "",
        f"- **Stage:** {ctx.stage} · **Runtime:** {ctx.runtime_seconds:.1f}s · "
        f"**Generated:** {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        f"- **Market data as of:** {ctx.data_as_of} · **Session:** {ctx.session_note}",
        "",
        "| LLM tier | Model | Calls | Prompt tok | Completion tok | Total tok | Est. cost |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    for tier in ("fast", "smart", "deep"):
        usage = ledger.by_tier.get(tier)
        if not usage:
            continue
        lines.append(
            f"| {tier} | `{ledger.by_model.get(tier, '—')}` | {usage.calls} | "
            f"{usage.prompt_tokens:,} | {usage.completion_tokens:,} | {usage.total_tokens:,} | "
            f"${usage.cost_usd:.4f} |"
        )
    if not ledger.by_tier:
        lines.append("| _no LLM calls this run_ | | | | | | |")
    lines += [
        f"| **total** | | **{ledger.total_calls}** | | | **{ledger.total_tokens:,}** | "
        f"**${ledger.total_cost_usd:.4f}** |",
        "",
        f"_{DISCLAIMER}_",
        "",
    ]
    return "\n".join(lines)


def render_daily_brief(ctx: ReportContext) -> str:
    header = (
        f"# Daily Trading Research Brief — {ctx.run_date.isoformat()}\n\n"
        f"> Research only. Paper trading. The human makes every decision.\n"
    )
    return "\n".join(
        [
            header,
            _section_market_overview(ctx),
            _section_macro(ctx),
            _section_sectors(ctx),
            _section_shortlist(ctx),
            _section_deep(ctx),
            _section_options(ctx),
            _section_degraded(ctx),
            _footer(ctx),
        ]
    )
