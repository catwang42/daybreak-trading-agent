"""The trade arithmetic, computed here and nowhere else.

The models decide *intent* — direction, where the thesis breaks, what would
have to be true to enter, how long the view runs. Every number that follows
from that intent is computed in this module from the run's snapshot: entry,
stop, risk per share, risk as a percentage, the R multiple, the size cap.

This split exists because of a specific defect. One report proposed STZ with
"2.5% risk"; the stop and the entry actually in the plan were 3.6% apart. The
model had not lied — it had quoted a risk figure computed against an entry
reference that was no longer the entry by the time the verdict was written.
Prose that carries its own arithmetic is unfalsifiable at a glance, and a human
sizing a position off "2.5%" would have taken 44% more risk than they thought.

So:

- The LLM never emits a derived number. It emits an entry *type* and, when the
  chart supports one, a level; an invalidation *type* and a level. Both are
  levels a reader can find on a chart, not results of a calculation.
- :func:`build_trade_plan` computes the rest and asserts the plan is coherent:
  the stop is on the losing side of the entry, the risk fits the cap, the
  reward-to-risk clears the floor, and every price traces to the same snapshot.
  A plan that fails an assertion is not silently softened — it is published as
  ``NO TRADE — inconsistent plan`` with the reason.
- :func:`quoted_figure_corrections` reads the model's prose back and flags any
  figure that disagrees with the computed plan, so the STZ case would print a
  correction instead of a contradiction.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

log = logging.getLogger(__name__)

Direction = Literal["long", "short", "flat"]

#: Widest entry-to-stop distance we will publish as a plan. The screener already
#: rejects setups wider than 12%; by the time a stop has been argued down by a
#: risk committee, anything past 8% is a different trade from the one screened.
MAX_RISK_PCT = 8.0
#: A target closer than this to the stop is not worth the spread.
MIN_REWARD_RISK = 1.5
#: Portfolio fraction risked per idea. Size follows from this and the stop
#: distance — that is the whole point of computing the stop first.
RISK_BUDGET_PCT = 0.5
#: However tight the stop, one research idea does not get more than this.
MAX_POSITION_PCT = 10.0
#: A stop further than this from the last close is a level from another month.
MAX_LEVEL_DRIFT_PCT = 25.0
#: How far a quoted percentage may sit from the computed one before it is a
#: correction rather than rounding.
QUOTED_PCT_TOLERANCE = 0.3
#: Same, for a quoted price, as a fraction of the entry.
QUOTED_PRICE_TOLERANCE_PCT = 0.75

LONG_RATINGS = {"Buy", "Overweight"}
SHORT_RATINGS = {"Sell", "Underweight"}

NO_TRADE = "NO TRADE — inconsistent plan"
PLAN = "PLAN"
FLAT = "NO TRADE — the verdict is Hold"
UNPRICED = "NO TRADE — no usable price"


def direction_for(rating: str | None) -> Direction:
    if rating in LONG_RATINGS:
        return "long"
    if rating in SHORT_RATINGS:
        return "short"
    return "flat"


@dataclass
class TradePlan:
    """Computed trade arithmetic for one ticker, with its own provenance."""

    symbol: str
    direction: Direction
    status: str = PLAN
    entry: float | None = None
    entry_basis: str = ""
    stop: float | None = None
    stop_basis: str = ""
    risk_per_share: float | None = None
    risk_pct: float | None = None
    target: float | None = None
    target_basis: str = ""
    reward_risk: float | None = None
    size_pct: float | None = None
    size_basis: str = ""
    snapshot_id: str = ""
    market_as_of: date | None = None
    #: Assertion failures. Non-empty means ``status`` is NO TRADE.
    failures: list[str] = field(default_factory=list)
    #: Things a reader should know that do not invalidate the plan.
    warnings: list[str] = field(default_factory=list)
    #: Figures the models quoted that disagree with the computed ones.
    corrections: list[str] = field(default_factory=list)

    @property
    def actionable(self) -> bool:
        return self.status == PLAN

    def table(self) -> str:
        """Section 4's table: every row a computed value with its basis."""
        rows = [
            "| Field | Value | Basis |",
            "|---|---:|---|",
            f"| Entry | {_money(self.entry)} | {self.entry_basis or '—'} |",
            f"| Stop | {_money(self.stop)} | {self.stop_basis or '—'} |",
            f"| Risk / share | {_money(self.risk_per_share)} | entry − stop |",
            f"| Risk | {_pct(self.risk_pct)} | risk per share ÷ entry (cap {MAX_RISK_PCT:.0f}%) |",
            f"| Target | {_money(self.target)} | {self.target_basis or '—'} |",
            f"| Reward : risk | {_x(self.reward_risk)} | (target − entry) ÷ risk per share "
            f"(floor {MIN_REWARD_RISK:.1f}×) |",
            f"| Size cap | {_pct(self.size_pct)} of portfolio | {self.size_basis or '—'} |",
        ]
        return "\n".join(rows)

    def note(self) -> str:
        """One line stating who computed these numbers and from what."""
        stamp = self.market_as_of.isoformat() if self.market_as_of else "unknown"
        return (
            f"_Computed by the pipeline from snapshot `{self.snapshot_id or 'none'}` "
            f"({stamp} close), not quoted by a model. The models chose the direction, "
            f"the entry condition and the invalidation level; the arithmetic is code._"
        )

    def journal_payload(self) -> dict:
        return {
            "status": self.status,
            "direction": self.direction,
            "entry": self.entry,
            "stop": self.stop,
            "risk_pct": round(self.risk_pct, 2) if self.risk_pct is not None else None,
            "target": self.target,
            "reward_risk": round(self.reward_risk, 2) if self.reward_risk is not None else None,
            "size_pct": round(self.size_pct, 2) if self.size_pct is not None else None,
            "snapshot_id": self.snapshot_id,
            "failures": list(self.failures),
        }


def build_trade_plan(
    evidence,
    proposal,
    rating: str | None,
    target: float | None = None,
    snapshot=None,
) -> TradePlan:
    """Turn intent into arithmetic. Never raises; a broken plan is a NO TRADE.

    ``rating`` is whoever is final at this point in the pipeline: the research
    manager before the risk committee sits, the portfolio manager after.
    """
    symbol = evidence.symbol
    plan = TradePlan(
        symbol=symbol,
        direction=direction_for(rating),
        snapshot_id=evidence.snapshot_id,
        market_as_of=evidence.market_as_of,
    )

    if snapshot is not None and evidence.snapshot_id and snapshot.snapshot_id != evidence.snapshot_id:
        plan.failures.append(
            f"the evidence carries {evidence.snapshot_id} and the plan was asked to price "
            f"against {snapshot.snapshot_id} — mixed snapshots"
        )

    last, price_basis = _last_price(evidence)
    if last is None:
        plan.status = UNPRICED
        plan.failures.append("no usable close in the snapshot for this ticker")
        return plan

    if plan.direction == "flat":
        plan.status = FLAT
        plan.entry, plan.entry_basis = last, price_basis
        return plan

    plan.entry, plan.entry_basis = _entry(evidence, proposal, last, price_basis, plan)
    plan.stop, plan.stop_basis = _stop(evidence, proposal, plan)

    if plan.stop is None or plan.entry is None:
        plan.status = NO_TRADE
        plan.failures.append("no defensible stop: neither an invalidation level nor an ATR")
        return plan

    losing_side = plan.stop < plan.entry if plan.direction == "long" else plan.stop > plan.entry
    if not losing_side:
        plan.failures.append(
            f"the stop {plan.stop:,.2f} is on the wrong side of the entry {plan.entry:,.2f} "
            f"for a {plan.direction}"
        )

    plan.risk_per_share = abs(plan.entry - plan.stop)
    plan.risk_pct = plan.risk_per_share / plan.entry * 100 if plan.entry else None
    if plan.risk_pct is not None and plan.risk_pct > MAX_RISK_PCT:
        plan.failures.append(
            f"risk to stop is {plan.risk_pct:.1f}% of entry, past the {MAX_RISK_PCT:.0f}% cap"
        )
    if plan.risk_per_share == 0:
        plan.failures.append("the stop and the entry are the same price")

    plan.target, plan.target_basis = _target(target, plan)
    if plan.target is not None and plan.risk_per_share:
        reward = (
            plan.target - plan.entry if plan.direction == "long" else plan.entry - plan.target
        )
        plan.reward_risk = reward / plan.risk_per_share
        if plan.reward_risk < MIN_REWARD_RISK:
            plan.failures.append(
                f"reward:risk is {plan.reward_risk:.2f}×, below the {MIN_REWARD_RISK:.1f}× floor"
                + (" (the target is on the wrong side of the entry)" if reward < 0 else "")
            )
    elif plan.target is None:
        plan.warnings.append(
            "no price target was stated, so the reward:risk floor could not be checked"
        )

    plan.size_pct, plan.size_basis = _size(plan)

    if plan.failures:
        plan.status = NO_TRADE
        log.info("Trade plan %s: %s — %s", symbol, NO_TRADE, "; ".join(plan.failures))
    return plan


# --- the pieces -----------------------------------------------------------


def _last_price(evidence) -> tuple[float | None, str]:
    """The snapshot's close, and what to call it in the report."""
    observation = getattr(evidence, "price_observation", None)
    if observation is not None:
        return observation.value, f"snapshot close, {observation.effective_at.isoformat()}"
    price = evidence.price
    if price is None:
        return None, ""
    return price, "last close (not covered by the run's snapshot)"


def _entry(evidence, proposal, last: float, basis: str, plan: TradePlan) -> tuple[float, str]:
    """The entry the arithmetic uses.

    A named level is honoured when it is on the right side of the close and
    within a sane distance of it; anything else is the close, said plainly.
    A level the trader invented is a worse anchor than the price everything
    else in the run was screened on.
    """
    entry_type = _attr(proposal, "entry_type", "market")
    level = _number(_attr(proposal, "entry_level", None))
    if entry_type == "market" or level is None:
        return last, basis
    drift = abs(level - last) / last * 100
    if drift > MAX_LEVEL_DRIFT_PCT:
        plan.warnings.append(
            f"the proposed {entry_type} entry {level:,.2f} is {drift:.0f}% from the close; "
            f"priced against the close instead"
        )
        return last, basis
    # A pullback entry is below the close for a long and above it for a short;
    # a breakout entry is the other way round. A level on the wrong side is a
    # different trade from the one described, so it is not silently used.
    below = level < last
    wants_below = (entry_type == "pullback") == (plan.direction == "long")
    if below != wants_below:
        plan.warnings.append(
            f"the proposed {entry_type} entry {level:,.2f} sits the wrong side of the "
            f"{last:,.2f} close; priced against the close instead"
        )
        return last, basis
    return level, f"{entry_type} entry proposed by the trader, {basis}"


def _stop(evidence, proposal, plan: TradePlan) -> tuple[float | None, str]:
    """The invalidation level, or an ATR stop when none was given."""
    entry = plan.entry or 0.0
    kind = _attr(proposal, "invalidation_type", "level")
    level = _number(_attr(proposal, "invalidation_level", None))
    if level is not None and entry:
        drift = abs(level - entry) / entry * 100
        if drift <= MAX_LEVEL_DRIFT_PCT:
            return level, f"trader's {_stop_word(kind)}"
        plan.warnings.append(
            f"the invalidation level {level:,.2f} is {drift:.0f}% from the entry; "
            f"an ATR stop was used instead"
        )
    atr = evidence.indicators.get("atr") if evidence.indicators else None
    if not atr:
        return None, ""
    stop = entry - 2 * atr if plan.direction == "long" else entry + 2 * atr
    return stop, f"2 × ATR(14) {atr:,.2f} from the entry (no usable invalidation level)"


def _stop_word(kind: str) -> str:
    return {
        "moving_average": "invalidation level (a moving average)",
        "atr": "invalidation level (an ATR band)",
        "percent": "invalidation level (a percentage stop)",
        "level": "invalidation level (structural)",
    }.get(kind, "invalidation level")


def _target(target: float | None, plan: TradePlan) -> tuple[float | None, str]:
    value = _number(target)
    if value is None:
        return None, "no target stated by the verdict"
    return value, "portfolio manager's soft price target"


def _size(plan: TradePlan) -> tuple[float | None, str]:
    if not plan.risk_pct:
        return None, ""
    raw = RISK_BUDGET_PCT / (plan.risk_pct / 100)
    capped = min(raw, MAX_POSITION_PCT)
    basis = (
        f"{RISK_BUDGET_PCT:.2f}% of portfolio at risk ÷ {plan.risk_pct:.1f}% stop distance"
        + (f", capped at {MAX_POSITION_PCT:.0f}%" if raw > MAX_POSITION_PCT else "")
    )
    return capped, basis


# --- reading the models' prose back --------------------------------------

_RISK_PCT_PATTERNS = (
    re.compile(r"(\d+(?:\.\d+)?)\s*%\s*(?:of\s+)?risk", re.I),
    re.compile(r"risk(?:ing)?\s+(?:of\s+|about\s+|~\s*)?(\d+(?:\.\d+)?)\s*%", re.I),
)
_STOP_PATTERN = re.compile(r"stop(?:-loss|\s+loss)?[^.$\n]{0,24}\$\s?([\d,]+(?:\.\d+)?)", re.I)
_ENTRY_PATTERN = re.compile(r"entr(?:y|ies)[^.$\n]{0,24}\$\s?([\d,]+(?:\.\d+)?)", re.I)


def quoted_figure_corrections(plan: TradePlan, texts: dict[str, str]) -> list[str]:
    """Every figure the prose quotes, checked against the computed plan.

    The computed value wins. We do not rewrite the model's paragraph — an
    edited thesis is a thesis nobody can audit — we print the disagreement
    next to it, which is what a reader needs to know anyway.
    """
    if plan.entry is None:
        return []
    out: list[str] = []
    for label, text in texts.items():
        if not text:
            continue
        for pattern in _RISK_PCT_PATTERNS:
            for match in pattern.finditer(text):
                quoted = _number(match.group(1))
                if quoted is None or plan.risk_pct is None:
                    continue
                if abs(quoted - plan.risk_pct) > QUOTED_PCT_TOLERANCE:
                    out.append(
                        f"{label} says {quoted:.1f}% risk; the computed plan risks "
                        f"{plan.risk_pct:.1f}% ({_money(plan.entry)} entry to "
                        f"{_money(plan.stop)} stop). The computed figure is the one to use."
                    )
        out += _price_mismatches(label, text, _STOP_PATTERN, plan.stop, "stop", plan.entry)
        out += _price_mismatches(label, text, _ENTRY_PATTERN, plan.entry, "entry", plan.entry)
    # The same sentence often reaches two roles; say each thing once.
    return list(dict.fromkeys(out))


def _price_mismatches(
    label: str, text: str, pattern: re.Pattern, computed: float | None, what: str, entry: float
) -> list[str]:
    if computed is None or not entry:
        return []
    tolerance = entry * QUOTED_PRICE_TOLERANCE_PCT / 100
    out: list[str] = []
    for match in pattern.finditer(text):
        quoted = _number(match.group(1).replace(",", ""))
        if quoted is None:
            continue
        if abs(quoted - computed) > tolerance:
            out.append(
                f"{label} quotes a {what} of {_money(quoted)}; the computed plan uses "
                f"{_money(computed)}."
            )
    return out


def plan_texts(proposal, decision) -> dict[str, str]:
    """The prose fields that are allowed to name a number."""
    texts: dict[str, str] = {}
    if proposal is not None:
        texts["The trader's reasoning"] = _attr(proposal, "reasoning", "") or ""
        texts["The trader's entry condition"] = _attr(proposal, "entry_condition", "") or ""
    if decision is not None:
        texts["The verdict summary"] = getattr(decision, "executive_summary", "") or ""
        texts["The thesis"] = getattr(decision, "investment_thesis", "") or ""
        texts["The risk ruling"] = getattr(decision, "risk_ruling", "") or ""
        texts["The invalidation line"] = getattr(decision, "invalidation", "") or ""
    return texts


# --- formatting -----------------------------------------------------------


def _attr(obj, name: str, default=None):
    return getattr(obj, name, default) if obj is not None else default


def _number(value) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out else None  # reject NaN


def _money(value: float | None) -> str:
    return "—" if value is None else f"${value:,.2f}"


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value:.1f}%"


def _x(value: float | None) -> str:
    return "—" if value is None else f"{value:.2f}×"
