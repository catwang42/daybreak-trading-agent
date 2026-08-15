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
