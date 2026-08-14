"""Macro and earnings calendar for the "Macro & Events Today" section.

Earnings come from Finnhub's free tier. The economic calendar is the one place
where every cookbook reaches for a paid API — tradermonty's
`economic-calendar-fetcher` requires an FMP key, and Finnhub's
`/calendar/economic` is premium-only. Rather than add a paid service we derive
the recurring US release schedule from published rules (BLS/BEA/Fed/Census
publish on fixed weekday-of-month patterns) and mark the source DEGRADED so the
report never implies it is a live feed.
"""

from __future__ import annotations

import calendar as _cal
from dataclasses import dataclass
from datetime import date, timedelta

from ..data.finnhub_client import EarningsEvent, FinnhubFree
from ..data.validate import DegradedTracker

HIGH_IMPACT = "High"
MEDIUM_IMPACT = "Medium"


@dataclass
class MacroEvent:
    date: date
    name: str
    impact: str
    source: str


@dataclass
class CalendarView:
    macro: list[MacroEvent]
    earnings_today: list[EarningsEvent]
    earnings_week: list[EarningsEvent]
    macro_is_live: bool


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

    Indicative, not a live feed: exact dates shift for holidays and FOMC meeting
    dates are set annually. Always reported alongside a DEGRADED note.
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
                events.append(MacroEvent(when, name, impact, "static release schedule"))
        month_cursor = (
            date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)
        )

    day = start
    while day <= end:
        if day.weekday() == 3:  # Thursday
            events.append(MacroEvent(day, "Initial Jobless Claims", MEDIUM_IMPACT, "static release schedule"))
        day += timedelta(days=1)

    return sorted(events, key=lambda e: (e.date, e.name))


def build_calendar(
    finnhub: FinnhubFree,
    run_date: date,
    universe_symbols: set[str],
    degraded: DegradedTracker,
    horizon_days: int = 7,
) -> CalendarView:
    end = run_date + timedelta(days=horizon_days)

    live_macro = finnhub.economic_calendar(run_date, end)
    macro_is_live = bool(live_macro)
    if macro_is_live:
        macro = [
            MacroEvent(
                date=date.fromisoformat(str(row.get("time", ""))[:10]),
                name=str(row.get("event", "")).strip(),
                impact=str(row.get("impact", "")).title() or MEDIUM_IMPACT,
                source="Finnhub",
            )
            for row in live_macro
            if str(row.get("country", "US")).upper() in {"US", "USA"}
        ]
    else:
        macro = static_release_calendar(run_date, end)
        degraded.add(
            "Economic calendar",
            "no free live source (Finnhub premium / FMP paid); using an indicative "
            "static release schedule — verify exact dates and times before acting",
        )

    all_earnings = finnhub.earnings_calendar(run_date, end)
    # Keep the ones that matter to our universe; the free feed returns the world.
    relevant = [e for e in all_earnings if e.symbol in universe_symbols]
    return CalendarView(
        macro=macro,
        earnings_today=[e for e in relevant if e.date == run_date],
        earnings_week=sorted(relevant, key=lambda e: (e.date, e.symbol)),
        macro_is_live=macro_is_live,
    )


def earnings_within(view: CalendarView, symbol: str, days: int, run_date: date) -> EarningsEvent | None:
    """Earnings for ``symbol`` inside the next ``days`` days, if any."""
    horizon = run_date + timedelta(days=days)
    for event in view.earnings_week:
        if event.symbol == symbol and run_date <= event.date <= horizon:
            return event
    return None
