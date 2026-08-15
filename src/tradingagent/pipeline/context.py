"""The handoff between the discovery stage and the deep stage.

``--stage all`` passes a :class:`DeepContext` in memory. ``--stage deep`` run on
its own reads the JSON that discovery left in the report directory, so the deep
dive works from the same market picture and the same queue the brief published
instead of silently re-deriving a different one.

Only the *summaries* the prompts actually consume are persisted — a rendered
market-context block, the per-ticker screener metrics, and the quick take. The
deep stage then downloads bars for the handful of queued tickers rather than the
whole universe, which is what makes a standalone ``--stage deep`` cheap.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

CONTEXT_FILENAME = "discovery-context.json"
# v2 (M3) adds the signal layer. v3 (M6) names the research snapshot the queue
# was built from. Bumped rather than defaulted silently, both times: a v2
# context would parse cleanly with an empty snapshot id, and the deep stage
# would then go and download its own bars — which is the exact drift M6 exists
# to remove. Forcing a discovery re-run is the honest failure.
SCHEMA_VERSION = 3


@dataclass
class QueuedTicker:
    """One name the discovery stage nominated for a deep dive."""

    symbol: str
    name: str = ""
    sector: str = ""
    industry: str = ""
    priority: int = 0
    quick_rating: str = "n/a"
    quick_thesis: str = ""
    quick_risk: str = ""
    earnings_flag: str = "—"
    earnings_note: str = "no confirmed earnings in the next 10 days"
    sector_note: str = "sector data unavailable"
    news_headline: str | None = None
    screener: dict[str, Any] = field(default_factory=dict)
    #: Rendered per-ticker signal layer (M3), already markdown. Carried as text
    #: rather than as objects because this file is the stage boundary and a
    #: standalone `--stage deep` must not need the source clients to re-run.
    signal_block: str = ""
    #: source name -> -1/0/+1 as read at decision time. The journal records this
    #: so `signals.accuracy` can grade each source against the realised move.
    signal_readings: dict[str, int] = field(default_factory=dict)
    #: Points the signal layer actually added to this name's screener score.
    #: Zero while every source is shadowed (M6 item 1).
    signal_adjustment: float = 0.0
    #: What it would have added at full trust. Kept beside the applied figure
    #: so the deep stage inherits the same shadow/applied distinction the brief
    #: shows, rather than reading 0.0 and concluding the layer said nothing.
    signal_shadow_adjustment: float = 0.0

    def screener_markdown(self) -> str:
        if not self.screener:
            return "Screener detail unavailable for this ticker."
        s = self.screener
        return "\n".join(
            [
                f"- Screener score: {s.get('score', '?')}/100 "
                f"({s.get('rating', '?')}, {s.get('state', '?')})",
                f"- Triggers fired: {s.get('triggers') or 'none'}",
                f"- Day move: {_signed(s.get('day_gain_pct'))}%",
                f"- Volume vs 20-day average: {_plain(s.get('volume_ratio_20d'))}x",
                f"- Close location in the day's range: {_plain(s.get('close_location_pct'), 0)}%",
                f"- Prior base: {s.get('prior_base_days', '?')} days, "
                f"width {_plain(s.get('base_width_pct'), 1)}%",
                f"- Entry reference ${_plain(s.get('entry_ref'))} / "
                f"stop reference ${_plain(s.get('stop_ref'))} "
                f"(risk {_plain(s.get('risk_pct'), 1)}%)",
                f"- Distance from the 52-week high: {_signed(s.get('dist_52w_high_pct'), 1)}%",
                f"- 3-month relative strength vs SPY: {_signed(s.get('rs_vs_spy_3mo'), 1)} pp",
                f"- Soft flags raised by the screener: {s.get('reject_reasons') or 'none'}",
            ]
        )


@dataclass
class DeepContext:
    """Market picture plus queue, as handed from discovery to the deep stage."""

    run_date: str
    market_context: str = "Market context unavailable."
    macro_note: str = "none scheduled"
    data_as_of: str = "unknown"
    queue: list[QueuedTicker] = field(default_factory=list)
    discovery_degraded: list[str] = field(default_factory=list)
    #: The research snapshot discovery screened against. The deep stage reads
    #: bars from it rather than downloading its own, and refuses to reason from
    #: a different one — see :mod:`tradingagent.snapshot`.
    snapshot_id: str = ""
    #: The session those bars belong to, ISO. Repeated here so the deep stage
    #: can state its as-of even if the snapshot file is missing.
    market_as_of: str = ""
    version: int = SCHEMA_VERSION

    # -- persistence -----------------------------------------------------
    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=False)

    @classmethod
    def from_json(cls, text: str) -> DeepContext:
        raw = json.loads(text)
        version = int(raw.get("version", 0))
        if version != SCHEMA_VERSION:
            raise ValueError(
                f"discovery context is schema v{version}, this build expects "
                f"v{SCHEMA_VERSION}; re-run the discovery stage"
            )
        queue = [QueuedTicker(**item) for item in raw.get("queue", [])]
        return cls(**{**raw, "queue": queue})

    def write(self, directory: Path) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / CONTEXT_FILENAME
        path.write_text(self.to_json())
        return path

    @classmethod
    def read(cls, directory: Path) -> DeepContext:
        path = directory / CONTEXT_FILENAME
        if not path.exists():
            raise FileNotFoundError(
                f"No discovery context at {path}. Run `--stage discovery` for "
                f"this date first, or use `--stage all`."
            )
        return cls.from_json(path.read_text())

    @property
    def date(self) -> date:
        return date.fromisoformat(self.run_date)

    def limit(self, cap: int, only: list[str] | None = None) -> list[QueuedTicker]:
        """Apply ``DEEP_TICKER_CAP``, or an explicit ``--tickers`` override."""
        if only:
            wanted = [s.strip().upper() for s in only if s.strip()]
            by_symbol = {q.symbol: q for q in self.queue}
            # A ticker the human names but discovery never queued still gets
            # analysed — with an empty screener block the analysts can see.
            picked = [by_symbol.get(s) or QueuedTicker(symbol=s) for s in wanted]
            return picked[:cap]
        return self.queue[:cap]


def _plain(value: Any, digits: int = 2) -> str:
    try:
        return f"{float(value):,.{digits}f}"
    except (TypeError, ValueError):
        return "unavailable"


def _signed(value: Any, digits: int = 2) -> str:
    try:
        return f"{float(value):+,.{digits}f}"
    except (TypeError, ValueError):
        return "unavailable"
