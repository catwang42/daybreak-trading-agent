"""News tone from Finnhub company news plus market-wide RSS.

Two transports, one source, because they answer the same question and their
failure modes are complementary: Finnhub is keyed and rate-limited but knows
which company a story is about, while RSS is unkeyed and unlimited but only
gives us a headline to match symbols against ourselves.

Scoring is a lexicon, not an LLM. Every headline is already going into the
analyst prompts verbatim, so paying for a second model pass to label them
would be buying an opinion we are about to form anyway. What the lexicon buys
is something the LLM cannot give: a number that is stable across runs and can
therefore be scored against outcomes by :mod:`tradingagent.signals.accuracy`.

Ported in spirit from `reference/TradingAgents/tradingagents/dataflows/finnhub_utils.py`
(Apache-2.0, commit a33fd4c) — the 7-day company-news window and the
headline-plus-source rendering are upstream's. Upstream feeds raw headlines to
a news analyst and stops there; the tone score and the RSS leg are ours.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import date, datetime, timedelta, timezone

from ..data.finnhub_client import NEWS_WINDOW_DAYS, FinnhubFree, news_window
from ..snapshot import utcnow
from .base import Signal, SignalSource

log = logging.getLogger(__name__)

# Unkeyed, no rate limit published, no ToS problem with reading a public feed.
MARKET_FEEDS: tuple[tuple[str, str], ...] = (
    ("Nasdaq", "https://www.nasdaq.com/feed/rssoutbound?category=Markets"),
    ("SeekingAlpha", "https://seekingalpha.com/market_currents.xml"),
    ("Yahoo Finance", "https://finance.yahoo.com/news/rssindex"),
)

# Headline tone is a nice-to-have on top of Finnhub's company news. Nothing here
# is worth making the daily run wait for, so the feeds get a per-request timeout
# and the set of them gets a total budget; whatever answers in time is used.
RSS_CONNECT_TIMEOUT = 5.0
RSS_READ_TIMEOUT = 10.0
RSS_TOTAL_BUDGET_SECONDS = 30.0
# Several feed hosts return 403 to a bare urllib/requests agent.
RSS_USER_AGENT = "daybreak-research/1.0 (+https://github.com/; RSS reader)"

# Headline lexicon. Weights are ordinal, not calibrated — the accuracy tracker
# is what decides whether the whole source deserves weight, so tuning
# individual words here would be fitting noise.
BULLISH_TERMS: dict[str, float] = {
    "beats": 1.0, "beat": 1.0, "tops": 0.9, "raises": 1.0, "raised": 0.8,
    "upgrade": 1.0, "upgrades": 1.0, "outperform": 0.8, "record": 0.7,
    "surge": 0.8, "surges": 0.8, "soars": 0.9, "jumps": 0.7, "rally": 0.6,
    "buyback": 0.8, "repurchase": 0.7, "dividend increase": 0.8,
    "approval": 0.7, "wins": 0.6, "expands": 0.5, "partnership": 0.4,
    "strong demand": 0.9, "guidance raise": 1.0, "acquisition": 0.4,
}
BEARISH_TERMS: dict[str, float] = {
    "misses": 1.0, "miss": 0.9, "cuts": 0.9, "cut": 0.7, "slashes": 1.0,
    "downgrade": 1.0, "downgrades": 1.0, "underperform": 0.8,
    "plunge": 0.9, "plunges": 0.9, "sinks": 0.8, "tumbles": 0.8, "slumps": 0.7,
    "lawsuit": 0.7, "probe": 0.8, "investigation": 0.8, "subpoena": 0.9,
    "recall": 0.8, "layoffs": 0.6, "warns": 0.9, "warning": 0.8,
    "guidance cut": 1.0, "delays": 0.6, "resigns": 0.6, "fraud": 1.0,
    "short seller": 0.9, "bankruptcy": 1.0, "halted": 0.8,
}

# A headline that merely mentions a hedge — "despite the miss" — should not
# score as hard as one that leads with it, but we are not parsing syntax on a
# free tier. Negation is the one case cheap enough to be worth catching.
_NEGATORS = ("no ", "not ", "denies", "despite", "avoids", "without")


def score_headline(headline: str) -> tuple[float, list[str]]:
    """Signed tone in roughly -1..+1, plus the terms that drove it."""
    text = f" {headline.lower()} "
    hits: list[str] = []
    score = 0.0
    for term, weight in BULLISH_TERMS.items():
        if _contains(text, term):
            score += weight * _polarity(text, term)
            hits.append(f"+{term}")
    for term, weight in BEARISH_TERMS.items():
        if _contains(text, term):
            score -= weight * _polarity(text, term)
            hits.append(f"-{term}")
    if not hits:
        return 0.0, []
    # Normalise by hit count so a headline stuffed with synonyms does not
    # outrank a clear one-word beat.
    return max(-1.0, min(1.0, score / len(hits))), hits


def _contains(padded_text: str, term: str) -> bool:
    return re.search(rf"(?<![a-z]){re.escape(term)}(?![a-z])", padded_text) is not None


def _polarity(text: str, term: str) -> float:
    """-1 when the term is negated nearby, +1 otherwise."""
    index = text.find(term)
    window = text[max(0, index - 24) : index]
    return -1.0 if any(neg in window for neg in _NEGATORS) else 1.0


class NewsToneSource(SignalSource):
    """Company headline tone (Finnhub) and market headline tone (RSS)."""

    name = "news_tone"
    scope = "ticker"
    describes = "Finnhub company news + market RSS, headline lexicon tone"

    def __init__(self, finnhub: FinnhubFree, degraded=None, days: int = NEWS_WINDOW_DAYS,
                 limit: int = 12, session=None, as_of: date | None = None):
        super().__init__(degraded)
        self.finnhub = finnhub
        self.days = days
        self.limit = limit
        self._session = session
        #: The snapshot's market date. The window ends here rather than at the
        #: run date so a tone score is computed from the same headlines the
        #: analysts read, and a ``--date`` re-run cannot score this week's news.
        self.as_of = as_of

    def session(self):
        if self._session is None:
            import requests

            self._session = requests.Session()
        return self._session

    def available(self) -> tuple[bool, str]:
        # RSS still works without a key, so the source is never fully dark.
        return True, ""

    def collect(self, symbols: list[str], run_date: date) -> list[Signal]:
        signals = [s for symbol in symbols for s in self._company(symbol, run_date)]
        signals += self._market(symbols, run_date)
        return signals

    def _company(self, symbol: str, run_date: date) -> list[Signal]:
        if not self.finnhub.enabled:
            return []
        start, end = news_window(self.as_of or run_date, self.days)
        items = self.finnhub.company_news(symbol, start, end, limit=self.limit)
        scored = [(item, *score_headline(item.headline)) for item in items]
        toned = [(item, tone, hits) for item, tone, hits in scored if hits]
        if not toned:
            return []
        mean = sum(tone for _, tone, _ in toned) / len(toned)
        strongest = max(toned, key=lambda row: abs(row[1]))
        detail = "\n".join(
            f"  - {_stamp(item.datetime_utc)} [{tone:+.2f}] {item.headline} ({item.source})"
            for item, tone, _ in sorted(toned, key=lambda row: -abs(row[1]))[:5]
        )
        return [
            Signal(
                source=self.name,
                kind="company_news_tone",
                symbol=symbol,
                direction=_sign(mean),
                strength=min(1.0, abs(mean) * (0.6 + 0.1 * min(len(toned), 4))),
                headline=(
                    f"{len(toned)} of {len(items)} headlines carry directional language; "
                    f"mean tone {mean:+.2f}. Strongest: {strongest[0].headline}"
                ),
                detail=detail,
                as_of=run_date,
                url=strongest[0].url,
            )
        ]

    def _market(self, symbols: list[str], run_date: date) -> list[Signal]:
        """Market-wide RSS, plus any headline that names one of our tickers.

        The feeds serve whatever is current and take no date parameter, so on a
        historical run they are pure look-ahead: a ``--date 2026-06-01`` scan
        would score June's prices against today's headlines. There is no
        as-of-safe way to read them, so the leg is skipped and said so.
        """
        as_of = self.as_of or run_date
        if as_of < utcnow().date():
            self.degraded.add(
                f"signals:{self.name} (market RSS)",
                f"feeds serve current headlines only and cannot be read as of "
                f"{as_of.isoformat()} — market tone skipped for this backfill",
            )
            return []
        entries = self._rss_entries()
        if not entries:
            return []
        out: list[Signal] = []
        toned = [(title, feed, *score_headline(title)) for title, feed in entries]
        toned = [row for row in toned if row[3]]
        if toned:
            mean = sum(row[2] for row in toned) / len(toned)
            out.append(
                Signal(
                    source=self.name,
                    kind="market_news_tone",
                    symbol=None,
                    direction=_sign(mean),
                    strength=min(1.0, abs(mean)),
                    headline=(
                        f"{len(toned)} of {len(entries)} market headlines carry directional "
                        f"language; mean tone {mean:+.2f}"
                    ),
                    detail="\n".join(f"  - [{t:+.2f}] {title} ({feed})" for title, feed, t, _ in toned[:5]),
                    as_of=run_date,
                )
            )
        for symbol in symbols:
            named = [(title, feed, tone) for title, feed, tone, _ in toned if _names(title, symbol)]
            if not named:
                continue
            mean = sum(tone for _, _, tone in named) / len(named)
            out.append(
                Signal(
                    source=self.name,
                    kind="market_feed_mention",
                    symbol=symbol,
                    direction=_sign(mean),
                    strength=min(1.0, abs(mean) * 0.8),
                    headline=f"named in {len(named)} market-wide headline(s), mean tone {mean:+.2f}",
                    detail="\n".join(f"  - [{tone:+.2f}] {title} ({feed})" for title, feed, tone in named[:3]),
                    as_of=run_date,
                )
            )
        return out

    def _rss_entries(self) -> list[tuple[str, str]]:
        """Headlines from the market feeds, with a hard ceiling on waiting.

        We fetch the bytes ourselves rather than handing ``feedparser`` a URL.
        Given a URL it does its own retrieval through ``urllib`` with no
        timeout, so a feed host that accepts the connection and then never
        answers hangs the entire daily run — observed live against one of these
        three feeds, which sat in ESTABLISHED with zero bytes for nine minutes
        before the run was killed. A source that dies must degrade visibly; one
        that hangs is worse than one that fails, because in Cloud Run Jobs it
        burns the whole job timeout and produces nothing at all.
        """
        import feedparser

        out: list[tuple[str, str]] = []
        deadline = time.monotonic() + RSS_TOTAL_BUDGET_SECONDS
        for label, url in MARKET_FEEDS:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                log.info("RSS budget of %.0fs spent; skipping %s", RSS_TOTAL_BUDGET_SECONDS, label)
                continue
            try:
                response = self.session().get(
                    url,
                    timeout=(RSS_CONNECT_TIMEOUT, min(RSS_READ_TIMEOUT, remaining)),
                    headers={"User-Agent": RSS_USER_AGENT},
                )
                response.raise_for_status()
                parsed = feedparser.parse(response.content)
            except Exception as exc:  # noqa: BLE001 - one dead feed is not a failure
                log.info("RSS feed %s unavailable: %s", label, exc)
                continue
            entries = getattr(parsed, "entries", []) or []
            if not entries:
                log.info("RSS feed %s returned no entries", label)
                continue
            out += [(str(e.get("title", "")).strip(), label) for e in entries[:40] if e.get("title")]
        return out


def _names(headline: str, symbol: str) -> bool:
    """Ticker mentioned as a word or in a (TICKER) parenthetical."""
    return re.search(rf"(?<![A-Za-z]){re.escape(symbol)}(?![A-Za-z])", headline) is not None


def _sign(value: float) -> int:
    if value > 0.05:
        return 1
    if value < -0.05:
        return -1
    return 0


def _stamp(epoch: int) -> str:
    if not epoch:
        return "undated"
    try:
        return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d")
    except (OverflowError, OSError, ValueError):
        return "undated"


def window_start(run_date: date, days: int) -> date:
    return run_date - timedelta(days=days)
