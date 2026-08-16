"""GCS mirroring for reports and the journal.

Cloud Run Jobs containers are stateless: the filesystem a run writes to is gone
when the task exits. For the reports that is merely inconvenient — they were
emailed. For the journal it is fatal to the whole point of the project, because
the journal *is* the benchmark (BUILD_PLAN.md), and the source-accuracy tracker
scores signals by comparing what they said against what the journal recorded
weeks earlier. A journal that resets nightly makes every source look untested
forever.

So the journal round-trips: restored from GCS at startup, mirrored back after
the stages have appended to it. Reports are mirrored one file at a time as they
are written, by :mod:`tradingagent.report.writer`.

Every operation here is best-effort and returns rather than raises. A cloud
outage must not lose a local report or abort a run that has already spent its
tokens — it degrades to "this run's history did not sync", which the caller
reports as a DEGRADED line.

Layout in the bucket mirrors the repo, so ``gsutil rsync`` in either direction
does the obvious thing::

    gs://<bucket>/reports/<date>/daily-brief.md
    gs://<bucket>/reports/<date>/deep/<TICKER>.md
    gs://<bucket>/journal/journal.jsonl
"""

from __future__ import annotations

import logging
from pathlib import Path

from .config import REPO_ROOT

log = logging.getLogger(__name__)

JOURNAL_BLOB = "journal/journal.jsonl"


def normalize_bucket(bucket: str) -> str:
    """Accept ``gs://name``, ``gs://name/``, or bare ``name``.

    deploy/setup.sh exports ``REPORTS_BUCKET=gs://...`` because that is what
    every other gcloud command wants, and the storage client wants the bare
    name. Tolerating both is cheaper than a rule nobody remembers.
    """
    return bucket.removeprefix("gs://").strip("/")


def blob_name(local_path: Path) -> str:
    """Path inside the bucket, mirroring the path inside the repo."""
    path = Path(local_path).resolve()
    try:
        return path.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        # Outside the repo (a temp dir in tests, say) — fall back to the leaf so
        # the object still lands somewhere predictable.
        return path.name


def _bucket_handle(bucket: str):
    """Return a GCS bucket handle, or None when the cloud is unreachable."""
    try:
        from google.cloud import storage  # optional dependency, cloud runs only

        return storage.Client().bucket(normalize_bucket(bucket))
    except Exception as exc:  # noqa: BLE001 - any failure here means "no cloud"
        log.warning("GCS unavailable (%s); staying local-only.", exc)
        return None


def upload_text(bucket: str, name: str, text: str, content_type: str = "text/plain") -> bool:
    handle = _bucket_handle(bucket)
    if handle is None:
        return False
    try:
        handle.blob(name).upload_from_string(text, content_type=content_type)
        log.info("Uploaded gs://%s/%s", normalize_bucket(bucket), name)
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("GCS upload of %s failed (%s); local copy retained.", name, exc)
        return False


def download_text(bucket: str, name: str) -> str | None:
    """Fetch an object's text, or None when it is absent or unreachable."""
    handle = _bucket_handle(bucket)
    if handle is None:
        return None
    try:
        blob = handle.blob(name)
        if not blob.exists():
            return None
        return blob.download_as_text()
    except Exception as exc:  # noqa: BLE001
        log.warning("GCS download of %s failed (%s).", name, exc)
        return None


# --- journal round trip --------------------------------------------------------


def _merge_lines(remote: str, local: str) -> list[str]:
    """Remote history first, then local lines it does not already contain.

    The daily job is a single writer, so this is normally just "remote, plus
    what this run appended". The dedupe matters when a run is retried after
    writing but before mirroring: replaying it must not double-count a
    recommendation, because the accuracy tracker weights sources by how often
    they were right and duplicates would silently inflate that.

    Identical lines really are duplicates rather than distinct events: an entry
    is keyed by date, ticker and stage (journal.py), so two byte-identical rows
    mean the same verdict was recorded twice.
    """
    merged: list[str] = []
    seen: set[str] = set()
    for line in (*remote.splitlines(), *local.splitlines()):
        if line.strip() and line not in seen:
            merged.append(line)
            seen.add(line)
    return merged


def restore_journal(bucket: str, journal_path: Path) -> int:
    """Seed the local journal from GCS at startup. Returns lines restored.

    Merges rather than overwrites: a container that somehow starts with a local
    journal keeps those entries, since losing a recommendation is worse than
    carrying a duplicate.
    """
    if not bucket:
        return 0
    remote = download_text(bucket, JOURNAL_BLOB)
    if remote is None:
        log.info("No journal in gs://%s yet; starting fresh.", normalize_bucket(bucket))
        return 0

    journal_path = Path(journal_path)
    local = journal_path.read_text(encoding="utf-8") if journal_path.exists() else ""
    merged = _merge_lines(remote, local)

    journal_path.parent.mkdir(parents=True, exist_ok=True)
    journal_path.write_text("\n".join(merged) + "\n" if merged else "", encoding="utf-8")
    log.info("Restored %d journal entries from GCS.", len(merged))
    return len(merged)


def mirror_journal(bucket: str, journal_path: Path) -> bool:
    """Push the journal back to GCS after the stages have appended to it."""
    journal_path = Path(journal_path)
    if not bucket or not journal_path.exists():
        return False
    # Re-read the remote copy first: if another execution appended while this
    # one was running, last-writer-wins would silently drop its entries.
    remote = download_text(bucket, JOURNAL_BLOB) or ""
    merged = _merge_lines(remote, journal_path.read_text(encoding="utf-8"))
    body = "\n".join(merged) + "\n" if merged else ""
    return upload_text(bucket, JOURNAL_BLOB, body, content_type="application/x-ndjson")


# --- experiment ledger round trip ----------------------------------------------
#
# Same merge-and-dedupe as the journal, four times over, for the same reason and
# one more: the outcomes job resolves horizons weeks after the decision was
# written, so the ledger it reads has to contain every prior day's rows. A
# ledger that reset nightly would leave every decision permanently unresolved —
# the M6 failure mode (a journal that resets makes every source look untested)
# repeated one level up.
#
# Ordering matters within a stream and not across them: rows are appended in
# time order and `latest()` is last-write-wins, so remote-then-local keeps a
# replay's correction after the row it corrects.


def ledger_blob(stream: str) -> str:
    from .evaluation.ledger import LEDGER_DIRNAME

    return f"journal/{LEDGER_DIRNAME}/{stream}"


def restore_ledger(bucket: str, ledger_root: Path) -> dict[str, int]:
    """Seed every ledger stream from GCS. Returns ``{stream: rows restored}``."""
    from .evaluation.ledger import STREAMS

    restored: dict[str, int] = {}
    if not bucket:
        return restored
    ledger_root = Path(ledger_root)
    for stream in STREAMS:
        remote = download_text(bucket, ledger_blob(stream))
        if remote is None:
            continue
        path = ledger_root / stream
        local = path.read_text(encoding="utf-8") if path.exists() else ""
        merged = _merge_lines(remote, local)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(merged) + "\n" if merged else "", encoding="utf-8")
        restored[stream] = len(merged)
    if restored:
        log.info("Restored ledger from GCS: %s", restored)
    return restored


def mirror_ledger(bucket: str, ledger_root: Path) -> dict[str, bool]:
    """Push every ledger stream back to GCS. Returns ``{stream: uploaded}``."""
    from .evaluation.ledger import STREAMS

    pushed: dict[str, bool] = {}
    ledger_root = Path(ledger_root)
    if not bucket:
        return pushed
    for stream in STREAMS:
        path = ledger_root / stream
        if not path.exists():
            continue
        remote = download_text(bucket, ledger_blob(stream)) or ""
        merged = _merge_lines(remote, path.read_text(encoding="utf-8"))
        body = "\n".join(merged) + "\n" if merged else ""
        pushed[stream] = upload_text(
            bucket, ledger_blob(stream), body, content_type="application/x-ndjson"
        )
    return pushed
