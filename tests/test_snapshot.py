"""Research-snapshot tests: one picture per run, and every price traceable to it.

The regression these guard against was invisible in every existing test,
because each stage was individually correct: discovery downloaded bars and
screened them, the deep stage downloaded bars and analysed them. Only reading
the two reports side by side showed V at 365.45 in one and 364.15 in the other.
So the tests here are mostly about *identity* — the deep stage must return the
same float discovery ranked on, not a close one.
"""

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from tradingagent.data.finnhub_client import NewsItem, news_window
from tradingagent.data.validate import DegradedTracker
from tradingagent.pipeline.context import DeepContext, QueuedTicker
from tradingagent.pipeline.evidence import EvidenceBuilder
from tradingagent.snapshot import (
    MANIFEST_FILENAME,
    SNAPSHOT_DIRNAME,
    LookAhead,
    Observation,
    ResearchSnapshot,
)

RUN = date(2026, 8, 14)


def frame(last_close=100.0, sessions=300, end="2026-08-13"):
    closes = np.linspace(last_close * 0.6, last_close, sessions)
    return pd.DataFrame(
        {
            "Open": closes,
            "High": closes * 1.01,
            "Low": closes * 0.99,
            "Close": closes,
            "Volume": np.full(sessions, 2_000_000.0),
        },
        index=pd.bdate_range(end=end, periods=sessions),
    )


def snapshot(bars=None, news=True, **kwargs):
    snap = ResearchSnapshot.from_bars(
        bars if bars is not None else {"V": frame(365.45), "UNP": frame(297.79)},
        RUN,
        session="market CLOSED (last completed session 2026-08-13)",
        universe_version="sp500.json@2026-08-14",
        **kwargs,
    )
    if news:
        # Discovery froze the window for everything it priced, which is the
        # state the deep stage actually meets.
        for symbol in snap.prices:
            snap.freeze_news(symbol, [], window=news_window(snap.market_as_of))
    return snap


# --- what a snapshot says ------------------------------------------------


def test_the_market_date_is_the_last_session_in_the_data_not_today():
    """A pre-market run on Friday is looking at Thursday, and must say so."""
    snap = snapshot()
    assert snap.run_date == RUN
    assert snap.market_as_of == date(2026, 8, 13)
    assert snap.market_as_of < snap.run_date


def test_every_price_carries_its_lineage():
    snap = snapshot()
    v = snap.price("V")
    assert isinstance(v, Observation)
    assert v.value == pytest.approx(365.45)
    assert v.effective_at == date(2026, 8, 13)
    assert v.snapshot_id == snap.snapshot_id
    assert "yfinance" in v.source
    # observed_at is the fetch clock, and is a different question from the
    # session the value describes.
    assert v.observed_at.tzinfo is timezone.utc
    assert v.effective_at.isoformat() in v.cite() and snap.snapshot_id in v.cite()


def test_the_snapshot_id_names_the_run_the_date_and_the_picture():
    snap = snapshot()
    assert snap.snapshot_id.startswith("snap-2026-08-14-primary-")
    assert snap.label.startswith(f"`{snap.snapshot_id}`")
    assert "2026-08-13" in snap.label


def test_a_ticker_with_no_usable_close_is_left_out_and_counted():
    bad = frame(50.0)
    bad.loc[bad.index[-1], "Close"] = float("nan")
    snap = snapshot({"V": frame(365.45), "JUNK": bad}, requested=3)
    assert snap.price("JUNK") is None
    assert snap.data_quality.requested == 3 and snap.data_quality.usable == 1
    assert "1/3 symbols usable (33%)" in snap.data_quality.line()


def test_symbols_are_looked_up_case_insensitively():
    snap = snapshot()
    assert snap.close("v") == snap.close("V")
    assert snap.frame("unp") is not None


# --- the assertions ------------------------------------------------------


def test_data_from_after_the_market_date_is_recorded_as_a_look_ahead():
    snap = snapshot()
    assert snap.check("V bars", date(2026, 8, 13)) is True
    assert snap.check("V bars", date(2026, 8, 14)) is False
    assert not snap.violations[:1] == []
    assert "look-ahead" in snap.violations[0]


def test_a_figure_claiming_another_snapshot_is_a_violation_too():
    snap = snapshot()
    assert snap.check("V close", date(2026, 8, 13), snapshot_id="snap-other") is False
    assert "mixed snapshots" in snap.violations[0]


def test_require_raises_where_continuing_would_be_dishonest():
    snap = snapshot()
    with pytest.raises(LookAhead, match="look-ahead"):
        snap.require("earnings", date(2026, 9, 1))


def test_a_missing_effective_date_is_not_treated_as_a_violation():
    """Absent lineage is a gap to report, not a look-ahead to invent."""
    snap = snapshot()
    assert snap.check("fundamentals", None) is True
    assert snap.violations == []


# --- persistence ---------------------------------------------------------


def test_the_manifest_round_trips_with_prices_and_the_queue_s_bars(tmp_path):
    snap = snapshot({"V": frame(365.45), "UNP": frame(297.79), "STZ": frame(136.35)})
    snap.write(tmp_path, keep_bars=["V", "STZ"])

    back = ResearchSnapshot.read(tmp_path)
    assert back.snapshot_id == snap.snapshot_id
    assert back.market_as_of == snap.market_as_of
    assert back.universe_version == "sp500.json@2026-08-14"
    # Prices for the whole screened universe; bars only for the deep queue.
    assert set(back.prices) == {"V", "UNP", "STZ"}
    assert set(back.bars) == {"V", "STZ"}
    assert back.close("V") == pytest.approx(snap.close("V"))
    assert len(back.frame("V").index) == len(snap.frame("V").index)


def test_a_stale_snapshot_schema_is_refused_rather_than_half_read(tmp_path):
    root = tmp_path / SNAPSHOT_DIRNAME
    root.mkdir()
    (root / MANIFEST_FILENAME).write_text('{"version": 0, "snapshot_id": "x"}')
    with pytest.raises(ValueError, match="re-run the discovery stage"):
        ResearchSnapshot.read(tmp_path)


def test_a_missing_snapshot_names_the_command_that_produces_it(tmp_path):
    with pytest.raises(FileNotFoundError, match="--stage discovery"):
        ResearchSnapshot.read(tmp_path)


# --- the second, named snapshot -----------------------------------------


def test_a_stage_that_needs_fresher_data_gets_a_named_snapshot_of_its_own():
    snap = snapshot()
    quotes = snap.derive("options-quotes")
    assert quotes.snapshot_id != snap.snapshot_id
    assert "options-quotes" in quotes.snapshot_id
    # Same run, same session it is measured against — a different moment.
    assert quotes.run_date == snap.run_date and quotes.market_as_of == snap.market_as_of
    assert snap.snapshot_id in quotes.data_quality.notes[0]


# --- the drift this module exists to remove ------------------------------


class _RefusesToDownload:
    """A MarketData that fails the test if the deep stage reaches for the wire."""

    def __init__(self):
        self.calls: list[list[str]] = []

    def load_many(self, symbols, min_rows=0, period=None):
        self.calls.append(list(symbols))
        raise AssertionError(f"the deep stage re-downloaded {symbols}")


class _Downloads(_RefusesToDownload):
    def load_many(self, symbols, min_rows=0, period=None):
        self.calls.append(list(symbols))
        return {s: frame(42.0) for s in symbols}


class _NoNews:
    def company_news(self, symbol, start_date, end_date, limit=8):
        self.windows = getattr(self, 'windows', [])
        self.windows.append((symbol, start_date, end_date))
        return []


class _ThinFundamentals:
    """Enough of the client's shape to build evidence; none of the network."""

    class _Empty:
        missing: list[str] = []

        def suspect_fields(self):
            return []

    def fundamentals(self, symbol):
        return self._Empty()

    def positioning(self, symbol):
        return None


def _builder(snap, market=None):
    context = DeepContext(
        run_date=RUN.isoformat(),
        snapshot_id=snap.snapshot_id if snap else "",
        market_as_of=snap.market_as_of.isoformat() if snap else "",
    )
    builder = EvidenceBuilder(
        context,
        _NoNews(),
        DegradedTracker(),
        market=market or _RefusesToDownload(),
        fundamentals=_ThinFundamentals(),
        snapshot=snap,
    )
    return builder


def test_the_deep_stage_reads_the_snapshot_instead_of_downloading_again():
    snap = snapshot()
    market = _RefusesToDownload()
    builder = _builder(snap, market)
    builder.prefetch(["V", "UNP"])
    assert market.calls == []  # the whole point


def test_the_close_the_deep_stage_reports_is_the_one_discovery_ranked_on():
    """The V 365.45 / 364.15 bug, as a test."""
    snap = snapshot()
    builder = _builder(snap)
    builder.prefetch(["V"])
    evidence = builder.build(QueuedTicker(symbol="V"))

    assert evidence.price == pytest.approx(snap.close("V"))
    assert evidence.snapshot_id == snap.snapshot_id
    assert evidence.market_as_of == snap.market_as_of
    assert evidence.price_observation is snap.price("V")
    assert evidence.off_snapshot == []


def test_the_deep_report_can_name_the_snapshot_every_price_came_from():
    snap = snapshot()
    builder = _builder(snap)
    builder.prefetch(["V"])
    provenance = builder.build(QueuedTicker(symbol="V")).provenance()

    assert snap.snapshot_id in provenance
    assert "2026-08-13" in provenance
    assert "365.45" in provenance
    assert "discovery snapshot" in builder.build(QueuedTicker(symbol="V")).sources()


def test_a_ticker_the_snapshot_never_saw_is_fetched_but_labelled_as_such():
    """`--tickers ZZZ` is legitimate; pretending it came from the snapshot is not."""
    snap = snapshot()
    market = _Downloads()
    builder = _builder(snap, market)
    builder.prefetch(["V", "ZZZ"])
    assert market.calls == [["ZZZ"]]

    evidence = builder.build(QueuedTicker(symbol="ZZZ"))
    assert evidence.off_snapshot == [
        "ZZZ daily bars (not in the snapshot)",
        "ZZZ company news (fetched by this stage)",
    ]
    assert evidence.price_observation is None
    assert "Fetched outside the snapshot" in evidence.provenance()
    # The one that was in the snapshot is unaffected by its neighbour.
    assert builder.build(QueuedTicker(symbol="V")).off_snapshot == []


def test_without_a_snapshot_the_stage_still_runs_and_says_it_cannot_prove_lineage():
    market = _Downloads()
    builder = _builder(None, market)
    builder.prefetch(["V"])
    evidence = builder.build(QueuedTicker(symbol="V"))

    assert evidence.usable
    assert market.calls == [["V"]]
    assert "Provenance unavailable" in evidence.provenance()
    assert "re-downloaded by this stage" in evidence.sources()


def test_a_snapshot_carrying_bars_newer_than_its_own_market_date_is_caught():
    """The file was edited, or newer bars were merged into a frozen picture."""
    snap = snapshot()
    tampered = frame(365.45, end="2026-08-20")
    snap.bars["V"] = tampered

    builder = _builder(snap)
    builder.prefetch(["V"])
    builder.build(QueuedTicker(symbol="V"))
    assert any("look-ahead" in v for v in snap.violations)


def test_a_short_frame_in_the_snapshot_is_topped_up_rather_than_analysed_thin():
    snap = snapshot({"V": frame(365.45, sessions=20)})
    market = _Downloads()
    builder = _builder(snap, market)
    builder.prefetch(["V"])
    assert market.calls == [["V"]]  # 20 bars is below MIN_BARS


# --- the file the next stage reads --------------------------------------


def test_the_written_snapshot_is_small_enough_to_keep_every_run(tmp_path):
    """~10 queued names, not the 500 screened ones."""
    bars = {f"T{i:03d}": frame(100.0) for i in range(120)}
    snap = snapshot(bars)
    snap.write(tmp_path, keep_bars=[f"T{i:03d}" for i in range(10)])

    written = list((tmp_path / SNAPSHOT_DIRNAME).rglob("*"))
    csvs = [p for p in written if p.suffix == ".csv"]
    assert len(csvs) == 10
    total_kb = sum(p.stat().st_size for p in written if p.is_file()) / 1024
    assert total_kb < 500


def test_the_manifest_is_stamped_with_the_moment_it_was_taken(tmp_path):
    observed = datetime(2026, 8, 14, 13, 42, 1, tzinfo=timezone.utc)
    snap = ResearchSnapshot.from_bars(
        {"V": frame(365.45)}, RUN, observed_at=observed
    )
    assert snap.snapshot_id == "snap-2026-08-14-primary-134201Z"
    snap.write(tmp_path)
    assert ResearchSnapshot.read(tmp_path).observed_at == observed


def test_bars_are_written_only_for_symbols_the_snapshot_actually_holds(tmp_path):
    snap = snapshot()
    snap.write(tmp_path, keep_bars=["V", "NOPE"])
    back = ResearchSnapshot.read(tmp_path)
    assert set(back.bars) == {"V"}
    assert Path(tmp_path, SNAPSHOT_DIRNAME, "bars", "NOPE.csv").exists() is False


def test_an_empty_run_still_produces_a_snapshot_rather_than_nothing():
    """A holiday, or a total data outage: the picture is empty, not absent."""
    snap = snapshot({}, requested=503)
    assert snap.market_as_of == RUN  # nothing to date it by but the run itself
    assert snap.prices == {}
    assert snap.data_quality.coverage_pct == 0.0


def test_the_run_date_can_precede_today_without_the_snapshot_objecting():
    """A `--date` backfill: the snapshot must date itself by its data."""
    old = ResearchSnapshot.from_bars(
        {"V": frame(300.0, end="2026-06-01")}, date(2026, 6, 2)
    )
    assert old.market_as_of == date(2026, 6, 1)
    assert old.check("V bars", date(2026, 6, 1)) is True
    # Anything from the months since is a contamination, whatever today is.
    assert old.check("V news", date(2026, 8, 14)) is False


# --- what a standalone stage does when the picture is missing ------------


def test_a_standalone_stage_says_so_when_it_cannot_load_the_snapshot(tmp_path):
    from tradingagent.stages import _load_snapshot

    degraded = DegradedTracker()
    assert _load_snapshot(tmp_path, DeepContext(run_date=RUN.isoformat()), degraded) is None
    assert "Research snapshot" in degraded.sources
    assert any("download its own bars" in reason for _, reason in degraded.entries)


def test_a_snapshot_that_is_not_the_one_the_queue_was_built_from_is_flagged(tmp_path):
    from tradingagent.stages import _load_snapshot

    snapshot().write(tmp_path)
    degraded = DegradedTracker()
    context = DeepContext(run_date=RUN.isoformat(), snapshot_id="snap-2026-08-14-primary-090000Z")
    loaded = _load_snapshot(tmp_path, context, degraded)

    assert loaded is not None  # still usable, but the reader is told
    assert any("prices may differ from the brief" in r for _, r in degraded.entries)


def test_derived_snapshots_do_not_inherit_the_parent_s_prices():
    """The overlay must re-read quotes, not silently reuse yesterday's closes."""
    snap = snapshot()
    quotes = snap.derive("options-quotes", observed_at=snap.observed_at + timedelta(hours=2))
    assert quotes.prices == {} and quotes.bars == {}
