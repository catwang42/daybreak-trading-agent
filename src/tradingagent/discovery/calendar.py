"""Macro and earnings calendar for the "Macro & Events Today" section.

Earnings come from Finnhub's free tier. Macro dates come from the issuing
agencies wherever we can reach them — FRED's release calendar (BLS/BEA/Census)
and the Fed's own FOMC page — and from a static weekday-of-month rule when we
cannot. The two are not interchangeable and are no longer presented as if they
were: every event carries the confidence class of its source, and only a
``VERIFIED`` date is allowed to gate a decision. See
:mod:`tradingagent.discovery.release_schedule` for the matrix and the reason.

No paid service. tradermonty's `economic-calendar-fetcher` wants an FMP key and
Finnhub's `/calendar/economic` is premium-only; both are still tried first and
neither is required.
"""

from __future__ import annotations

import calendar as _cal
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from ..data.finnhub_client import EarningsEvent, FinnhubFree
from ..data.validate import DegradedTracker
from .release_schedule import (
    HIGH_IMPACT,
    INDICATIVE,
    MEDIUM_IMPACT,
    MISSING,
    PERMITTED_USE,
    STALE,
    STATIC_SOURCE,
    VERIFIED,
    MacroEvent,
    fomc_meeting_dates,
    fred_release_dates,
)

__all__ = [
    "CalendarView",
    "MacroEvent",
    "HIGH_IMPACT",
    "MEDIUM_IMPACT",
    "VERIFIED",
    "INDICATIVE",
    "STALE",
    "MISSING",
    "PERMITTED_USE",
    "build_calendar",
    "static_release_calendar",
    "earnings_within",
]


@dataclass
class CalendarView:
    macro: list[MacroEvent]
    earnings_today: list[EarningsEvent]
    earnings_week: list[EarningsEvent]

    @property
    def has_verified_dates(self) -> bool:
        """Did any macro date come from an issuing agency this run?"""
        return any(e.confidence == VERIFIED for e in self.macro)

    def gating_events(self) -> list[MacroEvent]:
        """The only events a model may wait for or size around."""
        return [e for e in self.macro if e.may_gate_entries]

    def confidence_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for event in self.macro:
            counts[event.confidence] = counts.get(event.confidence, 0) + 1
        return counts

    def note(self, limit: int = 8) -> str:
        """The macro block as every prompt and report sees it.

        The permitted-use line travels with the dates, always. A model handed a
        bare list has no way to know that one of them is a guess — and one
        handed an all-VERIFIED list still needs to know it may not wait for a
        release that is not on it.
        """
        shown = self.macro[:limit]
        lines = [f"- {e.label()}" for e in shown] or ["- none scheduled"]
        lines.append(
            "- Only a VERIFIED date may be waited for or sized around. An "
            "INDICATIVE, STALE or MISSING date is background context and must "
            "never become an instruction to wait for a release, and a release "
            "absent from this list has no date at all."
        )
        return "\n".join(lines)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """The n-th ``weekday`` (0=Mon) of a month; n=1 is the first."""
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (n - 1))


def _business_day(year: int, month: int, n: int) -> date:
    """The n-th business day of a month (holidays ignored — indicative only)."""
    day = date(year, month, 1)
    counted = 0
    while True:
        if day.weekday() < 5:
            counted += 1
            if counted == n:
                return day
        day += timedelta(days=1)


def static_release_calendar(start: date, end: date) -> list[MacroEvent]:
    """Recurring US macro releases by their published scheduling rule.

    A guess, and marked as one: every event returned is INDICATIVE. Exact dates
    shift for holidays and the agencies set them a year ahead, so this is only
    ever the fallback for a release whose real schedule we could not fetch.
    """
    events: list[MacroEvent] = []
    month_cursor = date(start.year, start.month, 1)
    while month_cursor <= end:
        y, m = month_cursor.year, month_cursor.month
        candidates = [
            # Employment Situation: first Friday of the month (BLS).
            (_nth_weekday(y, m, 4, 1), "Employment Situation (Nonfarm Payrolls)", HIGH_IMPACT),
            # CPI: around the 8th-13th business day; BLS targets mid-month.
            (_business_day(y, m, 9), "CPI (Consumer Price Index)", HIGH_IMPACT),
            (_business_day(y, m, 10), "PPI (Producer Price Index)", MEDIUM_IMPACT),
            # Retail Sales: mid-month (Census, ~16th).
            (date(y, m, min(16, _cal.monthrange(y, m)[1])), "Retail Sales", MEDIUM_IMPACT),
            # ISM Manufacturing: first business day; ISM Services: third.
            (_business_day(y, m, 1), "ISM Manufacturing PMI", MEDIUM_IMPACT),
            (_business_day(y, m, 3), "ISM Services PMI", MEDIUM_IMPACT),
            # PCE price index: near month end (BEA).
            (date(y, m, min(27, _cal.monthrange(y, m)[1])), "PCE Price Index", HIGH_IMPACT),
            # Initial jobless claims: every Thursday (handled below).
        ]
        for when, name, impact in candidates:
            if start <= when <= end:
                events.append(MacroEvent(when, name, impact, STATIC_SOURCE, INDICATIVE))
        month_cursor = (
            date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)
        )

    day = start
    while day <= end:
        if day.weekday() == 3:  # Thursday
            events.append(
                MacroEvent(day, "Initial Jobless Claims", MEDIUM_IMPACT, STATIC_SOURCE, INDICATIVE)
            )
        day += timedelta(days=1)

    return sort_events(events)


def sort_events(events: list[MacroEvent]) -> list[MacroEvent]:
    """Dated events first, in order; undated (MISSING) ones last."""
    return sorted(events, key=lambda e: (e.date or date.max, e.name))


def build_calendar(
    finnhub: FinnhubFree,
    run_date: date,
    universe_symbols: set[str],
    degraded: DegradedTracker,
    horizon_days: int = 7,
    as_of: date | None = None,
    session: Any = None,
    api_key: str | None = None,
) -> CalendarView:
    """The window's macro releases and earnings, each labelled by source.

    ``as_of`` is the snapshot's market date. The macro window is asked for from
    there, not from the wall clock, so a ``--date`` backfill gets the schedule
    that stood then (see :func:`.release_schedule.fred_release_dates`).
    """
    as_of = as_of or run_date
    end = as_of + timedelta(days=horizon_days)

    macro: list[MacroEvent] = []
    notes: list[str] = []

    # 1. A live feed, if the account has one. Free tiers usually do not.
    for row in finnhub.economic_calendar(as_of, end) or []:
        if str(row.get("country", "US")).upper() not in {"US", "USA"}:
            continue
        when = _iso(row.get("time"))
        if when is None:
            continue
        macro.append(
            MacroEvent(
                date=when,
                name=str(row.get("event", "")).strip() or "unnamed release",
                impact=str(row.get("impact", "")).title() or MEDIUM_IMPACT,
                source="Finnhub economic calendar",
                confidence=VERIFIED,
            )
        )

    # 2. The agencies themselves.
    fred, fred_notes, answered = fred_release_dates(as_of, end, session=session, api_key=api_key)
    fomc, fomc_notes = fomc_meeting_dates(as_of, end, session=session)
    macro += fred + fomc
    notes += fred_notes + fomc_notes
    if fomc_notes:
        # The Fed page is the only source for meeting dates and there is no
        # rule to fall back on, so an unreachable page is a named unknown.
        macro.append(MacroEvent(None, "FOMC decision", HIGH_IMPACT, "no source reached", MISSING))

    # 3. The static rule, only for releases nobody authoritative covered. A
    # release the agency schedule answered for is covered even when the answer
    # was "nothing due" — printing a guessed date there would invent a release.
    covered = {e.name for e in macro if e.confidence in (VERIFIED, STALE, MISSING)} | answered
    macro += [e for e in static_release_calendar(as_of, end) if e.name not in covered]

    macro = _dedupe(sort_events(macro))
    counts = CalendarView(macro, [], []).confidence_counts()
    if counts.get(VERIFIED):
        if notes:
            degraded.add(
                "Economic calendar",
                "partially authoritative — "
                + "; ".join(notes[:3])
                + f"; {counts.get(INDICATIVE, 0)} date(s) fall back to the indicative "
                "static schedule and may not gate an entry",
            )
    else:
        degraded.add(
            "Economic calendar",
            "no authoritative schedule reached ("
            + ("; ".join(notes[:3]) or "no source answered")
            + ") — every macro date below is an indicative approximation and may not "
            "gate an entry; verify before acting",
        )

    all_earnings = finnhub.earnings_calendar(run_date, end)
    # Keep the ones that matter to our universe; the free feed returns the world.
    relevant = [e for e in all_earnings if e.symbol in universe_symbols]
    return CalendarView(
        macro=macro,
        earnings_today=[e for e in relevant if e.date == run_date],
        earnings_week=sorted(relevant, key=lambda e: (e.date, e.symbol)),
    )


def _iso(raw: Any) -> date | None:
    try:
        return date.fromisoformat(str(raw)[:10])
    except (TypeError, ValueError):
        return None


def _dedupe(events: list[MacroEvent]) -> list[MacroEvent]:
    """One row per (date, release). The most confident source wins."""
    best: dict[tuple[Any, str], MacroEvent] = {}
    rank = {VERIFIED: 0, STALE: 1, INDICATIVE: 2, MISSING: 3}
    for event in events:
        key = (event.date, event.name.lower())
        current = best.get(key)
        if current is None or rank[event.confidence] < rank[current.confidence]:
            best[key] = event
    return sort_events(list(best.values()))


def earnings_within(view: CalendarView, symbol: str, days: int, run_date: date) -> EarningsEvent | None:
    """Earnings for ``symbol`` inside the next ``days`` days, if any."""
    horizon = run_date + timedelta(days=days)
    for event in view.earnings_week:
        if event.symbol == symbol and run_date <= event.date <= horizon:
            return event
    return None
