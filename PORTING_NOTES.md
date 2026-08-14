# PORTING_NOTES.md — cookbooks → provider-agnostic modules

> reference/ repos are read-only. Ideas, prompts, and logic are ported into src/ with attribution (TradingAgents: Apache-2.0, cite repo + arXiv:2412.20138; note licenses of the two skills repos when mining).
>
> Nothing under `reference/` is imported or executed by the runtime. `pytest.ini`
> excludes it from collection; `.gitignore` keeps the clones out of this repo.

## Upstream versions studied
| Cookbook | Repo | Release/commit | Date |
|---|---|---|---|
| TradingAgents | github.com/TauricResearch/TradingAgents | `a33fd4c` | 2026-07-18 |
| tradermonty skills | github.com/tradermonty/claude-trading-skills | `769a6c8` | 2026-08-12 |
| staskh trading_skills | github.com/staskh/trading_skills | `658dcc1` | 2026-07-30 |

## Pipeline mapping (TradingAgents → src/tradingagent/pipeline/)
| Upstream | Local module / prompt file | Changes | Data swapped |
|---|---|---|---|
| fundamentals analyst | pipeline/analysts.py + prompts/analyst_fundamentals.md | M2 | Alpha Vantage → yfinance/Alpaca |
| technical analyst | prompts/analyst_technical.md | M2 | |
| news analyst | prompts/analyst_news.md | M2 | Finnhub + signal bundle (M3) |
| sentiment analyst | prompts/analyst_sentiment.md | M2 | Reddit/PRAW (M3) |
| bull/bear researchers | pipeline/debate.py + prompts/researcher_bull.md / _bear.md | rounds capped 1–2 | |
| research manager | pipeline/debate.py (arbiter) | M2 | |
| trader | pipeline/trader.py + prompts/trader.md | M2 | |
| risk agg/cons/neutral + judge | pipeline/risk.py + prompts/risk_*.md | M2 | |
| portfolio manager | pipeline/portfolio_manager.py + prompts/portfolio_manager.md | 5-tier + soft target per report-schema | |
| LangGraph state machine | main.py stage orchestration + pydantic schemas | deliberate simplification | |

**M1 status:** nothing from TradingAgents is ported yet. What M1 *did* take from it is
structural: the "one analyst = one plain-text prompt + one pydantic schema, called
through a single gateway" shape used by `pipeline/prompts_loader.py`,
`pipeline/prompts/quick_take.md`, and `discovery/shortlist.QuickTake`. The M1 quick-take
is a single FAST-tier call, not a debate — it exists to rank the deep-dive queue that M2
will consume.

## Discovery mapping (tradermonty → src/tradingagent/discovery/)
| Skill | Local module | Notes |
|---|---|---|
| market-breadth-analyst | discovery/breadth.py | 6 weighted components ported; data source swapped (see below) |
| theme/sector detector | discovery/sectors.py | GICS-sector aggregate; industry granularity is FINVIZ-Elite-gated upstream |
| stockbee momentum burst screener | discovery/screener.py | 4 triggers + 100-point budget ported; S&P 500 universe only |
| VCP / CANSLIM screeners | discovery/screener.py (technical parts only) | fundamental C/A/I components are FMP-gated — not ported |
| economic/earnings calendar | discovery/calendar.py | earnings = Finnhub free; macro = static schedule (FMP-gated upstream) |

### What was ported, concretely
- **breadth.py** — the six-component composite and its weights (level & trend 0.25,
  8MA/200MA crossover 0.20, peak/trough cycle position 0.20, bearish-signal status 0.15,
  historical percentile 0.10, S&P divergence 0.10), the health-zone → equity-exposure
  table, and the 8MA level → score bands. Added: proportional weight redistribution so a
  missing component rescales the rest instead of silently dragging the score down, plus a
  data-quality label in the report.
- **sectors.py** — cyclical / defensive / commodity bucketing, the cyclical−defensive
  momentum spread as a risk-regime proxy, and the four-phase cycle model with its
  leader/laggard sector sets.
- **screener.py** — the Stockbee momentum-burst trigger set (4% breakout, dollar
  breakout, range expansion, 9M-share volume), the hard-rejection floors (price ≥ $5,
  volume ≥ 100k, risk-to-stop ≤ 12%, base width ≤ 20%), the soft failure filters
  (extended run-up, recent breakdown, narrow prior-day range), and the
  A / A− / B / Watch / Reject rating bands over a 100-point component budget.

## Options mapping (staskh → src/tradingagent/options/)
| Skill/tool | Local module | Notes |
|---|---|---|
| covered-call finder / CSP logic | options/strategies.py | M4; IBKR → Alpaca chains |

## Deliberate deviations & upstream releases reviewed

- **Breadth and sector uptrend ratios are computed from our own universe, not from
  upstream's hosted CSV pipeline.** tradermonty's breadth skill reads pre-built
  market-breadth CSVs produced by a separate data job. We recompute "% of universe above
  the N-day MA" directly from the yfinance OHLCV pull we already do. Cost: our history is
  ~2 years, so the "historical percentile" and "peak/trough cycle position" components
  are percentiles of a shorter window than upstream's. Benefit: no third-party pipeline
  and no paid feed.
- **Sector overbought/oversold thresholds rescaled 0.37 / 0.097 → 0.80 / 0.20.**
  Upstream compares against its own stricter "uptrend ratio", which lives on a lower
  scale. Ours is plainly "% of sector members above their 50DMA", which sits in the
  40–80% band on an ordinary day. Applying the upstream constants verbatim tagged nine of
  eleven sectors "Overbought" and made the column meaningless. Locked in by
  `tests/test_discovery.py::test_status_thresholds_are_calibrated_to_our_metric`.
- **Universe is the bundled S&P 500 snapshot** (`data/sp500.json`, 503 names, GICS
  sector/industry, refreshable with `--refresh-universe`). Upstream screeners scan the
  full US market via FMP. Small/mid-cap momentum bursts outside the S&P 500 are invisible
  to us — flagged in report section 7, not silently absorbed.
- **Macro calendar degrades to a static release schedule.** Finnhub's
  `calendar_economic()` is premium (403 on free tier) and upstream uses FMP. Rather than
  add a paid service, `discovery/calendar.py` derives the recurring release dates (NFP,
  CPI, PPI, retail sales, ISM, PCE, jobless claims) and the report says in-line that it
  is indicative, not a live feed.
- **No LangGraph, no agent framework.** Stage orchestration is plain Python in
  `stages.py`; every LLM call goes through `llm.LLMGateway` so the provider stays an env
  variable.
- **Screener liquidity floor uses average *share* volume**, per `config/preferences.md`
  (1M shares), not the dollar-volume floor some upstream variants use.

### Paid bottlenecks identified (not purchased — surfaced in report section 7)
| Capability | Upstream source | Cost | Our substitute |
|---|---|---|---|
| Full US universe scan | FMP | $22–29/mo | S&P 500 snapshot |
| CANSLIM C/A/I fundamentals | FMP or Finviz Elite | ~$40/mo | technical-only screening |
| Live economic calendar | FMP / Finnhub premium | paid tier | static recurring schedule |
| Industry-granularity theme detection | FINVIZ Elite | ~$40/mo | GICS sector aggregates |
