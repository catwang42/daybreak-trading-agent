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
    try:
        from google.cloud import storage  # optional dependency, cloud runs only

        client = storage.Client()
        blob_name = "/".join(local_path.parts[-3:]) if len(local_path.parts) >= 3 else local_path.name
        client.bucket(bucket).blob(blob_name).upload_from_string(content, content_type="text/markdown")
        log.info("Uploaded report to gs://%s/%s", bucket, blob_name)
    except Exception as exc:  # noqa: BLE001
        log.warning("GCS upload to %s failed (%s); local copy retained at %s", bucket, exc, local_path)
