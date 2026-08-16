"""Freeze the live pipeline objects into a :class:`PresentationContext`.

Called once, at the end of ``--stage all``, while the breadth result, the trade
plans and the option plans are all still objects. Everything downstream — the
sheet, the charts, the HTML — reads the artefact this writes and never the
objects, so a re-delivery tomorrow renders exactly what was sent today.
"""

from __future__ import annotations

import logging
from datetime import date

import pandas as pd

from ..pipeline.macro_gate import may_gate
from ..pipeline.trade_plan import LONG_RATINGS, SHORT_RATINGS
from .context import (
    SPY_CHART_SESSIONS,
    TICKER_CHART_SESSIONS,
    Avoid,
    Consensus,
    Gate,
    Overlay,
    OverlaySkip,
    PresentationContext,
    ReadingRow,
    Regime,
    SectorBar,
    SeriesPoint,
    Setup,
)

log = logging.getLogger(__name__)

#: How close entry has to sit to the last close before "wait for it" is a lie.
#: Half a percent is inside a normal day's range on most S&P names.
AT_MARKET_BAND_PCT = 0.5

#: Ratings the sheet lists under "Avoids" rather than "Best setups". A Hold is
#: an avoid too: the pipeline spent four analysts and a debate on it and
#: concluded there is nothing to do, and that conclusion is worth one line.
AVOID_RATINGS = {"Hold", "Neutral", *SHORT_RATINGS}


def series_from(frame: pd.DataFrame | None, sessions: int, with_mas: bool = False) -> list[SeriesPoint]:
    """Tail of a bar frame as JSON-able points, with the moving averages the
    chart draws computed over the *full* history before the tail is taken.

    Computing them after slicing would produce a 50-day average of the last
    sixty-three sessions — a line that looks right, tracks nothing, and is
    wrong by exactly as much as the market moved before the window opened.
    """
    if frame is None or frame.empty or "Close" not in frame:
        return []
    close = pd.to_numeric(frame["Close"], errors="coerce").dropna()
    if close.empty:
        return []
    sma50 = close.rolling(50).mean() if with_mas else None
    sma200 = close.rolling(200).mean() if with_mas else None
    index = close.index[-sessions:]
    points: list[SeriesPoint] = []
    for stamp in index:
        points.append(
            SeriesPoint(
                d=_iso(stamp),
                close=round(float(close.loc[stamp]), 4),
                sma50=_maybe(sma50, stamp),
                sma200=_maybe(sma200, stamp),
            )
        )
    return points


def _maybe(series: pd.Series | None, stamp) -> float | None:
    if series is None:
        return None
    value = series.get(stamp)
    if value is None or pd.isna(value):
        return None
    return round(float(value), 4)


def _iso(stamp) -> str:
    try:
        return pd.Timestamp(stamp).date().isoformat()
    except (TypeError, ValueError):
        return str(stamp)


def build_regime(report_context, spy_frame: pd.DataFrame | None) -> Regime:
    """Section 1, plus the market chart, the sector bars and the breadth gauge."""
    breadth = report_context.breadth
    sectors = report_context.sector_map
    return Regime(
        composite=float(breadth.composite) if breadth is not None else None,
        zone=breadth.zone if breadth is not None else "",
        # Through Reading.describe(), not through a formatted string of our own:
        # the "[UNVALIDATED]" marker on the exposure band is part of the reading
        # and must not be something the email layer decides whether to show.
        posture=ReadingRow.of(breadth.posture_reading()) if breadth is not None else ReadingRow(),
        rotation=ReadingRow.of(sectors.rotation_reading()) if sectors is not None else ReadingRow(),
        risk_regime=sectors.risk_regime if sectors is not None else "",
        risk_score=float(sectors.risk_score) if sectors is not None else 0.0,
        pct_above_50dma=breadth.breadth_pct_above_50dma if breadth is not None else None,
        universe_size=breadth.universe_size if breadth is not None else 0,
        vix=report_context.vix,
        leaders=[r.sector for r in sectors.leaders()] if sectors is not None else [],
        laggards=[r.sector for r in sectors.laggards()] if sectors is not None else [],
        overbought=list(sectors.overbought) if sectors is not None else [],
        oversold=list(sectors.oversold) if sectors is not None else [],
        sectors=[
            SectorBar(
                sector=row.sector,
                momentum=round(float(row.momentum), 3),
                status=row.status,
                etf=row.etf,
            )
            for row in sorted(
                sectors.rows if sectors is not None else [],
                key=lambda r: r.momentum,
                reverse=True,
            )
        ],
        spy=series_from(spy_frame, SPY_CHART_SESSIONS, with_mas=True),
    )


def build_gates(events, as_of: date) -> list[Gate]:
    """The releases that may actually stop a human acting.

    :func:`may_gate` is the single authority: it requires VERIFIED confidence
    *and* a date still ahead of the market date. An INDICATIVE date on a "do not
    act before" line stops being background and becomes an instruction, and it
    was never better than a guess.
    """
    gates = [event for event in events if may_gate(event, as_of)]
    gates.sort(key=lambda e: e.date)
    return [
        Gate(
            date=event.date.isoformat(),
            name=event.name,
            impact=event.impact,
            source=event.source,
            confidence=event.confidence,
        )
        for event in gates
    ]


def wait_condition(plan, spot: float | None) -> str:
    """One line saying what has to happen before the entry is live.

    Computed from the plan's own entry and the last close, never lifted from the
    executive summary. A sentence an LLM wrote about a level is not the level.
    """
    if plan is None or not plan.entry:
        return ""
    entry = float(plan.entry)
    basis = f" ({plan.entry_basis})" if plan.entry_basis else ""
    if not spot:
        return f"Entry ${entry:,.2f}{basis}."
    gap_pct = (entry / float(spot) - 1) * 100
    if abs(gap_pct) <= AT_MARKET_BAND_PCT:
        return f"At the market — entry ${entry:,.2f} is within {AT_MARKET_BAND_PCT:.1f}% of the last close{basis}."
    if gap_pct < 0:
        return f"Wait for a pullback to ${entry:,.2f} ({gap_pct:+.1f}% from the last close){basis}."
    return f"Wait for a break above ${entry:,.2f} ({gap_pct:+.1f}% from the last close){basis}."


def consensus_from(positioning) -> Consensus:
    if positioning is None:
        return Consensus()
    return Consensus(
        recommendation=positioning.recommendation_key,
        analysts=positioning.analyst_count,
        mean=positioning.target_mean,
        median=getattr(positioning, "target_median", None),
        low=positioning.target_low,
        high=positioning.target_high,
        spread=positioning.recommendation_spread,
    )


def avoid_reason(result) -> str:
    """Why this name is on the avoid list, in typed terms.

    The rating is the reason; the plan status says what that rating cost. Both
    come from fields, so a Hold whose executive summary reads bullishly still
    lands here saying "Hold".
    """
    decision = getattr(result, "decision", None)
    plan = getattr(result, "trade_plan", None)
    if decision is None:
        return "the deep analysis degraded — no verdict was produced"
    bits = [f"{decision.rating}"]
    if decision.confidence:
        bits[0] += f" ({decision.confidence} confidence)"
    if plan is not None and not plan.actionable and plan.status:
        bits.append(plan.status)
    if plan is not None and plan.failures:
        bits.append(plan.failures[0])
    return " — ".join(bits)


def build_setups_and_avoids(results, snapshot) -> tuple[list[Setup], list[Avoid]]:
    setups: list[Setup] = []
    avoids: list[Avoid] = []
    for result in results:
        decision = getattr(result, "decision", None)
        plan = getattr(result, "trade_plan", None)
        evidence = getattr(result, "evidence", None)
        rating = decision.rating if decision else "DEGRADED"
        name = getattr(result.queued, "name", "") or result.symbol
        spot = getattr(evidence, "price", None)

        if rating in AVOID_RATINGS or decision is None:
            avoids.append(
                Avoid(
                    symbol=result.symbol,
                    name=name,
                    rating=rating,
                    confidence=decision.confidence if decision else "",
                    reason=avoid_reason(result),
                )
            )
            continue

        setups.append(
            Setup(
                symbol=result.symbol,
                name=name,
                rating=rating,
                confidence=decision.confidence,
                spot=spot,
                price_target=decision.price_target,
                time_horizon=decision.time_horizon or "",
                direction=getattr(plan, "direction", "") or "",
                status=getattr(plan, "status", "") or "",
                entry=getattr(plan, "entry", None),
                entry_basis=getattr(plan, "entry_basis", "") or "",
                stop=getattr(plan, "stop", None),
                stop_basis=getattr(plan, "stop_basis", "") or "",
                target=getattr(plan, "target", None),
                target_basis=getattr(plan, "target_basis", "") or "",
                risk_pct=getattr(plan, "risk_pct", None),
                reward_risk=getattr(plan, "reward_risk", None),
                size_pct=getattr(plan, "size_pct", None),
                wait_condition=wait_condition(plan, spot),
                invalidation=decision.invalidation or "",
                consensus=consensus_from(getattr(evidence, "positioning", None)),
                degraded=bool(getattr(result, "degraded", False)),
                series=series_from(
                    snapshot.frame(result.symbol) if snapshot else None,
                    TICKER_CHART_SESSIONS,
                    with_mas=True,
                ),
            )
        )
    # Strongest conviction first; the sheet is read top-down on a phone.
    setups.sort(key=lambda s: (_rating_rank(s.rating), _confidence_rank(s.confidence)))
    return setups, avoids


def _rating_rank(rating: str) -> int:
    order = ["Buy", "Overweight", "Hold", "Neutral", "Underweight", "Sell"]
    return order.index(rating) if rating in order else len(order)


def _confidence_rank(confidence: str) -> int:
    return {"H": 0, "M": 1, "L": 2}.get((confidence or "").upper()[:1], 3)


def breakeven_status(candidate, invalidation: float | None) -> str:
    """Where assignment lands relative to the line the equity thesis drew.

    The conflict list the screen already computed is authoritative; this only
    puts the arithmetic on the same row as the strike, because a reader deciding
    go/no-go should not have to hold two numbers from two sections in their head.
    """
    if candidate is None:
        return ""
    if invalidation is None:
        return "no equity invalidation to check the breakeven against"
    gap = candidate.breakeven - invalidation
    if gap <= 0:
        return (
            f"breakeven ${candidate.breakeven:,.2f} is ${abs(gap):,.2f} BELOW the "
            f"${invalidation:,.2f} invalidation — assignment lands where the equity plan is wrong"
        )
    return (
        f"breakeven ${candidate.breakeven:,.2f} clears the ${invalidation:,.2f} "
        f"invalidation by ${gap:,.2f}"
    )


def build_overlays(plans, invalidations: dict[str, float]) -> tuple[list[Overlay], list[OverlaySkip]]:
    """Only candidates that survived every hard filter reach the sheet.

    ``plan.candidates`` is already the post-filter list — :func:`hard_filters`
    rejected the rest before scoring — and ``plan.chosen`` is the strategist's
    pick from it. The sheet shows the pick; everything else becomes one honest
    line under "no overlay", because a screen that found nothing is a result.
    """
    overlays: list[Overlay] = []
    skips: list[OverlaySkip] = []
    for plan in plans:
        chosen = plan.chosen
        if chosen is None:
            skips.append(OverlaySkip(symbol=plan.symbol, reason=_skip_reason(plan)))
            continue
        quote = chosen.quote
        overlays.append(
            Overlay(
                symbol=plan.symbol,
                strategy=plan.strategy or "",
                strike=float(chosen.strike),
                expiry=str(quote.expiry),
                dte=quote.dte,
                delta=abs(float(chosen.delta)),
                credit=float(chosen.credit),
                annualized_yield_pct=float(chosen.annualized_yield_pct),
                breakeven=float(chosen.breakeven),
                invalidation=invalidations.get(plan.symbol),
                breakeven_status=breakeven_status(chosen, invalidations.get(plan.symbol)),
                earnings_flag=chosen.earnings_flag,
                conflicts=list(chosen.conflicts),
            )
        )
    return overlays, skips


def _skip_reason(plan) -> str:
    if plan.strategy is None:
        return plan.skipped or "no overlay proposed"
    if plan.error:
        return plan.error
    if not plan.candidates:
        return "no strike passed the hard filters"
    return f"the strategist declined all {len(plan.candidates)} screened candidate(s)"


def build_presentation_context(
    run_date: date,
    report_context,
    results,
    plans,
    *,
    snapshot=None,
    spy_frame: pd.DataFrame | None = None,
    macro_events=(),
    market_as_of: date | None = None,
    stage: str = "all",
) -> PresentationContext:
    """Assemble the artefact. Pure: reads objects, writes data, touches no I/O."""
    as_of = market_as_of or (snapshot.market_as_of if snapshot else run_date)
    setups, avoids = build_setups_and_avoids(results, snapshot)
    invalidations = {s.symbol: s.stop for s in setups if s.stop}
    overlays, skips = build_overlays(plans, invalidations)
    return PresentationContext(
        run_date=run_date.isoformat(),
        data_as_of=getattr(report_context, "data_as_of", "unknown"),
        market_as_of=as_of.isoformat() if isinstance(as_of, date) else str(as_of),
        snapshot_id=snapshot.snapshot_id if snapshot else "",
        session_note=getattr(report_context, "session_note", ""),
        stage=stage,
        regime=build_regime(report_context, spy_frame),
        gates=build_gates(list(macro_events), as_of if isinstance(as_of, date) else run_date),
        setups=setups,
        avoids=avoids,
        overlays=overlays,
        overlay_skips=skips,
        degraded=list(getattr(report_context, "degraded", None).sources)
        if getattr(report_context, "degraded", None) is not None
        else [],
    )


__all__ = [
    "AT_MARKET_BAND_PCT",
    "AVOID_RATINGS",
    "LONG_RATINGS",
    "avoid_reason",
    "breakeven_status",
    "build_gates",
    "build_overlays",
    "build_presentation_context",
    "build_regime",
    "build_setups_and_avoids",
    "consensus_from",
    "series_from",
    "wait_condition",
]
