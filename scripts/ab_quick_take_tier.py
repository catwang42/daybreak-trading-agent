"""One-off evaluation harness: does the SMART tier disagree with FAST on quick takes?

Gate 1 review flagged that every quick take came back Overweight/Hold at
confidence M. This runs the *identical* prompt through both tiers for a handful
of tickers and prints what changed, so the answer to "is the model hedging or is
the prompt not asking for differentiation?" is evidence rather than a guess.

Not part of the runtime. Nothing here is imported by ``tradingagent``.

    python scripts/ab_quick_take_tier.py V PYPL XOM
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tradingagent.config import load_settings  # noqa: E402
from tradingagent.data.finnhub_client import FinnhubFree  # noqa: E402
from tradingagent.data.market import MarketData  # noqa: E402
from tradingagent.data.universe import load_universe, normalize_sector  # noqa: E402
from tradingagent.data.validate import DegradedTracker  # noqa: E402
from tradingagent.discovery.breadth import analyze_breadth  # noqa: E402
from tradingagent.discovery.calendar import build_calendar  # noqa: E402
from tradingagent.discovery.screener import market_gate_from_breadth, screen_universe  # noqa: E402
from tradingagent.discovery.sectors import build_sector_map  # noqa: E402
from tradingagent.discovery.shortlist import (  # noqa: E402
    QuickTake,
    _earnings_note,
    quick_take_prompt,
)
from tradingagent.llm import LLMGateway, TokenLedger  # noqa: E402


def main(symbols: list[str]) -> int:
    settings = load_settings()
    degraded = DegradedTracker()
    prefs = settings.preferences

    constituents = load_universe()
    market = MarketData(degraded=degraded, period="2y")
    bars = market.load_many([c.symbol for c in constituents], min_rows=60)
    spy = market.load_many(["SPY"], min_rows=250).get("SPY")

    breadth = analyze_breadth(bars, spx_close=spy["Close"] if spy is not None else None)
    sector_map = build_sector_map(
        constituents, bars, market.sector_etf_quotes(), prefs.target_sectors
    )
    candidates = screen_universe(
        bars,
        constituents,
        gate=market_gate_from_breadth(breadth.composite),
        spy_close=spy["Close"] if spy is not None else None,
        preferred_sectors={normalize_sector(s) for s in prefs.target_sectors},
        min_avg_share_volume=prefs.min_avg_volume,
    )
    by_symbol = {c.symbol: c for c in candidates}

    finnhub = FinnhubFree(settings, degraded=degraded)
    calendar = build_calendar(finnhub, settings.run_date, {c.symbol for c in constituents}, degraded)

    ledger = TokenLedger()
    gateway = LLMGateway(settings, ledger)
    rows: list[dict[str, object]] = []

    for symbol in symbols:
        candidate = by_symbol.get(symbol)
        if candidate is None:
            print(f"!! {symbol} is not a candidate today; skipping.")
            continue
        earnings_note, _ = _earnings_note(calendar, symbol, settings.run_date)
        news = finnhub.company_news(symbol, days=7, limit=3)
        news_note = " | ".join(f"{n.headline} ({n.source})" for n in news) or "none retrieved"
        prompt = quick_take_prompt(candidate, breadth, sector_map, earnings_note, news_note)

        takes: dict[str, QuickTake] = {}
        for tier in ("fast", "smart"):
            takes[tier] = gateway.complete(prompt, tier=tier, schema=QuickTake, max_tokens=700)
        rows.append({"symbol": symbol, "score": candidate.score, **takes})

    print(_render(rows, ledger, gateway))
    return 0


def _render(rows: list[dict[str, object]], ledger: TokenLedger, gateway: LLMGateway) -> str:
    out = [
        "# Quick-take tier A/B",
        "",
        "Identical prompt, same temperature, FAST vs SMART.",
        "",
        "| Ticker | Screener | FAST rating | SMART rating | FAST conf | SMART conf | "
        "FAST prio | SMART prio | Changed? |",
        "|---|---:|---|---|---|---|---:|---:|---|",
    ]
    for row in rows:
        fast, smart = row["fast"], row["smart"]
        changed = []
        if fast.rating != smart.rating:
            changed.append("rating")
        if fast.confidence != smart.confidence:
            changed.append("confidence")
        if abs(fast.deep_dive_priority - smart.deep_dive_priority) >= 2:
            changed.append("priority")
        out.append(
            f"| {row['symbol']} | {row['score']} | {fast.rating} | {smart.rating} | "
            f"{fast.confidence} | {smart.confidence} | {fast.deep_dive_priority} | "
            f"{smart.deep_dive_priority} | {', '.join(changed) or 'no'} |"
        )

    out += ["", "## Theses", ""]
    for row in rows:
        out += [
            f"### {row['symbol']}",
            f"- **FAST ({gateway.model_for('fast')}):** {row['fast'].thesis}",
            f"  - risk: {row['fast'].key_risk}",
            f"- **SMART ({gateway.model_for('smart')}):** {row['smart'].thesis}",
            f"  - risk: {row['smart'].key_risk}",
            "",
        ]

    out += ["## Cost", "", "| Tier | Calls | Prompt tok | Completion tok | Cost |", "|---|---:|---:|---:|---:|"]
    for tier, usage in ledger.by_tier.items():
        out.append(
            f"| {tier} | {usage.calls} | {usage.prompt_tokens:,} | "
            f"{usage.completion_tokens:,} | ${usage.cost_usd:.4f} |"
        )
    return "\n".join(out)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:] or ["V", "PYPL", "SPGI"]))
