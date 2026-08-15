"""Cash-secured put and covered-call candidate generation.

Ported from `reference/trading_skills` (staskh, MIT) — principally
`scanner_pmcc.py` (`find_strike_by_delta`, `compute_base_score`,
`compute_earnings_score`, `compute_short_premium_score`) and
`broker/roll.py::evaluate_short_candidates`. Upstream screens the short leg of a
poor-man's covered call; the short-leg question is the same one a CSP or a
covered call asks — *which strike, at what delta, for what annualised premium,
and is there an earnings print inside the contract's life* — so the selection
and scoring logic ports across directly. What does not port is upstream's
transport (IBKR / yfinance) and its assumption that the vendor supplies greeks.

Deliberate deviations, all recorded in PORTING_NOTES.md:

- Upstream reads ``impliedVolatility`` when present and only derives it as a
  fallback. Alpaca's free feed never supplies it, so IV is always solved from
  the quote here — and a strike whose quote cannot produce an IV is dropped
  rather than defaulted to upstream's 0.30, because a made-up IV produces a
  made-up delta and delta is the whole selection criterion.
- Upstream's liquidity screen uses volume *and* open interest. Per-contract
  volume does not exist on the free feed, so the screen is OI-only and every
  candidate carries that limitation in its basis record.
- Strikes are anchored to the levels the deep pipeline already argued over
  (support/Bollinger/SMA/stop for puts; resistance/Bollinger/price target for
  calls). Upstream has no equity thesis to anchor to; we do, and the whole
  point of running this after the portfolio manager is to use it.

RESEARCH ONLY. These are candidates for a human to evaluate. Nothing here
places, prices for execution, or sizes a real position.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date

from ..data.option_chain import ChainSlice, OptionQuote
from .black_scholes import DAYS_PER_YEAR, bs_delta, bs_theta, implied_volatility
from .levels import (
    ACQUIRE_AFTER_FAILURE,
    INVALIDATION,
    TARGET,
    PriceLevel,
    assignment_conflict,
    chart_levels,
    of_role,
    upside_conflict,
)

log = logging.getLogger(__name__)

CSP = "cash-secured put"
CC = "covered call"


@dataclass(frozen=True)
class StrategyRules:
    """The screen. Upstream's IBKR defaults, moved to a monthly income cadence.

    staskh's PMCC scanner sells 7-30 DTE short legs against a LEAPS; a
    standalone CSP or covered call is conventionally sold at 30-45 days, where
    theta is still steep but a single earnings print or gap has room to be
    survived. ``roll.py`` already prefers 21-60 there, so this is upstream's
    own preferred band rather than a new invention.
    """

    min_dte: int = 21
    max_dte: int = 45
    target_dte: int = 35
    delta_low: float = 0.20
    delta_high: float = 0.30
    delta_tolerance: float = 0.05
    min_open_interest: int = 20
    max_spread_pct: float = 20.0
    min_credit: float = 0.10
    max_candidates: int = 3


@dataclass
class LevelAnchor:
    """The price level a strike is being placed against, and where it came from."""

    price: float | None
    label: str

    def note(self, strike: float, strategy: str) -> str:
        if self.price is None:
            return "no level to anchor against"
        gap = (strike - self.price) / self.price * 100
        if strategy == CSP:
            side = "below" if strike <= self.price else "above"
            return f"strike {abs(gap):.1f}% {side} {self.label} ${self.price:,.2f}"
        side = "above" if strike >= self.price else "below"
        return f"strike {abs(gap):.1f}% {side} {self.label} ${self.price:,.2f}"


@dataclass
class OptionCandidate:
    """One strike, fully priced, with every input that produced the numbers."""

    strategy: str
    quote: OptionQuote
    spot: float
    credit: float
    iv: float | None
    delta: float | None
    theta: float | None
    collateral: float
    static_yield_pct: float
    annualized_yield_pct: float
    breakeven: float
    moneyness_pct: float
    anchor: LevelAnchor
    earnings_before_expiry: date | None = None
    earnings_checked: bool = True
    if_called_return_pct: float | None = None
    score: float = 0.0
    notes: list[str] = field(default_factory=list)
    #: Set when the strike is only defensible as an acquire-after-setup-failure
    #: trade — assignment lands at or above the equity invalidation.
    acquire_after_failure: bool = False
    #: Ways this strike disagrees with the equity plan. Printed, never hidden.
    conflicts: list[str] = field(default_factory=list)

    @property
    def symbol(self) -> str:
        return self.quote.symbol

    @property
    def strike(self) -> float:
        return self.quote.strike

    @property
    def earnings_flag(self) -> str:
        if not self.earnings_checked:
            return "unchecked"
        if self.earnings_before_expiry is None:
            return "clear"
        return f"⚠ {self.earnings_before_expiry.isoformat()}"

    def basis(self) -> dict:
        """Everything the journal records — the numbers *and* their provenance.

        A recommendation whose premium came from a three-day-old print is a
        different recommendation from one quoted live, and the journal has to
        be able to tell them apart when the outcome is graded.
        """
        q = self.quote
        return {
            "strategy": self.strategy,
            "contract": q.symbol,
            "strike": q.strike,
            "expiry": q.expiry.isoformat(),
            "dte": q.dte,
            "credit_per_share": round(self.credit, 4),
            "credit_per_contract": round(self.credit * 100, 2),
            "collateral": round(self.collateral, 2),
            "static_yield_pct": round(self.static_yield_pct, 3),
            "annualized_yield_pct": round(self.annualized_yield_pct, 2),
            "if_called_return_pct": (
                round(self.if_called_return_pct, 2) if self.if_called_return_pct is not None else None
            ),
            "breakeven": round(self.breakeven, 2),
            "delta": round(self.delta, 4) if self.delta is not None else None,
            "implied_vol_pct": round(self.iv * 100, 2) if self.iv is not None else None,
            "theta_per_day": round(self.theta, 4) if self.theta is not None else None,
            "spot": round(self.spot, 2),
            "moneyness_pct": round(self.moneyness_pct, 2),
            "anchor_level": self.anchor.price,
            "anchor_source": self.anchor.label,
            "open_interest": q.open_interest,
            "open_interest_date": (
                q.open_interest_date.isoformat() if q.open_interest_date else None
            ),
            "bid": q.bid,
            "ask": q.ask,
            "spread_pct": round(q.spread_pct, 2) if q.spread_pct is not None else None,
            "price_basis": q.price_basis,
            "price_as_of": q.pricing_stamp.isoformat() if q.pricing_stamp else None,
            "earnings_before_expiry": (
                self.earnings_before_expiry.isoformat() if self.earnings_before_expiry else None
            ),
            "earnings_checked": self.earnings_checked,
            "score": round(self.score, 2),
            "score_notes": list(self.notes),
            "acquire_after_failure": self.acquire_after_failure,
            "plan_conflicts": list(self.conflicts),
            "greeks_source": "computed (Black-Scholes) — the free feed supplies none",
            "volume_available": False,
        }


# --------------------------------------------------------------------------
# strategy selection
# --------------------------------------------------------------------------
def strategy_for(rating: str) -> str | None:
    """Which options overlay, if any, the equity verdict justifies.

    Buy/Overweight means we want the shares and think the entry matters — that
    is a cash-secured put: get paid to bid below the market. Hold means an
    existing holder has no reason to add and no reason to sell — that is a
    covered call. Underweight/Sell gets nothing: selling puts on a name you do
    not want to own is a short-vol bet dressed as an entry plan, and writing
    calls presumes shares the verdict says not to hold.
    """
    if rating in {"Buy", "Overweight"}:
        return CSP
    if rating == "Hold":
        return CC
    return None


def skip_reason(rating: str) -> str:
    if rating in {"Underweight", "Sell"}:
        return (
            f"{rating} — no overlay proposed. A cash-secured put obliges us to buy a name "
            "the verdict is negative on, and a covered call presumes shares it says not "
            "to hold."
        )
    return f"{rating} — no overlay rule defined for this verdict."


# --------------------------------------------------------------------------
# level anchoring
# --------------------------------------------------------------------------
def support_anchor(levels: list[PriceLevel], spot: float) -> LevelAnchor:
    """Nearest chart level *below* spot — where a put strike wants to sit.

    Nearest rather than lowest: the first level under the market is the one
    price has to break for assignment to be live, so it is the one the strike
    is really being placed against. Only SUPPORT and RESISTANCE levels are
    eligible: the plan's invalidation is a line to stay below, not a shelf to
    sit on (see :mod:`.levels`).
    """
    below = {k: v for k, v in chart_levels(levels).items() if 0 < v < spot}
    if not below:
        return LevelAnchor(None, "no support level below spot")
    label = max(below, key=lambda k: below[k])
    return LevelAnchor(below[label], label)


def resistance_anchor(levels: list[PriceLevel], spot: float) -> LevelAnchor:
    """Nearest chart level *above* spot — the floor for a covered-call strike."""
    above = {k: v for k, v in chart_levels(levels).items() if v > spot}
    if not above:
        return LevelAnchor(None, "no resistance level above spot")
    label = min(above, key=lambda k: above[k])
    return LevelAnchor(above[label], label)


# --------------------------------------------------------------------------
# pricing one strike
# --------------------------------------------------------------------------
def _iv_years(quote: OptionQuote, as_of: date) -> float:
    """Years to expiry *as of the moment the reference price was observed*.

    staskh's ``find_strike_by_delta`` does the same thing: a last-trade price
    from three sessions ago describes a contract that had three more days of
    life, and solving it against today's shorter T inflates the IV. The delta
    is then evaluated at today's T with that IV.
    """
    observed = as_of
    stamp = quote.pricing_stamp
    if stamp is not None and quote.price_basis in {"last trade", "prior close"}:
        observed = stamp.date()
    days = max((quote.expiry - observed).days, 1)
    return days / DAYS_PER_YEAR


def price_candidate(
    quote: OptionQuote,
    *,
    strategy: str,
    spot: float,
    risk_free_rate: float,
    dividend_yield: float = 0.0,
    as_of: date,
    anchor: LevelAnchor,
    earnings_dates: list[date] | None = None,
    earnings_checked: bool = True,
) -> OptionCandidate | None:
    """Solve IV and the greeks for one strike, then compute the seller's economics.

    Returns ``None`` when the quote cannot support a candidate — no credit, or
    no IV that reproduces the mark. Both are common on the free feed and
    neither is an error; they are strikes we decline to have an opinion about.
    """
    credit = quote.credit
    if credit is None or credit <= 0 or spot <= 0 or quote.dte <= 0:
        return None
    right = "put" if strategy == CSP else "call"
    mark = quote.reference_price
    if mark is None:
        return None

    iv = implied_volatility(mark, spot, quote.strike, _iv_years(quote, as_of), risk_free_rate, right, dividend_yield)
    if iv is None:
        return None

    years = max(quote.dte, 1) / DAYS_PER_YEAR
    delta = bs_delta(spot, quote.strike, years, risk_free_rate, iv, right, dividend_yield)
    theta = bs_theta(spot, quote.strike, years, risk_free_rate, iv, right, dividend_yield)

    if strategy == CSP:
        # Collateral is the cash actually set aside: strike x 100, less the credit
        # received. Yield on strike alone understates the return by the premium.
        collateral = (quote.strike - credit) * 100
        breakeven = quote.strike - credit
        if_called = None
    else:
        # A covered call's capital is the stock, so the yield is on spot.
        collateral = spot * 100
        breakeven = spot - credit
        if_called = (quote.strike - spot + credit) / spot * 100

    if collateral <= 0:
        return None
    static_yield = credit * 100 / collateral * 100
    annualized = static_yield * (DAYS_PER_YEAR / max(quote.dte, 1))

    return OptionCandidate(
        strategy=strategy,
        quote=quote,
        spot=spot,
        credit=credit,
        iv=iv,
        delta=delta,
        theta=theta,
        collateral=collateral,
        static_yield_pct=static_yield,
        annualized_yield_pct=annualized,
        breakeven=breakeven,
        moneyness_pct=(quote.strike - spot) / spot * 100,
        anchor=anchor,
        earnings_before_expiry=_earnings_inside(quote.expiry, as_of, earnings_dates),
        earnings_checked=earnings_checked,
        if_called_return_pct=if_called,
    )


def _earnings_inside(expiry: date, as_of: date, dates: list[date] | None) -> date | None:
    for when in sorted(dates or []):
        if as_of <= when <= expiry:
            return when
    return None


# --------------------------------------------------------------------------
# scoring — ported from scanner_pmcc.compute_base_score
# --------------------------------------------------------------------------
def score_candidate(candidate: OptionCandidate, rules: StrategyRules) -> OptionCandidate:
    """Rank one candidate against the screen, recording why.

    The five upstream components (delta fit, liquidity, spread, IV level,
    annualised yield) plus upstream's earnings and thin-premium penalties, plus
    one of ours: whether the strike actually sits on the right side of the
    level the equity analysis identified. Every component appends a note, so
    the report can show the human what the number is made of instead of a bare
    score.
    """
    notes: list[str] = []
    score = 0.0
    delta_abs = abs(candidate.delta) if candidate.delta is not None else None

    # 1. Delta fit — the primary criterion. Upstream: +2 inside the band.
    if delta_abs is None:
        notes.append("delta unavailable (−1.0)")
        score -= 1.0
    elif rules.delta_low <= delta_abs <= rules.delta_high:
        score += 2.0
        notes.append(f"delta {delta_abs:.2f} in the {rules.delta_low:.2f}–{rules.delta_high:.2f} band (+2.0)")
    elif (
        rules.delta_low - rules.delta_tolerance
        <= delta_abs
        <= rules.delta_high + rules.delta_tolerance
    ):
        score += 1.0
        notes.append(f"delta {delta_abs:.2f} just outside the band (+1.0)")
    else:
        score -= 1.0
        notes.append(f"delta {delta_abs:.2f} well outside the band (−1.0)")

    # 2. Liquidity. Upstream screens volume AND open interest; the free feed has
    #    no volume, so this is OI-only and says so when OI is absent entirely.
    oi = candidate.quote.open_interest
    if oi is None:
        notes.append("open interest unreported (0.0)")
    elif oi >= 500:
        score += 1.5
        notes.append(f"open interest {oi:,} (+1.5)")
    elif oi >= 100:
        score += 1.0
        notes.append(f"open interest {oi:,} (+1.0)")
    elif oi >= rules.min_open_interest:
        score += 0.5
        notes.append(f"open interest {oi:,} — thin (+0.5)")
    else:
        score -= 1.0
        notes.append(f"open interest {oi:,} — illiquid (−1.0)")

    # 3. Spread: the cost of being wrong about the entry.
    spread = candidate.quote.spread_pct
    if spread is None:
        score -= 1.0
        notes.append(f"one-sided book, priced off the {candidate.quote.price_basis} (−1.0)")
    elif spread < 5:
        score += 1.0
        notes.append(f"spread {spread:.1f}% (+1.0)")
    elif spread < 10:
        score += 0.5
        notes.append(f"spread {spread:.1f}% (+0.5)")
    elif spread < rules.max_spread_pct:
        notes.append(f"spread {spread:.1f}% — wide (0.0)")
    else:
        score -= 1.0
        notes.append(f"spread {spread:.1f}% — too wide to price (−1.0)")

    # 4. IV level: too low is not worth the obligation, too high is a warning.
    if candidate.iv is not None:
        iv_pct = candidate.iv * 100
        if 25 <= iv_pct <= 50:
            score += 1.0
            notes.append(f"IV {iv_pct:.0f}% in the sweet spot (+1.0)")
        elif 20 <= iv_pct <= 60:
            score += 0.5
            notes.append(f"IV {iv_pct:.0f}% acceptable (+0.5)")
        elif iv_pct > 60:
            notes.append(f"IV {iv_pct:.0f}% — the market expects a move (0.0)")
        else:
            notes.append(f"IV {iv_pct:.0f}% — little premium to sell (0.0)")

    # 5. Annualised yield.
    ann = candidate.annualized_yield_pct
    if ann > 50:
        score += 1.5
        notes.append(f"{ann:.1f}% annualised (+1.5)")
    elif ann > 30:
        score += 1.0
        notes.append(f"{ann:.1f}% annualised (+1.0)")
    elif ann > 15:
        score += 0.5
        notes.append(f"{ann:.1f}% annualised (+0.5)")
    else:
        notes.append(f"{ann:.1f}% annualised — thin (0.0)")

    # 6. Absolute premium: upstream's guard against a good-looking percentage on
    #    a credit that commissions and slippage would eat.
    if candidate.credit < 0.10:
        score -= 1.0
        notes.append(f"${candidate.credit:.2f} credit — below the noise floor (−1.0)")
    elif candidate.credit < 0.50:
        score -= 0.5
        notes.append(f"${candidate.credit:.2f} credit — small (−0.5)")

    # 7. Earnings inside the contract's life. Upstream's penalty, unchanged: a
    #    print before expiry is the single most common way a sold option that
    #    looked safe on delta stops being safe.
    if not candidate.earnings_checked:
        notes.append("earnings calendar unavailable (0.0)")
    elif candidate.earnings_before_expiry is not None:
        score -= 2.0
        notes.append(f"earnings {candidate.earnings_before_expiry.isoformat()} before expiry (−2.0)")
    else:
        score += 1.0
        notes.append("no earnings before expiry (+1.0)")

    # 8. Ours: does the strike respect the level the equity work identified?
    if candidate.anchor.price is not None:
        right_side = (
            candidate.strike <= candidate.anchor.price
            if candidate.strategy == CSP
            else candidate.strike >= candidate.anchor.price
        )
        gap_pct = abs(candidate.strike - candidate.anchor.price) / candidate.anchor.price * 100
        if right_side:
            score += 1.0
            notes.append(f"{candidate.anchor.note(candidate.strike, candidate.strategy)} (+1.0)")
        elif gap_pct <= 2.0:
            score += 0.5
            notes.append(f"{candidate.anchor.note(candidate.strike, candidate.strategy)} (+0.5)")
        else:
            score -= 1.0
            notes.append(f"{candidate.anchor.note(candidate.strike, candidate.strategy)} (−1.0)")

    candidate.score = score
    candidate.notes = notes
    return candidate


# --------------------------------------------------------------------------
# agreement with the equity plan
# --------------------------------------------------------------------------
def check_against_plan(
    candidate: OptionCandidate,
    levels: list[PriceLevel],
    *,
    allow_acquire_after_failure: bool = False,
) -> str | None:
    """Does this strike contradict the equity plan? Returns a rejection reason.

    Two rules, both from shipped defects (see :mod:`.levels`):

    - A put whose assignment breakeven sits at or below the equity invalidation
      is not the trade it is being sold as: it can only be assigned after the
      plan has stopped out. It is rejected unless the caller has said it wants
      an acquire-after-setup-failure trade, in which case it is kept and
      labelled — never presented as an entry.
    - A call struck below the base-case target sells the upside the thesis is
      built on. That is a warning, not a rejection: an income overlay on a Hold
      is allowed to cap a target it does not believe in, as long as the report
      says out loud what it is capping.
    """
    if candidate.strategy == CSP:
        conflict = assignment_conflict(candidate.breakeven, of_role(levels, INVALIDATION))
        if conflict:
            if not allow_acquire_after_failure:
                return "assignment breakeven at or below the equity invalidation"
            candidate.acquire_after_failure = True
            candidate.conflicts.append(f"{ACQUIRE_AFTER_FAILURE}: {conflict}")
        return None

    conflict = upside_conflict(candidate.strike, of_role(levels, TARGET))
    if conflict:
        candidate.conflicts.append(conflict)
    return None


# --------------------------------------------------------------------------
# the screen
# --------------------------------------------------------------------------
def hard_filters(candidate: OptionCandidate, rules: StrategyRules) -> str | None:
    """Reasons to drop a candidate outright. Returns the reason, or ``None``.

    The reasons are fixed phrases, not formatted numbers, because they are
    tallied into a rejection summary — "6 strikes below the credit floor" is
    useful; six separate lines each naming a different credit is not.
    """
    q = candidate.quote
    if candidate.credit < rules.min_credit:
        return f"credit under the ${rules.min_credit:.2f} floor"
    if candidate.delta is None:
        return "no delta could be computed"
    if candidate.strategy == CSP and q.strike > candidate.spot:
        return "in the money"
    if candidate.strategy == CC and q.strike < candidate.spot:
        return "in the money"
    if abs(candidate.delta) > rules.delta_high + 0.20:
        return "delta too high — too close to the money"
    if abs(candidate.delta) < rules.delta_low - 0.12:
        return "delta too low — not worth the obligation"
    if q.open_interest is not None and q.open_interest < rules.min_open_interest:
        return f"open interest under {rules.min_open_interest}"
    return None


def build_candidates(
    chain: ChainSlice,
    *,
    strategy: str,
    spot: float,
    levels: list[PriceLevel],
    risk_free_rate: float,
    as_of: date,
    dividend_yield: float = 0.0,
    earnings_dates: list[date] | None = None,
    earnings_checked: bool = True,
    rules: StrategyRules | None = None,
    allow_acquire_after_failure: bool = False,
) -> tuple[list[OptionCandidate], list[str]]:
    """Score one chain slice and return the best strikes, plus what was rejected.

    The rejection tally is returned rather than logged away: "nothing qualified"
    and "we never looked" produce the same empty list, and the report has to be
    able to tell the human which one happened.
    """
    rules = rules or StrategyRules()
    anchor = (
        support_anchor(levels, spot) if strategy == CSP else resistance_anchor(levels, spot)
    )
    expiry = chain.nearest_expiry(rules.target_dte)
    if expiry is None:
        return [], ["no expiry listed in the requested window"]

    rejected: dict[str, int] = {}
    scored: list[OptionCandidate] = []
    for quote in chain.for_expiry(expiry):
        candidate = price_candidate(
            quote,
            strategy=strategy,
            spot=spot,
            risk_free_rate=risk_free_rate,
            dividend_yield=dividend_yield,
            as_of=as_of,
            anchor=anchor,
            earnings_dates=earnings_dates,
            earnings_checked=earnings_checked,
        )
        if candidate is None:
            rejected["no usable quote"] = rejected.get("no usable quote", 0) + 1
            continue
        reason = hard_filters(candidate, rules) or check_against_plan(
            candidate, levels, allow_acquire_after_failure=allow_acquire_after_failure
        )
        if reason:
            rejected[reason] = rejected.get(reason, 0) + 1
            log.debug(
                "%s: rejected %s (delta %s, credit %s) — %s",
                chain.underlying, quote.symbol, candidate.delta, candidate.credit, reason,
            )
            continue
        scored.append(score_candidate(candidate, rules))
        if candidate.conflicts:
            # A conflict is not a rejection, but it must not be outranked into
            # invisibility either: it is stated in the notes and it costs score.
            candidate.notes += [f"{note} (−1.5)" for note in candidate.conflicts]
            candidate.score -= 1.5 * len(candidate.conflicts)

    scored.sort(key=lambda c: (-c.score, abs(abs(c.delta or 1.0) - 0.25)))
    tally = [f"{count} × {reason}" for reason, count in sorted(rejected.items(), key=lambda kv: -kv[1])]
    log.info(
        "%s %s, %s expiry: %d of %d strikes qualified; rejected %s",
        chain.underlying,
        strategy,
        expiry,
        len(scored),
        len(chain.for_expiry(expiry)),
        "; ".join(tally) or "none",
    )
    return scored[: rules.max_candidates], tally
