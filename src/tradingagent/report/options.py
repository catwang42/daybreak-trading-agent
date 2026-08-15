"""Rendering for the options overlay — brief section 6 and deep-report section 6.

Two audiences, one source. The brief gets the schema's row —
``ticker | strategy | strike | exp | delta | premium | annualized yield |
earnings flag`` — for scanning; the deep report gets the full candidate set, the
strategist's reasoning, and the data caveats that make the numbers readable.

Nothing here recomputes anything. Every figure printed comes from an
:class:`~tradingagent.options.strategies.OptionCandidate` that was scored in
Python and, where the strategist chose it, named by OCC symbol.
"""

from __future__ import annotations

from ..options.strategies import CC, CSP, OptionCandidate
from ..options.strategist import OptionsPlan

BRIEF_HEADING = "## 6. Options Candidates"
DEEP_HEADING = "## 6. Options View"

_SHORT = {CSP: "CSP", CC: "CC"}


def _short(strategy: str | None) -> str:
    return _SHORT.get(strategy or "", "—")


def brief_row(plan: OptionsPlan) -> str:
    """One line in the brief's table. Never blank: a skip is a result."""
    if plan.strategy is None:
        return f"| {plan.symbol} | — | — | — | — | — | — | {plan.skipped or 'no overlay'} |"
    c = plan.chosen
    if c is None:
        why = plan.error or ("no candidate passed the screen" if not plan.candidates else "strategist recommended none")
        return f"| {plan.symbol} | {_short(plan.strategy)} | — | — | — | — | — | {why} |"
    return (
        f"| {plan.symbol} | {_short(plan.strategy)} | ${c.strike:,.2f} | {c.quote.expiry} | "
        f"{abs(c.delta):.2f} | ${c.credit:.2f} | {c.annualized_yield_pct:.1f}% | {c.earnings_flag} |"
    )


def render_options_index(
    plans: list[OptionsPlan], feed_note: str = "", cost_note: str = ""
) -> str:
    """Brief section 6.

    ``cost_note`` is what section 5 does for the deep stage: when the overlay is
    patched into an existing brief, the footer belongs to the run that wrote the
    brief and does not know about this one, so the stage states its own spend
    where the numbers it bought are printed.
    """
    if not plans:
        return (
            "_No options overlay this run — the deep stage produced no verdicts to "
            "build one from._"
        )
    lines = [
        "| Ticker | Strategy | Strike | Exp | Delta | Premium | Ann. yield | Earnings / note |",
        "|---|---|---|---|---|---|---|---|",
    ]
    lines += [brief_row(p) for p in plans]
    proposed = sum(1 for p in plans if p.chosen is not None)
    lines += [
        "",
        f"{proposed} of {len(plans)} deep-analysed names carry a proposed overlay. "
        "Detail, alternatives and the strategist's reasoning are in each "
        "`deep/<ticker>.md`, section 6.",
    ]
    if feed_note:
        lines += ["", f"_{feed_note}_"]
    if cost_note:
        lines += ["", f"_{cost_note}_"]
    return "\n".join(lines)


def _candidate_block(c: OptionCandidate, chosen: bool) -> str:
    q = c.quote
    mark = "**recommended** — " if chosen else ""
    called = (
        f"; if called away {c.if_called_return_pct:+.1f}%"
        if c.if_called_return_pct is not None
        else ""
    )
    oi = f"{q.open_interest:,}" if q.open_interest is not None else "unreported"
    spread = f"{q.spread_pct:.0f}%" if q.spread_pct is not None else "one-sided book"
    return "\n".join(
        [
            f"- {mark}`{q.symbol}` — ${q.strike:,.2f} {q.right} expiring {q.expiry} ({q.dte} DTE), "
            f"score {c.score:.1f}",
            f"  - Delta {abs(c.delta):.2f}, implied vol {c.iv * 100:.0f}%, "
            f"theta ${c.theta:.3f}/day",
            f"  - Credit ${c.credit:.2f}/share (${c.credit * 100:,.0f}/contract) on "
            f"${c.collateral:,.0f} collateral — {c.static_yield_pct:.2f}% over {q.dte} days, "
            f"{c.annualized_yield_pct:.1f}% annualised{called}",
            f"  - Breakeven ${c.breakeven:,.2f}; {c.anchor.note(c.strike, c.strategy)}",
            f"  - Open interest {oi}, spread {spread}, priced off the {q.price_basis} "
            f"({q.freshness()})",
            f"  - Earnings before expiry: {c.earnings_flag}",
        ]
    )


def render_options_section(plan: OptionsPlan, data_notes: list[str] | None = None) -> str:
    """Deep-report section 6 body (the heading is added by the caller)."""
    notes = data_notes if data_notes is not None else plan.data_notes
    if plan.strategy is None:
        body = [plan.skipped or "No options overlay was proposed for this verdict."]
        return "\n".join(body)

    lines = [f"**Strategy: {plan.strategy}** (from the portfolio manager's verdict).", ""]

    if not plan.candidates:
        lines += [
            "No strike passed the screen. What was rejected, and why:",
            "",
            *[f"- {row}" for row in (plan.rejected or ["the chain returned nothing"])],
        ]
    else:
        if plan.recommendation is not None and plan.chosen is not None:
            r = plan.recommendation
            lines += [
                f"### Recommended: `{plan.chosen.symbol}` (conviction {r.conviction})",
                "",
                r.rationale,
                "",
                f"**Placing it.** {r.entry_note}",
                "",
                f"**If assigned.** {r.assignment_view}",
                "",
                f"**Main risk.** {r.risk_note}",
                "",
            ]
        elif plan.error:
            lines += [
                f"_DEGRADED — the strategist produced no pick: {plan.error}. The screened "
                "candidates below stand on their own numbers._",
                "",
            ]
        else:
            lines += [
                "_The strategist declined every candidate; the screen output is below "
                "for the record._",
                "",
            ]

        lines += ["### Screened candidates", ""]
        chosen_symbol = plan.chosen.symbol if plan.chosen else None
        lines += [_candidate_block(c, c.symbol == chosen_symbol) for c in plan.candidates]
        if plan.rejected:
            lines += ["", f"Rejected by the screen: {'; '.join(plan.rejected)}."]

    if notes:
        lines += ["", "### Data quality", ""]
        lines += [f"- {note}" for note in notes]
    return "\n".join(lines)
