"""GCS mirroring: blob naming, the journal round trip, and failing gracefully."""

from __future__ import annotations

from pathlib import Path

import pytest

from tradingagent import storage as S
from tradingagent.config import REPO_ROOT


class FakeBlob:
    def __init__(self, store: dict, name: str):
        self._store = store
        self.name = name
        self.content_type = ""

    def exists(self) -> bool:
        return self.name in self._store

    def upload_from_string(self, data: str, content_type: str = "") -> None:
        self._store[self.name] = data
        self.content_type = content_type

    def download_as_text(self) -> str:
        return self._store[self.name]


class FakeBucket:
    """Enough of ``google.cloud.storage.Bucket`` for the three calls we make."""

    def __init__(self, store: dict | None = None):
        self.store: dict[str, str] = store if store is not None else {}
        self.content_types: dict[str, str] = {}

    def blob(self, name: str) -> FakeBlob:
        return FakeBlob(self.store, name)


class BrokenBucket(FakeBucket):
    def blob(self, name: str) -> FakeBlob:
        raise RuntimeError("503 backend error")


@pytest.fixture
def bucket(monkeypatch) -> FakeBucket:
    handle = FakeBucket()
    monkeypatch.setattr(S, "_bucket_handle", lambda _bucket: handle)
    return handle


# --- naming -------------------------------------------------------------------


@pytest.mark.parametrize(
    "given",
    ["gs://daybreak-reports", "gs://daybreak-reports/", "daybreak-reports", "daybreak-reports/"],
)
def test_bucket_names_normalize_whether_or_not_they_carry_the_scheme(given):
    assert S.normalize_bucket(given) == "daybreak-reports"


def test_blob_names_mirror_the_path_inside_the_repo():
    # The bug this guards: naming by the trailing path components got the brief
    # right but silently dropped `reports/` from deep reports and prefixed the
    # journal with whatever the checkout directory happened to be called.
    assert (
        S.blob_name(REPO_ROOT / "reports" / "2026-08-14" / "daily-brief.md")
        == "reports/2026-08-14/daily-brief.md"
    )
    assert (
        S.blob_name(REPO_ROOT / "reports" / "2026-08-14" / "deep" / "NVDA.md")
        == "reports/2026-08-14/deep/NVDA.md"
    )
    assert S.blob_name(REPO_ROOT / "journal" / "journal.jsonl") == S.JOURNAL_BLOB


def test_a_path_outside_the_repo_falls_back_to_its_leaf(tmp_path):
    assert S.blob_name(tmp_path / "daily-brief.md") == "daily-brief.md"


# --- transfer -----------------------------------------------------------------


def test_upload_and_download_round_trip(bucket):
    assert S.upload_text("gs://b", "reports/x.md", "hello", content_type="text/markdown")
    assert bucket.store["reports/x.md"] == "hello"
    assert S.download_text("gs://b", "reports/x.md") == "hello"


def test_downloading_an_absent_object_is_none_not_an_error(bucket):
    assert S.download_text("gs://b", "reports/nope.md") is None


def test_an_unreachable_bucket_degrades_instead_of_raising(monkeypatch):
    monkeypatch.setattr(S, "_bucket_handle", lambda _bucket: None)
    assert S.upload_text("gs://b", "x", "hello") is False
    assert S.download_text("gs://b", "x") is None


def test_a_failing_transfer_degrades_instead_of_raising(monkeypatch):
    monkeypatch.setattr(S, "_bucket_handle", lambda _bucket: BrokenBucket())
    assert S.upload_text("gs://b", "x", "hello") is False
    assert S.download_text("gs://b", "x") is None


def test_missing_google_cloud_library_is_just_no_cloud(monkeypatch):
    # Local runs do not install google-cloud-storage; the import failure inside
    # _bucket_handle must read as "stay local", not as a crash.
    import builtins

    real_import = builtins.__import__

    def refuse(name, *args, **kwargs):
        if name.startswith("google"):
            raise ImportError("No module named 'google'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", refuse)
    assert S._bucket_handle("gs://b") is None


# --- journal round trip --------------------------------------------------------


def test_merge_keeps_remote_history_first_then_new_local_lines():
    assert S._merge_lines("a\nb\n", "b\nc\n") == ["a", "b", "c"]


def test_merge_drops_blank_lines_and_duplicates():
    # A retried run replays lines it already mirrored. Double-counting them
    # would inflate the accuracy tracker's hit rate for whichever source
    # produced them.
    assert S._merge_lines("a\n\na\n", "a\n\n") == ["a"]


def test_restore_writes_remote_history_onto_an_empty_container(bucket, tmp_path):
    bucket.store[S.JOURNAL_BLOB] = '{"d":1}\n{"d":2}\n'
    journal = tmp_path / "journal" / "journal.jsonl"

    assert S.restore_journal("gs://b", journal) == 2
    assert journal.read_text().splitlines() == ['{"d":1}', '{"d":2}']


def test_restore_merges_rather_than_clobbering_a_local_journal(bucket, tmp_path):
    bucket.store[S.JOURNAL_BLOB] = '{"d":1}\n'
    journal = tmp_path / "journal.jsonl"
    journal.write_text('{"d":2}\n')

    assert S.restore_journal("gs://b", journal) == 2
    assert journal.read_text().splitlines() == ['{"d":1}', '{"d":2}']


def test_restore_with_no_remote_journal_leaves_the_local_one_alone(bucket, tmp_path):
    journal = tmp_path / "journal.jsonl"
    journal.write_text('{"d":1}\n')

    assert S.restore_journal("gs://b", journal) == 0
    assert journal.read_text() == '{"d":1}\n'


def test_restore_is_a_no_op_without_a_bucket(tmp_path):
    assert S.restore_journal("", tmp_path / "journal.jsonl") == 0


def test_mirror_uploads_the_local_journal(bucket, tmp_path):
    journal = tmp_path / "journal.jsonl"
    journal.write_text('{"d":1}\n')

    assert S.mirror_journal("gs://b", journal) is True
    assert bucket.store[S.JOURNAL_BLOB] == '{"d":1}\n'


def test_mirror_re_reads_the_remote_so_a_concurrent_run_is_not_overwritten(bucket, tmp_path):
    # Two executions of the same job (a retry, or a manual run beside the
    # schedule) each hold a partial journal. Last-writer-wins would drop one.
    bucket.store[S.JOURNAL_BLOB] = '{"d":"other"}\n'
    journal = tmp_path / "journal.jsonl"
    journal.write_text('{"d":"mine"}\n')

    assert S.mirror_journal("gs://b", journal) is True
    assert bucket.store[S.JOURNAL_BLOB] == '{"d":"other"}\n{"d":"mine"}\n'


def test_mirror_is_a_no_op_without_a_bucket_or_a_journal(bucket, tmp_path):
    journal = tmp_path / "journal.jsonl"
    journal.write_text("{}\n")
    assert S.mirror_journal("", journal) is False
    assert S.mirror_journal("gs://b", tmp_path / "missing.jsonl") is False
    assert bucket.store == {}


def test_the_journal_survives_a_full_stateless_cycle(bucket, tmp_path):
    """Day one writes, day two starts with an empty disk and still sees day one."""
    day_one = tmp_path / "run1" / "journal.jsonl"
    day_one.parent.mkdir()
    day_one.write_text('{"day":1}\n')
    S.mirror_journal("gs://b", day_one)

    day_two = tmp_path / "run2" / "journal.jsonl"
    assert S.restore_journal("gs://b", day_two) == 1
    with day_two.open("a") as fh:
        fh.write('{"day":2}\n')
    S.mirror_journal("gs://b", day_two)

    day_three = tmp_path / "run3" / "journal.jsonl"
    assert S.restore_journal("gs://b", day_three) == 2
    assert day_three.read_text().splitlines() == ['{"day":1}', '{"day":2}']


# --- experiment ledger round trip ----------------------------------------------


def test_the_ledger_survives_a_full_stateless_cycle(bucket, tmp_path):
    """A decision written today must still be resolvable in sixty days.

    The outcomes job reads decisions written weeks earlier. A ledger that reset
    with the container would leave every one of them permanently unresolved.
    """
    day_one = tmp_path / "run1" / "ledger"
    day_one.mkdir(parents=True)
    (day_one / "decisions.jsonl").write_text('{"decision_id":"2026-08-16:V:deep"}\n')
    (day_one / "candidates.jsonl").write_text('{"candidate_id":"run-a:V"}\n')
    assert S.mirror_ledger("gs://b", day_one) == {
        "candidates.jsonl": True,
        "decisions.jsonl": True,
    }

    day_two = tmp_path / "run2" / "ledger"
    restored = S.restore_ledger("gs://b", day_two)
    assert restored == {"candidates.jsonl": 1, "decisions.jsonl": 1}
    with (day_two / "decisions.jsonl").open("a") as fh:
        fh.write('{"decision_id":"2026-08-17:V:deep"}\n')
    S.mirror_ledger("gs://b", day_two)

    day_three = tmp_path / "run3" / "ledger"
    assert S.restore_ledger("gs://b", day_three)["decisions.jsonl"] == 2


def test_ledger_blobs_sit_under_the_journal_prefix():
    assert S.ledger_blob("decisions.jsonl") == "journal/ledger/decisions.jsonl"


def test_a_replayed_ledger_row_is_not_mirrored_twice(bucket, tmp_path):
    root = tmp_path / "ledger"
    root.mkdir()
    (root / "runs.jsonl").write_text('{"run":1}\n')
    S.mirror_ledger("gs://b", root)
    S.mirror_ledger("gs://b", root)
    assert bucket.store[S.ledger_blob("runs.jsonl")] == '{"run":1}\n'


def test_ledger_round_trip_is_a_no_op_without_a_bucket(tmp_path):
    assert S.restore_ledger("", tmp_path / "ledger") == {}
    assert S.mirror_ledger("", tmp_path / "ledger") == {}


def test_an_absent_ledger_stream_is_skipped_not_created(bucket, tmp_path):
    root = tmp_path / "ledger"
    root.mkdir()
    (root / "runs.jsonl").write_text('{"run":1}\n')
    S.mirror_ledger("gs://b", root)
    assert set(bucket.store) == {S.ledger_blob("runs.jsonl")}

    fresh = tmp_path / "fresh" / "ledger"
    assert set(S.restore_ledger("gs://b", fresh)) == {"runs.jsonl"}
    assert not (fresh / "outcomes.jsonl").exists()


def test_reports_are_uploaded_under_their_repo_relative_path(bucket):
    from tradingagent.report.writer import write_report

    path = REPO_ROOT / "reports" / "1999-01-01" / "deep" / "TEST.md"
    try:
        write_report(path, "# hi", bucket="gs://b")
        assert bucket.store["reports/1999-01-01/deep/TEST.md"] == "# hi"
        assert path.read_text() == "# hi"
    finally:
        path.unlink(missing_ok=True)
        for parent in (path.parent, path.parent.parent):
            if parent.exists() and not any(parent.iterdir()):
                parent.rmdir()


def test_a_gcs_outage_still_leaves_the_report_on_disk(monkeypatch, tmp_path):
    from tradingagent.report.writer import write_report

    monkeypatch.setattr(S, "_bucket_handle", lambda _bucket: None)
    path = tmp_path / "daily-brief.md"
    assert write_report(path, "# hi", bucket="gs://b") == path
    assert path.read_text() == "# hi"
