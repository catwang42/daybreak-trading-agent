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
import re
from dataclasses import dataclass
from datetime import date
from typing import Any

from .base import Signal, SignalSource

log = logging.getLogger(__name__)

GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"


@dataclass(frozen=True)
class Topic:
    """A question worth reading, and how to read it.

    ``equity_direction`` is what a RISING probability means for equity risk
    appetite; ``None`` means the mapping is contested, so the odds are supplied
    as context and no direction is taken.

    ``leg_yes`` matters because most Polymarket events are multi-outcome. "Fed
    Decision in September?" is five separate Yes/No markets, and index 0 of the
    first one is not the probability of anything useful. The pattern names the
    legs that mean "the topic happened", and their probabilities are summed. A
    topic with no pattern, or an event where no leg matches, is skipped rather
    than guessed at — reading the wrong leg of a five-way market is worse than
    reading nothing.
    """

    label: str
    keywords: tuple[str, ...]
    equity_direction: int | None
    leg_yes: str = ""


TOPICS: tuple[Topic, ...] = (
    Topic("Fed rate cut", ("fed ", "rate cut", "fomc", "interest rate"), 1, r"decrease|cut"),
    Topic("Recession", ("recession",), -1, r"yes|recession"),
    Topic("Government shutdown", ("shutdown", "debt ceiling", "debt default"), -1, r"yes|shutdown"),
    Topic("Tariffs", ("tariff", "trade war"), -1, r"yes|tariff"),
    Topic("Inflation", ("inflation", "cpi"), None),
    # US races only. A Brazilian presidential market is a real market about a
    # real thing; it is not information about the S&P, and letting it into the
    # backdrop dilutes the topics that are.
    Topic("US election", ("us election", "presidential election 2028", "us senate",
                          "us house", "house majority", "midterm"), None),
)

MIN_LIQUIDITY_USD = 20_000.0  # below this the price is one person's opinion
MAX_EVENTS = 8
# Gamma caps a page at 100 and orders by liquidity, so the first page is all
# the crowded markets. Paging further reaches the thinner macro questions
# (recession, tariffs) that sit below the sports and crypto books.
PAGE_SIZE = 100
PAGES = 3
# A market trading at 1% or 99% has effectively resolved. It is maximally
# "decided" and minimally informative, and it would otherwise sit in the
# backdrop as a fact everyone already knows.
SETTLED_BAND = 0.02
# A weekly move of this many probability points is a full-strength read. Macro
# probabilities are sticky: P(recession) travelling 10pp in a week is a repricing
# of the year, not a wiggle.
CHANGE_FULL_SCALE = 0.10
# Below this the move is indistinguishable from bid/ask noise on a thin book.
MIN_MEANINGFUL_CHANGE = 0.01


@dataclass
class EventOdds:
    topic: str
    question: str
    probability: float
    liquidity: float
    end_date: str
    equity_direction: int | None
    #: Change in ``probability`` over the past week, ``None`` if Gamma did not
    #: report one. This, not the level, is what the signal reads — see
    #: :func:`odds_signal`.
    week_change: float | None = None

    @property
    def move(self) -> float:
        """Signed weekly move for equity risk appetite, 0 when there is no news."""
        if self.equity_direction is None or self.week_change is None:
            return 0.0
        if abs(self.week_change) < MIN_MEANINGFUL_CHANGE:
            return 0.0
        scaled = self.week_change / CHANGE_FULL_SCALE
        return self.equity_direction * max(-1.0, min(1.0, scaled))

    def line(self) -> str:
        view = (
            "risk-on if it happens" if self.equity_direction == 1
            else "risk-off if it happens" if self.equity_direction == -1
            else "direction for equities is contested"
        )
        drift = (
            f", {self.week_change:+.0%} over the week" if self.week_change is not None
            else ", weekly change unreported"
        )
        return (
            f"  - [{self.topic}] {self.question}: {self.probability:.0%}{drift} "
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
        events: list[Any] = []
        for page in range(PAGES):
            response = self.session().get(
                GAMMA_EVENTS_URL,
                params={
                    "closed": "false",
                    "limit": PAGE_SIZE,
                    "offset": page * PAGE_SIZE,
                    "order": "liquidity",
                    "ascending": "false",
                },
                timeout=20,
            )
            response.raise_for_status()
            batch = response.json()
            if not isinstance(batch, list) or not batch:
                break
            events += batch
        odds = parse_events(events, min_liquidity=self.min_liquidity)
        if not odds:
            log.info("Polymarket: no tracked topic over the liquidity floor in %d events", len(events))
            return []
        return [odds_signal(odds, run_date)]


def parse_events(payload: Any, min_liquidity: float) -> list[EventOdds]:
    """Pick the market-relevant events out of the Gamma events feed.

    One event per topic. "Fed Decision in October?" and "Fed Decision in
    December?" are two contracts on one question, and netting both would let a
    single fact vote twice — the deepest book wins and the rest are dropped.
    """
    best: dict[str, EventOdds] = {}
    for event in payload if isinstance(payload, list) else []:
        title = str(event.get("title") or "")
        topic = classify(title)
        if topic is None:
            continue
        liquidity = _num(event.get("liquidity")) or 0.0
        if liquidity < min_liquidity:
            continue
        probability = probability_of(event, topic)
        if probability is None or not (SETTLED_BAND <= probability <= 1.0 - SETTLED_BAND):
            continue
        odds = EventOdds(
            topic=topic.label,
            question=title[:160],
            probability=probability,
            liquidity=liquidity,
            end_date=str(event.get("endDate") or "")[:10] or "unknown",
            equity_direction=topic.equity_direction,
            week_change=change_of(event, topic),
        )
        incumbent = best.get(topic.label)
        if incumbent is None or odds.liquidity > incumbent.liquidity:
            best[topic.label] = odds
    out = sorted(best.values(), key=lambda o: -o.liquidity)
    return out[:MAX_EVENTS]


def classify(title: str) -> Topic | None:
    lowered = title.lower()
    return next((t for t in TOPICS if any(word in lowered for word in t.keywords)), None)


def probability_of(event: dict[str, Any], topic: Topic) -> float | None:
    """Probability that ``topic`` happens, according to this event."""
    matched = _matching_legs(event, topic)
    if matched is None:
        return None
    return min(1.0, sum(_yes_price(m) or 0.0 for m in matched))


def change_of(event: dict[str, Any], topic: Topic) -> float | None:
    """Weekly change in :func:`probability_of`, or ``None`` if unreported.

    Summed over the same legs for the same reason the probability is: if the
    25bp leg gained 8pp and the 50bp leg lost 1pp, P(cut) rose 7pp.
    """
    matched = _matching_legs(event, topic)
    if not matched:
        return None
    changes = [_num(m.get("oneWeekPriceChange")) for m in matched]
    reported = [c for c in changes if c is not None]
    if len(reported) != len(changes):
        return None  # a partial sum is a wrong sum, not an approximate one
    return sum(reported)


def _matching_legs(event: dict[str, Any], topic: Topic) -> list[dict[str, Any]] | None:
    """The legs of ``event`` that mean "the topic happened".

    A single binary market is itself. A multi-outcome event is the legs whose
    group title matches the topic's ``leg_yes`` pattern — "Fed Decision in
    September?" is a cut if either the 25bp or the 50bp leg resolves, so P(cut)
    covers both, not whichever leg happens to be first. An event where nothing
    matches is skipped rather than guessed at: reading the wrong leg of a
    five-way market is worse than reading nothing.
    """
    markets = [m for m in (event.get("markets") or []) if _yes_price(m) is not None]
    if not markets:
        return None
    if len(markets) == 1:
        return markets
    if not topic.leg_yes:
        return None
    matched = [
        m for m in markets
        if re.search(topic.leg_yes, str(m.get("groupItemTitle") or ""), re.I)
    ]
    if not matched:
        log.debug("No %r leg matched /%s/ in %r", topic.label, topic.leg_yes, event.get("title"))
        return None
    return matched


def _yes_price(market: dict[str, Any]) -> float | None:
    """First outcome price, but only for a two-outcome market.

    Gamma serves ``outcomePrices`` as a JSON-encoded string on most endpoints
    and a list on others. Multi-outcome markets are rejected rather than read:
    index 0 is only "Yes" when there are exactly two outcomes, and treating one
    leg of a five-way race as a probability would be silently wrong.
    """
    raw = market.get("outcomePrices")
    if isinstance(raw, str):
        import json

        try:
            raw = json.loads(raw)
        except ValueError:
            return None
    if isinstance(raw, list) and len(raw) == 2:
        value = _num(raw[0])
        return value if value is not None and 0.0 <= value <= 1.0 else None
    return None


def odds_signal(odds: list[EventOdds], run_date: date) -> Signal:
    """One market-wide signal, read off the week's repricing.

    The level is deliberately not the signal. P(Fed cut in December) sitting at
    6% is a fact the whole market has already priced, and scoring it by distance
    from a coin flip would print the same maximal risk-off reading every day
    until the meeting — a constant is not information. What the other four
    sources cannot see is the *move*: P(recession) going 12% → 22% in a week is
    the crowd repricing the year, and that is what gets a direction here.
    """
    directional = [o for o in odds if o.equity_direction is not None]
    movers = [o for o in directional if o.move]
    net = sum(o.move for o in movers) / len(movers) if movers else 0.0
    direction = 1 if net > 0.15 else -1 if net < -0.15 else 0
    if movers:
        loudest = max(movers, key=lambda o: abs(o.move))
        read = (
            f"Biggest repricing: {loudest.question} {loudest.probability:.0%}, "
            f"{loudest.week_change:+.0%} on the week"
        )
    else:
        read = "No tracked topic repriced meaningfully this week; levels supplied as context"
    return Signal(
        source=PredictionMarketSource.name,
        kind="event_odds",
        symbol=None,
        direction=direction,
        strength=min(1.0, abs(net)),
        headline=(
            f"{len(odds)} liquid event market(s) tracked, {len(directional)} with an agreed "
            f"equity direction, {len(movers)} repriced; net {net:+.2f}. {read}"
        ),
        detail="\n".join(o.line() for o in odds),
        as_of=run_date,
    )


def _num(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
