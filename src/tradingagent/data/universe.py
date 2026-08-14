"""The screening universe: S&P 500 constituents with GICS sectors.

A snapshot ships in ``sp500.json`` so a headless cloud run never depends on
Wikipedia being reachable; :func:`load_universe` refreshes from Wikipedia when
asked and silently falls back to the snapshot.
"""

from __future__ import annotations

import io
import json
import logging
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

SNAPSHOT = Path(__file__).with_name("sp500.json")
WIKI_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"

# Sector ETF proxies, keyed by GICS sector name.
SECTOR_ETFS: dict[str, str] = {
    "Information Technology": "XLK",
    "Health Care": "XLV",
    "Financials": "XLF",
    "Consumer Discretionary": "XLY",
    "Communication Services": "XLC",
    "Industrials": "XLI",
    "Consumer Staples": "XLP",
    "Energy": "XLE",
    "Utilities": "XLU",
    "Real Estate": "XLRE",
    "Materials": "XLB",
}

# Sector rotation buckets — ported from tradermonty/claude-trading-skills
# `sector-analyst/scripts/analyze_sector_rotation.py` (Yahoo sector names),
# remapped onto GICS names used by our universe.
CYCLICAL_SECTORS = {
    "Information Technology",
    "Consumer Discretionary",
    "Communication Services",
    "Financials",
    "Industrials",
}
DEFENSIVE_SECTORS = {"Utilities", "Consumer Staples", "Health Care", "Real Estate"}
COMMODITY_SECTORS = {"Energy", "Materials"}

# Free-text sector names a human might write in preferences.md -> GICS.
_SECTOR_ALIASES = {
    "tech": "Information Technology",
    "technology": "Information Technology",
    "information technology": "Information Technology",
    "it": "Information Technology",
    "healthcare": "Health Care",
    "health care": "Health Care",
    "financial": "Financials",
    "financials": "Financials",
    "finance": "Financials",
    "materials": "Materials",
    "basic materials": "Materials",
    "energy": "Energy",
    "industrials": "Industrials",
    "utilities": "Utilities",
    "real estate": "Real Estate",
    "consumer discretionary": "Consumer Discretionary",
    "consumer cyclical": "Consumer Discretionary",
    "consumer staples": "Consumer Staples",
    "consumer defensive": "Consumer Staples",
    "communication services": "Communication Services",
    "communications": "Communication Services",
}


def normalize_sector(name: str) -> str:
    """Map a loosely-written sector name onto its GICS spelling."""
    return _SECTOR_ALIASES.get(name.strip().lower(), name.strip())


@dataclass(frozen=True)
class Constituent:
    symbol: str
    name: str
    sector: str
    industry: str


def _parse(rows: list[dict]) -> list[Constituent]:
    return [
        Constituent(
            symbol=str(r["symbol"]).strip().upper(),
            name=str(r.get("name", "")).strip(),
            sector=str(r.get("sector", "")).strip(),
            industry=str(r.get("industry", "")).strip(),
        )
        for r in rows
        if str(r.get("symbol", "")).strip()
    ]


def load_snapshot() -> list[Constituent]:
    payload = json.loads(SNAPSHOT.read_text())
    return _parse(payload["constituents"])


def fetch_live() -> list[Constituent]:
    """Scrape the current constituent table. Raises on any failure."""
    import pandas as pd
    import requests

    resp = requests.get(WIKI_URL, headers={"User-Agent": "daybreak-trading-agent/1.0"}, timeout=30)
    resp.raise_for_status()
    table = pd.read_html(io.StringIO(resp.text))[0]
    rows = [
        {
            # Wikipedia writes class shares as BRK.B; Yahoo/Alpaca want BRK-B.
            "symbol": str(sym).replace(".", "-"),
            "name": str(name),
            "sector": str(sector),
            "industry": str(industry),
        }
        for sym, name, sector, industry in zip(
            table["Symbol"], table["Security"], table["GICS Sector"], table["GICS Sub-Industry"]
        )
    ]
    if len(rows) < 400:
        raise ValueError(f"constituent table looks truncated ({len(rows)} rows)")
    return _parse(rows)


def load_universe(refresh: bool = False) -> list[Constituent]:
    """Universe for the daily scan; falls back to the bundled snapshot."""
    if refresh:
        try:
            return fetch_live()
        except Exception as exc:  # noqa: BLE001 - the snapshot is a fine answer
            log.warning("Universe refresh failed (%s); using bundled snapshot.", exc)
    return load_snapshot()


def by_sector(constituents: list[Constituent]) -> dict[str, list[Constituent]]:
    grouped: dict[str, list[Constituent]] = {}
    for c in constituents:
        grouped.setdefault(c.sector, []).append(c)
    return grouped
