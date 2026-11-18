# CLAUDE.md — build instructions (Claude Code is the BUILDER, never a runtime dependency)

## Mission (V2)
Evolve Daybreak per `DAYBREAK_V2_EVOLUTION_PLAN.md`. Its core product is an **AI-economy intelligence module**: owner-approved theses, value-chain monitoring, and who captures the value. The existing V1 trading research pipeline (scan → shortlist → TradingAgents-style debate → CSP/covered-call overlay → report) becomes its **actionability layer**. It is delivered as a bilingual brief at two Singapore decision windows. Deployed on Google Cloud (Cloud Run Jobs). The human makes all decisions.

`BUILD_PLAN.md` is the **completed V1 record** (M1–M8), kept for history. It is superseded by `DAYBREAK_V2_EVOLUTION_PLAN.md`. Where the two disagree, the V2 plan wins.

## Workstreams (exactly three)
- **WS1 — AI-economy intelligence** (`src/tradingagent/intel/`):
  - Ingests SemiAnalysis, earnings-call transcripts, and SEC EDGAR events (8-K, 10-Q/10-K, and 6-K/20-F for ADRs).
  - Keeps an append-only thesis ledger: evidence, linked tickers, confidence, invalidation conditions, and dated predictions.
  - Output is change-driven. Never restate an unchanged thesis.
  - Produces a daily "what changed" section and a weekly deep-dive.
- **WS2 — Decision-window delivery:**
  - One bilingual decision sheet (中文 executive summary + English detail) per window.
  - Email carries the long form, on the M8 HTML/PDF machinery.
  - Telegram carries batched, deduplicated alerts.
- **WS3 — Trading-layer upgrades:**
  - Universe = S&P 500 + Nasdaq-100 + watchlist ADRs.
  - Exactly one new scanner (quality-pullback/dislocation), A/B'd against the momentum baseline in the M7 ledger.
  - OPRA-quality option data where the feed allows.
  - Outcome evaluation extended to the new scanner and to thesis-prediction scoring.

## Binding constraints (owner-approved — do not re-litigate; record disagreement in the plan's ADR)
- **Windows:** ~21:00 SGT (US pre-market) and ~07:30 SGT (US post-close). All monitoring batches to these windows.
  - No intraday, streaming, or always-on infrastructure. No webhooks.
  - No dashboard.
  - The post-close product is deterministic, with zero LLM calls.
- **Output:** bilingual. The Chinese text may only restate numbers present in the English source facts. It must never say "recommend" or claim accuracy. WhatsApp is deferred.
- **Budget:** $300–500/month all-in. Spend buys information quality, not latency. At most two paid content sources initially: SemiAnalysis + one transcript API. The Information is a later option.
- **Universe:** S&P 500 + Nasdaq-100 + `config/watchlist.yaml`, which must include TSM, ASML, AMD, GOOGL/GOOG, MSFT, UNH, NVDA, AVGO, LRCX.
  - Focus is tech / AI / AI-infra. Other sectors surface only through the deterministic regime-shift gate.
  - No Russell 1000.
  - Breadth is computed over S&P 500 members only.
- **Portfolio:** position-blind, advisory only.
- **Options:** 21–45 DTE cash-secured puts and covered calls only.
- **Discovery:** the momentum screener is the control. The pullback/dislocation scanner is the only new scanner. No other new scanners, signal sources, agents, or debate rounds.
- **Deep debate:** KEEP as is (`DEBATE_ROUNDS=1` default, 2 max; cap 3–5 tickers/day). The `DEEP_PROFILE` A/B hook stays default-off until the scanner A/B reaches its minimum sample.
- **Timeline:** phases ordered by decision value. No calendar dates. Each phase is independently shippable, with acceptance criteria.

## NOT building
Intraday/streaming, dashboards, portfolio/position accounting, WhatsApp, new agents or debate rounds, new signal sources beyond the stated ones, Russell 1000, compliance/restricted-list features (owner manages personally), live trading of any kind.

## Architecture rules (non-negotiable)
- **The runtime is provider-agnostic.**
  - All LLM calls go through `src/tradingagent/llm.py` (the LiteLLM wrapper).
  - Model names come from env (`LLM_FAST_MODEL`, `LLM_SMART_MODEL`, `LLM_DEEP_MODEL`). Never hardcode a provider or model.
  - The app must run identically with the Anthropic API, Vertex AI, Gemini, or Ollama.
  - Changing a model is a deliberate, logged change at a phase boundary, never mid-experiment, because it partitions the ledger.
- **No Claude Code constructs in the runtime.** No `.claude/` folder, no skills, no MCP servers as runtime deps.
  - External data comes from direct API clients in `src/tradingagent/data/`, `signals/`, and `intel/sources/`.
  - The V1 clients are: yfinance, Alpaca SDK, Finnhub, feedparser, EDGAR, FRED, PRAW, and Polymarket REST.
  - V2 adds: the transcript API, SemiAnalysis via IMAP, and the Telegram Bot API over HTTPS.
- **Prompts** are plain markdown files in `src/tradingagent/pipeline/prompts/`, loaded at runtime. WS1 prompts use the `intel_` prefix there so provenance hashes them.
- **WS1 is separable.**
  - `intel/` shares only `llm.py`, `snapshot.py` as-of discipline, `evaluation/ledger.py` + `storage.py`, and delivery/presentation via a written `intel-digest.json`.
  - `intel/` must not import `pipeline/`, `discovery/`, or `options/`, and vice versa. `tests/test_boundaries.py` enforces this.
  - Thesis-linked tickers are a read-only lookup into trade-layer state. They are never queued into deep analysis.
- **Cross-run state lives in GCS.** A fresh container restores the journal, the ledger, and recent window contexts. Never rely on local disk between runs.
- **Report artifacts are namespaced by date and window:** `reports/<date>/` for pre-market, `reports/<date>/postclose/` for post-close.
- **`reference/` repos are read-only cookbooks.** These are TradingAgents, tradermonty/claude-trading-skills, and staskh/trading_skills. Mine them with attribution in `PORTING_NOTES.md`. Never import or execute them.
- **Entrypoint:** `python -m tradingagent`, with `--stage discovery|deep|options|report|all|outcomes|evaluate|postclose|intel|weekly`. It must run headless in a container.

## Hard guardrails
- **RESEARCH ONLY.** Alpaca PAPER endpoints only. Refuse any live-order code path. `ALPACA_PAPER=true` is asserted at startup.
- **Secrets** only via env / Secret Manager. Never in code, logs, reports, or git.
- **Licensed content** (SemiAnalysis, transcripts):
  - No scraping of paid sites.
  - Never persist full text. Store metadata plus quotes of at most 50 words per evidence item.
  - No redistribution.
  - SemiAnalysis ingestion waits on the owner's ToS decision (plan ADR D6).
- **Free data tiers first.** Flag paid bottlenecks in the report/PR. Don't add paid services unprompted.
- **Disclaimer and journal:** every recommendation carries the disclaimer footer from `config/report-schema.md` and is appended to `journal/journal.jsonl` locally, or to GCS in the cloud.
- **Integrity safeguards:** preserve every M6 safeguard (one snapshot per run, as-of-safe retrieval, look-ahead guard, computed trade math, shadowed signals). Preserve the M7 ledger's append-only, provenance-stamped semantics.

## Engineering conventions
- Python 3.11+, pinned `requirements.txt`, `uv` or venv. Typer for the CLI, pydantic for schemas, tenacity for retries.
- **Tests:** pytest for any helper of 30 lines or more. `pytest -q` must be green before done.
- **Market data:** validate every response (NaN, zero volume, empty).
  - A failed source produces a visible "DEGRADED — missing: X" report section, never a silently thin report.
  - DEGRADED is threshold-based for bulk pulls: more than 2% of the universe, or any benchmark, shortlist, or watchlist name.
  - New listings below the bar floor are "ineligible", not degraded.
- **Malformed LLM output** vs `config/report-schema.md`: re-prompt once, then mark the ticker or section DEGRADED. Log `finish_reason` on empty completions.
- **Workflow:**
  - Feature branches, small conventional commits.
  - **Chores** may auto-merge only when CI passes (tests + docker build + secret scan + boundary test) and the PR states explicit criteria. Paths that are never chores: `pipeline/prompts/**`, `deploy/**`, `Dockerfile`, `config/report-schema.md`, `evaluation/ledger.py`, `llm.py`.
  - **Milestones** are merged manually by the owner.
- **Deploys:** stamp `GIT_COMMIT` on every deploy.

## Cost discipline (API tokens)
- Deep-analysis cap 3–5 tickers/day. Debate 1 round by default, 2 max.
- **Model tiers:**
  - `LLM_FAST_MODEL` (Haiku-class) for the 4 analysts, summarization, WS1 extraction, and the Chinese summary.
  - `LLM_SMART_MODEL` / `LLM_DEEP_MODEL` only for the research manager, risk seats, PM, WS1 adjudication, and the weekly deep-dive.
- WS1 has a per-run token cap (`INTEL_MAX_TOKENS_PER_RUN`).
- Log token usage and cost per run into the report footer. The weekly report shows monthly spend vs the plan's §5.2 projection.

## Deployment target
- **Cloud Run Jobs:**
  - `ta-daily`, pre-market: `0 8 * * 1-5` America/New_York.
  - `ta-postclose`, post-close: `30 18 * * 1-5` America/New_York.
  - Both use Cloud Scheduler, Secret Manager, and GCS (see `deploy/`). Reports, journal, and ledger persist to `REPORTS_BUCKET`.
- **Alternative:** Compute Engine e2-micro + cron.

## Evaluation
- No win-rate or accuracy claims below `MIN_SAMPLE` (20) per cell. Print counts and INSUFFICIENT instead.
- Every comparison names its baseline: SPY, sector ETF, SOXX/QQQ for AI baskets, the momentum control, or random-k.
- Theses are scored only through dated predictions (Brier, calibration), never through confidence moves.

## Definition of done (per phase)
A phase is done when all of the following hold:
- Its stage runs end-to-end headless on a real trading day.
- Output matches the schema.
- Journal and ledger are written and mirrored.
- Tests pass and CI is green.
- `PORTING_NOTES.md` and `README.md` are updated.

Then STOP and present the phase's acceptance evidence from `DAYBREAK_V2_EVOLUTION_PLAN.md` §6.
