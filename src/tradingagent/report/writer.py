"""Persist reports locally, and to GCS when ``REPORTS_BUCKET`` is set."""

from __future__ import annotations

import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)


def replace_section(markdown: str, heading: str, body: str) -> str:
    """Swap one ``## n. Title`` section's body, leaving the rest untouched.

    The deep stage runs after the daily brief is already on disk, so it patches
    section 5 in place rather than re-deriving the whole brief. Returns the
    input unchanged when the heading is absent, so a schema change upstream
    degrades to "the brief still says pending" instead of a crash.
    """
    pattern = re.compile(
        rf"^{re.escape(heading)}\s*$.*?(?=^## |\Z)", re.MULTILINE | re.DOTALL
    )
    if not pattern.search(markdown):
        log.warning("Section %r not found; brief left unpatched.", heading)
        return markdown
    return pattern.sub(lambda _: f"{heading}\n\n{body.strip()}\n\n", markdown, count=1)


def write_report(path: Path, content: str, bucket: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    if bucket:
        upload_to_gcs(bucket, path, content)
    return path


def upload_to_gcs(bucket: str, local_path: Path, content: str) -> None:
    """Best-effort upload; a cloud failure must not lose the local report."""
    from ..storage import blob_name, upload_text

    upload_text(bucket, blob_name(local_path), content, content_type="text/markdown")


def mirror_json(bucket: str, path: Path) -> None:
    """Push a stage-handoff artefact to GCS.

    Only the presentation context uses this, and for a reason the other context
    files do not have: a Cloud Run container starts with an empty disk, so a
    re-delivery — ``--stage report`` after a bounced email — has nothing to
    rebuild the sheet from unless the artefact outlived the container that wrote
    it. Best effort; the local file is what this run's own delivery reads.
    """
    if not bucket:
        return
    try:
        from ..storage import blob_name, upload_text

        upload_text(
            bucket, blob_name(path), path.read_text(), content_type="application/json"
        )
    except Exception as exc:  # noqa: BLE001 - the local artefact is authoritative
        log.warning("Could not mirror %s to %s: %s", path.name, bucket, exc)
