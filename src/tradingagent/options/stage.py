"""The options stage: verdict in, screened overlay out.

Runs strictly after the portfolio manager. For each deep verdict it picks the
strategy, pulls the matching side of the Alpaca paper chain, scores the strikes,
asks the strategist (SMART tier) to choose, and hands back a plan per ticker for
the report and the journal.

Everything the chain cannot tell us is carried as a data note rather than
dropped, because the free feed's limits change how the numbers should be read:
an indicative quote from Friday afternoon is a real number, but it is not a
number you can hit on Monday morning.

RESEARCH ONLY. Every call in this path is a read.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Sequence

from ..config import Settings
from ..data.finnhub_client import FinnhubFree
from ..data.option_chain import STALE_QUOTE_MINUTES, AlpacaOptionChain, ChainSlice
from ..data.validate import DegradedTracker
from ..llm import LLMGateway
from .context import OptionsContext, VerdictRow
from .strategies import CSP, StrategyRules, build_candidates, skip_reason, strategy_for
from .strategist import OptionsPlan, run_options_strategist, verdict_block

log = logging.getLogger(__name__)


def _dividend_q(row: VerdictRow) -> float:
    """Continuous dividend yield for Black-Scholes.

    ``VerdictRow.dividend_yield_pct`` is in percentage *points* — yfinance
    changed that field from a ratio to points and
    :mod:`tradingagent.data.fundamentals` documents the hazard. Dividing here
    rather than at the source keeps the stored value in the unit the rest of
    the report prints.
    """
    raw = row.dividend_yield_pct
    if raw is None or raw <= 0 or raw > 25:
        return 0.0
    return raw / 100.0


def _chain_notes(
    chain: ChainSlice | None,
    feed_note: str,
    run_date: date,
    provenance: Sequence[str] = (),
) -> list[str]:
    """What a reader needs to know before trusting the premiums.

    Written from the data, not from assumptions about the clock: the freshest
    quote in the slice is the honest statement of how live these prices are.

    ``provenance`` names the two moments this section mixes on purpose: the
    equity snapshot the strikes are anchored to, and the live quote snapshot
    they are priced against.
    """
    notes = [*provenance, feed_note]
    if chain is None:
        return notes
    stamps = [q.quote_at for q in chain.quotes if q.quote_at is not None]
    now = datetime.now(timezone.utc)
    if not stamps:
        notes.append("No quote carried a timestamp; premiums are priced off trades or prior closes.")
        return notes
    newest = max(stamps)
    age_min = (now - newest).total_seconds() / 60
    if age_min > STALE_QUOTE_MINUTES:
        notes.append(
            f"The freshest quote in this chain is from {newest:%Y-%m-%d %H:%M UTC}, "
            f"{age_min / 60:.1f}h old — the book was closed when this ran. Treat every "
            "premium as the last session's mark, not a fill."
        )
    else:
        notes.append(f"Quotes are live as of {newest:%Y-%m-%d %H:%M UTC}.")
    priced = len(chain.priced())
    notes.append(
        f"{priced} of {len(chain.quotes)} listed contracts in the window carried a usable "
        "price; the rest had no bid, no trade and no prior close."
    )
    oi_dates = {q.open_interest_date for q in chain.quotes if q.open_interest_date}
    if oi_dates:
        notes.append(
            f"Open interest is as of {max(oi_dates)} (the contracts endpoint settles a day "
            "behind). Per-contract volume is not available on the free tier."
        )
    return notes


def build_plan(
    row: VerdictRow,
    *,
    settings: Settings,
    chains: AlpacaOptionChain,
    finnhub: FinnhubFree,
    run_date: date,
    rules: StrategyRules | None = None,
    provenance: Sequence[str] = (),
) -> OptionsPlan:
    """Screen one ticker. No LLM call happens here."""
    rules = rules or StrategyRules()
    strategy = strategy_for(row.rating)
    if strategy is None:
        return OptionsPlan(symbol=row.symbol, strategy=None, skipped=skip_reason(row.rating))
    if not row.spot or row.spot <= 0:
        return OptionsPlan(
            symbol=row.symbol,
            strategy=None,
            skipped="no spot price from the deep stage — nothing to price a strike against",
        )

    right = "put" if strategy == CSP else "call"
    chain = chains.chain(
        row.symbol, right, min_dte=rules.min_dte, max_dte=rules.max_dte, run_date=run_date
    )
    notes = _chain_notes(chain, chains.feed_note, run_date, provenance)
    if chain is None:
        return OptionsPlan(
            symbol=row.symbol,
            strategy=strategy,
            rejected=["the option chain could not be read"],
            data_notes=notes,
        )

    earnings = finnhub.earnings_for(
        row.symbol, run_date, run_date + timedelta(days=rules.max_dte + 7)
    )
    if earnings is None:
        notes.append(
            "The earnings calendar could not be read, so no candidate below is "
            "confirmed clear of a print before expiry."
        )

    candidates, rejected = build_candidates(
        chain,
        strategy=strategy,
        spot=row.spot,
        levels=row.levels,
        risk_free_rate=settings.risk_free_rate,
        as_of=run_date,
        dividend_yield=_dividend_q(row),
        earnings_dates=[e.date for e in (earnings or [])],
        earnings_checked=earnings is not None,
        rules=rules,
    )
    return OptionsPlan(
        symbol=row.symbol,
        strategy=strategy,
        candidates=candidates,
        rejected=rejected,
        data_notes=notes,
    )


def run_ticker(
    row: VerdictRow,
    *,
    settings: Settings,
    chains: AlpacaOptionChain,
    finnhub: FinnhubFree,
    gateway: LLMGateway,
    degraded: DegradedTracker,
    run_date: date,
    rules: StrategyRules | None = None,
    provenance: Sequence[str] = (),
) -> OptionsPlan:
    """Screen, then ask the strategist — but only if there is a choice to make."""
    plan = build_plan(
        row, settings=settings, chains=chains, finnhub=finnhub, run_date=run_date, rules=rules,
        provenance=provenance,
    )
    if not plan.candidates:
        log.info(
            "Options %s: %s",
            row.symbol,
            plan.skipped or f"{plan.strategy} — no candidate passed the screen",
        )
        return plan

    log.info(
        "Options %s: %d %s candidate(s), strategist deciding (smart tier)",
        row.symbol,
        len(plan.candidates),
        plan.strategy,
    )
    return run_options_strategist(
        gateway,
        plan,
        degraded,
        name=row.name,
        verdict=verdict_block(
            row.rating,
            row.confidence,
            row.price_target,
            row.time_horizon,
            row.executive_summary or "not stated",
            row.invalidation or "not stated",
        ),
        price_context=row.price_context or "Price context unavailable.",
        data_quality="\n".join(f"- {note}" for note in plan.data_notes),
    )


def run_plans(
    settings: Settings,
    context: OptionsContext,
    gateway: LLMGateway,
    degraded: DegradedTracker,
    *,
    only: list[str] | None = None,
    rules: StrategyRules | None = None,
    provenance: Sequence[str] = (),
) -> tuple[list[OptionsPlan], AlpacaOptionChain]:
    """Every queued verdict, in order. Returns the plans and the chain reader.

    The reader comes back with the plans because its ``feed_note`` records
    whether OPRA was refused this run, which the brief prints once rather than
    per ticker.
    """
    chains = AlpacaOptionChain(settings, degraded=degraded)
    finnhub = FinnhubFree(settings, degraded=degraded)
    rows = context.select(only)
    if not rows:
        degraded.add("Options stage", "no deep verdicts to build an overlay from")
        return [], chains

    log.info(
        "Options stage: %d verdict(s) — %s",
        len(rows),
        ", ".join(f"{r.symbol} {r.rating}" for r in rows),
    )
    return [
        run_ticker(
            row,
            settings=settings,
            chains=chains,
            finnhub=finnhub,
            gateway=gateway,
            degraded=degraded,
            run_date=context.date,
            rules=rules,
            provenance=provenance,
        )
        for row in rows
    ], chains
