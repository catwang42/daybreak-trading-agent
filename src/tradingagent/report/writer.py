"""Persist reports locally, and to GCS when ``REPORTS_BUCKET`` is set."""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)


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
