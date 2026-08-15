"""Where a macro release date came from, and what it is therefore allowed to do.

Every macro date used to arrive from the same place — a weekday-of-month rule
in :func:`tradingagent.discovery.calendar.static_release_calendar` — and every
date was printed the same way. So a report told a reader to wait for "Thursday's
PPI" when PPI was on Wednesday, and to size around a Retail Sales print two days
after it had already happened. The rule is a decent guess at when an agency
*usually* publishes; it is not a schedule, and nothing downstream could tell the
difference.

So a date now carries the confidence class of its source:

``VERIFIED``
    The issuing agency's own published schedule, fetched for this run: FRED's
    release-date mirror of BLS / BEA / Census, or the Federal Reserve's FOMC
    calendar. This is the only class that may gate a decision.
``INDICATIVE``
    Our static weekday-of-month rule. Right to within a few days, which is
    worthless for a date-specific instruction. Context only.
``STALE``
    An authoritative schedule answered, but the newest date it offers for that
    release is already behind the run's market date — we know when it last
    published, not when it next will.
``MISSING``
    We expect the release and have no date for it from any source. Named as
    unknown rather than guessed at.

The permitted-use matrix is :data:`PERMITTED_USE` and it is enforced, not
documented: :func:`tradingagent.pipeline.macro_gate.suppressed_gates` strips a
"wait until <release>" instruction that rests on anything but a VERIFIED date.

Free tiers only. FRED is already a dependency (``FRED_API_KEY``); the Fed's
calendar page needs no key. Neither is a new paid service, and both degrade to
the static rule rather than failing the run.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from ..config import env

log = logging.getLogger(__name__)

HIGH_IMPACT = "High"
MEDIUM_IMPACT = "Medium"

VERIFIED = "VERIFIED"
INDICATIVE = "INDICATIVE"
STALE = "STALE"
MISSING = "MISSING"

CONFIDENCE_ORDER = (VERIFIED, INDICATIVE, STALE, MISSING)


@dataclass(frozen=True)
class Use:
    """What one confidence class may be used for."""

    may_gate_entries: bool
    may_gate_options: bool
    narrative: str


#: The matrix. Only VERIFIED gates anything.
PERMITTED_USE: dict[str, Use] = {
    VERIFIED: Use(True, True, "may be waited for, sized around, and named with its date"),
    INDICATIVE: Use(
        False,
        False,
        "context only — an approximate date may never become an instruction to wait",
    ),
    STALE: Use(
        False,
        False,
        "context only — this is when the release last printed, not when it next will",
    ),
    MISSING: Use(False, False, "named as unknown; treat the window as unpriced for this release"),
}

STATIC_SOURCE = "static release schedule"
FRED_SOURCE = "FRED release calendar (BLS/BEA/Census)"
FOMC_SOURCE = "Federal Reserve FOMC calendar"


@dataclass
class MacroEvent:
    #: ``None`` only for a MISSING release — one we expect and have no date for.
    date: date | None
    name: str
    impact: str
    source: str
    confidence: str = INDICATIVE

    @property
    def may_gate_entries(self) -> bool:
        return PERMITTED_USE[self.confidence].may_gate_entries

    @property
    def may_gate_options(self) -> bool:
        return PERMITTED_USE[self.confidence].may_gate_options

    def to_dict(self) -> dict[str, Any]:
        return {
            "date": self.date.isoformat() if self.date else None,
            "name": self.name,
            "impact": self.impact,
            "source": self.source,
            "confidence": self.confidence,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "MacroEvent":
        return cls(
            date=_parse_date(raw.get("date")) if raw.get("date") else None,
            name=str(raw.get("name", "")),
            impact=str(raw.get("impact", MEDIUM_IMPACT)),
            source=str(raw.get("source", "")),
            confidence=str(raw.get("confidence", INDICATIVE)),
        )

    def label(self) -> str:
        """How the event is written wherever a model or a human reads it."""
        if self.confidence == VERIFIED:
            return f"{self.date.isoformat()} {self.name} ({self.impact}, VERIFIED — {self.source})"
        if self.confidence == STALE:
            return (
                f"{self.name} ({self.impact}, STALE — last published "
                f"{self.date.isoformat()}, next date unknown)"
            )
        if self.confidence == MISSING:
            return f"{self.name} ({self.impact}, MISSING — no date from any source)"
        return (
            f"~{self.date.isoformat()} {self.name} ({self.impact}, INDICATIVE — "
            f"{self.source}, approximate, do not wait for it)"
        )


# --- FRED: the BLS / BEA / Census schedule --------------------------------

FRED_DATES_URL = "https://api.stlouisfed.org/fred/release/dates"

#: ``release_id`` -> (our name for it, impact). These are the releases whose
#: dates have actually appeared in our entry instructions. ISM is deliberately
#: absent: it is a private survey, FRED does not carry its schedule, and the
#: static rule for it stays INDICATIVE.
FRED_RELEASES: tuple[tuple[int, str, str], ...] = (
    (50, "Employment Situation (Nonfarm Payrolls)", HIGH_IMPACT),
    (10, "CPI (Consumer Price Index)", HIGH_IMPACT),
    (46, "PPI (Producer Price Index)", MEDIUM_IMPACT),
    (9, "Retail Sales", MEDIUM_IMPACT),
    (54, "PCE Price Index", HIGH_IMPACT),
    (180, "Initial Jobless Claims", MEDIUM_IMPACT),
)

FRED_TIMEOUT = 15


def _session(session: Any = None):
    if session is not None:
        return session
    import requests

    return requests.Session()


def fred_release_dates(
    as_of: date, end: date, session: Any = None, api_key: str | None = None
) -> tuple[list[MacroEvent], list[str], set[str]]:
    """The agencies' own dates for ``as_of..end``.

    Returns ``(events, failures, answered)``. ``answered`` is every release the
    schedule spoke about at all, including the ones it said nothing is due for —
    a 200 with an empty list is the agency saying "no release in that window",
    which is a fact, and a better answer than falling back to a rule that would
    print a date that is not real.

    As-of-safe by construction. FRED's realtime window is set to exactly the
    window we are reporting on, so the call cannot see a schedule revision
    published after the horizon it describes — a ``--date`` backfill gets the
    calendar that stood then, not today's.
    """
    key = api_key or env("FRED_API_KEY")
    if not key:
        return [], ["FRED_API_KEY not set — no authoritative release dates"], set()

    http = _session(session)
    events: list[MacroEvent] = []
    notes: list[str] = []
    answered: set[str] = set()
    for release_id, name, impact in FRED_RELEASES:
        try:
            response = http.get(
                FRED_DATES_URL,
                params={
                    "release_id": release_id,
                    "api_key": key,
                    "file_type": "json",
                    "include_release_dates_with_no_data": "true",
                    "sort_order": "asc",
                    "limit": 200,
                    "realtime_start": as_of.isoformat(),
                    "realtime_end": end.isoformat(),
                },
                timeout=FRED_TIMEOUT,
            )
            response.raise_for_status()
            rows = response.json().get("release_dates", [])
        except Exception as exc:  # noqa: BLE001 - one dead release is not a dead calendar
            log.info("FRED release %s unavailable: %s", release_id, exc)
            notes.append(f"{name}: FRED schedule unavailable ({str(exc)[:80]})")
            continue

        answered.add(name)
        dates = sorted({_parse_date(row.get("date")) for row in rows} - {None})
        forward = [d for d in dates if as_of <= d <= end]
        if forward:
            events += [MacroEvent(d, name, impact, FRED_SOURCE, VERIFIED) for d in forward]
        elif dates:
            # The schedule answered but has nothing forward in our window: what
            # we hold is when it last printed. That is a STALE fact, not a date.
            events.append(MacroEvent(max(dates), name, impact, FRED_SOURCE, STALE))
        # An empty answer means nothing is due in the window. Nothing to print,
        # and — because the release is in ``answered`` — nothing to guess at.
    return events, notes, answered


def _parse_date(raw: Any) -> date | None:
    try:
        return date.fromisoformat(str(raw)[:10])
    except (TypeError, ValueError):
        return None


# --- The Fed: FOMC meeting dates ------------------------------------------

FOMC_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
FOMC_TIMEOUT = 15

_FOMC_YEAR = re.compile(r'<a id="\d+">\s*(\d{4})\s+FOMC\s+Meetings?\s*</a>', re.I)
_FOMC_ROW = re.compile(
    r'fomc-meeting__month[^>]*>\s*<strong>\s*([A-Za-z]+(?:\s*/\s*[A-Za-z]+)?)\s*</strong>'
    r'.*?fomc-meeting__date[^>]*>\s*([0-9]{1,2}(?:\s*[-–]\s*[0-9]{1,2})?)\s*\*?\s*<',
    re.S | re.I,
)
_MONTHS = {
    m.lower(): i
    for i, m in enumerate(
        ("January", "February", "March", "April", "May", "June", "July", "August",
         "September", "October", "November", "December"),
        start=1,
    )
}


def parse_fomc_calendar(html: str) -> list[date]:
    """Decision days from the Fed's calendar page.

    A meeting is one or two days and the decision lands on the last of them,
    which is the only day that moves a price. A meeting spanning a month
    boundary is written "April/May 28-1", so the second month owns the second
    day.
    """
    out: list[date] = []
    panels = list(_FOMC_YEAR.finditer(html))
    for index, panel in enumerate(panels):
        year = int(panel.group(1))
        stop = panels[index + 1].start() if index + 1 < len(panels) else len(html)
        for row in _FOMC_ROW.finditer(html, panel.end(), stop):
            months = [m.strip().lower() for m in row.group(1).split("/")]
            days = [int(d) for d in re.split(r"[-–]", row.group(2))]
            month = _MONTHS.get(months[-1] if len(days) > 1 else months[0])
            if month is None:
                continue
            try:
                out.append(date(year, month, days[-1]))
            except ValueError:
                continue
    return sorted(set(out))


def fomc_meeting_dates(
    as_of: date, end: date, session: Any = None
) -> tuple[list[MacroEvent], list[str]]:
    """FOMC decision days inside the window, from the Fed's own calendar."""
    try:
        response = _session(session).get(
            FOMC_URL,
            timeout=FOMC_TIMEOUT,
            headers={"User-Agent": "daybreak-trading-agent research (contact via repo)"},
        )
        response.raise_for_status()
        meetings = parse_fomc_calendar(response.text)
    except Exception as exc:  # noqa: BLE001
        log.info("FOMC calendar unavailable: %s", exc)
        return [], [f"FOMC calendar unavailable ({str(exc)[:80]})"]

    if not meetings:
        return [], ["FOMC calendar page returned no parseable meeting dates"]
    return (
        [
            MacroEvent(d, "FOMC decision", HIGH_IMPACT, FOMC_SOURCE, VERIFIED)
            for d in meetings
            if as_of <= d <= end
        ],
        [],
    )


def horizon_end(as_of: date, horizon_days: int) -> date:
    return as_of + timedelta(days=horizon_days)
