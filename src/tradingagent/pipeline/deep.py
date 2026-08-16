"""Deep-analysis orchestrator — one ticker end to end, then the queue.

This is our replacement for `reference/TradingAgents/tradingagents/graph/`
(Apache-2.0, commit a33fd4c). Upstream compiles the roles into a LangGraph
StateGraph with conditional edges; we run the same sequence as plain Python
because the order is fixed and the branching is a two-line loop:

    4 analysts (FAST)
      -> bull/bear debate, N rounds (SMART)
      -> research manager ruling (SMART)
      -> trader proposal (SMART)
      -> risk committee, 3 seats (SMART)
      -> portfolio manager verdict (DEEP)

Calls per ticker at the default 1 round: 4 fast + 2 + 1 + 1 + 3 = 7 smart + 1
deep = 12. Every role degrades independently; a ticker only aborts when there is
no price history at all, or when every analyst failed and there is nothing to
debate.

RESEARCH ONLY: nothing in this path can place an order.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from ..config import Settings
from ..data.finnhub_client import FinnhubFree
from ..data.validate import DegradedTracker
from ..llm import LLMGateway, TokenLedger
from ..snapshot import ResearchSnapshot
from .analysts import AnalystResult, run_analysts, stance_spread
from .context import DeepContext, QueuedTicker
from .debate import DebateResult, run_debate
from .evidence import Evidence, EvidenceBuilder
from .portfolio_manager import run_portfolio_manager
from .risk import RiskReview, run_risk_committee
from .schemas import PortfolioDecision, TraderProposal
from .macro_gate import suppressed_gates
from .trade_plan import TradePlan, build_trade_plan, plan_texts, quoted_figure_corrections
from .trader import run_trader

log = logging.getLogger(__name__)


@dataclass
class TierCost:
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass
class DeepResult:
    """Everything one ticker's deep dive produced, plus what it cost."""

    queued: QueuedTicker
    evidence: Evidence | None = None
    analysts: list[AnalystResult] = field(default_factory=list)
    debate: DebateResult | None = None
    proposal: TraderProposal | None = None
    proposal_error: str | None = None
    risk: RiskReview | None = None
    decision: PortfolioDecision | None = None
    decision_error: str | None = None
    #: The computed arithmetic. Built once from the trader's levels so the risk
    #: committee critiques real numbers, then rebuilt against the final verdict.
    trade_plan: TradePlan | None = None
    aborted: str | None = None
    seconds: float = 0.0
    cost_by_tier: dict[str, TierCost] = field(default_factory=dict)

    @property
    def symbol(self) -> str:
        return self.queued.symbol

    @property
    def degraded(self) -> bool:
        return self.decision is None or self.aborted is not None or any(
            not a.ok for a in self.analysts
        )

    @property
    def verdict(self) -> str:
        if self.decision is None:
            return "DEGRADED"
        return f"{self.decision.rating} ({self.decision.confidence})"

    @property
    def total_cost_usd(self) -> float:
        return sum(t.cost_usd for t in self.cost_by_tier.values())

    @property
    def total_tokens(self) -> int:
        return sum(t.total_tokens for t in self.cost_by_tier.values())

    @property
    def total_calls(self) -> int:
        return sum(t.calls for t in self.cost_by_tier.values())

    def degraded_reasons(self) -> list[str]:
        reasons: list[str] = []
        if self.aborted:
            reasons.append(self.aborted)
        reasons += [f"{a.label} did not report" for a in self.analysts if not a.ok]
        if self.debate:
            reasons += [
                f"{t.side} researcher round {t.round_number} did not report"
                for t in self.debate.turns
                if t.turn is None
            ]
            if self.debate.plan is None and not self.aborted:
                reasons.append("research manager produced no plan")
        if self.proposal is None and self.debate is not None and not self.aborted:
            reasons.append("trader produced no proposal")
        if self.risk:
            reasons += [f"{v.seat} risk analyst did not report" for v in self.risk.voices if v.take is None]
        if self.decision is None and not self.aborted:
            reasons.append("portfolio manager produced no verdict")
        if self.evidence:
            reasons += [f"missing {m}" for m in self.evidence.missing]
        return reasons


def _snapshot(ledger: TokenLedger) -> dict[str, TierCost]:
    return {
        tier: TierCost(u.calls, u.prompt_tokens, u.completion_tokens, u.cost_usd)
        for tier, u in ledger.by_tier.items()
    }


def _delta(before: dict[str, TierCost], after: dict[str, TierCost]) -> dict[str, TierCost]:
    out: dict[str, TierCost] = {}
    for tier, now in after.items():
        was = before.get(tier, TierCost())
        out[tier] = TierCost(
            calls=now.calls - was.calls,
            prompt_tokens=now.prompt_tokens - was.prompt_tokens,
            completion_tokens=now.completion_tokens - was.completion_tokens,
            cost_usd=now.cost_usd - was.cost_usd,
        )
    return {tier: cost for tier, cost in out.items() if cost.calls}


def analyze_ticker(
    gateway: LLMGateway,
    evidence: Evidence,
    degraded: DegradedTracker,
    rounds: int = 1,
    snapshot: ResearchSnapshot | None = None,
) -> DeepResult:
    """Run the full role sequence for one ticker. Never raises."""
    started = time.monotonic()
    before = _snapshot(gateway.ledger)
    result = DeepResult(queued=evidence.queued, evidence=evidence)

    def finish() -> DeepResult:
        result.seconds = time.monotonic() - started
        result.cost_by_tier = _delta(before, _snapshot(gateway.ledger))
        return result

    if not evidence.usable:
        result.aborted = "no usable price history — the deep dive was not attempted"
        log.warning("Deep %s: %s", evidence.symbol, result.aborted)
        return finish()

    log.info("Deep %s: running 4 analysts (fast tier)", evidence.symbol)
    result.analysts = run_analysts(gateway, evidence, degraded)
    if not any(a.ok for a in result.analysts):
        result.aborted = "all four analysts failed — nothing to debate"
        degraded.add(f"Deep {evidence.symbol}", result.aborted)
        return finish()
    log.info("Deep %s: analyst stances — %s", evidence.symbol, stance_spread(result.analysts))

    log.info("Deep %s: bull/bear debate, %d round(s) (smart tier)", evidence.symbol, rounds)
    result.debate = run_debate(gateway, evidence, result.analysts, degraded, rounds=rounds)

    log.info("Deep %s: trader proposal (smart tier)", evidence.symbol)
    result.proposal, result.proposal_error = run_trader(
        gateway, evidence, result.analysts, result.debate.plan, degraded, result.debate.plan_error
    )

    result.trade_plan = build_trade_plan(
        evidence,
        result.proposal,
        result.debate.plan.recommendation if result.debate.plan else None,
        snapshot=snapshot,
    )

    log.info("Deep %s: risk committee (smart tier)", evidence.symbol)
    result.risk = run_risk_committee(
        gateway,
        evidence,
        result.analysts,
        result.debate.plan,
        result.proposal,
        degraded,
        rounds=1,  # one pass of three seats; the PM arbitrates rather than re-running them
        plan_error=result.debate.plan_error,
        proposal_error=result.proposal_error,
        trade_plan=result.trade_plan,
    )

    log.info("Deep %s: portfolio manager verdict (deep tier)", evidence.symbol)
    result.decision, result.decision_error = run_portfolio_manager(
        gateway,
        evidence,
        result.analysts,
        result.debate,
        result.proposal,
        result.risk,
        degraded,
        proposal_error=result.proposal_error,
        trade_plan=result.trade_plan,
    )
    # Rebuilt against the verdict that will actually be published: the manager
    # can overrule the research manager's rating and states the price target,
    # and both change the arithmetic.
    if result.decision is not None:
        result.trade_plan = build_trade_plan(
            evidence,
            result.proposal,
            result.decision.rating,
            result.decision.price_target,
            snapshot=snapshot,
        )
        texts = plan_texts(result.proposal, result.decision)
        result.trade_plan.corrections = quoted_figure_corrections(result.trade_plan, texts)
        # A wait that rests on an approximate release date is struck out here,
        # not argued with: the models never see which dates are schedules.
        result.trade_plan.suppressed_gates = suppressed_gates(
            texts, evidence.macro_events, as_of=evidence.market_as_of
        )
        if result.trade_plan.suppressed_gates:
            log.warning(
                "Deep %s: %d macro gate(s) suppressed — unverified release date",
                evidence.symbol, len(result.trade_plan.suppressed_gates),
            )
        if result.trade_plan.corrections:
            log.warning(
                "Deep %s: %d quoted figure(s) disagree with the computed plan",
                evidence.symbol, len(result.trade_plan.corrections),
            )
    log.info(
        "Deep %s: verdict %s in %.1fs", evidence.symbol, result.verdict, time.monotonic() - started
    )
    return finish()


def run_queue(
    settings: Settings,
    context: DeepContext,
    gateway: LLMGateway,
    finnhub: FinnhubFree,
    degraded: DegradedTracker,
    only: list[str] | None = None,
    snapshot: ResearchSnapshot | None = None,
) -> list[DeepResult]:
    """Analyse the queued tickers, honouring ``DEEP_TICKER_CAP``."""
    queue = context.limit(settings.deep_ticker_cap, only=only)
    if not queue:
        degraded.add("Deep stage", "the discovery queue was empty; no ticker was analysed")
        return []

    log.info(
        "Deep stage: %d ticker(s) — %s (cap %d, %d debate round(s))",
        len(queue),
        ", ".join(q.symbol for q in queue),
        settings.deep_ticker_cap,
        settings.debate_rounds,
    )
    builder = EvidenceBuilder(context, finnhub, degraded, snapshot=snapshot)
    builder.prefetch([q.symbol for q in queue])

    results: list[DeepResult] = []
    for queued in queue:
        evidence = builder.build(queued)
        results.append(
            analyze_ticker(
                gateway, evidence, degraded, rounds=settings.debate_rounds, snapshot=snapshot
            )
        )
    return results
