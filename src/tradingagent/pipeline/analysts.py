"""The four analysts — FAST tier, one call each, run independently.

Ported from `reference/TradingAgents/tradingagents/agents/analysts/`
(Apache-2.0, commit a33fd4c): `market_analyst.py`, `fundamentals_analyst.py`,
`news_analyst.py`, `social_media_analyst.py`. The four-role split, each role's
brief, and the instruction to *select* rather than enumerate indicators are
upstream's.

Deviations:
- Upstream's analysts are tool-calling LangGraph nodes; ours read a pre-built
  evidence pack (see :mod:`tradingagent.pipeline.evidence`).
- Upstream's social-media analyst reads Reddit and StockTwits. We have no social
  source at all — the Reddit API application was rejected and M3 shipped without
  a replacement — so the role is narrowed to sentiment *positioning* (sell-side
  posture, short interest, crowding) and its prompt forbids it from inventing
  retail sentiment it cannot source.
- Output is schema-enforced (:class:`~.schemas.AnalystReport`) with one
  re-prompt; a second failure marks that analyst DEGRADED and the pipeline
  continues with three voices rather than aborting the ticker.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ..data.validate import DegradedTracker
from ..llm import LLMError, LLMGateway
from .evidence import Evidence
from .prompts_loader import render
from .schemas import AnalystReport

log = logging.getLogger(__name__)

# (key, human label, prompt file, evidence slice)
ANALYSTS: tuple[tuple[str, str, str, str], ...] = (
    ("technical", "Technical Analyst", "analyst_technical", "technical_block"),
    ("fundamentals", "Fundamentals Analyst", "analyst_fundamentals", "fundamentals_block"),
    ("news", "News Analyst", "analyst_news", "news_block"),
    ("sentiment", "Sentiment Analyst", "analyst_sentiment", "sentiment_block"),
)


@dataclass
class AnalystResult:
    key: str
    label: str
    report: AnalystReport | None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.report is not None

    def digest(self) -> str:
        """Compact form fed to every downstream role."""
        if self.report is None:
            return f"**{self.label}** — DEGRADED, no report this run ({self.error or 'unknown error'})."
        r = self.report
        points = "\n".join(f"  - {p}" for p in r.key_points)
        gaps = f"\n  - Gaps: {r.evidence_gaps}" if r.evidence_gaps.strip().lower() != "none" else ""
        return f"**{self.label}** — {r.stance} (confidence {r.confidence})\n  {r.summary}\n{points}{gaps}"


def run_analysts(
    gateway: LLMGateway, evidence: Evidence, degraded: DegradedTracker
) -> list[AnalystResult]:
    """Run all four analysts on the FAST tier. Failures degrade, they do not raise."""
    results: list[AnalystResult] = []
    for key, label, prompt_name, slice_name in ANALYSTS:
        prompt = render(
            prompt_name,
            symbol=evidence.symbol,
            name=evidence.queued.name or evidence.symbol,
            sector=evidence.queued.sector or "unknown sector",
            industry=evidence.queued.industry or "unknown industry",
            run_date=evidence.run_date.isoformat(),
            evidence=getattr(evidence, slice_name)(),
            market_context=evidence.market_context,
        )
        try:
            report = gateway.complete(prompt, tier="fast", schema=AnalystReport)
            results.append(AnalystResult(key=key, label=label, report=report))
        except LLMError as exc:
            reason = str(exc)[:200]
            degraded.add(f"{label} {evidence.symbol}", reason)
            results.append(AnalystResult(key=key, label=label, report=None, error=reason))
    return results


def analyst_digest(results: list[AnalystResult]) -> str:
    """The shared briefing every downstream role starts from."""
    return "\n\n".join(r.digest() for r in results) or "No analyst reported this run."


def stance_spread(results: list[AnalystResult]) -> str:
    """One line naming where the analysts agree and where they do not."""
    stances = [(r.label.split()[0], r.report.stance) for r in results if r.report]
    if not stances:
        return "no analyst reported"
    unique = {s for _, s in stances}
    joined = ", ".join(f"{who} {stance}" for who, stance in stances)
    verdict = "unanimous" if len(unique) == 1 else f"{len(unique)} distinct readings"
    return f"{joined} ({verdict})"
