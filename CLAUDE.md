# CLAUDE.md — build instructions (Claude Code is the BUILDER, never a runtime dependency)

## Mission
Build a Claude-agnostic Python application per BUILD_PLAN.md: a daily trading RESEARCH agent that scans the market, shortlists tickers, runs a ported TradingAgents-style multi-agent analysis, adds options strategies (CSP/covered calls), and delivers a daily report. Deployed on Google Cloud (Cloud Run Jobs). Human makes all decisions.

## Architecture rules (non-negotiable)
- RUNTIME IS PROVIDER-AGNOSTIC. All LLM calls go through `src/tradingagent/llm.py` (LiteLLM wrapper). Model names come from env (`LLM_FAST_MODEL`, `LLM_SMART_MODEL`) — never hardcode a provider or model. The app must run identically with Anthropic API, Vertex AI, Gemini, or Ollama.
- NO Claude Code constructs in the runtime: no `.claude/` folder, no skills, no MCP servers as runtime deps. External data comes from direct API clients in `src/tradingagent/data/` and `signals/` (yfinance, Alpaca SDK, Finnhub, feedparser, EDGAR, FRED, PRAW, Polymarket REST).
- Prompts are plain text/markdown files in `src/tradingagent/pipeline/prompts/`, loaded at runtime — portable to any provider.
- `reference/` repos (TradingAgents, tradermonty/claude-trading-skills, staskh/trading_skills) are READ-ONLY cookbooks: mine their prompts/logic/scripts into our modules with attribution in PORTING_NOTES.md. Never import or execute them.
- Entrypoint: `python -m tradingagent` (full daily scan) with stage flags (`--stage discovery|deep|options|report`). Must run headless in a container.

## Hard guardrails
- RESEARCH ONLY. Alpaca PAPER endpoints only; refuse any live-order code path. `ALPACA_PAPER=true` is asserted at startup.
- Secrets only via env / Secret Manager. Never in code, logs, reports, or git.
- Free data tiers first; flag paid bottlenecks in the report/PR, don't add paid services unprompted.
- Every recommendation carries the disclaimer footer from `config/report-schema.md` and is appended to `journal/journal.jsonl` (locally) / GCS (in cloud).

## Engineering conventions
- Python 3.11+, pinned `requirements.txt`, `uv` or venv. Typer for CLI, pydantic for schemas, tenacity for retries.
- pytest for any helper ≥30 lines; `pytest -q` green before done. Small conventional commits.
- Validate every market-data response (NaN/zero-volume/empty). Failed sources → visible "DEGRADED — missing: X" report section, never a silently thin report.
- Malformed LLM output vs `config/report-schema.md`: re-prompt once, then mark ticker DEGRADED.

## Cost discipline (API tokens now, not subscription)
- Deep-analysis cap 3–5 tickers/day; debate 1 round default, 2 max.
- `LLM_FAST_MODEL` (Haiku-class) for the 4 analysts and summarization; `LLM_SMART_MODEL` (Sonnet-class) only for research-manager, risk-judge, portfolio-manager. Log token usage per run into the report footer.

## Deployment target
- Primary: Cloud Run Jobs + Cloud Scheduler + Secret Manager + GCS (see `deploy/`). Alternative: Compute Engine e2-micro + cron. Reports/journal persist to `REPORTS_BUCKET` when set, else local dirs.

## Definition of done (per milestone)
Milestone done when: its stage runs end-to-end headless on a real trading day, output matches the schema, journal written, tests pass, PORTING_NOTES/README updated. Then STOP and present the decision-gate evidence from BUILD_PLAN.md.
