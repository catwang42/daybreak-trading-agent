"""Turn a stage's artefacts into ledger rows.

Kept out of :mod:`tradingagent.stages` for the same reason the journal's
``entries_from_*`` helpers are: the stage should hand over what it produced and
not know the shape of the file it lands in. It also means these can be tested
against hand-built candidates and results without running a pipeline.

Nothing here raises. A ledger that can abort a run has inverted the priority —
the recommendation is the product, the record of it is the instrument, and an
instrument that breaks the thing it measures is worse than no instrument.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any, Iterable

from .ledger import CandidateRecord, DecisionRecord, RunRecord
from .provenance import Provenance

log = logging.getLogger(__name__)


def run_record(
    provenance: Provenance,
    stage: str,
    *,
    started_at: str = "",
    universe_size: int = 0,
    candidates: int = 0,
    shortlisted: int = 0,
    queued: int = 0,
    degraded: Iterable[str] = (),
    notes: Iterable[str] = (),
) -> RunRecord:
    return RunRecord(
        provenance=provenance,
        stage=stage,
        started_at=started_at,
        universe_size=universe_size,
        candidates=candidates,
        shortlisted=shortlisted,
        queued=queued,
        degraded=list(degraded),
        notes=list(notes),
    )


def candidate_records(
    provenance: Provenance,
    run_date: date,
    candidates: list,
    *,
    eligible: list | None = None,
    shortlist: list | None = None,
    hub=None,
    queued: Iterable[str] = (),
) -> list[CandidateRecord]:
    """One row per screened name — the whole pool, not the shortlist.

    The pool is the experiment. A day where the screener ranked 43 names and
    we deep-dived 5 is 43 observations of the ranking and 5 of the pipeline;
    recording only the 5 throws away the comparison that says whether the rank
    ordering meant anything.

    ``selected`` (the shipped, price-only shortlist) and
    ``counterfactual_selected`` (what the shadowed signal layer would have
    picked) are the control and treatment of item 4. They are recorded from the
    same run because the signals are shadowed: no second, paid-for control arm
    is needed while every source's weight is zero.
    """
    eligible_symbols = {c.symbol for c in (eligible if eligible is not None else candidates)}
    shortlist = shortlist or []
    bundles = {e.symbol: e.signals for e in shortlist if getattr(e, "signals", None)}
    ranks = {e.symbol: e.screen_rank for e in shortlist}
    chosen = {e.symbol for e in shortlist}
    counterfactual = set(getattr(getattr(hub, "shadow", None), "shadow", []) or [])
    queued_set = {s for s in queued}

    # Final rank is the order selection actually used: screener score plus
    # whatever the signal layer had earned the right to add, which is zero for
    # every source today. Computed over the eligible pool, since a name the
    # sector cap removed was never in the running whatever its score.
    ordered = sorted(
        (c for c in candidates if c.symbol in eligible_symbols),
        key=lambda c: c.score + _adjustment(bundles.get(c.symbol)),
        reverse=True,
    )
    final_rank = {c.symbol: i + 1 for i, c in enumerate(ordered)}

    rows: list[CandidateRecord] = []
    for index, candidate in enumerate(candidates):
        symbol = candidate.symbol
        bundle = bundles.get(symbol)
        if bundle is None and hub is not None:
            # Names outside the shortlist have no bundle attached, but the hub
            # already fetched over the wider signal pool — asking it costs
            # nothing and is the difference between grading a source on 8 rows
            # a day and on 16.
            try:
                bundle = hub.bundle(symbol, run_date)
            except Exception as exc:  # noqa: BLE001 - a record is never worth a run
                log.debug("No signal bundle for %s: %s", symbol, exc)
                bundle = None
        rows.append(
            CandidateRecord(
                provenance=provenance,
                ticker=symbol,
                date=run_date.isoformat(),
                screener_score=getattr(candidate, "score", 0),
                screen_rank=ranks.get(symbol, index + 1),
                final_rank=final_rank.get(symbol, 0),
                signal_adjustment=_adjustment(bundle),
                shadow_adjustment=_shadow(bundle),
                per_signal_shadow=(bundle.per_source_shadow() if bundle else {}),
                signal_readings=(bundle.readings() if bundle else {}),
                sector=getattr(candidate, "sector", ""),
                eligible=symbol in eligible_symbols,
                selected=symbol in chosen,
                counterfactual_selected=symbol in counterfactual,
                queued=symbol in queued_set,
            )
        )
    return rows


def _adjustment(bundle) -> float:
    return round(bundle.score_adjustment(), 3) if bundle else 0.0


def _shadow(bundle) -> float:
    return round(bundle.shadow_adjustment(), 3) if bundle else 0.0


def discovery_decisions(
    provenance: Provenance, shortlist: list, run_date: date, report: str
) -> list[DecisionRecord]:
    """The quick take is a decision and is graded as one.

    Recorded at the same grain as the deep verdict so the two can be compared
    directly: "did the deep dive improve on the FAST-tier take" is the cheapest
    real question in the whole lab, and it needs both sides written down.
    """
    rows: list[DecisionRecord] = []
    for entry in shortlist:
        take = getattr(entry, "take", None)
        rows.append(
            DecisionRecord(
                provenance=provenance,
                ticker=entry.symbol,
                date=run_date.isoformat(),
                stage="discovery",
                rating=take.rating if take else "DEGRADED",
                confidence=take.confidence if take else "",
                sector=getattr(entry.candidate, "sector", ""),
                report=report,
                seat_tiers={"quick_take": "fast"},
                signal_readings=(entry.signals.readings() if entry.signals else {}),
                degraded=take is None,
                degraded_reasons=[entry.degraded_reason] if entry.degraded_reason else [],
            )
        )
    return rows


def deep_decisions(
    provenance: Provenance, results: list, run_date: date, report_dir: str
) -> list[DecisionRecord]:
    """The portfolio manager's verdict plus the arithmetic that was published.

    ``entry_condition``/``invalidation``/``target`` come from the computed
    :class:`~tradingagent.pipeline.trade_plan.TradePlan` where there is one and
    from the decision's prose otherwise, because the outcomes job tests levels
    against bars and can only do that with numbers.
    """
    rows: list[DecisionRecord] = []
    for result in results:
        decision = getattr(result, "decision", None)
        plan = getattr(result, "trade_plan", None)
        payload = plan.journal_payload() if plan is not None else None
        rows.append(
            DecisionRecord(
                provenance=provenance,
                ticker=result.symbol,
                date=run_date.isoformat(),
                stage="deep",
                rating=decision.rating if decision else "DEGRADED",
                confidence=decision.confidence if decision else "",
                horizon=_text(decision, "time_horizon"),
                entry_condition=entry_condition(payload),
                invalidation=_text(decision, "invalidation"),
                target=decision.price_target if decision else None,
                trade_plan=payload,
                sector=result.queued.sector,
                report=f"{report_dir}/{result.symbol}.md",
                seat_tiers=dict(getattr(result, "seat_tiers", {}) or {}),
                signal_readings=dict(result.queued.signal_readings),
                degraded=bool(getattr(result, "degraded", False)),
                degraded_reasons=list(result.degraded_reasons())[:6],
            )
        )
    return rows


def _text(decision, field: str) -> str:
    value = getattr(decision, field, "") if decision else ""
    return str(value or "")


def entry_condition(payload: dict | None) -> str:
    """The published trigger, in the form the outcomes job can test.

    Taken from the computed plan rather than from the manager's prose: the
    outcomes job asks "did a bar ever trade through this level", and a sentence
    saying "wait for a pullback into the low seventies" cannot be asked that.
    A NO TRADE plan has no trigger, and says so rather than inventing one.
    """
    from ..pipeline.trade_plan import PLAN

    if not payload or payload.get("status") != PLAN:
        return ""
    entry, direction = payload.get("entry"), payload.get("direction") or "long"
    if entry is None:
        return ""
    return f"{direction} entry at {float(entry):.2f}"


def options_decisions(
    provenance: Provenance, plans: list, run_date: date, report_dir: str
) -> list[DecisionRecord]:
    """One row per ticker the overlay looked at, including the declines.

    A declined overlay is a decision — "no strike on this name was worth the
    assignment risk" is a claim the market can prove wrong, and it can only be
    graded if it was written down.
    """
    rows: list[DecisionRecord] = []
    for plan in plans:
        payload = plan.journal_payload()
        if payload is None:
            continue
        chosen = payload.get("recommended") if isinstance(payload, dict) else None
        rows.append(
            DecisionRecord(
                provenance=provenance,
                ticker=plan.symbol,
                date=run_date.isoformat(),
                stage="options",
                rating=plan.strategy or "no overlay",
                confidence=(plan.recommendation.conviction if plan.recommendation else ""),
                target=chosen.get("strike") if isinstance(chosen, dict) else None,
                trade_plan=payload if isinstance(payload, dict) else None,
                report=f"{report_dir}/{plan.symbol}.md",
                seat_tiers={"options_strategist": "smart"},
                degraded=chosen is None,
                degraded_reasons=[r for r in (plan.skipped, plan.error) if r],
            )
        )
    return rows


def write(ledger, stream: str, records: list[Any]) -> int:
    """Append and never let the attempt take the run down."""
    try:
        return ledger.append(stream, records)
    except OSError as exc:  # pragma: no cover - disk-level failure
        log.warning("Ledger write to %s failed (%s); the run continues.", stream, exc)
        return 0
