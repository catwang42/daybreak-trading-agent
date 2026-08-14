# Daily Trading Research Agent — Build Plan v3 (Claude-Agnostic Runtime, 5 Milestones)

> Strategy: Claude Code is the BUILDER; the product is a provider-agnostic Python app (see CLAUDE.md architecture rules), deployed on Google Cloud Run Jobs. TradingAgents, tradermonty skills, and staskh skills are READ-ONLY cookbooks in reference/ — their prompts and logic get ported into src/ (see PORTING_NOTES.md), never installed as runtime deps.
>
> Billing reality: an agnostic runtime cannot use the Claude Code subscription — LLM calls are API-billed (Anthropic direct or Claude via Vertex AI, or any provider). Cost control = model tiering + ticker caps (CLAUDE.md). Expect ~$10–60/mo at 3–5 deep tickers/day with a Haiku-heavy mix.
>
> Research tool only. Paper trading. Human decides. Not financial advice.

## Milestone 1 (Days 1–4): Runnable MVP — `python -m tradingagent`
Goal: one command produces reports/<date>/daily-brief.md (market overview, macro/earnings calendar, sector map, 5–10 ticker shortlist, quick LLM take + preliminary rating per ticker) and a journal entry.
Tasks: project setup (venv, pinned requirements, pytest) → implement llm.py gateway → data clients (yfinance, Alpaca paper, Finnhub free) → mine tradermonty screener/breadth/sector logic into discovery/ (flag anything FMP/Finviz-Elite-gated; do NOT add paid) → report renderer per config/report-schema.md → journal writer.
LLM usage starts here (quick takes on FAST model — cents/day).
GATE 1: report useful? runtime + token cost per run acceptable? (log tokens in report footer)

## Milestone 2 (Week 2): Port the TradingAgents pipeline into src/pipeline/
Goal: top 3–5 shortlisted tickers get analysts → bull/bear debate (1–2 rounds) → trader → 3 risk voices + judge → portfolio manager → 5-tier verdict + soft target, written to reports/<date>/deep/<ticker>.md.
Tasks: study reference/TradingAgents (agents/ + graph/) → fill PORTING_NOTES mapping → port role prompts into pipeline/prompts/*.md → implement pipeline modules with pydantic schemas + schema enforcement (re-prompt once, then DEGRADED) → tiering: analysts on FAST, manager/judge/PM on SMART → wire as --stage deep.
OPTIONAL one-off benchmark: run the real TradingAgents on the same 3 tickers via API, compare, delete install.
GATE 2: does deep analysis change decisions vs M1 quick takes often enough to justify tokens?

## Milestone 3 (Week 3): Signal-fusion layer (direct APIs, no MCP)
Goal: per-ticker signal bundle (news tone, insider buys/sells, Reddit mention spikes, prediction-market odds, macro) re-ranks the shortlist and feeds analyst/debate prompts.
Tasks: signals/ clients — Finnhub news + RSS (feedparser), SEC EDGAR (insider), FRED (macro), Polymarket REST, PRAW (personal OAuth, non-commercial) → source-accuracy tracker scored weekly against journal outcomes → DEFER YouTube; skip Discord/Substack (ToS).
GATE 3: signals measurably change decisions or just burn tokens? Drop noisy sources.
Paid triggers (optional): Unusual Whales $48/mo for real options flow; Tavily/AskNews ~$25/mo if free news too shallow.

## Milestone 4 (Week 4): Options strategy stage
Goal: per recommended ticker, CSP (bullish entry) and covered-call (holdings) candidates — strike ≈ support/target, delta ~0.2–0.3, premium, annualized yield, earnings flag — merged into report + journal.
Tasks: mine staskh logic into options/strategies.py → Alpaca paper option chains (adapt from IBKR) → options_strategist prompt runs AFTER PM verdict with full context → --stage options.

## Milestone 5 (Week 5): Deploy on Google Cloud + delivery
Goal: unattended daily run, report delivered by your afternoon (SGT).
Tasks: Dockerfile finalized → deploy/setup.sh (Cloud Run Jobs + Scheduler 08:00 ET weekdays + Secret Manager + GCS for reports/journal) → Telegram delivery in delivery/ → retries, data validation, DEGRADED banner → one full unattended cloud run verified. Alternative: deploy/compute-engine.md.
GATE: clean unattended run 3 days straight.

## Free vs Paid quick reference
LLM API ~$10–60/mo (the real cost; Vertex AI option keeps it on GCP billing) · GCP infra <$2/mo (Jobs+Scheduler+GCS+Registry) · Alpaca paper/EDGAR/FRED/Polymarket/Reddit-personal/yfinance/Finnhub free · Paid only on proven bottleneck: FMP $22–29, Finviz Elite ~$40, Unusual Whales $48, Polygon $79, Tavily ~$25.

## After Milestone 5
8+ weeks daily paper runs; weekly journal review (win rate, vs SPY). Look-Ahead-Bench / Profit Mirage show LLM "alpha" typically halves out-of-sample — the journal is the only benchmark that counts. Backlog: YouTube transcripts → options flow → sector-rotation memory → upstream prompt re-diffs → live brokerage (only after evidence).
