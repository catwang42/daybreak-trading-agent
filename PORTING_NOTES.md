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

Upstream paths are relative to `reference/TradingAgents/tradingagents/`. Tier is ours,
set by CLAUDE.md's cost policy; upstream runs one "quick" and one "deep" model.

| Upstream | Local module / prompt file | Tier | Changes | Data swapped |
|---|---|---|---|---|
| `agents/analysts/market_analyst.py` | pipeline/analysts.py + prompts/analyst_technical.md | FAST | tool belt → pre-computed indicator table | its 8 `get_stockstats_*` tools → data/indicators.py (pure pandas over the yfinance pull) |
| `agents/analysts/fundamentals_analyst.py` | prompts/analyst_fundamentals.md | FAST | same | SimFin/Finnhub premium statements → data/fundamentals.py (yfinance free tier) |
| `agents/analysts/news_analyst.py` | prompts/analyst_news.md | FAST | same; macro read folded into the shared market-context block | Google News + Finnhub premium → Finnhub free company news + our breadth/sector context |
| `agents/analysts/sentiment_analyst.py` + `social_media_analyst.py` | prompts/analyst_sentiment.md | FAST | two upstream seats merged into one | Reddit/StockTwits → sell-side posture, target dispersion, short interest, holder mix (M3 adds PRAW) |
| `agents/researchers/bull_researcher.py` / `bear_researcher.py` | pipeline/debate.py + prompts/researcher_bull.md / _bear.md | SMART | rounds capped 1 default / 2 max; each turn must name a concession | memory recall dropped (see deviations) |
| `agents/managers/research_manager.py` | pipeline/debate.py (`run_debate` arbiter) + prompts/research_manager.md | SMART | emits the 5-tier rating, not upstream's free-text verdict | |
| `agents/trader/trader.py` | pipeline/trader.py + prompts/trader.md | SMART | Buy/Hold/Sell + entry, stop, sizing as typed fields | |
| `agents/risk_mgmt/aggressive_debator.py` / `conservative_debator.py` / `neutral_debator.py` | pipeline/risk.py + prompts/risk_aggressive.md / _conservative.md / _neutral.md | SMART | one sequential pass (upstream's `3 * max_risk_discuss_rounds` with rounds=1) | |
| `agents/managers/portfolio_manager.py` (risk judge) | pipeline/portfolio_manager.py + prompts/portfolio_manager.md | DEEP | 5-tier rating + confidence + soft price target per `config/report-schema.md`; the risk ruling is folded in here rather than a separate judge seat | |
| `agents/schemas.py`, `agents/utils/structured.py`, `agents/utils/rating.py` | pipeline/schemas.py | — | every role typed, not just the three decision-makers; retry policy is exactly one re-prompt then DEGRADED | |
| `graph/trading_graph.py`, `graph/setup.py`, `graph/conditional_logic.py`, `graph/propagation.py` | pipeline/deep.py + stages.py | — | LangGraph state machine → straight-line Python; upstream's debate-termination arithmetic is reproduced in `run_debate` / `run_risk_committee` | |
| `agents/utils/agent_utils.py` (toolkit) | pipeline/evidence.py | — | tool calls → one pre-computed evidence pack per ticker | |
| `graph/reflection.py`, `agents/utils/memory.py` | — | — | not ported (M3) | |

**M1 status (unchanged):** M1 took nothing from TradingAgents but its shape — "one
analyst = one plain-text prompt + one pydantic schema, called through a single gateway" —
used by `pipeline/prompts_loader.py`, `pipeline/prompts/quick_take.md`, and
`discovery/shortlist.QuickTake`. The quick take is a single FAST-tier call that ranks the
deep-dive queue M2 consumes.

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

## Signal mapping (M3 → src/tradingagent/signals/)

Mostly *not* a port. Only one of the four sources has an upstream ancestor; the
other three are endpoints no cookbook touches, and the fusion layer is ours
because upstream has nothing equivalent to fuse.

| Local module | Upstream ancestor | What is ours |
|---|---|---|
| `news.py` | `dataflows/finnhub_utils.py` (Apache-2.0, `a33fd4c`) — the 7-day company-news window and the headline+source rendering | the tone lexicon, the negation window, the market-wide RSS leg |
| `insiders.py` | none — EDGAR is in no cookbook | all of it: CIK lookup, Form 4 XML parsing, 10b5-1 labelling, the 5 req/s throttle |
| `macro.py` | none — no cookbook reads FRED | all of it: the six series, the per-series direction rules and materiality bands |
| `prediction.py` | none — no cookbook reads a prediction market | all of it: topic classification, multi-leg probability aggregation, change-based scoring |
| `bundle.py`, `accuracy.py` | none | the fusion rule and the source-accuracy tracker |

Three decisions worth stating, because each is a place a reader could
reasonably expect the opposite:

- **Headline tone is a lexicon, not an LLM call.** Every headline already goes
  into the analyst prompts verbatim, so a second model pass would be buying an
  opinion we are about to form anyway. What the lexicon buys is the thing the
  LLM cannot give: a number that is stable across runs and can therefore be
  scored against outcomes. Cost: it is blind to sarcasm and to any phrasing
  outside the word list, and it abstains rather than guessing when it sees one.
- **Market-wide signals do not move the ranking.** Macro and prediction-market
  reads are identical for every candidate, so scoring them would shift all
  scores equally and reorder nothing. They go into the shared market context
  instead, where they can change an argument even though they cannot change a
  rank.
- **Prediction markets are read by their weekly change, not their level.** A
  probability everyone already knows is not information; the repricing is. See
  the commit message on `prediction.py` for the three live-feed defects that
  forced this.

## Options mapping (staskh → src/tradingagent/options/)

Upstream: `staskh/trading_skills` @ `658dcc1`, MIT. It is a live IBKR trading
toolkit; we took its option maths and its short-strike selection and left the
broker behind.

| Upstream | Local | What came across |
|---|---|---|
| `black_scholes.py::black_scholes_price / _delta / _vega / _greeks`, `implied_volatility` | `options/black_scholes.py` | the pricing and greeks formulae, the continuous dividend yield `q`, and Newton-Raphson IV with a bisection fallback |
| `scanner_pmcc.py::find_strike_by_delta` (l.316) | `options/strategies.py::build_candidates` | select the short strike by *delta band*, not by moneyness percentage or a fixed strike offset |
| `scanner_pmcc.py::compute_base_score` (l.600), `compute_short_premium_score` (l.584), `compute_earnings_score` (l.506) | `options/strategies.py::score_candidate` | the additive component score with a per-component reason string, the thin-premium penalty, and earnings-before-expiry as a scoring term rather than a hard filter |
| `broker/roll.py::evaluate_short_candidates` | `options/strategies.py::hard_filters` | the reject-before-you-score gates: ITM, delta outside band, open interest floor, spread ceiling |
| `broker/options.py`, `options.py` | `data/option_chain.py` | OCC symbology, the DTE window, and mid-price-with-fallback quote handling |

Deliberate deviations:

- **IBKR → Alpaca paper, and the free feed is thinner than upstream assumes.**
  staskh reads a live IBKR chain that carries greeks, IV, volume and open
  interest per contract. Alpaca's free `indicative` feed returns
  `implied_volatility=None` and `greeks=None` for every snapshot, has no volume
  field at all, and only exposes open interest through the separate
  `get_option_contracts` endpoint, which settles a day behind. So every delta,
  IV and theta in section 6 is *computed by us* from the quote — the journal
  records `"greeks_source": "computed (Black-Scholes) — the free feed supplies
  none"` so a later review cannot mistake our number for the exchange's.
- **Upstream's volume filter is dropped, not silently ignored.** No per-contract
  volume exists on this tier. Open interest carries the liquidity test alone,
  and the data-quality block says so on every report.
- **Strikes are anchored to the deep analysis, which upstream has no concept
  of.** staskh's scanner is standalone: it picks a delta and takes what the
  chain gives. Ours starts from the portfolio manager's verdict and scores the
  strike against the levels the analysts actually argued over — nearest support
  below spot for a CSP, nearest resistance or the price target above spot for a
  covered call. A 0.25-delta strike on the wrong side of the level is penalised
  even though upstream would rank it first.
- **The score does not decide.** Upstream's scanner outputs a ranked list and
  the top row is the answer. Ours hands the top three, their component scores
  and their data caveats to a SMART-tier strategist that must name one of them
  by OCC symbol or say `none` — and a symbol that was not in the table is
  treated as a hallucination, not a pick.
- **One-sided books are priced as a seller would see them.** Far-OTM contracts
  routinely quote bid 0.00 against a real ask. Upstream's mid would report half
  the ask as the premium; we fall back bid → mid → last trade → prior close and
  print which one produced the number, because a mid you cannot sell at is a
  worse answer than an honest bid of zero.
- **No roll logic, no position management, no order path.** `broker/roll.py`'s
  surrounding machinery assumes open positions and an execution venue. We hold
  no positions and place no orders; only the candidate-evaluation core came
  across.
- **Risk-free rate is a config constant (0.045, `RISK_FREE_RATE`), not a FRED
  series.** At 21–45 DTE a 100bp error in `r` moves a 0.25-delta put's fair
  value by well under a cent. The FRED dependency would buy precision the
  decision cannot use.

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
- **Tool-calling analysts → a pre-computed evidence pack.** This is the largest M2
  deviation. Upstream binds ~20 tools to the four analysts and lets each decide what to
  fetch inside a LangGraph loop; the number of LLM calls per analyst is therefore
  unbounded. We have no agent framework and a per-ticker cost target, so
  `pipeline/evidence.py` fetches everything once per ticker — 2y of bars, 13 indicators,
  fundamentals, quarterly trend, positioning, company news — and injects the relevant
  rendered slice into each analyst's prompt. Cost: an analyst cannot chase a follow-up
  question, so the evidence menu is fixed at design time rather than at run time.
  Benefit: exactly 12 LLM calls per ticker, known in advance, and every analyst is
  reasoning over the same validated numbers. Each analyst reports `evidence_gaps`, which
  is where a missing tool would show up.
- **Analyst prose is length-capped and the cap is stated in characters.** Each role's
  output feeds the next role's prompt, so an unbounded analyst report inflates all eleven
  downstream calls. The budget lives in the pydantic field description in the same unit
  the validator enforces — an early CRM run capped `risk_ruling` at 900 characters while
  telling the model nothing, overran it twice, and lost the verdict to DEGRADED.
- **No reflection or memory.** Upstream's `graph/reflection.py` and
  `agents/utils/memory.py` embed past decisions and their P&L into a vector store and
  recall similar situations into each researcher's prompt. We have no realised outcomes
  to reflect on until the journal has history, so M2 writes the journal and skips the
  recall. Deferred to M3.
- **Risk judge merged into the portfolio manager.** Upstream runs three risk debators and
  then a separate judge. We keep the three seats and let the portfolio manager rule on
  them in the same DEEP-tier call, saving one DEEP call per ticker (the most expensive
  tier) for a decision that was already the PM's to make. The ruling is a required field
  (`risk_ruling`), so it cannot be skipped.
- **Sentiment analyst reads positioning, not social media.** Upstream has separate
  sentiment and social-media seats over Reddit and StockTwits. Free, reliable social data
  is still the gap, and now a permanent one on this route: PRAW was scoped for M3, and the
  Reddit API application was **rejected**, so this is a closed door rather than a queue we
  are waiting in. The single merged seat reads sell-side posture, target dispersion, short
  interest and holder mix, plus the M3 insider and news-tone signals — positioning and
  disclosed action rather than chatter. Neither is a proxy for retail sentiment, and the
  prompt says so rather than letting a role treat them as one. The extension point is
  unchanged and unused: one class implementing `SignalSource`, registered in
  `signals/bundle.py`, is the whole change whenever a workable social feed turns up;
  nothing in the bundle, ranking, prompts or tracker moves.
- **Screener liquidity floor uses average *share* volume**, per `config/preferences.md`
  (1M shares), not the dollar-volume floor some upstream variants use.
- **Delivery is email over SMTP, not Telegram.** `BUILD_PLAN.md` Milestone 5 and
  `PROMPTS.md` both say Telegram; those files record the plan as written and were left
  alone. The operator asked for email instead, and the artefact settles it: the daily
  brief renders to ~75 KB of HTML with a dozen tables, which a chat bubble cannot show
  and an inbox can. `delivery/email.py` needs no bot registration, no third-party
  service holding the content, and attaches the markdown and the per-ticker deep reports
  alongside the inline HTML. The trade is that push-to-phone becomes whatever the mail
  client does, and there is no read receipt.
- **Reflection is an audited ledger, not a vector store (M7).** Upstream's
  `graph/reflection.py` and `agents/utils/memory.py` embed past decisions and their P&L
  and recall similar situations into each researcher's prompt. We took the measurement
  half and left the recall half out. `evaluation/` records the full pre-selection pool,
  the provenance of the code that produced each row, and the resolved excess return at
  five horizons, then grades sources on **lift over the price-only screen** — a number an
  embedding lookup cannot produce, and the one that decides whether a source graduates out
  of SHADOW. Nothing is fed back into a prompt: a recalled "similar situation" is an
  unfalsifiable influence on a decision, and the point of the ledger is that every
  influence on the shortlist is attributable. Cost: the researchers do not learn from last
  month. Benefit: we can say whether last month worked, and the ±1/±3/±5 ladder stays the
  only path from evidence to influence. This is the M2 bullet above ("Deferred to M3")
  finally answered — deferred twice, because grading needs the as-of-safe snapshot
  machinery M6 built.
- **Deep reports are attached rather than inlined.** Gmail clips a message body over
  ~102 KB behind a "view entire message" link, and the brief alone is ~75 KB. Inlining
  five deep reports would push the mandatory disclaimer footer behind that link, which is
  the one part of the report that may not be hidden.

### Paid bottlenecks identified (not purchased — surfaced in report section 7)
| Capability | Upstream source | Cost | Our substitute |
|---|---|---|---|
| Full US universe scan | FMP | $22–29/mo | S&P 500 snapshot |
| CANSLIM C/A/I fundamentals | FMP or Finviz Elite | ~$40/mo | technical-only screening |
| Live economic calendar | FMP / Finnhub premium | paid tier | static recurring schedule |
| Industry-granularity theme detection | FINVIZ Elite | ~$40/mo | GICS sector aggregates |
| Real-time OPRA option quotes, exchange greeks/IV, per-contract volume | Alpaca OPRA feed (agreement unsigned → 403) or IBKR | OPRA subscriber agreement + market-data fee | `indicative` feed quotes, greeks and IV computed by us, open interest from the T-1 contracts endpoint, volume unavailable |
