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
back, finds a wait that rests on a non-VERIFIED date, and records the
suppression next to the plan. The paragraph is left as the model wrote it, for
the same reason the quoted-figure corrections are — an edited thesis is one
nobody can audit — but the instruction is marked as not to be followed.
"""

from __future__ import annotations

import re

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
    known = _ALIASES.get(event.name, ())
    return known or (event.name.lower(),)


def suppressed_gates(texts: dict[str, str], events: list[MacroEvent]) -> list[str]:
    """Waits that rest on a date we cannot stand behind.

    ``texts`` is ``{label: prose}`` — the same shape
    :func:`tradingagent.pipeline.trade_plan.plan_texts` produces. Returns one
    line per (label, release), ready to print beneath the plan.
    """
    out: list[str] = []
    gateable = {e.name for e in events if e.confidence == VERIFIED}
    listed = {e.name: e for e in events if e.confidence != VERIFIED}
    # Every release we can recognise by name and cannot stand behind today —
    # the ones on the calendar with a weak date, *and* the ones with no date at
    # all. A release the agency schedule said nothing about produces no event,
    # so matching only against ``events`` left "wait for the PPI print" — the
    # original defect — unsuppressed on a run where PPI simply was not due.
    ungateable = [
        (name, listed.get(name))
        for name in dict.fromkeys([*listed, *_ALIASES])
        if name not in gateable
    ]
    for label, text in texts.items():
        if not text:
            continue
        lowered = text.lower()
        for name, event in ungateable:
            hit = _gated_on(lowered, aliases(event) if event else _ALIASES.get(name, (name.lower(),)))
            if hit is None:
                continue
            out.append(
                f"{label} waits for {name}, but {_unstandable(event)}. {_RULE} The wait "
                f"is not part of the plan; the entry stands on the levels in the table."
            )
    return list(dict.fromkeys(out))


def _unstandable(event: MacroEvent | None) -> str:
    """Why this run may not let a reader wait for the release."""
    if event is None:
        return "this run holds no VERIFIED date for it in the reporting window"
    return f"that date is {event.confidence} ({_why(event)})"


_RULE = (
    "Only a VERIFIED date — one published by the issuing agency — may gate an entry."
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
