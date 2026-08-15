"""Smoke check: can this environment actually reach Alpaca's option chain?

Not a unit test — the suite mocks Alpaca on purpose. This is the one command
that proves a freshly built environment has working credentials and a working
alpaca-py, before the options stage relies on it.

    PYTHONPATH=src python scripts/smoke_option_chain.py [TICKER]

Read-only: fetches a chain snapshot. It places no orders, and asserts the
paper flag the same way the app does at startup.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    load_dotenv(REPO_ROOT / "config" / ".env")

    if os.getenv("ALPACA_PAPER", "true").lower() != "true":
        print("REFUSED: ALPACA_PAPER is not true. Research only.", file=sys.stderr)
        return 2

    key, secret = os.getenv("ALPACA_API_KEY"), os.getenv("ALPACA_SECRET_KEY")
    if not key or not secret:
        print(
            "MISSING CREDS: set ALPACA_API_KEY / ALPACA_SECRET_KEY in config/.env",
            file=sys.stderr,
        )
        return 2

    from alpaca.data.historical.option import OptionHistoricalDataClient
    from alpaca.data.requests import OptionChainRequest

    symbol = sys.argv[1] if len(sys.argv) > 1 else "SPY"
    client = OptionHistoricalDataClient(api_key=key, secret_key=secret)
    chain = client.get_option_chain(OptionChainRequest(underlying_symbol=symbol))

    print(f"{symbol}: {len(chain)} contracts")
    if not chain:
        print(
            f"EMPTY CHAIN for {symbol} — creds work but the feed returned nothing.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
