# Daily Trading Research Agent

A provider-agnostic Python application, built with Claude Code as the coding assistant, that runs a daily market scan → shortlist → multi-agent deep analysis (ported from TradingAgents) → options strategies → report delivery. Deployed on Google Cloud Run Jobs. Human makes all trading decisions. Paper trading only. **Not financial advice.**

## Status

| Milestone | Stage | State |
|---|---|---|
| M1 | `--stage discovery` — breadth, sectors, screener, calendar, shortlist, report, journal | **done** |
| M2 | `--stage deep` — TradingAgents debate pipeline (4 analysts → bull/bear debate → trader → risk committee → portfolio manager) | **done** |
| M3 | signal bundle — news tone, SEC Form 4, FRED macro, Polymarket odds, plus a source-accuracy tracker | **done** |
| M4 | `--stage options` — CSP / covered-call candidates screened off the Alpaca paper chain | **done** |
| M5 | Cloud Run Jobs schedule + delivery | scaffolded in `deploy/` |

## Local quickstart

Python 3.11+ is required, and it is deliberately **not** taken from whatever
`python3` happens to be on the box. Debian bullseye ships 3.9 and the conda
`(base)` env is 3.10 — neither can run this project. `uv` provisions its own
3.11 so the environment does not depend on the host's Python at all.

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh   # one-time, installs to ~/.local/bin
make env                                          # uv venv --python 3.11 + pinned requirements
cp config/.env.example config/.env                # fill free keys + LLM provider
make test                                         # 281 tests, must be green
```

Then each session:

```bash
source .venv/bin/activate && export PYTHONPATH=src   # or: make hint
python -m tradingagent --stage all
```

`PYTHONPATH=src` is required because the package is not pip-installed; the
container image sets it itself. The `make` targets (`test`, `smoke`, `run`)
call `.venv/bin/python` directly and need no activation.

> Do not run the suite from conda `(base)`. It is Python 3.10 with an
> unrelated, older `typer`, and it will fail on missing `feedparser`/`alpaca`
> plus a `make_metavar()` TypeError. See [Environment](#environment).

Output lands in `reports/<date>/daily-brief.md`, per-ticker analyses in
`reports/<date>/deep/<SYM>.md`, and appends to `journal/journal.jsonl` (all git-ignored;
set `REPORTS_BUCKET` to mirror them to GCS instead).

### CLI

```
python -m tradingagent --stage all              # discovery + deep + options, one shared ledger
                       --stage discovery        # scan, screen, shortlist, queue only
                       --stage deep             # deep-dive the queue from an earlier discovery
                       --stage options          # option overlay on an earlier deep run's verdicts
                       --date 2026-08-13        # re-run for a past session
                       --shortlist 5            # shortlist size (default 10)
                       --limit 100              # cap universe, for quick smoke runs
                       --tickers CRM,V,FDX      # deep-stage override, ignores the queue
                       --refresh-universe       # re-pull S&P 500 constituents
                       --skip-llm               # data + screener only, zero token cost
                       --verbose
```

`--skip-llm` is the cheapest way to sanity-check a data change: it produces the same
discovery report minus the commentary and quick takes. It is refused on `deep` and `all`,
which are nothing but LLM calls.

### The deep stage

`discovery` writes `reports/<date>/discovery-context.json` — the market-context block plus
a sector-diversified queue of up to 10 names. `deep` reads it, takes the top
`DEEP_TICKER_CAP` (default 3, cap 5), and runs each ticker through **12 LLM calls**:

| Step | Seats | Tier |
|---|---|---|
| analysts | technical, fundamentals, news, sentiment/positioning | fast × 4 |
| debate | bull, bear (1 round default, `DEBATE_ROUNDS` max 2), research manager | smart × 3 |
| trade | trader | smart × 1 |
| risk | aggressive, conservative, neutral | smart × 3 |
| verdict | portfolio manager — rating, confidence, target, risk ruling | deep × 1 |

Each ticker gets its own report and section 5 of the daily brief becomes an index of
verdicts, links, and per-ticker cost. A role whose output fails its schema is re-prompted
once; if it fails again that ticker is marked DEGRADED and the report says which seat
fell over, rather than dropping the name.

Running `deep` standalone the morning after a discovery run is supported — it reuses the
stored context instead of re-screening the universe, so it only downloads bars for the
queued names.

### The options stage

`deep` writes `reports/<date>/options-context.json` — each verdict plus the spot, the
named support/resistance levels the analysts argued over, and the dividend yield. `options`
reads it, so the overlay can run hours later without re-running a debate.

The verdict picks the strategy, and a verdict that wants neither gets no call:

| PM rating | Overlay | Strike anchored to |
|---|---|---|
| Buy, Overweight | cash-secured put — get paid to bid below the market | nearest support below spot |
| Hold | covered call — sell upside the analysis does not expect | nearest resistance or the price target above spot |
| Underweight, Sell | none, with the reason printed | — |

For each ticker it pulls that side of the Alpaca **paper** chain at 21–45 DTE, solves IV
and delta per contract with Black-Scholes (the free feed supplies neither), rejects
in-the-money strikes, deltas outside 0.20–0.30, open interest under 20, spreads over 20%
and credits under $0.10, scores the survivors on delta fit, liquidity, IV level,
annualised yield, earnings-before-expiry and level alignment, then hands the top three to
a **smart-tier** strategist that must name one by OCC symbol or answer `none`. A symbol
that was not in the table is treated as a hallucination: the pick is dropped and the
ticker marked DEGRADED.

That is **at most one extra LLM call per ticker**, and none for names the verdict skips
or the screen empties. Section 6 of the brief carries the index; section 6 of each deep
report carries the candidates, the reasoning and the data caveats; every recommendation is
journalled with its full basis and the alternatives it beat.

Free-tier reality, stated on every report rather than hidden: Alpaca's OPRA feed requires
a signed subscriber agreement (403 without one), so quotes come from the `indicative`
feed, which carries no greeks, no IV and no per-contract volume. Open interest comes from
the contracts endpoint and settles a day behind. Run outside market hours and the
premiums are the previous session's marks — the report says which timestamp produced
each number and how old it is.

### LLM configuration

Every model call goes through `src/tradingagent/llm.py` (LiteLLM). Three cost tiers, all
set by env — the code never names a provider:

| Tier | Env var | Used by |
|---|---|---|
| fast | `LLM_FAST_MODEL` | the four analysts, quick takes, summarization |
| smart | `LLM_SMART_MODEL` | bull/bear researchers, research manager, trader, the three risk seats, the options strategist |
| deep | `LLM_DEEP_MODEL` | portfolio-manager verdict only; falls back to smart if unset |

Default config targets Vertex AI with Application Default Credentials
(`VERTEXAI_PROJECT` / `VERTEXAI_LOCATION`, no API key). Switching to Anthropic, Gemini,
or a local Ollama is three env-var edits — see the commented block in
`config/.env.example`. Verify a provider before relying on it:

```bash
PYTHONPATH=src python -c "from tradingagent.config import load_settings; from tradingagent.llm import LLMGateway; \
           print(LLMGateway(load_settings()).smoke_test('fast'))"
```

Per-run token usage and estimated cost are accumulated in a `TokenLedger` and printed in
the report footer, broken out by tier. The deep stage additionally prints per-ticker cost
and attributes each ticker's spend to the tier that incurred it.

## Data sources (free tiers only)

| Source | Used for | Free-tier limits |
|---|---|---|
| yfinance | OHLCV for the S&P 500 universe, index proxies, sector ETFs, VIX; fundamentals, quarterly statements, analyst targets, short interest | unofficial API, rate-limited; `info` fields come and go, each is validated |
| Alpaca (paper) | market clock/calendar, snapshot cross-check, option chains and contract open interest | paper endpoints only, enforced in code; OPRA needs a signed agreement (403) → `indicative` quotes, no greeks/IV/volume, open interest settles T-1 |
| Finnhub | earnings calendar, company news, news-tone signal | economic calendar is premium (403) → static fallback |
| RSS (Nasdaq, Seeking Alpha, Yahoo) | market-wide headline tone | unkeyed, no published limit |
| SEC EDGAR | Form 4 insider transactions | unkeyed but fair-access rules apply: `SEC_USER_AGENT` must carry a real contact address or the source skips itself; throttled to 5 req/s against their 10 |
| FRED | macro regime — credit spreads, VIX, curve, yields, claims, dollar | free with a key, no meaningful limit at this volume |
| Polymarket Gamma | event odds on Fed, recession, shutdown, tariffs | public read API, unkeyed |
| bundled `sp500.json` | universe + GICS sectors | snapshot; refresh with `--refresh-universe` |

Any source that fails is named in report section 7 as
`DEGRADED — missing: …`; the run never silently produces a thin report. Paid upgrades
that would remove a limitation are listed there too, and never purchased automatically.

## Signal layer

Four independent sources run once per discovery pass and fuse into a per-ticker bundle
(`src/tradingagent/signals/`). They share nothing but the `SignalSource` contract, so a
fifth — social sentiment — is a registry edit, and dropping a noisy one is a one-line
change. That fifth slot is open, not scheduled: Reddit was the intended source and our
API application was rejected, so the agent has no retail-chatter input at all. Any client
implementing `SignalSource` can fill the slot without touching the bundle, the ranking,
the prompts or the accuracy tracker.

The bundle acts in two places, and deliberately nowhere else:

- **Ranking.** Ticker-level signals adjust the screener score by at most ±8 points, so
  they can promote a name a few places but never override the price screen. The shortlist
  scores twice as many candidates as it keeps, which is what lets a signal pull a name up
  from below the cut. Report section 4 shows every adjustment and the rank each name would
  have had on the screener score alone.
- **Prompts.** Ticker signals reach the news and sentiment analysts inside the evidence
  pack; the market-wide backdrop goes into the shared market context every role sees.
  Market-wide signals never touch the ranking — they shift all candidates equally, so
  scoring them would reorder nothing.

Each source's direction is recorded in the journal *before* the outcome is known, which is
what lets `signals/accuracy.py` grade it later: rolling hit rate over 90 days, rescored
weekly, mapped to a 0.5–1.5 weight and shrunk towards 1.0 while the sample is thin.
Abstentions are not scored, and moves inside a ±1% dead band are dropped rather than
graded as misses. Until a source has a record it runs at weight 1.000.

## Guardrails

- Research only. `ALPACA_PAPER=true` is asserted at startup and the Alpaca client refuses
  to construct otherwise; no order-placement code path exists. The option-chain reader is
  two read-only getters under the same rule — it can price a strike, not sell one.
- Secrets come from env / Secret Manager only, and are never written to reports or logs.
- Every report ends with the verbatim disclaimer from `config/report-schema.md`. Every
  shortlisted name is appended to `journal/journal.jsonl`, every deep verdict is appended
  again with its rating, confidence and price target, and every options recommendation is
  appended with the full basis behind it — credit, collateral, solved IV and delta, the
  level it was anchored to, the open interest and its as-of date, the price basis and its
  timestamp, and the alternatives it beat.

## Tests

```bash
make test     # 281 tests; reference/ cookbooks are excluded from collection
```

`pytest.ini` already sets `-q`, so pass no extra `-q` — `-qq` suppresses the
pass/fail summary line and the run looks like it produced nothing.

## Environment

One environment, built by `make env`, described entirely by `requirements.txt`.

| | |
|---|---|
| Interpreter | uv-managed CPython 3.11 (`~/.local/share/uv/python/…`), not the host's |
| Location | `.venv/` (a real virtualenv — has `bin/activate`) |
| Rebuild | `make clean-env && make env` |
| Live check | `make smoke` — read-only Alpaca option-chain fetch |

`make smoke` is the fastest proof that credentials and `alpaca-py` both work;
the unit tests mock Alpaca on purpose, so they cannot tell you that.

**Why not the host Pythons.** Debian bullseye's apt has no 3.11, and conda
`(base)` is 3.10 with an older `typer` (0.13.1) that calls
`Parameter.make_metavar()` without the `ctx` argument click 8.4 now requires —
plus it has none of this project's dependencies. `uv` sidesteps both by
fetching its own 3.11. An earlier session worked around this with a conda env
created at the path `./.venv`; that looked like a virtualenv but had no
`bin/activate`, which is why `source .venv/bin/activate` used to fail. It has
been replaced.

## Build with Claude Code

`git init`, push to GitHub, open Claude Code at repo root, paste the milestone prompt from `PROMPTS.md`. Claude Code follows `CLAUDE.md`; the app it produces has zero Claude-specific runtime dependencies — no `.claude/` folder, skills, or MCP servers are needed to run it.

## Deploy

See `deploy/cloudrun.md` (recommended: Cloud Run Jobs + Cloud Scheduler) or `deploy/compute-engine.md`.

## Key docs

`BUILD_PLAN.md` (5 milestones) · `PROMPTS.md` (kickoff prompts) · `PORTING_NOTES.md` (cookbook → module mapping, deliberate deviations, paid bottlenecks) · `config/report-schema.md` · `config/preferences.md`

Credits: pipeline design from [TauricResearch/TradingAgents](https://github.com/TauricResearch/TradingAgents) (Apache-2.0); screener/breadth ideas from [tradermonty/claude-trading-skills](https://github.com/tradermonty/claude-trading-skills); options logic ideas from [staskh/trading_skills](https://github.com/staskh/trading_skills).
