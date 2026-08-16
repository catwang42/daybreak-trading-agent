"""Resolution at maturity — what the market did after each decision.

The journal has carried ``outcome_7d: null`` on every line since M1. This is
the job that fills it in, and it is deliberately a separate stage rather than a
tail on the daily run: the answer to "was the 2026-08-16 call right" does not
exist on 2026-08-16, and a pipeline that pretends otherwise either look-aheads
or writes nothing.

Four choices, each of which changes the numbers:

**Reference close.** The close of the first session at or after the decision
date. The brief lands pre-market; the earliest honest fill is that session's
close. Measuring from the *prior* close — the snapshot the analysis read —
would credit the research with a move that had already happened before anyone
could act on it.

**Excess, not raw.** A 3% gain in a week the market rose 4% is a bad call. Every
horizon carries the raw return, the excess over SPY, and the excess over the
name's sector ETF, because a technology pick in a technology rally is mostly a
sector bet and the sector line is what says so.

**Trading days, not calendar days.** ``5`` means five sessions, counted in the
bar index. Calendar arithmetic silently shortens every window containing a
holiday.

**As-of safety.** Horizons resolve only when their end session exists at or
before the market date of a snapshot built for this job. There is no
``date.today()`` anywhere in this module: a horizon that has not matured is
absent from the record, never zero and never extrapolated.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import pandas as pd

from ..data.universe import SECTOR_ETFS, normalize_sector
from ..snapshot import ResearchSnapshot
from .ledger import OutcomeRecord
from .provenance import Provenance

log = logging.getLogger(__name__)

#: Trading days. 1 and 5 grade the entry, 10 and 20 the thesis, 60 the horizon
#: the portfolio manager actually writes ("4-8 weeks" is 20-40 sessions).
HORIZONS: tuple[int, ...] = (1, 5, 10, 20, 60)

BENCHMARK = "SPY"


def sector_etf(sector: str) -> str:
    """The sector's ETF, or "" when the sector is unknown to the map."""
    return SECTOR_ETFS.get(normalize_sector(sector or ""), "")


def reference_index(frame: pd.DataFrame, decision_date: date) -> int | None:
    """Row position of the first session at or after the decision date."""
    sessions = _sessions(frame)
    for i, session in enumerate(sessions):
        if session >= decision_date:
            return i
    return None


def _sessions(frame: pd.DataFrame) -> list[date]:
    index = frame.index
    if isinstance(index, pd.DatetimeIndex):
        return [ts.date() for ts in index]
    return [pd.Timestamp(value).date() for value in index]


def _pct(start: float, end: float) -> float:
    return (end / start - 1.0) * 100.0 if start else 0.0


def _close(frame: pd.DataFrame, position: int) -> float | None:
    try:
        value = float(frame["Close"].iloc[position])
    except (IndexError, KeyError, TypeError, ValueError):
        return None
    return value if value == value and value > 0 else None  # NaN-safe


@dataclass
class Resolution:
    """One decision's resolution, plus why anything was left out."""

    record: OutcomeRecord | None = None
    notes: list[str] = field(default_factory=list)


def resolve(
    decision: dict[str, Any],
    frame: pd.DataFrame,
    snapshot: ResearchSnapshot,
    *,
    benchmark: pd.DataFrame | None = None,
    sector: pd.DataFrame | None = None,
    provenance: Provenance | None = None,
) -> Resolution:
    """Price one decision against bars. Returns an empty resolution if nothing matured."""
    decision_date = _parse_date(decision.get("date"))
    ticker = str(decision.get("ticker", ""))
    if decision_date is None or frame is None or frame.empty:
        return Resolution(notes=[f"{ticker}: no usable bars"])

    start = reference_index(frame, decision_date)
    reference = _close(frame, start) if start is not None else None
    if start is None or reference is None:
        return Resolution(notes=[f"{ticker}: no session on or after {decision_date}"])

    sessions = _sessions(frame)
    if not snapshot.check(f"{ticker} reference close", sessions[start]):
        return Resolution(notes=[f"{ticker}: reference session is after the snapshot"])

    etf = str(decision.get("sector_etf") or sector_etf(str(decision.get("sector", ""))))
    horizons: dict[str, dict[str, Any]] = {}
    notes: list[str] = []
    longest = 0

    for horizon in HORIZONS:
        end = start + horizon
        if end >= len(sessions):
            continue
        if sessions[end] > snapshot.market_as_of:
            continue
        # A bar dated after the snapshot is look-ahead however it got here; the
        # snapshot records the violation and the horizon is dropped.
        if not snapshot.check(f"{ticker} +{horizon}d close", sessions[end]):
            continue
        close = _close(frame, end)
        if close is None:
            notes.append(f"{ticker}: +{horizon}d close unusable")
            continue
        row: dict[str, Any] = {
            "session": sessions[end].isoformat(),
            "return_pct": round(_pct(reference, close), 3),
        }
        spy_move = _benchmark_move(benchmark, sessions[start], sessions[end])
        if spy_move is not None:
            row["excess_spy_pct"] = round(row["return_pct"] - spy_move, 3)
        sector_move = _benchmark_move(sector, sessions[start], sessions[end])
        if sector_move is not None:
            row["excess_sector_pct"] = round(row["return_pct"] - sector_move, 3)
        horizons[str(horizon)] = row
        longest = horizon

    if not horizons:
        return Resolution(notes=[f"{ticker}: nothing matured as of {snapshot.market_as_of}"])

    if benchmark is None:
        notes.append("no SPY bars: excess vs the market is unavailable")
    if etf and sector is None:
        notes.append(f"no {etf} bars: excess vs the sector is unavailable")
    if not etf:
        notes.append(f"sector '{decision.get('sector', '')}' has no ETF in the map")

    window = frame.iloc[start : start + longest + 1]
    plan = decision.get("trade_plan") or {}
    direction = str(plan.get("direction") or _direction_for(str(decision.get("rating", ""))))
    mfe, mae = excursions(window, reference, direction)
    triggers = entry_status(window, plan, direction)

    return Resolution(
        record=OutcomeRecord(
            provenance=provenance or Provenance(run_id="", run_date=""),
            decision_id=str(decision.get("decision_id", "")),
            ticker=ticker,
            date=decision_date.isoformat(),
            stage=str(decision.get("stage", "")),
            as_of=snapshot.market_as_of.isoformat(),
            reference_close=round(reference, 4),
            benchmark=BENCHMARK,
            sector_etf=etf,
            horizons=horizons,
            mfe_pct=mfe,
            mae_pct=mae,
            excursion_window=longest,
            **triggers,
            notes=notes,
        ),
        notes=notes,
    )


def _benchmark_move(frame: pd.DataFrame | None, start: date, end: date) -> float | None:
    """The benchmark's return between the same two *sessions*, not the same rows.

    Matching on dates rather than on row offsets matters when a name was halted
    or newly listed: its index and SPY's would otherwise drift apart and the
    excess would silently compare different weeks.
    """
    if frame is None or frame.empty:
        return None
    sessions = _sessions(frame)
    try:
        i, j = sessions.index(start), sessions.index(end)
    except ValueError:
        return None
    a, b = _close(frame, i), _close(frame, j)
    return _pct(a, b) if a is not None and b is not None else None


def _direction_for(rating: str) -> str:
    from ..pipeline.trade_plan import direction_for

    return direction_for(rating)


def excursions(window: pd.DataFrame, reference: float, direction: str) -> tuple[float | None, float | None]:
    """Best and worst excursion from the reference close, signed by direction.

    A short that fell 6% before rallying had a +6% favourable excursion, not a
    -6% one. Reporting both sides unsigned would make every short look like a
    loss and every long like a win.
    """
    if window.empty or not reference:
        return None, None
    try:
        high = float(window["High"].max())
        low = float(window["Low"].min())
    except (KeyError, TypeError, ValueError):
        return None, None
    if high != high or low != low:  # NaN
        return None, None
    up, down = _pct(reference, high), _pct(reference, low)
    if direction == "short":
        return round(-down, 3), round(-up, 3)
    return round(up, 3), round(down, 3)


def entry_status(window: pd.DataFrame, plan: dict[str, Any], direction: str) -> dict[str, Any]:
    """Did the published entry trade, and did the stop or the target come first?

    ``None`` throughout when the plan carried no such level — a NO TRADE is not
    an entry that failed to trigger, and conflating the two would make the
    trigger rate meaningless.

    A bar that spans both the stop and the target is scored as the stop. Daily
    bars cannot say which came first intraday, and the assumption that costs
    money is the honest one to make about your own research.
    """
    entry = _number(plan.get("entry"))
    stop = _number(plan.get("stop"))
    target = _number(plan.get("target"))
    blank: dict[str, Any] = {
        "entry_triggered": None, "stop_hit": None, "target_hit": None, "first_hit": ""
    }
    if entry is None or direction not in {"long", "short"} or window.empty:
        return blank
    if "Low" not in window or "High" not in window:
        return blank

    lows = [float(v) for v in window["Low"].tolist()]
    highs = [float(v) for v in window["High"].tolist()]

    triggered_at: int | None = None
    for i, (low, high) in enumerate(zip(lows, highs)):
        if (direction == "long" and low <= entry) or (direction == "short" and high >= entry):
            triggered_at = i
            break
    if triggered_at is None:
        return {"entry_triggered": False, "stop_hit": False, "target_hit": False, "first_hit": ""}

    out: dict[str, Any] = {"entry_triggered": True, "stop_hit": False, "target_hit": False,
                           "first_hit": ""}
    for low, high in zip(lows[triggered_at:], highs[triggered_at:]):
        stopped = stop is not None and (
            (direction == "long" and low <= stop) or (direction == "short" and high >= stop)
        )
        hit = target is not None and (
            (direction == "long" and high >= target) or (direction == "short" and low <= target)
        )
        if stopped:
            out["stop_hit"] = True
            out["first_hit"] = out["first_hit"] or "stop"
            break
        if hit:
            out["target_hit"] = True
            out["first_hit"] = out["first_hit"] or "target"
            break
    if stop is None:
        out["stop_hit"] = None
    if target is None:
        out["target_hit"] = None
    return out


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and number > 0 else None


def _parse_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def is_complete(row: dict[str, Any]) -> bool:
    """True when every horizon has resolved and there is nothing left to do."""
    return len(row.get("horizons") or {}) >= len(HORIZONS)


def symbols_to_price(decisions: list[dict[str, Any]]) -> list[str]:
    """Every ticker plus SPY plus the sector ETFs the decisions actually need."""
    tickers = {str(d.get("ticker", "")).upper() for d in decisions if d.get("ticker")}
    etfs = {sector_etf(str(d.get("sector", ""))) for d in decisions}
    return sorted(tickers | {e for e in etfs if e} | {BENCHMARK})
