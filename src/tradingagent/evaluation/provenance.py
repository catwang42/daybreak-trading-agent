"""What produced a record — the half of an experiment that makes it one.

A ledger row saying "we rated V Overweight on the 14th and it went up 3%" is
not evidence of anything. Evidence needs the other half: which code, which
prompts, which models, which universe, which market picture. Without it the
first genuinely interesting question — "the hit rate improved in September, why
— did we change something?" — has no answer, and every A/B in item 5 of the
milestone is uncomputable after the fact.

So every ledger record carries a :class:`Provenance`, and the four fields that
actually change behaviour are captured as fingerprints rather than as prose:

- ``git_commit`` — the code. Read from ``GIT_COMMIT`` first because the
  container has no git; falling back to ``git rev-parse`` covers local runs.
- ``config_hash`` — the knobs. A hash rather than a dump, because the settings
  object also holds API keys and a ledger is a file we mirror to a bucket. The
  hashed payload is built by allow-list (:data:`HASHED_SETTINGS`), so a secret
  added to ``Settings`` tomorrow cannot leak into it by default.
- ``prompt_versions`` — the prompts, one short hash each. The role prompts are
  the largest uncontrolled variable in the whole pipeline and they are edited
  more often than the code around them.
- ``universe_version`` / ``snapshot_id`` — the data.

``run_id`` is derived from the snapshot id rather than from the clock, so every
stage of one daily run files under the same id even when ``--stage deep`` is run
hours after discovery. That is the point: they are one experiment, and a run id
that changed per process would split each day's evidence in two.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from ..config import REPO_ROOT, Settings
from ..snapshot import ResearchSnapshot, utcnow

log = logging.getLogger(__name__)

PROMPTS_DIR = REPO_ROOT / "src" / "tradingagent" / "pipeline" / "prompts"

#: Length of every fingerprint in this module. Twelve hex characters is 48 bits
#: — far past collision range for a few thousand prompt edits, and short enough
#: to sit in a markdown table.
DIGEST_CHARS = 12

#: The settings that change what a run *decides*. Allow-list, not deny-list: the
#: hash is written to a file we mirror to GCS, and CLAUDE.md's rule is that
#: secrets never reach a log, a report or the bucket. A new `alpaca_secret`-
#: shaped field added to Settings next month is excluded by construction rather
#: than by somebody remembering.
HASHED_SETTINGS: tuple[str, ...] = (
    "fast_model",
    "smart_model",
    "deep_model",
    "pm_tier",
    "deep_ticker_cap",
    "debate_rounds",
    "risk_free_rate",
)

#: Preference fields that steer the screen. Same allow-list reasoning.
HASHED_PREFERENCES: tuple[str, ...] = (
    "target_sectors",
    "min_market_cap",
    "min_avg_volume",
    "shortlist_min",
    "shortlist_max",
    "deep_cap",
)

_SNAPSHOT_ID = re.compile(r"^snap-(\d{4}-\d{2}-\d{2})-\w+-(\d{6}Z)$")


def git_commit() -> str:
    """The commit this code came from, or ``"unknown"``.

    ``GIT_COMMIT`` wins because the image has neither a git binary nor a
    ``.git`` directory — it is baked in at build time (see the Dockerfile).
    """
    from ..config import env

    stamped = env("GIT_COMMIT")
    if stamped:
        return stamped[:40]
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover - env dependent
        log.debug("git rev-parse unavailable: %s", exc)
        return "unknown"
    commit = out.stdout.strip()
    return commit[:40] if out.returncode == 0 and commit else "unknown"


def digest(payload: Any) -> str:
    """A short, stable fingerprint of any JSON-able value."""
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:DIGEST_CHARS]


def config_payload(settings: Settings) -> dict[str, Any]:
    """The behaviour-changing settings, by allow-list. Never any credential."""
    payload: dict[str, Any] = {
        name: getattr(settings, name, None) for name in HASHED_SETTINGS
    }
    prefs = getattr(settings, "preferences", None)
    payload["preferences"] = {
        name: getattr(prefs, name, None) for name in HASHED_PREFERENCES
    }
    return payload


def config_hash(settings: Settings) -> str:
    return digest(config_payload(settings))


def prompt_versions(directory: Path | None = None) -> dict[str, str]:
    """``{prompt name: short hash}`` for every role prompt on disk.

    Keyed by stem so the map reads as a list of roles; a prompt that is deleted
    simply stops appearing, which is itself the change worth seeing.
    """
    root = Path(directory or PROMPTS_DIR)
    if not root.is_dir():
        return {}
    out: dict[str, str] = {}
    for path in sorted(root.glob("*.md")):
        try:
            out[path.stem] = hashlib.sha256(path.read_bytes()).hexdigest()[:DIGEST_CHARS]
        except OSError as exc:  # pragma: no cover - unreadable file
            log.warning("Cannot hash prompt %s: %s", path, exc)
    return out


def run_id_for(run_date: date, snapshot_id: str = "", observed: str = "") -> str:
    """``run-2026-08-16-025713Z`` — one id per daily run, not per process.

    Taken from the snapshot's timestamp when there is one, because the snapshot
    *is* the run: a standalone ``--stage deep`` reading this morning's snapshot
    belongs to this morning's experiment and must file under its id. A run with
    no snapshot (the outcomes job, a degraded discovery) falls back to the wall
    clock, which is honest — there is nothing else to tie it to.
    """
    match = _SNAPSHOT_ID.match(snapshot_id or "")
    if match:
        return f"run-{match.group(1)}-{match.group(2)}"
    stamp = observed or f"{utcnow():%H%M%S}Z"
    return f"run-{run_date.isoformat()}-{stamp}"


@dataclass(frozen=True)
class Provenance:
    """Everything a later reader needs to know what produced a record."""

    run_id: str
    run_date: str
    snapshot_id: str = ""
    market_as_of: str = ""
    git_commit: str = "unknown"
    config_hash: str = ""
    models: dict[str, str] = field(default_factory=dict)
    prompt_versions: dict[str, str] = field(default_factory=dict)
    universe_version: str = "unknown"
    #: True when the record was reconstructed from an older artefact rather
    #: than written by the run it describes. Backfilled rows are missing most
    #: of the fields above and must never be counted as evidence that the
    #: pipeline recorded them at the time.
    backfilled: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "run_date": self.run_date,
            "snapshot_id": self.snapshot_id,
            "market_as_of": self.market_as_of,
            "git_commit": self.git_commit,
            "config_hash": self.config_hash,
            "models": dict(self.models),
            "prompt_versions": dict(self.prompt_versions),
            "universe_version": self.universe_version,
            "backfilled": self.backfilled,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Provenance":
        return cls(
            run_id=str(raw.get("run_id", "")),
            run_date=str(raw.get("run_date", "")),
            snapshot_id=str(raw.get("snapshot_id", "")),
            market_as_of=str(raw.get("market_as_of", "")),
            git_commit=str(raw.get("git_commit", "unknown")),
            config_hash=str(raw.get("config_hash", "")),
            models=dict(raw.get("models") or {}),
            prompt_versions=dict(raw.get("prompt_versions") or {}),
            universe_version=str(raw.get("universe_version", "unknown")),
            backfilled=bool(raw.get("backfilled", False)),
        )


def build_provenance(
    settings: Settings,
    snapshot: ResearchSnapshot | None = None,
    *,
    backfilled: bool = False,
) -> Provenance:
    """Fingerprint this run. Cheap enough to call once per stage."""
    snapshot_id = snapshot.snapshot_id if snapshot else ""
    return Provenance(
        run_id=run_id_for(settings.run_date, snapshot_id),
        run_date=settings.run_date.isoformat(),
        snapshot_id=snapshot_id,
        market_as_of=snapshot.market_as_of.isoformat() if snapshot else "",
        git_commit=git_commit(),
        config_hash=config_hash(settings),
        models={
            "fast": settings.fast_model,
            "smart": settings.smart_model,
            "deep": settings.deep_model,
        },
        prompt_versions=prompt_versions(),
        universe_version=(
            snapshot.universe_version if snapshot else "unknown"
        ),
        backfilled=backfilled,
    )


def env_snapshot() -> dict[str, str]:
    """Non-secret environment facts worth recording beside a run.

    Deliberately tiny and allow-listed for the same reason as
    :data:`HASHED_SETTINGS`.
    """
    return {
        key: os.environ[key]
        for key in ("K_SERVICE", "CLOUD_RUN_JOB", "CLOUD_RUN_EXECUTION")
        if os.environ.get(key)
    }
