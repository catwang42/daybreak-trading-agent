"""Stop an approximate release date from becoming an instruction to wait.

Two reports told a reader to hold off until a macro print: KMI "wait for
Thursday's PPI", V "enter after Retail Sales on the 16th". Neither date was a
schedule. Both came from
:func:`tradingagent.discovery.calendar.static_release_calendar`, a
weekday-of-month rule that is right to within a few days — which is fine as
background and useless as a trigger. PPI was on the Wednesday; Retail Sales had
printed on the 14th.

The confidence classes in :mod:`tradingagent.discovery.release_schedule` say
which dates may be used that way. This module enforces it: it reads the prose
back, finds a wait that rests on a date we cannot stand behind, and records the
suppression next to the plan. The paragraph is left as the model wrote it, for
the same reason the quoted-figure corrections are — an edited thesis is one
nobody can audit — but the instruction is marked as not to be followed.

A date has to pass two tests, not one. The first is confidence: only the issuing
agency's own schedule may gate anything. The second is recency, and it was the
half the V report actually failed — Retail Sales was VERIFIED for the 14th, the
run priced the 14th close, and "enter after Retail Sales on the 16th" survived
because the class was right even though the print was already in the price. A
macro release lands in the morning; a date on or before the run's market date is
history, and history cannot be waited for.
"""

from __future__ import annotations

import re
from datetime import date

from ..discovery.release_schedule import VERIFIED, MacroEvent

#: Ways a model writes "do not enter yet". Deliberately broad: a false positive
#: costs one printed line, a false negative costs a trade taken on a wrong date.
_WAIT_WORDS = (
    r"wait(?:ing)?\s+(?:for|until|till|on)",
    r"hold(?:ing)?\s+off\s+(?:for|until|till)",
    r"after\s+(?:the\s+)?(?:release|print|report)?",
    r"ahead\s+of",
    r"until\s+after",
    r"post[- ]",
    r"pending",
    r"once\s+(?:the\s+)?",
    r"not\s+before",
)
_WAIT = re.compile("|".join(_WAIT_WORDS), re.I)

#: How far either side of the wait phrase the release name has to appear for
#: the two to be the same instruction.
_WINDOW = 90

#: Words a reader would use for these releases, beyond the formal name.
_ALIASES: dict[str, tuple[str, ...]] = {
    "CPI (Consumer Price Index)": ("cpi", "consumer price index", "inflation print"),
    "PPI (Producer Price Index)": ("ppi", "producer price index"),
    "Employment Situation (Nonfarm Payrolls)": (
        "nonfarm", "non-farm", "payrolls", "jobs report", "employment situation",
    ),
    "Retail Sales": ("retail sales",),
    "PCE Price Index": ("pce",),
    "Initial Jobless Claims": ("jobless claims", "initial claims", "weekly claims"),
    "ISM Manufacturing PMI": ("ism manufacturing", "ism manufacturing pmi"),
    "ISM Services PMI": ("ism services", "ism non-manufacturing"),
    "FOMC decision": ("fomc", "fed decision", "fed meeting", "rate decision"),
}


def aliases(event: MacroEvent) -> tuple[str, ...]:
    return alias_words(event.name)


def alias_words(name: str) -> tuple[str, ...]:
    known = _ALIASES.get(name, ())
    return known or (name.lower(),)


def may_gate(event: MacroEvent, as_of: date | None = None) -> bool:
    """Can this run let a reader wait for ``event``?

    VERIFIED, *and* still ahead of the run. ``as_of`` is the market date the
    plan is priced against — the close of that session — so a release dated on
    it printed hours before the price the entry rests on. Without an ``as_of``
    only confidence is checked, which is the behaviour every caller had before.
    """
    if event.confidence != VERIFIED:
        return False
    if as_of is None or event.date is None:
        return True
    return event.date > as_of


def suppressed_gates(
    texts: dict[str, str], events: list[MacroEvent], as_of: date | None = None
) -> list[str]:
    """Waits that rest on a date we cannot stand behind.

    ``texts`` is ``{label: prose}`` — the same shape
    :func:`tradingagent.pipeline.trade_plan.plan_texts` produces. ``as_of`` is
    the run's market date. Returns one line per (label, release), ready to print
    beneath the plan.
    """
    out: list[str] = []
    gateable = {e.name for e in events if may_gate(e, as_of)}
    listed: dict[str, list[MacroEvent]] = {}
    for event in events:
        listed.setdefault(event.name, []).append(event)
    # Every release we can recognise by name and cannot stand behind today —
    # the ones on the calendar with a weak date, the ones whose only date has
    # already printed, *and* the ones with no date at all. A release the agency
    # schedule said nothing about produces no event, so matching only against
    # ``events`` left "wait for the PPI print" — the original defect —
    # unsuppressed on a run where PPI simply was not due.
    ungateable = [name for name in dict.fromkeys([*listed, *_ALIASES]) if name not in gateable]
    for label, text in texts.items():
        if not text:
            continue
        lowered = text.lower()
        for name in ungateable:
            if _gated_on(lowered, alias_words(name)) is None:
                continue
            out.append(
                f"{label} waits for {name}, but {_unstandable(listed.get(name, []), as_of)}. "
                f"{_RULE} The wait is not part of the plan; the entry stands on the levels "
                f"in the table."
            )
    return list(dict.fromkeys(out))


def _unstandable(candidates: list[MacroEvent], as_of: date | None) -> str:
    """Why this run may not let a reader wait for the release."""
    if not candidates:
        return "this run holds no VERIFIED date for it in the reporting window"
    printed = [e for e in candidates if e.confidence == VERIFIED and e.date is not None]
    if printed and as_of is not None:
        last = max(printed, key=lambda e: e.date)
        return (
            f"the VERIFIED date we hold for it, {last.date.isoformat()}, is not ahead of "
            f"this run's {as_of.isoformat()} market date — it has already printed"
        )
    event = candidates[0]
    return f"that date is {event.confidence} ({_why(event)})"


_RULE = (
    "Only a VERIFIED date — one published by the issuing agency, and still ahead of this "
    "run — may gate an entry."
)


def _why(event: MacroEvent) -> str:
    if event.confidence == "INDICATIVE":
        return f"{event.source}, approximate to within a few days"
    if event.confidence == "STALE":
        last = event.date.isoformat() if event.date else "unknown"
        return f"we know it last printed {last}, not when it next will"
    return "no source gave us a date"


def _gated_on(lowered: str, names: tuple[str, ...]) -> str | None:
    """Is one of ``names`` inside a waiting phrase?"""
    for match in _WAIT.finditer(lowered):
        window = lowered[match.start(): match.end() + _WINDOW]
        for name in names:
            if name in window:
                return name
    return None
