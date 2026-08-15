"""One immutable market picture per run, shared by every stage.

The bug this exists to kill is quiet and was found by reading two reports side
by side rather than by any test. Discovery downloaded the universe's bars,
screened them and published a shortlist; the deep stage then constructed its
*own* ``MarketData`` and downloaded the same tickers again, minutes later. On
one run that produced V at 365.45 in section 4 and 364.15 in the deep report,
UNP at 297.79 and 293.68, STZ at 136.35 and 139.18. Nothing failed. The two
stages simply argued about different days' closes, and the trade math — entry,
stop, risk percent — was computed against whichever one happened to be nearest.

So a run now takes exactly one picture and every stage reads from it:

- :class:`ResearchSnapshot` owns the bars, the per-ticker close, the market
  date those closes belong to, and the universe version they were screened
  against. It is written to the report directory, so a standalone
  ``--stage deep`` tomorrow reproduces today's numbers instead of fetching new
  ones.
- Every price carries an :class:`Observation`: value, source, when we read it,
  which session it describes, and which snapshot it belongs to. A figure that
  cannot name its snapshot is a figure nobody can check.
- :meth:`ResearchSnapshot.check` refuses data from after the snapshot's market
  date. A ``--date 2026-06-01`` re-run that quietly sees August bars is not a
  backtest, it is a look-ahead, and it is the one failure mode that makes every
  downstream number look better than it was.

A stage that genuinely needs fresher data than the snapshot — the options
overlay prices live quotes, and must — does not silently reach past it. It
takes a *second, named* snapshot (:meth:`derive`) and says so in the report, so
a reader can see which of two moments each number belongs to.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from .data.finnhub_client import NewsItem

log = logging.getLogger(__name__)

SNAPSHOT_DIRNAME = "snapshot"
MANIFEST_FILENAME = "snapshot.json"
BARS_DIRNAME = "bars"
SCHEMA_VERSION = 1

#: The name of the run's one authoritative picture. Anything else must justify
#: itself with a name of its own.
PRIMARY = "primary"


class LookAhead(Exception):
    """Data dated after the snapshot it claims to belong to."""


def utcnow() -> datetime:
    """Wall clock, for runtime metadata only.

    This is the *only* sanctioned `now` in the data path, and it never decides
    what gets fetched — it records when a fetch happened. Every question of
    "which day's data is this" is answered by ``market_as_of``.
    """
    return datetime.now(timezone.utc)


#: Kept as a module-private alias so existing call sites read the same.
_utcnow = utcnow


def make_snapshot_id(run_date: date, name: str, observed_at: datetime) -> str:
    """``snap-2026-08-14-primary-134201Z`` — sortable, and readable in a report."""
    return f"snap-{run_date.isoformat()}-{name}-{observed_at:%H%M%S}Z"


@dataclass(frozen=True)
class Observation:
    """One value with the lineage needed to check it later.

    ``observed_at`` is when we read it; ``effective_at`` is the session it
    describes. They are different questions and conflating them is how a
    Friday-morning run ends up presenting Thursday's close as today's price.
    """

    value: float
    source: str
    observed_at: datetime
    effective_at: date
    snapshot_id: str

    def cite(self) -> str:
        return (
            f"{self.value:,.2f} ({self.source}, {self.effective_at.isoformat()} close, "
            f"{self.snapshot_id})"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "source": self.source,
            "observed_at": self.observed_at.isoformat(),
            "effective_at": self.effective_at.isoformat(),
            "snapshot_id": self.snapshot_id,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Observation":
        return cls(
            value=float(raw["value"]),
            source=str(raw["source"]),
            observed_at=datetime.fromisoformat(raw["observed_at"]),
            effective_at=date.fromisoformat(raw["effective_at"]),
            snapshot_id=str(raw["snapshot_id"]),
        )


@dataclass
class DataQuality:
    """What the snapshot could not see, carried with the snapshot rather than
    reconstructed downstream from an absence."""

    requested: int = 0
    usable: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def coverage_pct(self) -> float:
        return 100.0 * self.usable / self.requested if self.requested else 0.0

    def line(self) -> str:
        note = f" — {'; '.join(self.notes)}" if self.notes else ""
        return f"{self.usable}/{self.requested} symbols usable ({self.coverage_pct:.0f}%){note}"

    def to_dict(self) -> dict[str, Any]:
        return {"requested": self.requested, "usable": self.usable, "notes": list(self.notes)}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "DataQuality":
        return cls(
            requested=int(raw.get("requested", 0)),
            usable=int(raw.get("usable", 0)),
            notes=list(raw.get("notes", [])),
        )


@dataclass
class ResearchSnapshot:
    """The one market picture a run is allowed to reason from."""

    snapshot_id: str
    run_date: date
    market_as_of: date
    observed_at: datetime
    name: str = PRIMARY
    session: str = "session state unavailable"
    universe_version: str = "unknown"
    #: symbol -> last close, with lineage.
    prices: dict[str, Observation] = field(default_factory=dict)
    #: symbol -> OHLCV. Held in memory for the whole run; only the queued
    #: tickers are persisted, because 500 frames of two-year history is 15 MB a
    #: day in a bucket to answer a question nobody asks after the run.
    bars: dict[str, pd.DataFrame] = field(default_factory=dict, repr=False)
    #: symbol -> the headlines discovery read, already trimmed to the window.
    #: A missing symbol means nobody fetched news for that name; an empty list
    #: means we looked and the window was quiet. The deep stage must be able to
    #: tell those apart, because only the first justifies a second call.
    news: dict[str, list[NewsItem]] = field(default_factory=dict, repr=False)
    #: The ``(start, end)`` every frozen headline was asked for, so a report can
    #: state the window instead of implying "recent".
    news_window: tuple[date, date] | None = None
    data_quality: DataQuality = field(default_factory=DataQuality)
    #: Assertion failures, kept rather than raised so one contaminated ticker
    #: degrades itself instead of ending a run that has already spent tokens.
    violations: list[str] = field(default_factory=list)

    # -- construction ----------------------------------------------------
    @classmethod
    def from_bars(
        cls,
        bars: dict[str, pd.DataFrame],
        run_date: date,
        *,
        session: str = "session state unavailable",
        universe_version: str = "unknown",
        requested: int | None = None,
        notes: Iterable[str] = (),
        source: str = "yfinance daily bars",
        name: str = PRIMARY,
        observed_at: datetime | None = None,
    ) -> "ResearchSnapshot":
        """Freeze a bulk download into the run's picture.

        ``market_as_of`` is the latest session present in the data, not today:
        a pre-market run on Friday is looking at Thursday's closes and every
        downstream assertion should be made against Thursday.
        """
        observed_at = observed_at or _utcnow()
        sessions = [
            _last_session(frame) for frame in bars.values() if _last_session(frame) is not None
        ]
        market_as_of = max(sessions) if sessions else run_date
        snapshot_id = make_snapshot_id(run_date, name, observed_at)

        prices: dict[str, Observation] = {}
        for symbol, frame in bars.items():
            effective = _last_session(frame)
            close = _last_close(frame)
            if effective is None or close is None:
                continue
            prices[symbol] = Observation(
                value=close,
                source=source,
                observed_at=observed_at,
                effective_at=effective,
                snapshot_id=snapshot_id,
            )
        return cls(
            snapshot_id=snapshot_id,
            run_date=run_date,
            market_as_of=market_as_of,
            observed_at=observed_at,
            name=name,
            session=session,
            universe_version=universe_version,
            prices=prices,
            bars=dict(bars),
            data_quality=DataQuality(
                requested=requested if requested is not None else len(bars),
                usable=len(prices),
                notes=list(notes),
            ),
        )

    def derive(self, name: str, observed_at: datetime | None = None) -> "ResearchSnapshot":
        """A second, named picture for a stage that must read fresher data.

        The options overlay is the honest case: a strike priced against a
        two-hour-old quote is wrong in a way a stale close is not. It gets its
        own snapshot id and its own row in the report rather than quietly
        mixing newer numbers into the primary one.
        """
        observed_at = observed_at or _utcnow()
        return ResearchSnapshot(
            snapshot_id=make_snapshot_id(self.run_date, name, observed_at),
            run_date=self.run_date,
            market_as_of=self.market_as_of,
            observed_at=observed_at,
            name=name,
            session=self.session,
            universe_version=self.universe_version,
            data_quality=DataQuality(notes=[f"derived from {self.snapshot_id}"]),
        )

    # -- reading ---------------------------------------------------------
    def price(self, symbol: str) -> Observation | None:
        return self.prices.get(symbol.upper())

    def close(self, symbol: str) -> float | None:
        observation = self.price(symbol)
        return observation.value if observation else None

    def frame(self, symbol: str) -> pd.DataFrame | None:
        return self.bars.get(symbol.upper())

    @property
    def label(self) -> str:
        return f"`{self.snapshot_id}` (market as of {self.market_as_of.isoformat()} close)"

    def cite(self, symbol: str) -> str:
        observation = self.price(symbol)
        return observation.cite() if observation else f"no price for {symbol} in {self.snapshot_id}"

    # -- news ------------------------------------------------------------
    @property
    def news_cutoff(self) -> int:
        """End of the market date in UTC epoch seconds.

        Prices in this snapshot are closes from ``market_as_of``; a headline
        filed after that close is commentary on a session the snapshot has not
        seen, and reading it alongside those prices is a look-ahead however
        small it looks.
        """
        return int(datetime.combine(self.market_as_of, time.max, tzinfo=timezone.utc).timestamp())

    def freeze_news(
        self,
        symbol: str,
        items: Iterable[NewsItem],
        window: tuple[date, date] | None = None,
    ) -> list[NewsItem]:
        """Store the headlines one fetch produced, dropping anything too new.

        The provider already answered a bounded window; this is the belt to
        that braces, because Finnhub's ``to`` is a date and a story filed at
        20:00 UTC on the market date is inside the date and outside the close.
        """
        key = symbol.upper()
        kept: list[NewsItem] = []
        for item in items:
            if item.datetime_utc and item.datetime_utc > self.news_cutoff:
                self.check(f"{key} headline {item.headline[:60]!r}", item.published_at)
                continue
            kept.append(item)
        self.news[key] = kept
        if window is not None:
            self.news_window = window
        return kept

    def headlines(self, symbol: str) -> list[NewsItem] | None:
        """Frozen headlines for a symbol, or ``None`` if none were fetched."""
        return self.news.get(symbol.upper())

    def news_note(self) -> str:
        if not self.news_window:
            return "no company-news window recorded"
        start, end = self.news_window
        return f"company news {start.isoformat()}..{end.isoformat()} (published on or before the close)"

    # -- assertions ------------------------------------------------------
    def check(self, what: str, effective_at: date | None, snapshot_id: str | None = None) -> bool:
        """Assert a value belongs to this snapshot. Records rather than raises.

        Two questions, both cheap and both worth asking every time a figure
        enters a report: is it from after the snapshot's market date (a
        look-ahead), and does it claim a different snapshot (a stage that went
        and fetched its own)?
        """
        ok = True
        if effective_at is not None and effective_at > self.market_as_of:
            self.violations.append(
                f"{what}: dated {effective_at.isoformat()}, after this snapshot's market date "
                f"{self.market_as_of.isoformat()} — look-ahead"
            )
            ok = False
        if snapshot_id is not None and snapshot_id != self.snapshot_id:
            self.violations.append(
                f"{what}: carries {snapshot_id}, not {self.snapshot_id} — mixed snapshots"
            )
            ok = False
        if not ok:
            log.warning("Snapshot violation — %s", self.violations[-1])
        return ok

    def require(self, what: str, effective_at: date | None, snapshot_id: str | None = None) -> None:
        """:meth:`check`, but for code paths where continuing would be dishonest."""
        if not self.check(what, effective_at, snapshot_id):
            raise LookAhead(self.violations[-1])

    # -- persistence -----------------------------------------------------
    def manifest(self) -> dict[str, Any]:
        return {
            "version": SCHEMA_VERSION,
            "snapshot_id": self.snapshot_id,
            "name": self.name,
            "run_date": self.run_date.isoformat(),
            "market_as_of": self.market_as_of.isoformat(),
            "observed_at": self.observed_at.isoformat(),
            "session": self.session,
            "universe_version": self.universe_version,
            "data_quality": self.data_quality.to_dict(),
            "violations": list(self.violations),
            "prices": {s: o.to_dict() for s, o in sorted(self.prices.items())},
            "news_window": [d.isoformat() for d in self.news_window] if self.news_window else None,
            "news": {s: [n.to_dict() for n in items] for s, items in sorted(self.news.items())},
        }

    def write(
        self, directory: Path, keep_bars: Iterable[str] = (), bucket: str | None = None
    ) -> Path:
        """Persist the manifest, plus bars for the symbols the next stage needs.

        ``keep_bars`` is the deep queue. Persisting it is what lets a
        standalone ``--stage deep`` run without a second download, which is the
        whole point: a re-download is a different snapshot wearing this one's
        name. ``bucket`` mirrors the same files to GCS, because on Cloud Run
        the container's disk does not survive the stage that wrote it.
        """
        root = Path(directory) / SNAPSHOT_DIRNAME
        (root / BARS_DIRNAME).mkdir(parents=True, exist_ok=True)
        wanted = [s.upper() for s in keep_bars]
        kept: list[str] = []
        for symbol in wanted:
            frame = self.bars.get(symbol)
            if frame is None or frame.empty:
                continue
            csv = root / BARS_DIRNAME / f"{symbol}.csv"
            text = frame.to_csv()
            csv.write_text(text)
            kept.append(symbol)
            _mirror(bucket, csv, text)
        path = root / MANIFEST_FILENAME
        # The full price map is kept, not just the queue's: it is small, and it
        # is what lets a reader confirm the shortlist ranked on these closes.
        payload = json.dumps({**self.manifest(), "bars": sorted(kept)}, indent=2)
        path.write_text(payload)
        _mirror(bucket, path, payload)
        log.info(
            "Snapshot %s written: %d price(s), %d bar set(s)",
            self.snapshot_id, len(self.prices), len(kept),
        )
        return path

    @classmethod
    def read(cls, directory: Path) -> "ResearchSnapshot":
        root = Path(directory) / SNAPSHOT_DIRNAME
        path = root / MANIFEST_FILENAME
        if not path.exists():
            raise FileNotFoundError(
                f"No research snapshot at {path}. Run `--stage discovery` for this date "
                f"first, or use `--stage all`."
            )
        raw = json.loads(path.read_text())
        version = int(raw.get("version", 0))
        if version != SCHEMA_VERSION:
            raise ValueError(
                f"snapshot is schema v{version}, this build expects v{SCHEMA_VERSION}; "
                f"re-run the discovery stage"
            )
        snapshot = cls(
            snapshot_id=str(raw["snapshot_id"]),
            run_date=date.fromisoformat(raw["run_date"]),
            market_as_of=date.fromisoformat(raw["market_as_of"]),
            observed_at=datetime.fromisoformat(raw["observed_at"]),
            name=str(raw.get("name", PRIMARY)),
            session=str(raw.get("session", "session state unavailable")),
            universe_version=str(raw.get("universe_version", "unknown")),
            prices={s: Observation.from_dict(o) for s, o in (raw.get("prices") or {}).items()},
            data_quality=DataQuality.from_dict(raw.get("data_quality") or {}),
            violations=list(raw.get("violations") or []),
            news={
                s: [NewsItem.from_dict(n) for n in items]
                for s, items in (raw.get("news") or {}).items()
            },
        )
        window = raw.get("news_window")
        if window:
            snapshot.news_window = (date.fromisoformat(window[0]), date.fromisoformat(window[1]))
        bars_dir = root / BARS_DIRNAME
        if bars_dir.is_dir():
            for csv in sorted(bars_dir.glob("*.csv")):
                try:
                    snapshot.bars[csv.stem.upper()] = pd.read_csv(
                        csv, index_col=0, parse_dates=True
                    )
                except (ValueError, pd.errors.ParserError) as exc:
                    log.warning("Unreadable snapshot bars %s: %s", csv, exc)
        return snapshot


def _mirror(bucket: str | None, path: Path, text: str) -> None:
    """Copy one snapshot file to GCS, if there is a bucket to copy it to."""
    if not bucket:
        return
    from .storage import blob_name, upload_text  # local: cloud-only dependency

    upload_text(bucket, blob_name(path), text, content_type="text/plain")


def _last_session(frame: pd.DataFrame | None) -> date | None:
    if frame is None or len(frame.index) == 0:
        return None
    try:
        return pd.Timestamp(frame.index[-1]).date()
    except (TypeError, ValueError):
        return None


def _last_close(frame: pd.DataFrame | None) -> float | None:
    if frame is None or "Close" not in getattr(frame, "columns", []):
        return None
    try:
        value = float(frame["Close"].iloc[-1])
    except (IndexError, TypeError, ValueError):
        return None
    return None if value != value or value <= 0 else value  # NaN or nonsense
