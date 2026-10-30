"""Fault injection at each durability phase, and honest startup reconciliation."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from filekeeper.core import operations
from filekeeper.core.errors import StorageError
from filekeeper.core.history.journal import OperationJournal
from filekeeper.core.service import Filekeeper


def _reopen(app: Filekeeper):
    reopened = Filekeeper(home=app.home)
    return reopened.workspace(name="project")


def _operation_rows(service) -> list[tuple[str, str]]:
    connection = sqlite3.connect(service.history.database_path)
    try:
        return [
            (str(row[0]), str(row[1]))
            for row in connection.execute("SELECT id, status FROM operations ORDER BY rowid")
        ]
    finally:
        connection.close()


def test_crash_after_blob_staging_before_the_pending_record(
    service, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = (service.root / "notes.txt").read_bytes()

    def crashing_begin(self, **kwargs):
        for item in kwargs["items"]:
            self._stage(item.before_bytes, item.before_hash, [], item.relative_path)
            self._stage(item.after_bytes, item.after_hash, [], item.relative_path)
        raise RuntimeError("simulated crash after blob staging")

    monkeypatch.setattr(OperationJournal, "begin", crashing_begin)

    with pytest.raises(RuntimeError):
        service.write_file("notes.txt", "never applied\n")

    assert (service.root / "notes.txt").read_bytes() == original
    assert service.history.database_path.is_file()
    assert _operation_rows(service) == []


def test_crash_after_the_pending_record_before_the_replace(
    service, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = (service.root / "notes.txt").read_bytes()
    original_begin = OperationJournal.begin

    def crashing_begin(self, **kwargs):
        original_begin(self, **kwargs)
        raise RuntimeError("simulated crash after the pending record")

    monkeypatch.setattr(OperationJournal, "begin", crashing_begin)

    with pytest.raises(RuntimeError):
        service.write_file("notes.txt", "never applied\n")

    assert (service.root / "notes.txt").read_bytes() == original

    reopened = _reopen(service.owner)
    rows = _operation_rows(reopened)
    assert len(rows) == 1
    assert rows[0][1] == "failed"
    record = reopened.get_operation(rows[0][0])
    assert record.metadata["reconciled"] == "failed"


def test_crash_after_the_atomic_replace_before_the_commit_mark(
    service, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = (service.root / "notes.txt").read_bytes()

    def crashing_commit(self, operation_id: str) -> None:
        raise RuntimeError("simulated crash after the replace")

    monkeypatch.setattr(OperationJournal, "commit", crashing_commit)

    with pytest.raises(RuntimeError):
        service.write_file("notes.txt", "applied\n")

    assert (service.root / "notes.txt").read_text(encoding="utf-8") == "applied\n"
    monkeypatch.undo()

    reopened = _reopen(service.owner)
    rows = _operation_rows(reopened)
    assert len(rows) == 1
    operation_id = rows[0][0]
    assert rows[0][1] == "committed"
    assert reopened.get_operation(operation_id).metadata["reconciled"] == "committed"

    reopened.undo(operation_id)
    assert (service.root / "notes.txt").read_bytes() == original


def test_failure_during_the_second_file_of_a_multi_file_delete(
    service, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree = service.root / "tree"
    tree.mkdir()
    for name in ("first.txt", "second.txt", "third.txt"):
        (tree / name).write_text(f"{name} content\n", encoding="utf-8")

    calls = {"count": 0}

    def failing_remove(path: Path) -> None:
        calls["count"] += 1
        if calls["count"] == 2:
            raise OSError("injected failure on the second file")
        path.unlink()

    monkeypatch.setattr(operations.directory, "_remove_file", failing_remove)

    with pytest.raises(StorageError):
        service.delete_directory("tree", recursive=True, confirm_recursive=True)

    remaining = sorted(path.name for path in tree.glob("*.txt"))
    assert len(remaining) == 2

    reopened = _reopen(service.owner)
    rows = _operation_rows(reopened)
    assert len(rows) == 1
    assert rows[0][1] == "failed"

    reopened.undo(rows[0][0])
    for name in ("first.txt", "second.txt", "third.txt"):
        assert (tree / name).read_text(encoding="utf-8") == f"{name} content\n"


def test_the_pending_record_is_written_with_full_synchronous_mode(service) -> None:
    service.create_file("sync.txt", "x\n")
    connection = sqlite3.connect(service.history.database_path)
    try:
        synchronous = connection.execute("PRAGMA synchronous").fetchone()[0]
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        connection.close()

    assert synchronous == 2  # FULL
    assert journal_mode.lower() == "wal"
