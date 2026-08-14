"""Direct API data clients (no MCP, no Claude Code constructs).

yfinance for OHLCV, Alpaca *paper* for session state and quote cross-checks,
Finnhub free tier for calendars and news.
"""

from .validate import DataUnavailable, DegradedTracker

__all__ = ["DataUnavailable", "DegradedTracker"]
