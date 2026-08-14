"""Rank screener candidates and attach an LLM quick take (FAST tier).

This is the only place Milestone 1 spends tokens: one FAST-tier call per
shortlisted ticker plus one for the market commentary. Cost per run lands in
the report footer via :class:`~tradingagent.llm.TokenLedger`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

from ..data.finnhub_client import FinnhubFree
from ..data.validate import DegradedTracker
from ..llm import LLMError, LLMGateway
from ..pipeline.prompts_loader import render
from .breadth import BreadthResult
from .calendar import CalendarView, earnings_within
from .screener import Candidate
from .sectors import SectorMap, SectorRow

log = logging.getLogger(__name__)

Rating = Literal["Buy", "Overweight", "Hold", "Underweight", "Sell"]
RATING_ORDER: dict[str, int] = {"Buy": 5, "Overweight": 4, "Hold": 3, "Underweight": 2, "Sell": 1}


class QuickTake(BaseModel):
    """Schema enforced on the FAST-tier reply (re-prompt once, then DEGRADED)."""

    rating: Rating
    confidence: Literal["L", "M", "H"]
    thesis: str = Field(max_length=600)
    key_risk: str = Field(max_length=400)
    deep_dive_priority: int = Field(ge=1, le=10)


@dataclass
class ShortlistEntry:
    candidate: Candidate
    take: QuickTake | None
    earnings_flag: str
    news_headline: str | None
    degraded_reason: str | None = None

    @property
    def symbol(self) -> str:
        return self.candidate.symbol

    @property
    def rating_label(self) -> str:
        if self.take is None:
            return "DEGRADED"
        return f"{self.take.rating} ({self.take.confidence})"

    @property
    def priority(self) -> int:
        return self.take.deep_dive_priority if self.take else 0


def _sector_note(sector_map: SectorMap, sector: str) -> str:
    row: SectorRow | None = next((r for r in sector_map.rows if r.sector == sector), None)
    if row is None:
        return "sector data unavailable"
    rank = [r.sector for r in sector_map.rows].index(sector) + 1
    return (
        f"{sector} ranks {rank}/{len(sector_map.rows)} by momentum, "
        f"{row.uptrend_ratio:.0%} of members above their 50DMA, status {row.status}"
        + (" (a preferred sector)" if row.preferred else "")
    )


def _earnings_note(view: CalendarView, symbol: str, run_date: date) -> tuple[str, str]:
    event = earnings_within(view, symbol, days=10, run_date=run_date)
    if event is None:
        return "no confirmed earnings in the next 10 days", "—"
    days = (event.date - run_date).days
    return (
        f"reports {event.date.isoformat()} ({event.timing}), in {days} day(s) — "
        "short-option and swing risk",
        f"{event.date.isoformat()} {event.timing}",
    )


def build_shortlist(
    candidates: list[Candidate],
    breadth: BreadthResult,
    sector_map: SectorMap,
    calendar_view: CalendarView,
    finnhub: FinnhubFree,
    gateway: LLMGateway,
    run_date: date,
    degraded: DegradedTracker,
    size: int = 8,
) -> list[ShortlistEntry]:
    """Take the top ``size`` candidates and enrich each with a FAST quick take."""
    entries: list[ShortlistEntry] = []
    for candidate in candidates[:size]:
        earnings_note, earnings_flag = _earnings_note(calendar_view, candidate.symbol, run_date)
        news = finnhub.company_news(candidate.symbol, days=7, limit=3)
        news_note = (
            " | ".join(f"{n.headline} ({n.source})" for n in news) if news else "none retrieved"
        )

        prompt = render(
            "quick_take",
            symbol=candidate.symbol,
            name=candidate.name,
            sector=candidate.sector,
            industry=candidate.industry,
            price=candidate.price,
            day_gain_pct=candidate.day_gain_pct,
            score=candidate.score,
            rating=candidate.rating,
            state=candidate.state,
            triggers=", ".join(candidate.triggers) or "none",
            volume_ratio_20d=candidate.volume_ratio_20d,
            close_location_pct=candidate.close_location_pct,
            prior_base_days=candidate.prior_base_days,
            base_width_pct=candidate.base_width_pct,
            entry_ref=candidate.entry_ref,
            stop_ref=candidate.stop_ref,
            risk_pct=candidate.risk_pct,
            dist_52w_high_pct=candidate.dist_52w_high_pct if candidate.dist_52w_high_pct is not None else 0.0,
            trend_note=_trend_note(candidate),
            rs_note=(
                f"{candidate.rs_vs_spy_3mo:+.1f} percentage points"
                if candidate.rs_vs_spy_3mo is not None
                else "unavailable"
            ),
            reject_reasons=", ".join(candidate.reject_reasons) or "none",
            breadth_composite=breadth.composite,
            breadth_zone=breadth.zone,
            breadth_guidance=breadth.guidance,
            risk_regime=sector_map.risk_regime,
            cycle_phase=sector_map.cycle_phase,
            sector_note=_sector_note(sector_map, candidate.sector),
            earnings_note=earnings_note,
            news_note=news_note,
        )

        take: QuickTake | None = None
        reason: str | None = None
        try:
            take = gateway.complete(prompt, tier="fast", schema=QuickTake, max_tokens=700)
        except LLMError as exc:
            reason = str(exc)[:200]
            degraded.add(f"Quick take {candidate.symbol}", reason)

        entries.append(
            ShortlistEntry(
                candidate=candidate,
                take=take,
                earnings_flag=earnings_flag,
                news_headline=news[0].headline if news else None,
                degraded_reason=reason,
            )
        )

    entries.sort(
        key=lambda e: (
            e.priority,
            RATING_ORDER.get(e.take.rating, 0) if e.take else 0,
            e.candidate.score,
        ),
        reverse=True,
    )
    return entries


def deep_dive_queue(
    entries: list[ShortlistEntry],
    sector_map: SectorMap | None = None,
    cap: int = 5,
) -> list[ShortlistEntry]:
    """Order the deep-analysis queue so it spans sectors instead of stacking one.

    Round-robin: the best name from each represented sector first (sectors taken
    in momentum order, leaders first), then second names, and so on. Within a
    sector the incoming order — priority, then rating, then screener score — is
    preserved. Entries with no quick take are never queued for a deep dive.
    """
    ranked = [e for e in entries if e.take is not None]
    if not ranked:
        return []

    by_sector: dict[str, list[ShortlistEntry]] = {}
    for entry in ranked:
        by_sector.setdefault(entry.candidate.sector, []).append(entry)

    momentum_rank = {row.sector: i for i, row in enumerate(sector_map.rows)} if sector_map else {}
    first_seen = {sector: ranked.index(group[0]) for sector, group in by_sector.items()}
    sectors = sorted(
        by_sector,
        # Sectors absent from the map sort last but keep their shortlist order.
        key=lambda s: (momentum_rank.get(s, len(momentum_rank)), first_seen[s]),
    )

    out: list[ShortlistEntry] = []
    depth = 0
    while len(out) < cap and depth < max(len(g) for g in by_sector.values()):
        for sector in sectors:
            group = by_sector[sector]
            if depth < len(group):
                out.append(group[depth])
                if len(out) == cap:
                    return out
        depth += 1
    return out


def _trend_note(candidate: Candidate) -> str:
    if candidate.above_50dma and candidate.above_200dma:
        return "above both the 50-day and 200-day moving averages"
    if candidate.above_200dma:
        return "above the 200-day but below the 50-day moving average"
    if candidate.above_50dma:
        return "above the 50-day but below the 200-day moving average"
    return "below both the 50-day and 200-day moving averages"


def market_commentary(
    gateway: LLMGateway,
    degraded: DegradedTracker,
    **fields: object,
) -> str:
    """One FAST-tier paragraph for the Market Overview section."""
    try:
        text = gateway.complete(render("market_commentary", **fields), tier="fast", max_tokens=500)
        return str(text).strip()
    except LLMError as exc:
        degraded.add("Market commentary", str(exc)[:200])
        return (
            "_Market commentary unavailable this run (LLM call failed); the tables below "
            "are unaffected._"
        )
