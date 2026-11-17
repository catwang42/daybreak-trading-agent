"""Render ``reports/<date>/deep/<ticker>.md`` in the exact order of
``config/report-schema.md``.

Section order is fixed and load-bearing: 1 Verdict, 2 Analyst summaries,
3 Bull vs Bear, 4 Trade proposal, 5 Risk review, 6 Options view (M4+),
7 Data sources used + timestamps. The disclaimer footer is mandatory and
verbatim on every file that carries a recommendation.
"""

from __future__ import annotations

from ..pipeline.analysts import stance_spread
from ..pipeline.deep import DeepResult
from ..pipeline.trade_plan import (
    INVALIDATION_LINE,
    RISK_RULING,
    THESIS,
    TRADER_ENTRY_CONDITION,
    TRADER_REASONING,
    VERDICT_SUMMARY,
)
from .render import DISCLAIMER

OPTIONS_HEADING = "## 6. Options View"


def _flag(result: DeepResult, label: str) -> list[str]:
    """The DEGRADED marker for one prose field, printed under the field itself.

    A paragraph that survived the restatement pass still contradicting the
    computed plan is marked where it is read, not only in a footnote at the
    bottom of section 4 — the reader who acts on it never gets that far.
    """
    plan = result.trade_plan
    reason = plan.degraded_fields.get(label) if plan is not None else None
    return [f"> **DEGRADED** — {reason}", ""] if reason else []


def consensus_gap(ours: float | None, theirs: float | None) -> str:
    """``+12.4% vs consensus`` — our target measured against the sell side's.

    Printed as a percentage of *their* number because that is the question:
    how far from the crowd is this verdict standing. A large positive gap is
    not a mistake, but it is a claim, and it should be visible next to the
    claim rather than three sections away in the positioning table.
    """
    if not ours or not theirs:
        return "—"
    return f"{(ours / theirs - 1) * 100:+.1f}%"


def _consensus_lines(result: DeepResult) -> list[str]:
    """The sell side's posture on the same row as ours, in section 1.

    The data was already in the evidence pack and already fed the sentiment
    analyst; it just never reached a reader deciding whether to act. Missing
    coverage is stated as missing — a name with no analysts is a fact about the
    name, not a gap in the report.
    """
    evidence = getattr(result, "evidence", None)
    positioning = getattr(evidence, "positioning", None)
    decision = result.decision
    if positioning is None:
        return []
    ours = decision.price_target if decision else None
    mix = positioning.recommendation_spread or positioning.recommendation_key or "no coverage reported"
    covering = (
        f"{positioning.analyst_count} analyst(s)"
        if positioning.analyst_count
        else "analyst count unavailable"
    )
    mean = f"${positioning.target_mean:,.2f}" if positioning.target_mean else "—"
    median = f"${positioning.target_median:,.2f}" if positioning.target_median else "—"
    mine = f"${ours:,.2f}" if ours else "none stated"
    return [
        "**Analyst consensus** (yfinance, free tier — the sell side's posture, not ours)",
        "",
        "| Covering | Recommendation mix | Mean target | Median target | Our target | Gap vs mean |",
        "|---|---|---:|---:|---:|---:|",
        f"| {covering} | {mix} | {mean} | {median} | {mine} | "
        f"{consensus_gap(ours, positioning.target_mean)} |",
        "",
    ]


def _verdict(result: DeepResult) -> str:
    lines = ["## 1. Verdict", ""]
    decision = result.decision
    if decision is None:
        lines += [
            "> **DEGRADED — no verdict this run.**",
            "",
            f"Reason: {result.decision_error or result.aborted or 'the pipeline did not complete'}.",
            "",
            "Nothing below should be read as a recommendation.",
            "",
        ]
        return "\n".join(lines)

    target = f"${decision.price_target:,.2f}" if decision.price_target is not None else "none stated"
    lines += [
        f"### **{decision.rating}** · soft price target {target} · confidence **{decision.confidence}**",
        "",
        f"**Horizon:** {decision.time_horizon or 'not stated'}",
        "",
        decision.executive_summary,
        "",
        *_flag(result, VERDICT_SUMMARY),
        "**Thesis**",
        "",
        decision.investment_thesis,
        "",
        *_flag(result, THESIS),
        f"**Invalidation:** {decision.invalidation}",
        "",
        *_flag(result, INVALIDATION_LINE),
        *_consensus_lines(result),
    ]
    if result.degraded:
        lines += [
            "> **DEGRADED** — this verdict was formed on incomplete evidence: "
            + "; ".join(result.degraded_reasons())
            + ".",
            "",
        ]
    return "\n".join(lines)


def _analysts(result: DeepResult) -> str:
    lines = ["## 2. Analyst Summaries", ""]
    if not result.analysts:
        lines += ["_No analyst ran for this ticker._", ""]
        return "\n".join(lines)

    lines += [f"**Stance spread:** {stance_spread(result.analysts)}", ""]
    for a in result.analysts:
        lines.append(f"### {a.label}")
        if a.report is None:
            lines += [f"> **DEGRADED** — no report: {a.error or 'LLM call failed'}", ""]
            continue
        lines += [
            f"_{a.report.stance}, confidence {a.report.confidence}_",
            "",
            a.report.summary,
            "",
        ]
        lines += [f"- {point}" for point in a.report.key_points]
        if a.report.evidence_gaps.strip().lower() != "none":
            lines.append(f"- _Gaps: {a.report.evidence_gaps}_")
        lines.append("")
    return "\n".join(lines)


def _debate(result: DeepResult) -> str:
    lines = ["## 3. Bull vs Bear", ""]
    debate = result.debate
    if debate is None:
        lines += ["_No debate took place._", ""]
        return "\n".join(lines)

    lines += [
        f"_{debate.rounds} round(s), {len([t for t in debate.turns if t.turn])} of "
        f"{len(debate.turns)} turns on the record._",
        "",
        f"**Bull's strongest argument:** {debate.strongest('Bull')}",
        "",
        f"**Bear's strongest argument:** {debate.strongest('Bear')}",
        "",
        "### Arbiter's resolution (research manager)",
        "",
    ]
    if debate.plan is None:
        lines += [f"> **DEGRADED** — no ruling: {debate.plan_error or 'LLM call failed'}", ""]
    else:
        lines += [
            f"**Recommendation: {debate.plan.recommendation}**",
            "",
            debate.plan.resolution,
            "",
            f"**Instructions to the trader:** {debate.plan.strategic_actions}",
            "",
        ]

    lines += ["<details>", "<summary>Full debate transcript</summary>", ""]
    for turn in debate.turns:
        lines += [turn.transcript_entry(), ""]
    lines += ["</details>", ""]
    return "\n".join(lines)


def _proposal(result: DeepResult) -> str:
    lines = ["## 4. Trade Proposal", ""]
    proposal = result.proposal
    if proposal is None:
        lines += [
            f"> **DEGRADED** — no proposal: {result.proposal_error or 'the trader did not run'}",
            "",
        ]
        return "\n".join(lines)

    plan = result.trade_plan
    lines += [
        f"**Action: {proposal.action}**",
        "",
        proposal.reasoning,
        "",
        *_flag(result, TRADER_REASONING),
    ]
    if proposal.entry_condition:
        lines += [f"**Entry condition:** {proposal.entry_condition}", ""]
        lines += _flag(result, TRADER_ENTRY_CONDITION)

    if plan is None:
        lines += ["_No trade plan was computed for this ticker._", ""]
        return "\n".join(lines)

    if not plan.actionable:
        lines += [f"> **{plan.status}** — {'; '.join(plan.failures) or 'see below'}.", ""]
    lines += [plan.table(), "", plan.note(), ""]
    if plan.warnings:
        lines += ["**Notes on the levels used:**", ""]
        lines += [f"- {note}" for note in plan.warnings]
        lines.append("")
    if plan.suppressed_gates:
        lines += ["**Macro gates removed from this plan:**", ""]
        lines += [f"- {note}" for note in plan.suppressed_gates]
        lines.append("")
    if plan.restatements:
        # Not an edit by the pipeline: the author was shown the computed table
        # and wrote the paragraph again. Saying which paragraphs those were is
        # what keeps the report auditable.
        lines += ["**Paragraphs restated against the computed plan:**", ""]
        lines += [f"- {note}" for note in plan.restatements]
        lines.append("")
    if plan.corrections:
        # The prose is left as written; the disagreement is printed beside it.
        # A silently edited thesis is one nobody can audit.
        lines += ["**Figures quoted in the prose that disagree with the computed plan:**", ""]
        lines += [f"- {note}" for note in plan.corrections]
        lines.append("")
    lines += [
        "_A proposal for a human to evaluate. This tool has no order path; the Alpaca "
        "integration is paper-only and read-only._",
        "",
    ]
    return "\n".join(lines)


def _risk(result: DeepResult) -> str:
    lines = ["## 5. Risk Review", ""]
    review = result.risk
    if review is None or not review.voices:
        lines += ["_The risk committee did not sit._", ""]
        return "\n".join(lines)

    for voice in review.voices:
        lines.append(f"### {voice.seat} Risk Analyst")
        if voice.take is None:
            lines += [f"> **DEGRADED** — no critique: {voice.error or 'LLM call failed'}", ""]
            continue
        lines += [
            voice.take.argument,
            "",
            f"**Wants changed:** {voice.take.recommended_adjustment}",
            "",
        ]

    lines += ["### Judge's ruling (portfolio manager)", ""]
    if result.decision is None:
        lines += [
            f"> **DEGRADED** — no ruling: {result.decision_error or 'the portfolio manager did not run'}",
            "",
        ]
    else:
        lines += [result.decision.risk_ruling, "", *_flag(result, RISK_RULING)]
    return "\n".join(lines)


def _options(plan=None) -> str:
    """Section 6.

    The deep stage renders this as a placeholder and the options stage patches
    it in place afterwards (:func:`tradingagent.report.writer.replace_section`),
    because the overlay needs the verdict this very report contains. ``--stage
    all`` passes the finished plan straight through and skips the round trip.
    """
    if plan is None:
        return (
            f"{OPTIONS_HEADING}\n\n"
            "_Pending — run `--stage options` for this date to add cash-secured put "
            "and covered-call candidates from the Alpaca paper option chain._\n"
        )
    from .options import render_options_section

    return f"{OPTIONS_HEADING}\n\n{render_options_section(plan)}\n"


def _sources(result: DeepResult) -> str:
    lines = ["## 7. Data Sources", ""]
    lines.append(
        result.evidence.sources() if result.evidence else "_No evidence pack was built._"
    )
    lines.append("")
    return "\n".join(lines)


def _footer(result: DeepResult, brief_path: str) -> str:
    lines = [
        "---",
        "",
        "### Run footer",
        "",
        f"- **Ticker runtime:** {result.seconds:.1f}s · **LLM calls:** {result.total_calls} · "
        f"**Tokens:** {result.total_tokens:,} · **Est. cost:** ${result.total_cost_usd:.4f}",
        f"- **Daily brief:** [{brief_path}]({brief_path})",
        "",
        "| Tier | Calls | Prompt tok | Completion tok | Est. cost |",
        "|---|---:|---:|---:|---:|",
    ]
    for tier in ("fast", "smart", "deep"):
        cost = result.cost_by_tier.get(tier)
        if not cost:
            continue
        lines.append(
            f"| {tier} | {cost.calls} | {cost.prompt_tokens:,} | "
            f"{cost.completion_tokens:,} | ${cost.cost_usd:.4f} |"
        )
    lines += ["", f"_{DISCLAIMER}_", ""]
    return "\n".join(lines)


def _consensus_cell(result: DeepResult) -> str:
    """The index's consensus column: the gap, and who it is a gap from."""
    positioning = getattr(getattr(result, "evidence", None), "positioning", None)
    decision = result.decision
    if positioning is None or not positioning.target_mean:
        return "—"
    gap = consensus_gap(decision.price_target if decision else None, positioning.target_mean)
    covering = f"{positioning.analyst_count}" if positioning.analyst_count else "?"
    return f"{gap} ({covering} an.)"


def render_deep_index(results: list[DeepResult]) -> str:
    """Body of section 5 of the daily brief, once the deep stage has run."""
    if not results:
        return "_The deep stage ran but the discovery queue was empty; no ticker was analysed._"

    lines = [
        "| Ticker | Sector | Verdict | Target | vs consensus | Horizon | Trader | Report |",
        "|---|---|---|---:|---:|---|---|---|",
    ]
    for r in results:
        d = r.decision
        target = f"${d.price_target:,.2f}" if d and d.price_target is not None else "—"
        lines.append(
            f"| **{r.symbol}** | {r.queued.sector or '—'} | {r.verdict} | {target} | "
            f"{_consensus_cell(r)} | "
            f"{(d.time_horizon if d else None) or '—'} | "
            f"{r.proposal.action if r.proposal else '—'} | "
            f"[deep/{r.symbol}.md](deep/{r.symbol}.md) |"
        )
    lines.append("")
    for r in results:
        if r.decision is not None:
            # A plan the arithmetic rejected must say so where the verdict is
            # skimmed, not only in the deep report nobody opens.
            flag = (
                f" · **{r.trade_plan.status}**"
                if r.trade_plan is not None and not r.trade_plan.actionable
                else ""
            )
            lines.append(f"- **{r.symbol}** — {r.decision.executive_summary}{flag}")
        else:
            lines.append(
                f"- **{r.symbol}** — DEGRADED: "
                f"{r.decision_error or r.aborted or 'the pipeline did not complete'}"
            )
    total_cost = sum(r.total_cost_usd for r in results)
    total_calls = sum(r.total_calls for r in results)
    lines += [
        "",
        f"_{len(results)} ticker(s) · {total_calls} LLM calls · est. ${total_cost:.4f} "
        f"(${total_cost / len(results):.4f} per ticker)._",
    ]
    return "\n".join(lines)


def render_deep_report(
    result: DeepResult, brief_path: str = "../daily-brief.md", options_plan=None
) -> str:
    q = result.queued
    evidence = result.evidence
    # Naming the snapshot in the header, not just in section 7, is deliberate:
    # the number a reader checks first is the one in the verdict, and they
    # should be able to see which moment it belongs to without scrolling.
    snapshot_line = ""
    if evidence is not None and evidence.snapshot_id:
        as_of = evidence.market_as_of.isoformat() if evidence.market_as_of else "unknown"
        snapshot_line = (
            f"Snapshot `{evidence.snapshot_id}` · every price below is the "
            f"{as_of} close.\n\n"
        )
    header = (
        f"# {q.symbol} — {q.name or q.symbol}\n\n"
        f"`{q.sector or 'unknown sector'} / {q.industry or 'unknown industry'}` · "
        f"deep analysis for {evidence.run_date.isoformat() if evidence else 'n/a'}\n\n"
        f"{snapshot_line}"
        "> Research only. Paper trading. The human makes every decision.\n"
    )
    return "\n".join(
        [
            header,
            _verdict(result),
            _analysts(result),
            _debate(result),
            _proposal(result),
            _risk(result),
            _options(options_plan),
            _sources(result),
            _footer(result, brief_path),
        ]
    )
