# PORTING_NOTES.md — cookbooks → provider-agnostic modules

> reference/ repos are read-only. Ideas, prompts, and logic are ported into src/ with attribution (TradingAgents: Apache-2.0, cite repo + arXiv:2412.20138; note licenses of the two skills repos when mining).

## Upstream versions studied
| Cookbook | Repo | Release/commit | Date |
|---|---|---|---|
| TradingAgents | github.com/TauricResearch/TradingAgents | `<fill>` | |
| tradermonty skills | github.com/tradermonty/claude-trading-skills | `<fill>` | |
| staskh trading_skills | github.com/staskh/trading_skills | `<fill>` | |

## Pipeline mapping (TradingAgents → src/tradingagent/pipeline/)
| Upstream | Local module / prompt file | Changes | Data swapped |
|---|---|---|---|
| fundamentals analyst | pipeline/analysts.py + prompts/analyst_fundamentals.md | | Alpha Vantage → yfinance/Alpaca |
| technical analyst | prompts/analyst_technical.md | | |
| news analyst | prompts/analyst_news.md | | Finnhub + signal bundle (M3) |
| sentiment analyst | prompts/analyst_sentiment.md | | Reddit/PRAW (M3) |
| bull/bear researchers | pipeline/debate.py + prompts/researcher_bull.md / _bear.md | rounds capped 1–2 | |
| research manager | pipeline/debate.py (arbiter) | | |
| trader | pipeline/trader.py + prompts/trader.md | | |
| risk agg/cons/neutral + judge | pipeline/risk.py + prompts/risk_*.md | | |
| portfolio manager | pipeline/portfolio_manager.py + prompts/portfolio_manager.md | 5-tier + soft target per report-schema | |
| LangGraph state machine | main.py stage orchestration + pydantic schemas | deliberate simplification | |

## Discovery mapping (tradermonty → src/tradingagent/discovery/)
| Skill | Local module | Notes |
|---|---|---|
| market-breadth-analyst | discovery/breadth.py | |
| theme/sector detector | discovery/sectors.py | |
| screeners | discovery/screener.py | free-data variants only |
| economic/earnings calendar | discovery/calendar.py | flag FMP-gated features |

## Options mapping (staskh → src/tradingagent/options/)
| Skill/tool | Local module | Notes |
|---|---|---|
| covered-call finder / CSP logic | options/strategies.py | IBKR → Alpaca chains |

## Deliberate deviations & upstream releases reviewed
- `<log as they arise>`
