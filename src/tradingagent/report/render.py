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
from ..signals.accuracy import GRADUATION, MIN_OBSERVATIONS
from ..signals.bundle import MAX_SCORE_ADJUSTMENT, ShadowRanking
from ..snapshot import ResearchSnapshot, utcnow

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
    #: The shortlist the shadowed signal layer would have picked (M6 item 1).
    signal_shadow: ShadowRanking | None = None
    #: The run's research snapshot (M6 item 2). Named in the footer so every
    #: price in this brief, and in the deep reports it links to, can be traced
    #: to one moment rather than to whenever each stage happened to fetch.
    snapshot: ResearchSnapshot | None = None
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
        f"**VIX** {_num(ctx.vix)} · **Breadth composite** {b.composite}/100",
        "",
        f"**{b.posture_reading().describe()}** — an inherited heuristic, never validated "
        "here, and not a position size. Sizes are computed per trade in section 4.",
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
    if not cal.has_verified_dates:
        lines.append(
            "> No authoritative release schedule was reachable this run, so every macro date "
            "below is an **indicative approximation** from a weekday-of-month rule (see Degraded "
            "Sources). None of them may be waited for; confirm dates and times before acting."
        )
        lines.append("")

    todays = [e for e in cal.macro if e.date == ctx.run_date]
    lines.append(
        "**Today:** "
        + (
            ", ".join(f"{e.name} ({e.impact}, {e.confidence})" for e in todays)
            if todays
            else "no scheduled US macro release."
        )
    )
    lines.append("")
    upcoming = [e for e in cal.macro if e.date is None or e.date > ctx.run_date][:8]
    if upcoming:
        lines += [
            "| Date | Event | Impact | Confidence | Source | May gate an entry? |",
            "|---|---|---|---|---|---|",
        ]
        lines += [
            f"| {e.date.isoformat() if e.date else '—'} | {e.name} | {e.impact} | "
            f"**{e.confidence}** | {e.source} | {'yes' if e.may_gate_entries else 'no'} |"
            for e in upcoming
        ]
        lines += [
            "",
            "_Only a **VERIFIED** date — one published by the issuing agency — may gate an entry "
            "or an options event-risk decision. INDICATIVE dates come from a weekday-of-month "
            "rule and are context only; STALE means we know when it last printed, not when it "
            "next will; MISSING means no source answered._",
            "",
        ]

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
        f"**{s.rotation_reading().describe()}** — a resemblance between today's sector "
        "leaders and a fixed table, not a reading of the economy",
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


def _shadow_block(ctx: ReportContext) -> list[str]:
    """What the signal layer did, and — while it is shadowed — what it wanted to do.

    Every source is currently worth zero ranking points, so the honest header
    is not "what the signals moved" but "what they would have moved". The two
    columns are kept side by side deliberately: the gap between them is the
    only evidence that will justify graduating a source later, and hiding it
    would leave the decision to intuition again.
    """
    size = len(ctx.shortlist)
    shadowed = [e for e in ctx.shortlist if e.is_shadow]
    applied = [e for e in ctx.shortlist if abs(e.score_adjustment) >= 0.05]
    ladder = ", ".join(f"{n}→±{p:.0f}" for n, p in GRADUATION)

    lines = [
        "**SHADOW — the signal layer does not pick names.** The screener ranks the whole pool "
        f"on price action alone and the shortlist is its top {size}. Signals are still "
        f"collected over the top {size * SIGNAL_POOL_MULTIPLE}, scored and journaled, but a "
        f"source is worth 0 ranking points until it has {MIN_OBSERVATIONS} resolved directional "
        f"calls in the journal, and then only {ladder} points (±{MAX_SCORE_ADJUSTMENT:.0f} once "
        "proven). No source has graduated yet."
        if shadowed or not applied
        else "**What the signals moved.** Sources that have earned ranking influence "
        "contributed the points below; the rest are shadowed at 0.",
        "",
    ]

    if ctx.signal_shadow is not None:
        lines += [f"_{ctx.signal_shadow.note()}_", ""]

    rows = [e for e in ctx.shortlist if abs(e.shadow_adjustment) >= 0.05 or abs(e.score_adjustment) >= 0.05]
    if not rows:
        lines += ["No ticker-level signal fired strongly enough to have moved a score, "
                  "shadowed or not.", ""]
        return lines

    lines += [
        "| Ticker | Screener score | Screener rank | Applied adj. | Shadow adj. | Would the shadow have changed this name's place? |",
        "|---|---:|---:|---:|---:|---|",
    ]
    would = set(ctx.signal_shadow.would_promote) if ctx.signal_shadow else set()
    for e in rows:
        if e.symbol in would:
            note = "**yes** — the shadow ranking promotes it"
        elif ctx.signal_shadow and e.symbol in ctx.signal_shadow.would_drop:
            note = "**yes** — the shadow ranking drops it"
        else:
            note = "no"
        lines.append(
            f"| **{e.symbol}** | {e.candidate.score} | {e.screen_rank or '—'} | "
            f"{e.score_adjustment:+.1f} | {e.shadow_adjustment:+.1f} | {note} |"
        )
    lines += [
        "",
        "_Ordering within the shortlist below is set first by the quick take's priority and "
        "rating and then interleaved by sector momentum, so a name's position there is not a "
        "signal-layer effect. Neither, while the layer is shadowed, is its membership._",
        "",
    ]
    return lines


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

    lines += _shadow_block(ctx)

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
        f"**Generated:** {utcnow().strftime('%Y-%m-%d %H:%M UTC')}",
        f"- **Market data as of:** {ctx.data_as_of} · **Session:** {ctx.session_note}",
    ]
    if ctx.snapshot is not None:
        snap = ctx.snapshot
        lines.append(
            f"- **Research snapshot:** {snap.label} · universe `{snap.universe_version}` · "
            f"{snap.data_quality.line()}"
        )
        lines.append(
            "  Every price in this brief and in the deep reports it links to comes from this "
            "one snapshot. The options overlay prices premiums against a second, named "
            "snapshot — section 6 says which."
        )
        if snap.violations:
            lines.append(
                f"- **Snapshot violations:** {len(snap.violations)} — "
                + "; ".join(snap.violations[:3])
            )
    lines += [
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
