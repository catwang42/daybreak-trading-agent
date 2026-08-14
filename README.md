# Daily Trading Research Agent

A provider-agnostic Python application, built with Claude Code as the coding assistant, that runs a daily market scan → shortlist → multi-agent deep analysis (ported from TradingAgents) → options strategies → report delivery. Deployed on Google Cloud Run Jobs. Human makes all trading decisions. Paper trading only. **Not financial advice.**

## Local quickstart
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp config/.env.example config/.env   # fill free keys + LLM provider
python -m tradingagent --stage discovery   # after Milestone 1 is built
```

## Build with Claude Code
`git init`, push to GitHub, open Claude Code at repo root, paste the Milestone 1 prompt from `PROMPTS.md`. Claude Code follows `CLAUDE.md`; the app it produces has zero Claude-specific runtime dependencies.

## Deploy
See `deploy/cloudrun.md` (recommended: Cloud Run Jobs + Cloud Scheduler) or `deploy/compute-engine.md`.

## Key docs
`BUILD_PLAN.md` (5 milestones) · `PROMPTS.md` (kickoff prompts) · `PORTING_NOTES.md` (cookbook → module mapping) · `config/report-schema.md`

Credits: pipeline design from [TauricResearch/TradingAgents](https://github.com/TauricResearch/TradingAgents) (Apache-2.0); screener/breadth ideas from [tradermonty/claude-trading-skills](https://github.com/tradermonty/claude-trading-skills); options logic ideas from [staskh/trading_skills](https://github.com/staskh/trading_skills).
