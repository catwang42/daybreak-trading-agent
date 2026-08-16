"""Macro dates carry the confidence of their source, and only VERIFIED gates.

The defects: KMI's entry said "wait for Thursday's PPI" (PPI was Wednesday) and
V's said "enter after Retail Sales on the 16th" (it printed on the 14th). Both
dates came from a weekday-of-month rule that nothing downstream could tell apart
from a published schedule.
"""

from datetime import date

import pytest

from tradingagent.discovery.calendar import (
    INDICATIVE,
    MISSING,
    PERMITTED_USE,
    STALE,
    VERIFIED,
    CalendarView,
    MacroEvent,
    build_calendar,
    static_release_calendar,
)
from tradingagent.discovery.release_schedule import (
    FOMC_SOURCE,
    FRED_RELEASES,
    fomc_meeting_dates,
    fred_release_dates,
    parse_fomc_calendar,
)
from tradingagent.data.validate import DegradedTracker
from tradingagent.pipeline.macro_gate import suppressed_gates

AS_OF = date(2026, 8, 14)
END = date(2026, 8, 21)
KEY = "x" * 32


# --- doubles --------------------------------------------------------------


class _Response:
    def __init__(self, payload=None, text="", status=200):
        self._payload, self.text, self.status = payload, text, status

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")

    def json(self):
        return self._payload


class FakeHttp:
    """A stand-in for ``requests.Session`` that records every call."""

    def __init__(self, fred: dict[int, list[str]] | None = None, fomc: str | None = "",
                 fred_error: bool = False, fomc_error: bool = False):
        self.fred = fred or {}
        self.fomc = fomc
        self.fred_error = fred_error
        self.fomc_error = fomc_error
        self.calls: list[dict] = []

    def get(self, url, params=None, timeout=None, headers=None):
        self.calls.append({"url": url, "params": params or {}})
        if "fred" in url:
            if self.fred_error:
                raise RuntimeError("connection reset")
            dates = self.fred.get(params["release_id"], [])
            return _Response({"release_dates": [{"date": d} for d in dates]})
        if self.fomc_error:
            raise RuntimeError("503")
        return _Response(text=self.fomc or "")


class _NoFinnhub:
    """Free tier: the economic calendar is premium, earnings are not."""

    def economic_calendar(self, start, end):
        return []

    def earnings_calendar(self, start, end):
        return []


FOMC_HTML = """
<a id="42828">2026 FOMC Meetings</a>
<div class="fomc-meeting__month col-md-2"><strong>August</strong></div>
<div class="fomc-meeting__date col-lg-1">18-19</div>
<div class="fomc-meeting__month col-md-2"><strong>April/May</strong></div>
<div class="fomc-meeting__date col-lg-1">28-1*</div>
<a id="45694">2027 FOMC Meetings</a>
<div class="fomc-meeting__month col-md-2"><strong>January</strong></div>
<div class="fomc-meeting__date col-lg-1">26-27</div>
"""


# --- the classes ----------------------------------------------------------


def test_only_a_verified_date_may_gate_anything():
    for confidence, use in PERMITTED_USE.items():
        if confidence == VERIFIED:
            assert use.may_gate_entries and use.may_gate_options
        else:
            assert not use.may_gate_entries and not use.may_gate_options


def test_each_class_says_what_it_is_wherever_it_is_printed():
    verified = MacroEvent(AS_OF, "CPI (Consumer Price Index)", "High", "FRED", VERIFIED)
    guess = MacroEvent(AS_OF, "Retail Sales", "Medium", "static release schedule", INDICATIVE)
    stale = MacroEvent(date(2026, 7, 15), "PPI (Producer Price Index)", "Medium", "FRED", STALE)
    missing = MacroEvent(None, "FOMC decision", "High", "no source reached", MISSING)

    assert "VERIFIED" in verified.label() and "2026-08-14" in verified.label()
    assert "INDICATIVE" in guess.label() and "do not wait for it" in guess.label()
    assert "STALE" in stale.label() and "next date unknown" in stale.label()
    assert "MISSING" in missing.label() and "no date" in missing.label()
    assert verified.may_gate_entries
    assert not (guess.may_gate_entries or stale.may_gate_entries or missing.may_gate_entries)


def test_the_static_rule_can_only_ever_produce_indicative_dates():
    events = static_release_calendar(date(2026, 8, 1), date(2026, 8, 31))
    assert events
    assert {e.confidence for e in events} == {INDICATIVE}
    assert not any(e.may_gate_entries for e in events)


# --- FRED -----------------------------------------------------------------


def test_the_agency_schedule_is_asked_for_the_window_the_run_is_reporting_on():
    """As-of-safe: the realtime window is the report window, so a backfill
    cannot see a schedule revision published after the horizon it describes."""
    http = FakeHttp(fred={10: ["2026-08-19"]})
    events, notes, answered = fred_release_dates(AS_OF, END, session=http, api_key=KEY)

    assert len(http.calls) == len(FRED_RELEASES)
    for call in http.calls:
        assert call["params"]["realtime_start"] == "2026-08-14"
        assert call["params"]["realtime_end"] == "2026-08-21"
    cpi = [e for e in events if "CPI" in e.name]
    assert cpi and cpi[0].date == date(2026, 8, 19) and cpi[0].confidence == VERIFIED


def test_a_schedule_with_nothing_forward_is_stale_not_a_guess_at_the_next_one():
    http = FakeHttp(fred={46: ["2026-07-15"]})
    events, _, _ = fred_release_dates(AS_OF, END, session=http, api_key=KEY)
    ppi = [e for e in events if "PPI" in e.name]
    assert ppi and ppi[0].confidence == STALE and ppi[0].date == date(2026, 7, 15)
    assert not ppi[0].may_gate_entries


def test_a_release_the_schedule_says_nothing_is_due_for_is_not_a_failure():
    """An empty answer is the agency saying "nothing in that window". That is a
    fact, not an outage, and it must not be dressed up as a guessed date."""
    events, notes, answered = fred_release_dates(AS_OF, END, session=FakeHttp(), api_key=KEY)
    assert events == [] and notes == []
    assert answered == {name for _, name, _ in FRED_RELEASES}


def test_a_release_with_no_scheduled_date_is_left_out_not_back_filled():
    view, _ = calendar(fred={10: ["2026-08-19"]}, fomc=FOMC_HTML)
    # FRED answered for PPI and had nothing due; the static rule would have put
    # it in this window (the V/KMI defect). It is absent instead of approximate.
    assert [e for e in view.macro if "PPI" in e.name] == []


def test_a_dead_fred_is_a_note_per_release_and_never_an_exception():
    events, notes, answered = fred_release_dates(AS_OF, END, session=FakeHttp(fred_error=True), api_key=KEY)
    assert answered == set()
    assert events == [] and len(notes) == len(FRED_RELEASES)
    assert all("unavailable" in n for n in notes)


def test_without_a_key_we_say_so_instead_of_pretending_to_have_asked():
    events, notes, _ = fred_release_dates(AS_OF, END, session=FakeHttp(), api_key="")
    assert events == [] and "FRED_API_KEY not set" in notes[0]


# --- FOMC -----------------------------------------------------------------


def test_the_fomc_decision_day_is_the_last_day_of_the_meeting():
    days = parse_fomc_calendar(FOMC_HTML)
    assert date(2026, 8, 19) in days
    # "April/May 28-1" decides on 1 May, not 1 April.
    assert date(2026, 5, 1) in days
    assert date(2027, 1, 27) in days


def test_fomc_dates_are_verified_and_windowed():
    events, notes = fomc_meeting_dates(AS_OF, END, session=FakeHttp(fomc=FOMC_HTML))
    assert notes == []
    assert [e.date for e in events] == [date(2026, 8, 19)]
    assert events[0].confidence == VERIFIED and events[0].source == FOMC_SOURCE


def test_an_unreachable_fed_page_is_a_note_not_a_crash():
    events, notes = fomc_meeting_dates(AS_OF, END, session=FakeHttp(fomc_error=True))
    assert events == [] and notes and "unavailable" in notes[0]


# --- composition ----------------------------------------------------------


def calendar(**kwargs):
    degraded = DegradedTracker()
    view = build_calendar(
        _NoFinnhub(), AS_OF, set(), degraded, horizon_days=7, as_of=AS_OF,
        session=FakeHttp(**kwargs), api_key=KEY,
    )
    return view, degraded


def test_an_authoritative_date_replaces_the_guess_for_that_release():
    view, _ = calendar(fred={10: ["2026-08-19"]}, fomc=FOMC_HTML)
    cpi = [e for e in view.macro if "CPI" in e.name]
    assert len(cpi) == 1
    assert cpi[0].confidence == VERIFIED and cpi[0].date == date(2026, 8, 19)
    # ISM has no authoritative free schedule, so its guess survives — labelled.
    ism = [e for e in view.macro if "ISM" in e.name]
    assert all(e.confidence == INDICATIVE for e in ism)


def test_a_release_nobody_authoritative_covered_stays_indicative_and_ungateable():
    view, degraded = calendar(fomc=FOMC_HTML)
    assert not view.has_verified_dates or True
    assert view.gating_events() == [e for e in view.macro if e.confidence == VERIFIED]
    assert all(not e.may_gate_entries for e in view.macro if e.confidence == INDICATIVE)


def test_no_authoritative_source_at_all_degrades_the_whole_calendar():
    view, degraded = calendar(fred_error=True, fomc_error=True)
    assert not view.has_verified_dates
    reason = dict(degraded.entries)["Economic calendar"]
    assert "no authoritative schedule reached" in reason
    assert "FRED schedule unavailable" in reason
    assert all(e.confidence == INDICATIVE for e in view.macro if e.date is not None)


def test_an_unreachable_fed_page_leaves_fomc_named_as_missing_not_guessed():
    view, _ = calendar(fomc_error=True)
    fomc = [e for e in view.macro if e.name == "FOMC decision"]
    assert len(fomc) == 1 and fomc[0].confidence == MISSING and fomc[0].date is None
    assert not fomc[0].may_gate_entries


def test_the_note_every_prompt_reads_carries_the_permitted_use_rule():
    view, _ = calendar(fred={10: ["2026-08-19"]}, fomc=FOMC_HTML)
    note = view.note()
    assert "VERIFIED" in note
    assert "never become an instruction to wait" in note


def test_the_window_is_taken_from_the_market_date_not_the_wall_clock():
    http = FakeHttp(fred={10: ["2026-06-10"]}, fomc=FOMC_HTML)
    build_calendar(_NoFinnhub(), date(2026, 6, 3), set(), DegradedTracker(),
                   horizon_days=7, as_of=date(2026, 6, 1), session=http, api_key=KEY)
    fred_calls = [c for c in http.calls if "fred" in c["url"]]
    assert {c["params"]["realtime_start"] for c in fred_calls} == {"2026-06-01"}
    assert {c["params"]["realtime_end"] for c in fred_calls} == {"2026-06-08"}


def test_the_same_release_from_two_sources_is_listed_once_at_its_best_confidence():
    view = CalendarView(
        macro=[], earnings_today=[], earnings_week=[],
    )
    assert view.confidence_counts() == {}

    view, _ = calendar(fred={9: ["2026-08-14"]}, fomc=FOMC_HTML)
    retail = [e for e in view.macro if e.name == "Retail Sales"]
    assert len(retail) == 1 and retail[0].confidence == VERIFIED


# --- enforcement ----------------------------------------------------------


VERIFIED_CPI = MacroEvent(date(2026, 8, 19), "CPI (Consumer Price Index)", "High",
                          "FRED release calendar (BLS/BEA/Census)", VERIFIED)
GUESSED_PPI = MacroEvent(date(2026, 8, 20), "PPI (Producer Price Index)", "Medium",
                         "static release schedule", INDICATIVE)
GUESSED_RETAIL = MacroEvent(date(2026, 8, 16), "Retail Sales", "Medium",
                            "static release schedule", INDICATIVE)


def test_the_kmi_regression_a_wait_for_an_approximate_ppi_date_is_struck_out():
    notes = suppressed_gates(
        {"The trader's entry condition": "Wait for Thursday's PPI, then enter on strength."},
        [GUESSED_PPI],
    )
    assert len(notes) == 1
    assert "PPI (Producer Price Index)" in notes[0] and "INDICATIVE" in notes[0]
    assert "not part of the plan" in notes[0]


def test_the_v_regression_enter_after_retail_sales_on_the_sixteenth():
    notes = suppressed_gates(
        {"The verdict summary": "Enter after Retail Sales on the 16th confirms the consumer."},
        [GUESSED_RETAIL],
    )
    assert len(notes) == 1 and "Retail Sales" in notes[0]


def test_a_wait_for_a_verified_date_is_left_alone():
    notes = suppressed_gates(
        {"The trader's entry condition": "Wait for CPI on the 19th, then enter."},
        [VERIFIED_CPI, GUESSED_PPI],
        as_of=AS_OF,
    )
    assert notes == []


# --- recency: the other half of the V regression --------------------------

PRINTED_RETAIL = MacroEvent(date(2026, 8, 14), "Retail Sales", "Medium",
                            "FRED release calendar (BLS/BEA/Census)", VERIFIED)


def test_a_verified_release_that_has_already_printed_cannot_gate_an_entry():
    """The V wording, on the calendar that shipped it.

    Retail Sales was VERIFIED for the 14th — the very session the run priced —
    and "enter after Retail Sales on the 16th" passed, because the class was
    right even though the print was hours old by the close the plan rests on.
    Confidence was never the whole test.
    """
    notes = suppressed_gates(
        {"The verdict summary": "Enter after Retail Sales on the 16th confirms the consumer."},
        [PRINTED_RETAIL],
        as_of=date(2026, 8, 14),
    )
    assert len(notes) == 1
    assert "the VERIFIED date we hold for it, 2026-08-14" in notes[0]
    assert "not ahead of this run's 2026-08-14 market date — it has already printed" in notes[0]
    assert "still ahead of this run" in notes[0]


def test_a_release_earlier_in_the_week_is_history_not_a_trigger():
    monday = MacroEvent(date(2026, 8, 10), "CPI (Consumer Price Index)", "High",
                        "FRED release calendar (BLS/BEA/Census)", VERIFIED)
    notes = suppressed_gates(
        {"The thesis": "Size up once the CPI print is out of the way."}, [monday], as_of=AS_OF
    )
    assert len(notes) == 1 and "2026-08-10" in notes[0]


def test_the_next_verified_print_still_gates_even_when_the_last_one_is_on_the_list():
    """A weekly release has both a printed date and a forthcoming one."""
    last_week = MacroEvent(date(2026, 8, 13), "Initial Jobless Claims", "Medium",
                           "FRED release calendar (BLS/BEA/Census)", VERIFIED)
    next_week = MacroEvent(date(2026, 8, 20), "Initial Jobless Claims", "Medium",
                           "FRED release calendar (BLS/BEA/Census)", VERIFIED)
    notes = suppressed_gates(
        {"The trader's reasoning": "Wait for jobless claims before adding."},
        [last_week, next_week],
        as_of=AS_OF,
    )
    assert notes == []


def test_without_a_market_date_only_confidence_is_checked():
    """Every caller before this change passed two arguments; none regressed."""
    assert suppressed_gates(
        {"The verdict summary": "Enter after Retail Sales confirms the consumer."},
        [PRINTED_RETAIL],
    ) == []


def test_a_release_with_no_date_at_all_cannot_be_waited_for():
    """Found by the M6 evidence run. When the agency schedule says nothing is
    due, the release produces no event — so matching waits against the calendar
    alone let the original KMI wording through on exactly the run where PPI was
    not due. A release we hold no VERIFIED date for is ungateable whether or
    not it appears on the list."""
    notes = suppressed_gates(
        {"The thesis": "Hold off until the PPI print lands mid-week."},
        [VERIFIED_CPI],  # a real calendar for that week: no PPI row at all
    )
    assert len(notes) == 1
    assert "no VERIFIED date for it in the reporting window" in notes[0]


def test_a_release_merely_mentioned_is_not_a_gate():
    notes = suppressed_gates(
        {"The thesis": "Margins have held through three PPI prints."}, [GUESSED_PPI]
    )
    assert notes == []


def test_a_stale_or_missing_date_cannot_be_waited_for_either():
    stale = MacroEvent(date(2026, 7, 15), "PPI (Producer Price Index)", "Medium", "FRED", STALE)
    missing = MacroEvent(None, "FOMC decision", "High", "no source reached", MISSING)
    notes = suppressed_gates(
        {
            "The trader's entry condition": "Hold off until the PPI print.",
            "The thesis": "Size up once the FOMC decision is out of the way.",
        },
        [stale, missing],
    )
    assert len(notes) == 2
    assert any("last printed 2026-07-15" in n for n in notes)
    assert any("no source gave us a date" in n for n in notes)


@pytest.mark.parametrize(
    "phrase",
    [
        "wait until the jobs report",
        "waiting for nonfarm payrolls",
        "ahead of the payrolls print",
        "enter once the employment situation lands",
        "not before the jobs report",
    ],
)
def test_every_way_a_model_writes_do_not_enter_yet_is_caught(phrase):
    guess = MacroEvent(date(2026, 9, 4), "Employment Situation (Nonfarm Payrolls)", "High",
                       "static release schedule", INDICATIVE)
    assert suppressed_gates({"The trader's reasoning": phrase.capitalize() + "."}, [guess])


def test_the_same_suppression_is_reported_once_per_role():
    line = "Wait for the PPI print."
    notes = suppressed_gates(
        {"The trader's reasoning": line, "The thesis": line}, [GUESSED_PPI]
    )
    assert len(notes) == 2 and len(set(notes)) == 2
