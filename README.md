# Daily Trading Research Agent

A provider-agnostic Python application, built with Claude Code as the coding assistant, that runs a daily market scan → shortlist → multi-agent deep analysis (ported from TradingAgents) → options strategies → report delivery. Deployed on Google Cloud Run Jobs. Human makes all trading decisions. Paper trading only. **Not financial advice.**

## Status

| Milestone | Stage | State |
|---|---|---|
| M1 | `--stage discovery` — breadth, sectors, screener, calendar, shortlist, report, journal | **done** |
| M2 | `--stage deep` — TradingAgents debate pipeline | not started |
| M3 | signal bundle (Reddit, EDGAR, FRED, Polymarket) | not started |
| M4 | `--stage options` — CSP / covered-call candidates | not started |
| M5 | Cloud Run Jobs schedule + delivery | scaffolded in `deploy/` |

## Local quickstart

Python 3.11+ is required.

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp config/.env.example config/.env   # fill free keys + LLM provider
python -m tradingagent --stage discovery
```

Output lands in `reports/<date>/daily-brief.md` and appends to `journal/journal.jsonl`
(both git-ignored; set `REPORTS_BUCKET` to mirror them to GCS instead).

### CLI

```
python -m tradingagent --stage discovery        # today's full scan
                       --date 2026-08-13        # re-run for a past session
                       --shortlist 5            # shortlist size (default 10)
                       --limit 100              # cap universe, for quick smoke runs
                       --refresh-universe       # re-pull S&P 500 constituents
                       --skip-llm               # data + screener only, zero token cost
                       --verbose
```

`--skip-llm` is the cheapest way to sanity-check a data change: it produces the same
report minus the commentary and quick takes.

### LLM configuration

Every model call goes through `src/tradingagent/llm.py` (LiteLLM). Three cost tiers, all
set by env — the code never names a provider:

| Tier | Env var | Used by |
|---|---|---|
| fast | `LLM_FAST_MODEL` | the four analysts, quick takes, summarization |
| smart | `LLM_SMART_MODEL` | research manager, risk judge (M2) |
| deep | `LLM_DEEP_MODEL` | portfolio manager (M2); falls back to smart if unset |

Default config targets Vertex AI with Application Default Credentials
(`VERTEXAI_PROJECT` / `VERTEXAI_LOCATION`, no API key). Switching to Anthropic, Gemini,
or a local Ollama is three env-var edits — see the commented block in
`config/.env.example`. Verify a provider before relying on it:

```bash
python -c "from tradingagent.config import load_settings; from tradingagent.llm import LLMGateway; \
           print(LLMGateway(load_settings()).smoke_test('fast'))"
```

Per-run token usage and estimated cost are accumulated in a `TokenLedger` and printed in
the report footer, broken out by tier.

## Data sources (free tiers only)

| Source | Used for | Free-tier limits |
|---|---|---|
| yfinance | OHLCV for the S&P 500 universe, index proxies, sector ETFs, VIX | unofficial API, rate-limited |
| Alpaca (paper) | market clock/calendar, snapshot cross-check | paper endpoints only, enforced in code |
| Finnhub | earnings calendar, company news | economic calendar is premium (403) → static fallback |
| bundled `sp500.json` | universe + GICS sectors | snapshot; refresh with `--refresh-universe` |

Any source that fails is named in report section 7 as
`DEGRADED — missing: …`; the run never silently produces a thin report. Paid upgrades
that would remove a limitation are listed there too, and never purchased automatically.

## Guardrails

- Research only. `ALPACA_PAPER=true` is asserted at startup and the Alpaca client refuses
  to construct otherwise; no order-placement code path exists.
- Secrets come from env / Secret Manager only, and are never written to reports or logs.
- Every report ends with the verbatim disclaimer from `config/report-schema.md`, and
  every shortlisted name is appended to `journal/journal.jsonl` for later outcome
  scoring.

## Tests

```bash
pytest -q     # 76 tests; reference/ cookbooks are excluded from collection
```

## Build with Claude Code

`git init`, push to GitHub, open Claude Code at repo root, paste the Milestone 1 prompt from `PROMPTS.md`. Claude Code follows `CLAUDE.md`; the app it produces has zero Claude-specific runtime dependencies.

## Deploy

See `deploy/cloudrun.md` (recommended: Cloud Run Jobs + Cloud Scheduler) or `deploy/compute-engine.md`.

## Key docs

`BUILD_PLAN.md` (5 milestones) · `PROMPTS.md` (kickoff prompts) · `PORTING_NOTES.md` (cookbook → module mapping, deliberate deviations, paid bottlenecks) · `config/report-schema.md` · `config/preferences.md`

Credits: pipeline design from [TauricResearch/TradingAgents](https://github.com/TauricResearch/TradingAgents) (Apache-2.0); screener/breadth ideas from [tradermonty/claude-trading-skills](https://github.com/tradermonty/claude-trading-skills); options logic ideas from [staskh/trading_skills](https://github.com/staskh/trading_skills).
