# PROMPTS.md — Claude Code kickoff prompts (builder only; runtime stays agnostic)

## Milestone 1 — MVP
```
Read CLAUDE.md and BUILD_PLAN.md Milestone 1, then execute it.
Clone the three cookbooks per reference/README.md. Implement llm.py (LiteLLM gateway,
env-driven models, retries, token accounting), data clients (yfinance/Alpaca-paper/
Finnhub free), discovery/ (mine tradermonty breadth/sector/screener logic — flag paid-
gated features, don't add paid services), report renderer per config/report-schema.md,
journal writer, and the Typer CLI (python -m tradingagent --stage discovery).
Pin requirements.txt. pytest green. Run it for today; show me the report, runtime,
and token cost. Stop at Gate 1.
```

## Milestone 2 — Port the pipeline
```
Read CLAUDE.md, BUILD_PLAN.md Milestone 2, PORTING_NOTES.md. Study
reference/TradingAgents (agents/ + graph/), fill the PORTING_NOTES mapping, port the
role prompts into src/tradingagent/pipeline/prompts/ (provider-agnostic markdown),
implement pipeline modules with pydantic schema enforcement (re-prompt once → DEGRADED),
FAST model for analysts / SMART for manager, judge, PM, debate 1 round (max 2),
cap DEEP_TICKER_CAP. Wire --stage deep. Run on 3 tickers from today's shortlist,
show one full deep report + token cost. Stop at Gate 2.
```

## Milestone 3 — Signals
```
Read CLAUDE.md and BUILD_PLAN.md Milestone 3, then execute it. Direct API clients only
(no MCP): Finnhub news + RSS, SEC EDGAR insider, FRED, Polymarket REST, PRAW personal.
Build the per-ticker signal bundle feeding shortlist ranking + analyst/debate prompts,
plus the source-accuracy tracker in signals/. Defer YouTube. Show me a report where a
signal visibly changed ranking or debate. Stop at Gate 3.
```

## Milestone 4 — Options
```
Read CLAUDE.md and BUILD_PLAN.md Milestone 4, then execute it. Mine staskh logic into
options/strategies.py, use Alpaca paper option chains, add the options_strategist step
after the PM verdict, merge per report-schema section 6, journal every options rec.
Show one ticker with equity verdict + CSP/CC candidates.
```

## Milestone 5 — Deploy
```
Read CLAUDE.md, BUILD_PLAN.md Milestone 5, and deploy/cloudrun.md. Finalize the
Dockerfile, verify current gcloud syntax, walk me through deploy/setup.sh (I run the
commands), implement delivery/telegram.py and GCS persistence (REPORTS_BUCKET),
add retries + DEGRADED handling, then verify one full unattended cloud execution.
```

## Parallel tracks (week 1) — git worktrees
Track A = Milestone 1. Track B = Milestone 2 STUDY ONLY (PORTING_NOTES + prompt files
under pipeline/prompts/ — no runtime code, no CLI edits). They touch disjoint files.
```bash
git worktree add ../ta-track-a track-a
git worktree add ../ta-track-b track-b
```
Merge track-a → main, rebase track-b, merge, then run M2 implementation in ONE session
on main. Don't parallelize pipeline implementation itself. Inside any session, let
Claude Code fan out subagents for read-only chores (e.g. "summarize all tradermonty
skills and which need paid APIs, in parallel").
