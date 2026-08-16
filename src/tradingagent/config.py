"""Runtime configuration: env loading, guardrail assertions, user preferences.

Provider-agnostic by construction — nothing here names an LLM vendor. Model
identifiers are opaque strings pulled from the environment and handed to
LiteLLM (see :mod:`tradingagent.llm`).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "config"

#: The three cost tiers. Declared here rather than in :mod:`tradingagent.llm`
#: because config has to validate ``PM_TIER`` before anything imports LiteLLM.
TIERS: tuple[str, ...] = ("fast", "smart", "deep")


class ConfigError(RuntimeError):
    """Raised when the environment is unusable or violates a guardrail."""


def _clean(value: str | None) -> str:
    """Strip whitespace and a trailing ` # comment` from a raw env value.

    ``config/.env.example`` documents values with inline comments and some
    dotenv versions keep them; be tolerant rather than silently mis-parsing
    ``ALPACA_PAPER=true    # guardrail``.
    """
    if value is None:
        return ""
    # `^#` matters too: dotenv strips leading whitespace, so a documented-but-
    # empty `KEY=   # comment` arrives here as a bare comment string.
    return re.sub(r"(?:^|\s+)#.*$", "", value).strip()


def env(key: str, default: str = "") -> str:
    raw = _clean(os.environ.get(key))
    return raw or default


def env_int(key: str, default: int) -> int:
    raw = env(key)
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def env_float(key: str, default: float) -> float:
    raw = env(key)
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


def env_bool(key: str, default: bool = False) -> bool:
    raw = env(key).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Preferences:
    """Parsed from ``config/preferences.md`` — the human's standing instructions."""

    target_sectors: list[str] = field(default_factory=lambda: ["Technology"])
    min_market_cap: float = 2e9
    min_avg_volume: float = 1e6
    shortlist_min: int = 5
    shortlist_max: int = 10
    deep_cap: int = 5
    raw_markdown: str = ""


_SECTION_RE = re.compile(r"^##\s+(.*)$", re.MULTILINE)


def parse_preferences(markdown: str) -> Preferences:
    """Extract the machine-usable knobs from preferences.md.

    Unparseable or missing fields fall back to the dataclass defaults so a
    hand-edited preferences file can never crash the daily run.
    """
    sections: dict[str, str] = {}
    matches = list(_SECTION_RE.finditer(markdown))
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(markdown)
        sections[m.group(1).strip().lower()] = markdown[m.end() : end]

    defaults = Preferences()

    sectors = [
        line.group(1).strip()
        for line in re.finditer(r"^\s*\d+\.\s+(.+)$", sections.get("target sectors (priority order)", ""), re.MULTILINE)
    ]

    universe = sections.get("universe", "")
    cap = _parse_money(re.search(r"market cap\s*>\s*\$?([\d.]+\s*[BMT]?)", universe, re.I))
    vol = _parse_money(re.search(r"volume\s*>\s*([\d.]+\s*[BMKT]?)", universe, re.I))

    risk = sections.get("risk profile", "")
    shortlist = re.search(r"[Ss]hortlist size:\s*(\d+)\s*[-–]\s*(\d+)", risk)
    deep = re.search(r"deep-analysis cap:\s*(\d+)", risk)

    return Preferences(
        target_sectors=sectors or defaults.target_sectors,
        min_market_cap=cap or defaults.min_market_cap,
        min_avg_volume=vol or defaults.min_avg_volume,
        shortlist_min=int(shortlist.group(1)) if shortlist else defaults.shortlist_min,
        shortlist_max=int(shortlist.group(2)) if shortlist else defaults.shortlist_max,
        deep_cap=int(deep.group(1)) if deep else defaults.deep_cap,
        raw_markdown=markdown,
    )


_SUFFIX = {"K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}


def _parse_money(match: re.Match[str] | None) -> float | None:
    if not match:
        return None
    token = match.group(1).replace(" ", "").upper()
    mult = _SUFFIX.get(token[-1:], 1.0)
    number = token[:-1] if token[-1:] in _SUFFIX else token
    try:
        return float(number) * mult
    except ValueError:
        return None


@dataclass(frozen=True)
class Settings:
    # --- LLM (opaque, provider-agnostic model strings) ---
    fast_model: str
    smart_model: str
    deep_model: str
    vertex_project: str
    vertex_location: str
    llm_max_retries: int
    llm_timeout: int
    # --- Data ---
    alpaca_key: str
    alpaca_secret: str
    alpaca_paper: bool
    finnhub_key: str
    # Signal layer (M3). All optional: each source skips itself with a visible
    # reason when its key is absent, so a partial configuration degrades the
    # report rather than failing the run.
    fred_key: str
    sec_user_agent: str
    # --- Output ---
    reports_bucket: str
    deep_ticker_cap: int
    debate_rounds: int
    preferences: Preferences
    run_date: date
    # Discount rate for the M4 option maths. A constant, not a fetched series:
    # at 21-45 DTE a 100bp error in r moves a 0.25-delta put's fair value by
    # well under a cent, so the FRED dependency would buy precision the decision
    # cannot use. Override with RISK_FREE_RATE if the curve moves materially.
    risk_free_rate: float = 0.045
    #: Which tier writes the portfolio manager's verdict. An A/B knob, not a
    #: tuning one: the milestone question is whether the DEEP tier's verdict is
    #: measurably better than the SMART tier's, and that can only be answered by
    #: running some days on each and grading them apart. Every ledger record
    #: names the tier that produced it, so the comparison stays computable.
    pm_tier: str = "deep"

    @property
    def reports_dir(self) -> Path:
        return REPO_ROOT / "reports"

    @property
    def journal_path(self) -> Path:
        return REPO_ROOT / "journal" / "journal.jsonl"

    def report_dir(self) -> Path:
        return self.reports_dir / self.run_date.isoformat()


def load_settings(run_date: date | None = None, env_file: Path | None = None) -> Settings:
    """Load .env, assert guardrails, and return the immutable run settings."""
    dotenv_path = env_file or (CONFIG_DIR / ".env")
    if dotenv_path.exists():
        load_dotenv(dotenv_path, override=False)

    prefs_path = CONFIG_DIR / "preferences.md"
    prefs = parse_preferences(prefs_path.read_text()) if prefs_path.exists() else Preferences()

    pm_tier = env("PM_TIER", "deep").lower()
    if pm_tier not in TIERS:
        raise ConfigError(
            f"PM_TIER must be one of {', '.join(TIERS)}; got {pm_tier!r}. The portfolio "
            f"manager's tier is an A/B variable and an unrecognised value would silently "
            f"fall back, making the two arms indistinguishable in the ledger."
        )

    paper = env_bool("ALPACA_PAPER", default=True)
    if not paper:
        # Hard guardrail from CLAUDE.md: this tool never touches a live account.
        raise ConfigError(
            "ALPACA_PAPER must be true. This is a research tool; live trading paths are refused."
        )

    settings = Settings(
        fast_model=env("LLM_FAST_MODEL"),
        smart_model=env("LLM_SMART_MODEL"),
        deep_model=env("LLM_DEEP_MODEL") or env("LLM_SMART_MODEL"),
        vertex_project=env("VERTEXAI_PROJECT"),
        vertex_location=env("VERTEXAI_LOCATION", "global"),
        llm_max_retries=env_int("LLM_MAX_RETRIES", 4),
        llm_timeout=env_int("LLM_TIMEOUT_SECONDS", 120),
        alpaca_key=env("ALPACA_API_KEY"),
        alpaca_secret=env("ALPACA_SECRET_KEY"),
        alpaca_paper=paper,
        finnhub_key=env("FINNHUB_API_KEY"),
        fred_key=env("FRED_API_KEY"),
        sec_user_agent=env("SEC_USER_AGENT"),
        reports_bucket=env("REPORTS_BUCKET"),
        deep_ticker_cap=env_int("DEEP_TICKER_CAP", prefs.deep_cap),
        debate_rounds=min(env_int("DEBATE_ROUNDS", 1), 2),
        preferences=prefs,
        run_date=run_date or date.today(),
        risk_free_rate=env_float("RISK_FREE_RATE", 0.045),
        pm_tier=pm_tier,
    )

    # Export the Vertex settings LiteLLM reads from the process environment.
    if settings.vertex_project:
        os.environ.setdefault("VERTEXAI_PROJECT", settings.vertex_project)
    if settings.vertex_location:
        os.environ.setdefault("VERTEXAI_LOCATION", settings.vertex_location)
    return settings


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()
