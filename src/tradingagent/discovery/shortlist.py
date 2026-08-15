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
from ..signals import SignalBundle, SignalHub
from ..signals.bundle import ShadowRanking
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
    #: What the M3 signal layer read for this name, if it ran.
    signals: SignalBundle | None = None
    #: 1-based place in the pool on screener score alone, before any signal
    #: moved it. Kept so the report can show what the signals actually changed
    #: rather than asserting that they did something.
    screen_rank: int = 0

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

    @property
    def score_adjustment(self) -> float:
        """What the signal layer was actually allowed to contribute."""
        return self.signals.score_adjustment() if self.signals else 0.0

    @property
    def shadow_adjustment(self) -> float:
        """What it would have contributed at full trust. Reported, not applied."""
        return self.signals.shadow_adjustment() if self.signals else 0.0

    @property
    def is_shadow(self) -> bool:
        return bool(self.signals and self.signals.is_shadow)

    @property
    def adjusted_score(self) -> float:
        return self.candidate.score + self.score_adjustment

    def signal_note(self) -> str:
        return self.signals.summary() if self.signals else "no signal layer this run"


@dataclass
class PoolStats:
    """Where today's candidates sit relative to each other.

    The Gate 1 A/B showed the quick take returning the same rating and the same
    confidence for every name, on both model tiers. The cause was the question:
    an *absolute* Buy/…/Sell scale applied to a population the screener has
    already made homogeneous can only produce the middle of the scale. Supplying
    the pool lets the prompt ask a relative question instead.
    """

    size: int
    best: int
    median: int
    worst: int

    @classmethod
    def build(cls, candidates: list[Candidate]) -> "PoolStats":
        scores = sorted((c.score for c in candidates), reverse=True)
        if not scores:
            return cls(size=0, best=0, median=0, worst=0)
        return cls(size=len(scores), best=scores[0], median=scores[len(scores) // 2], worst=scores[-1])

    def note(self, candidate: Candidate, candidates: list[Candidate]) -> str:
        if not self.size:
            return "pool statistics unavailable"
        better = sum(1 for c in candidates if c.score > candidate.score)
        same_sector = sum(1 for c in candidates if c.sector == candidate.sector)
        third = max(1, round(self.size / 3))
        placement = (
            "top third" if better < third
            else "bottom third" if better >= self.size - third
            else "middle third"
        )
        return (
            f"{self.size} candidates passed today's screen. This one scores "
            f"{candidate.score} and ranks {better + 1} of {self.size} — the {placement} "
            f"of today's pool. Pool scores: best {self.best}, median {self.median}, "
            f"worst {self.worst}. {same_sector} of today's candidates "
            f"{'is' if same_sector == 1 else 'are'} in {candidate.sector}."
        )


# Countable confirmations behind the quick take's confidence. The Gate 1 A/B
# found the old rubric ("H when the technical and regime evidence agree and
# there is no earnings risk") unsatisfiable: the prompt also instructs
# scepticism, and the context block always supplies at least one dissonant
# fact, so M was the only reachable value on both tiers. These six are things
# the screener already measured, so the model counts rather than judges.
CONFIRMATIONS: tuple[tuple[str, str], ...] = (
    ("volume", "volume at least 1.5x the 20-day average"),
    ("close_location", "closed in the top 20% of the day's range"),
    ("trend", "above both the 50-day and 200-day moving averages"),
    ("relative_strength", "outperforming SPY over 3 months"),
    ("base", "prior base no wider than 12%"),
    ("no_earnings", "no confirmed earnings inside 10 days"),
)


def confirmation_checklist(candidate: Candidate, earnings_flag: str) -> tuple[list[str], int]:
    """Render the six confirmations as checkbox lines, plus how many hold."""
    checks = {
        "volume": candidate.volume_ratio_20d >= 1.5,
        "close_location": candidate.close_location_pct >= 80,
        "trend": candidate.above_50dma and candidate.above_200dma,
        "relative_strength": (candidate.rs_vs_spy_3mo or 0) > 0,
        "base": 0 < candidate.base_width_pct <= 12,
        "no_earnings": earnings_flag == "—",
    }
    lines = [f"- [{'x' if checks[key] else ' '}] {label}" for key, label in CONFIRMATIONS]
    return lines, sum(checks.values())


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


def quick_take_prompt(
    candidate: Candidate,
    breadth: BreadthResult,
    sector_map: SectorMap,
    earnings_note: str,
    news_note: str,
    pool: list[Candidate] | None = None,
    earnings_flag: str = "—",
    bundle: SignalBundle | None = None,
) -> str:
    """Render the quick-take prompt. Shared with the tier A/B harness."""
    pool = pool if pool is not None else [candidate]
    checklist, confirmed = confirmation_checklist(candidate, earnings_flag)
    return render(
        "quick_take",
        pool_note=PoolStats.build(pool).note(candidate, pool),
        signal_note=(
            bundle.prompt_block() if bundle else "No signal source ran for this candidate today."
        ),
        checklist="\n".join(checklist),
        confirmed=confirmed,
        total_confirmations=len(CONFIRMATIONS),
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


#: How far past the shortlist the signal layer looks. Signals can only promote
#: a name they were run on, so the pool has to be wider than the shortlist for
#: promotion to be possible at all — but every extra name costs an EDGAR round
#: trip, so it is a multiple rather than the whole screen.
SIGNAL_POOL_MULTIPLE = 2


def select_with_signals(
    candidates: list[Candidate],
    hub: SignalHub | None,
    run_date: date,
    size: int,
) -> list[tuple[Candidate, SignalBundle | None, int]]:
    """Choose the ``size`` names worth spending quick-take tokens on.

    Returns ``(candidate, bundle, screen_rank)``. ``screen_rank`` is the name's
    1-based place on screener score alone.

    Membership is decided by ``candidate.score + bundle.score_adjustment()``,
    and that adjustment is zero for every source that has not resolved enough
    journal outcomes to graduate (:mod:`tradingagent.signals.accuracy`). With
    today's record that is all of them, so this is the screener's own top
    ``size`` — by arithmetic rather than by special case, so the mechanism
    resumes on its own the day a source earns its weight.

    The signals still run over the wider pool: the counterfactual ordering is
    recorded on ``hub.shadow`` for the report, because a layer that is never
    measured can never be graduated.
    """
    screen_rank = {c.symbol: i + 1 for i, c in enumerate(candidates)}
    if hub is None:
        return [(c, None, screen_rank[c.symbol]) for c in candidates[:size]]

    pool = candidates[: size * SIGNAL_POOL_MULTIPLE]
    hub.collect([c.symbol for c in pool], run_date)
    scored = [(c, hub.bundle(c.symbol, run_date)) for c in pool]
    # Stable within a tie so the screener's own ordering still decides when the
    # signal layer has nothing to say — which, while every source is shadowed,
    # is always.
    scored.sort(key=lambda pair: pair[0].score + pair[1].score_adjustment(), reverse=True)
    chosen = [(c, b, screen_rank[c.symbol]) for c, b in scored[:size]]

    shadow = sorted(scored, key=lambda pair: pair[0].score + pair[1].shadow_adjustment(), reverse=True)
    hub.shadow = ShadowRanking(
        size=size,
        chosen=[c.symbol for c, _, _ in chosen],
        shadow=[c.symbol for c, _ in shadow[:size]],
        adjustments={c.symbol: round(b.shadow_adjustment(), 2) for c, b in scored},
    )
    promoted = [c.symbol for c, _, rank in chosen if rank > size]
    if promoted:
        log.info(
            "Signal layer promoted %s into the shortlist over screener order", ", ".join(promoted)
        )
    log.info("Signal layer %s", hub.shadow.note())
    return chosen


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
    hub: SignalHub | None = None,
) -> list[ShortlistEntry]:
    """Take the top ``size`` candidates and enrich each with a FAST quick take."""
    entries: list[ShortlistEntry] = []
    for candidate, bundle, screen_rank in select_with_signals(candidates, hub, run_date, size):
        earnings_note, earnings_flag = _earnings_note(calendar_view, candidate.symbol, run_date)
        news = finnhub.company_news(candidate.symbol, days=7, limit=3)
        news_note = (
            " | ".join(f"{n.headline} ({n.source})" for n in news) if news else "none retrieved"
        )
        prompt = quick_take_prompt(
            candidate,
            breadth,
            sector_map,
            earnings_note,
            news_note,
            pool=candidates,
            earnings_flag=earnings_flag,
            bundle=bundle,
        )

        take: QuickTake | None = None
        reason: str | None = None
        try:
            take = gateway.complete(prompt, tier="fast", schema=QuickTake)
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
                signals=bundle,
                screen_rank=screen_rank,
            )
        )

    entries.sort(
        key=lambda e: (
            e.priority,
            RATING_ORDER.get(e.take.rating, 0) if e.take else 0,
            # Signal-adjusted, so the layer breaks ties the quick take left
            # level as well as deciding who made the pool in the first place.
            e.adjusted_score,
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
