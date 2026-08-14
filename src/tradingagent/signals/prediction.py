"""Event odds from the Polymarket public REST API.

Why a prediction market belongs in a stock-research pipeline: it prices the
things the other four sources cannot. FRED reports what already happened, news
reports what was said, insiders report what one company's officers did — none
of them price the probability of a Fed cut in September, or a tariff decision,
or a shutdown. A market with real money on it does, continuously, and reading
it costs one unkeyed HTTP request.

Scope is market-wide by design. Mapping a political event onto an individual
ticker is a judgement, and it is the debate's judgement to make, not this
module's — so the odds are supplied as context and the direction is only set
for events whose mapping to equity risk is not contested (a rate cut is
risk-on; a government shutdown is not).

Free and unkeyed. The Gamma API is Polymarket's documented public read
endpoint; nothing here scrapes, authenticates, or places an order.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Any

from .base import Signal, SignalSource

log = logging.getLogger(__name__)

GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"

# Topic filters, with what a RISING probability means for equity risk appetite.
# `None` means the direction is contested — supply the odds, take no view.
TOPICS: tuple[tuple[str, tuple[str, ...], int | None], ...] = (
    ("Fed rate cut", ("fed", "rate cut", "fomc", "interest rate"), 1),
    ("Recession", ("recession",), -1),
    ("Government shutdown", ("shutdown", "debt ceiling", "default"), -1),
    ("Tariffs", ("tariff", "trade war"), -1),
    ("Inflation", ("inflation", "cpi"), None),
    ("Election", ("election", "president", "senate", "house majority"), None),
)

MIN_LIQUIDITY_USD = 20_000.0  # below this the price is one person's opinion
MAX_EVENTS = 8


@dataclass
class EventOdds:
    topic: str
    question: str
    probability: float
    liquidity: float
    end_date: str
    equity_direction: int | None

    def line(self) -> str:
        view = (
            "risk-on if it happens" if self.equity_direction == 1
            else "risk-off if it happens" if self.equity_direction == -1
            else "direction for equities is contested"
        )
        return (
            f"  - [{self.topic}] {self.question}: {self.probability:.0%} "
            f"(${self.liquidity:,.0f} liquidity, resolves {self.end_date}) — {view}"
        )


class PredictionMarketSource(SignalSource):
    """Polymarket event probabilities on the macro and policy questions."""

    name = "polymarket"
    scope = "market"
    describes = "Polymarket Gamma API, public event odds"

    def __init__(self, degraded=None, session: Any = None, min_liquidity: float = MIN_LIQUIDITY_USD):
        super().__init__(degraded)
        self._session = session
        self.min_liquidity = min_liquidity

    def session(self):
        if self._session is None:
            import requests

            self._session = requests.Session()
        return self._session

    def collect(self, symbols: list[str], run_date: date) -> list[Signal]:
        response = self.session().get(
            GAMMA_EVENTS_URL,
            params={"closed": "false", "limit": 200, "order": "liquidity", "ascending": "false"},
            timeout=20,
        )
        response.raise_for_status()
        odds = parse_events(response.json(), min_liquidity=self.min_liquidity)
        if not odds:
            log.info("Polymarket returned no events over the liquidity floor")
            return []
        return [odds_signal(odds, run_date)]


def parse_events(payload: Any, min_liquidity: float) -> list[EventOdds]:
    """Pick the market-relevant events out of the Gamma events feed."""
    out: list[EventOdds] = []
    for event in payload if isinstance(payload, list) else []:
        title = str(event.get("title") or "")
        topic, direction = _classify(title)
        if topic is None:
            continue
        liquidity = _num(event.get("liquidity")) or 0.0
        if liquidity < min_liquidity:
            continue
        probability = _probability(event)
        if probability is None:
            continue
        out.append(
            EventOdds(
                topic=topic,
                question=title[:160],
                probability=probability,
                liquidity=liquidity,
                end_date=str(event.get("endDate") or "")[:10] or "unknown",
                equity_direction=direction,
            )
        )
    out.sort(key=lambda o: -o.liquidity)
    return out[:MAX_EVENTS]


def _classify(title: str) -> tuple[str | None, int | None]:
    lowered = title.lower()
    for topic, keywords, direction in TOPICS:
        if any(word in lowered for word in keywords):
            return topic, direction
    return None, None


def _probability(event: dict[str, Any]) -> float | None:
    """The 'Yes' price of the event's most liquid market, as a probability."""
    markets = event.get("markets") or []
    best, best_liquidity = None, -1.0
    for market in markets:
        liquidity = _num(market.get("liquidity")) or 0.0
        price = _yes_price(market)
        if price is not None and liquidity > best_liquidity:
            best, best_liquidity = price, liquidity
    return best


def _yes_price(market: dict[str, Any]) -> float | None:
    raw = market.get("outcomePrices")
    if isinstance(raw, str):
        import json

        try:
            raw = json.loads(raw)
        except ValueError:
            return None
    if isinstance(raw, list) and raw:
        value = _num(raw[0])
        return value if value is not None and 0.0 <= value <= 1.0 else None
    return None


def odds_signal(odds: list[EventOdds], run_date: date) -> Signal:
    """One market-wide signal; only the uncontested events move the direction."""
    directional = [o for o in odds if o.equity_direction is not None]
    # Weight each event by how far it is from a coin flip: a 50/50 market is
    # information about uncertainty, not about direction.
    net = sum(o.equity_direction * (o.probability - 0.5) * 2 for o in directional)
    net = net / len(directional) if directional else 0.0
    direction = 1 if net > 0.15 else -1 if net < -0.15 else 0
    loudest = max(odds, key=lambda o: abs(o.probability - 0.5))
    return Signal(
        source=PredictionMarketSource.name,
        kind="event_odds",
        symbol=None,
        direction=direction,
        strength=min(1.0, abs(net)),
        headline=(
            f"{len(odds)} liquid event market(s) tracked, {len(directional)} with an agreed "
            f"equity direction; net {net:+.2f}. Most decided: {loudest.question} at "
            f"{loudest.probability:.0%}"
        ),
        detail="\n".join(o.line() for o in odds),
        as_of=run_date,
    )


def _num(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
