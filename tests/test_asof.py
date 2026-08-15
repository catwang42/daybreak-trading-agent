"""Look-ahead contamination: a run must never see data newer than its snapshot.

The defect these guard is not a crash and not a wrong number in isolation. It
is a run dated 2026-06-01 that reads June's prices, this week's headlines and
today's RSS feed, and presents the three together as one morning's research.
Every figure is individually true; the report is a lie about time.

The `company_news(symbol, days=7)` signature was the concrete instance: `days`
counted back from wall-clock now, so the window moved with the clock rather
than with the snapshot. It is now `(symbol, start_date, end_date)`, and the
dates come from `snapshot.market_as_of`.
"""

from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from tradingagent.data.finnhub_client import (
    NEWS_WINDOW_DAYS,
    FinnhubFree,
    NewsItem,
    news_window,
)
from tradingagent.data.validate import DegradedTracker
from tradingagent.pipeline.context import DeepContext, QueuedTicker
from tradingagent.pipeline.evidence import EvidenceBuilder
from tradingagent.signals.news import NewsToneSource
from tradingagent.snapshot import ResearchSnapshot

# A deliberately historical run: every wall clock in the process says 2026-08,
# so anything dated after this date reached the report by looking ahead.
BACKFILL = date(2026, 6, 1)
MARKET = date(2026, 5, 29)  # the Friday before


def epoch(when: date, hour: int = 12) -> int:
    return int(datetime(when.year, when.month, when.day, hour, tzinfo=timezone.utc).timestamp())


def item(headline: str, when: date | None, hour: int = 12) -> NewsItem:
    return NewsItem(
        symbol="V",
        headline=headline,
        source="Reuters",
        url="https://example.invalid",
        datetime_utc=epoch(when, hour) if when else 0,
    )


def frame(last_close=100.0, sessions=120, end=MARKET):
    closes = np.linspace(last_close * 0.8, last_close, sessions)
    return pd.DataFrame(
        {
            "Open": closes,
            "High": closes * 1.01,
            "Low": closes * 0.99,
            "Close": closes,
            "Volume": np.full(sessions, 1_500_000.0),
        },
        index=pd.bdate_range(end=pd.Timestamp(end), periods=sessions),
    )


def backfill_snapshot(**kwargs) -> ResearchSnapshot:
    return ResearchSnapshot.from_bars(
        {"V": frame(320.0), "UNP": frame(240.0)},
        BACKFILL,
        session="market CLOSED (last completed session 2026-05-29)",
        universe_version="sp500.json@2026-06-01",
        **kwargs,
    )


class RecordingFinnhub:
    """Records the window it was asked for and answers with what it is given."""

    enabled = True

    def __init__(self, items=None):
        self.items = items or []
        self.calls: list[tuple[str, date, date, int]] = []

    def company_news(self, symbol, start_date, end_date, limit=5):
        self.calls.append((symbol, start_date, end_date, limit))
        return list(self.items)


class RefusesToFetch:
    enabled = True

    def company_news(self, symbol, start_date, end_date, limit=5):
        raise AssertionError(f"the deep stage re-fetched news for {symbol}")


class _ThinFundamentals:
    class _Empty:
        missing: list[str] = []

        def suspect_fields(self):
            return []

    def fundamentals(self, symbol):
        return self._Empty()

    def positioning(self, symbol):
        return None


class _RefusesToDownload:
    def load_many(self, symbols, min_rows=0, period=None):
        raise AssertionError(f"the deep stage re-downloaded {symbols}")


def builder(snapshot, finnhub, market=None) -> EvidenceBuilder:
    context = DeepContext(
        run_date=snapshot.run_date.isoformat(),
        snapshot_id=snapshot.snapshot_id,
        market_as_of=snapshot.market_as_of.isoformat(),
    )
    return EvidenceBuilder(
        context,
        finnhub,
        DegradedTracker(),
        market=market or _RefusesToDownload(),
        fundamentals=_ThinFundamentals(),
        snapshot=snapshot,
    )


# --- the window is the snapshot's, not the clock's ------------------------


def test_the_news_window_ends_at_the_as_of_date_it_was_given():
    start, end = news_window(MARKET)
    assert end == MARKET
    assert start == MARKET - timedelta(days=NEWS_WINDOW_DAYS)


def test_the_client_drops_headlines_filed_after_the_window_it_was_asked_for():
    """Finnhub's `to` is a date; a story filed at 20:00 on that date is not
    evidence a close-priced snapshot could have seen."""

    class Provider:
        def company_news(self, symbol, _from, to):
            return [
                {"headline": "in window", "source": "Reuters", "url": "", "datetime": epoch(MARKET, 9)},
                {"headline": "next week", "source": "Reuters", "url": "", "datetime": epoch(date(2026, 6, 3))},
                {"headline": "undated", "source": "Reuters", "url": "", "datetime": 0},
            ]

    client = FinnhubFree.__new__(FinnhubFree)
    client.degraded = DegradedTracker()
    client._key = "test"
    client._client = Provider()

    headlines = [n.headline for n in client.company_news("V", *news_window(MARKET), limit=10)]
    assert "next week" not in headlines
    assert "in window" in headlines
    # An undated headline is a provider gap, not a future story, and it is
    # printed as "undated" wherever it appears.
    assert "undated" in headlines


def test_a_headline_filed_after_the_close_is_refused_by_the_snapshot():
    snap = backfill_snapshot()
    kept = snap.freeze_news(
        "V",
        [item("before the close", MARKET, hour=13), item("filed on Monday", date(2026, 6, 1))],
        window=news_window(MARKET),
    )
    assert [n.headline for n in kept] == ["before the close"]
    assert snap.headlines("V") == kept
    assert any("look-ahead" in v for v in snap.violations)


def test_the_signal_source_windows_its_news_on_the_snapshot_not_the_run_date():
    finnhub = RecordingFinnhub()
    source = NewsToneSource(finnhub=finnhub, as_of=MARKET)
    source._company("V", BACKFILL)
    symbol, start, end, _ = finnhub.calls[0]
    assert (symbol, end) == ("V", MARKET)
    assert start == MARKET - timedelta(days=NEWS_WINDOW_DAYS)


def test_the_rss_leg_is_skipped_on_a_historical_run_rather_than_read_live():
    """The feeds take no date parameter, so on a backfill they are pure
    look-ahead. There is no as-of-safe read, so there is no read."""
    degraded = DegradedTracker()
    source = NewsToneSource(finnhub=RecordingFinnhub(), degraded=degraded, as_of=MARKET)
    source._session = object()  # any use of it would raise AttributeError

    assert source._market(["V"], BACKFILL) == []
    assert any("cannot be read as of" in reason for _, reason in degraded.entries)


# --- the deep stage reads what discovery froze ----------------------------


def test_the_deep_stage_reuses_the_frozen_headlines_instead_of_fetching_again():
    snap = backfill_snapshot()
    snap.freeze_news("V", [item("frozen at discovery", MARKET)], window=news_window(MARKET))

    evidence = builder(snap, RefusesToFetch()).build(QueuedTicker(symbol="V"))

    assert [n.headline for n in evidence.news] == ["frozen at discovery"]
    assert evidence.off_snapshot == []
    assert "2026-05-22..2026-05-29" in evidence.news_window_note


def test_a_quiet_window_is_frozen_as_empty_and_not_re_fetched():
    """`no headlines` and `nobody looked` are different facts, and only the
    second justifies a second call."""
    snap = backfill_snapshot()
    snap.freeze_news("V", [], window=news_window(MARKET))

    evidence = builder(snap, RefusesToFetch()).build(QueuedTicker(symbol="V"))

    assert evidence.news == []
    assert "company news" in evidence.missing


def test_a_ticker_discovery_never_saw_is_fetched_to_the_snapshots_window():
    snap = backfill_snapshot()
    finnhub = RecordingFinnhub([item("fetched now", MARKET)])

    evidence = builder(snap, finnhub).build(QueuedTicker(symbol="V"))

    symbol, start, end, _ = finnhub.calls[0]
    assert (symbol, start, end) == ("V", MARKET - timedelta(days=NEWS_WINDOW_DAYS), MARKET)
    assert evidence.off_snapshot == ["V company news (fetched by this stage)"]


def test_a_dated_run_sees_nothing_newer_than_its_snapshot():
    """The whole point, asserted over every dated thing in one evidence pack."""
    snap = backfill_snapshot()
    snap.freeze_news(
        "V",
        [item("in window", MARKET - timedelta(days=1)), item("undated", None)],
        window=news_window(MARKET),
    )

    evidence = builder(snap, RefusesToFetch()).build(QueuedTicker(symbol="V"))

    cutoff = datetime.combine(MARKET, time.max, tzinfo=timezone.utc).timestamp()
    assert all(n.datetime_utc <= cutoff for n in evidence.news)
    assert evidence.market_as_of == MARKET
    assert evidence.price_observation.effective_at == MARKET
    assert dict(evidence.source_notes)["bars"] == "2026-05-29 close"
    assert snap.violations == []


# --- the audit, as a test -------------------------------------------------

#: Wall clock is allowed to answer "when did this run happen", never "which
#: day's data is this". Every file below was read line by line for M6; a new
#: entry here needs the same reading, which is why the list is explicit.
CLOCK_IS_ALLOWED = {
    "snapshot.py": "utcnow(), the one sanctioned reader, used only for run metadata",
    "config.py": "the default run date when --date is not given",
    "data/alpaca_client.py": "market-clock lookup: 'is the market open right now'",
    "data/option_chain.py": "live quote freshness and DTE, the named second snapshot",
    "options/stage.py": "how stale the live quotes are, reported in the section",
}
CLOCK_CALLS = ("date.today()", "datetime.now(", "Timestamp.now(", "time.time()")


def _calls(code: str, token: str) -> bool:
    start = 0
    while (index := code.find(token, start)) != -1:
        if "`" not in code[max(0, index - 2) : index]:
            return True
        start = index + 1
    return False


def test_no_data_path_reads_the_wall_clock_to_decide_what_to_fetch():
    root = Path(__file__).resolve().parents[1] / "src" / "tradingagent"
    offenders: list[str] = []
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        if rel in CLOCK_IS_ALLOWED:
            continue
        for number, line in enumerate(path.read_text().splitlines(), 1):
            # Comments and ``quoted`` prose may name these calls; only code counts.
            code = line.split("#", 1)[0]
            if any(_calls(code, call) for call in CLOCK_CALLS):
                offenders.append(f"{rel}:{number}: {line.strip()}")
    assert offenders == [], (
        "wall-clock call outside the allowlist — if it records when a run "
        "happened, route it through tradingagent.snapshot.utcnow(); if it "
        "decides which data to fetch, drive it from the snapshot's market "
        "date instead:\n" + "\n".join(offenders)
    )
