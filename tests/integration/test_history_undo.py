"""Durable history and safe undo, one case per mutation shape."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from filekeeper.core.config import Config
from filekeeper.core.errors import (
    HistoryUnavailableError,
    UndoConflictError,
)
from filekeeper.core.service import Filekeeper


def test_write_undo_restores_exact_original_bytes(service) -> None:
    original = (service.root / "notes.txt").read_bytes()

    result = service.write_file("notes.txt", "alpha\nbeta\ngamma\nchanged\n")
    assert result.operation_id is not None
    assert result.undo_available is True
    assert (service.root / "notes.txt").read_bytes() != original

    undone = service.undo(result.operation_id)

    assert (service.root / "notes.txt").read_bytes() == original
    assert undone.operation_id is not None
    assert undone.operation_id != result.operation_id
    assert service.get_operation(result.operation_id).status == "undone"


def test_undo_is_itself_recorded_in_the_ledger(service) -> None:
    result = service.write_file("notes.txt", "replacement\n")

    undone = service.undo(result.operation_id)
    record = service.get_operation(undone.operation_id or "")

    assert record.operation_type == "undo"
    assert record.metadata["undone_operation"] == result.operation_id
    assert record.status == "committed"


def test_create_undo_removes_the_created_file(service) -> None:
    result = service.create_file("created.txt", "fresh\n")
    assert (service.root / "created.txt").is_file()

    service.undo(result.operation_id)

    assert not (service.root / "created.txt").exists()


def test_delete_undo_restores_the_file_when_the_destination_is_absent(service) -> None:
    original = (service.root / "notes.txt").read_bytes()
    result = service.delete_file("notes.txt")
    assert not (service.root / "notes.txt").exists()

    service.undo(result.operation_id)

    assert (service.root / "notes.txt").read_bytes() == original


def test_move_undo_checks_both_ends_before_moving_back(service) -> None:
    original = (service.root / "notes.txt").read_bytes()
    result = service.move_file("notes.txt", "sub/moved.txt")
    assert not (service.root / "notes.txt").exists()

    service.undo(result.operation_id)

    assert (service.root / "notes.txt").read_bytes() == original
    assert not (service.root / "sub" / "moved.txt").exists()


def test_directory_delete_undo_restores_the_whole_tree(service) -> None:
    tree = service.root / "tree"
    tree.mkdir()
    (tree / "first.txt").write_text("first\n", encoding="utf-8")
    (tree / "nested").mkdir()
    (tree / "nested" / "second.txt").write_text("second\n", encoding="utf-8")

    result = service.delete_directory("tree", recursive=True, confirm_recursive=True)
    assert not tree.exists()

    service.undo(result.operation_id)

    assert (tree / "first.txt").read_text(encoding="utf-8") == "first\n"
    assert (tree / "nested" / "second.txt").read_text(encoding="utf-8") == "second\n"


def test_directory_move_undo_restores_the_original_location(service) -> None:
    result = service.move_directory("sub", "renamed")
    assert (service.root / "renamed" / "nested.md").is_file()

    service.undo(result.operation_id)

    assert (service.root / "sub" / "nested.md").is_file()
    assert not (service.root / "renamed").exists()


def test_undo_conflict_leaves_a_later_edit_untouched(service) -> None:
    result = service.write_file("notes.txt", "first change\n")
    (service.root / "notes.txt").write_text("later edit\n", encoding="utf-8")

    with pytest.raises(UndoConflictError) as raised:
        service.undo(result.operation_id)

    assert raised.value.code == "UNDO_CONFLICT"
    assert (service.root / "notes.txt").read_text(encoding="utf-8") == "later edit\n"
    assert service.get_operation(result.operation_id).status == "committed"


def test_undo_of_create_refuses_a_modified_file(service) -> None:
    result = service.create_file("created.txt", "original\n")
    (service.root / "created.txt").write_text("edited\n", encoding="utf-8")

    with pytest.raises(UndoConflictError):
        service.undo(result.operation_id)

    assert (service.root / "created.txt").read_text(encoding="utf-8") == "edited\n"


def test_undo_of_copy_refuses_when_the_destination_was_moved_away(service) -> None:
    original = (service.root / "notes.txt").read_bytes()
    copied = service.copy_file("notes.txt", "copied.txt")
    service.move_file("copied.txt", "copied-renamed.txt")

    with pytest.raises(UndoConflictError) as raised:
        service.undo(copied.operation_id)

    assert raised.value.code == "UNDO_CONFLICT"
    assert "copied.txt" in raised.value.message
    assert raised.value.details["conflicting_paths"] == "copied.txt"
    assert not (service.root / "copied.txt").exists()
    assert (service.root / "copied-renamed.txt").read_bytes() == original
    assert service.get_operation(copied.operation_id).status == "committed"


def test_undo_of_move_refuses_when_the_destination_was_moved_again(service) -> None:
    original = (service.root / "notes.txt").read_bytes()
    first = service.move_file("notes.txt", "moved.txt")
    service.move_file("moved.txt", "moved-again.txt")

    with pytest.raises(UndoConflictError) as raised:
        service.undo(first.operation_id)

    assert raised.value.code == "UNDO_CONFLICT"
    assert "moved.txt" in raised.value.message
    assert raised.value.details["conflicting_paths"] == "moved.txt"
    assert not (service.root / "notes.txt").exists()
    assert not (service.root / "moved.txt").exists()
    assert (service.root / "moved-again.txt").read_bytes() == original
    assert service.get_operation(first.operation_id).status == "committed"


def test_undo_of_directory_create_refuses_a_non_empty_directory(service) -> None:
    result = service.create_directory("fresh")
    (service.root / "fresh" / "keep.txt").write_text("keep\n", encoding="utf-8")

    with pytest.raises(UndoConflictError):
        service.undo(result.operation_id)

    assert (service.root / "fresh" / "keep.txt").is_file()


def test_repeated_undo_is_refused(service) -> None:
    result = service.write_file("notes.txt", "once\n")
    service.undo(result.operation_id)

    with pytest.raises(UndoConflictError):
        service.undo(result.operation_id)


def test_corrupt_blob_yields_a_clear_recovery_error(service) -> None:
    result = service.write_file("notes.txt", "new content\n")
    record = service.get_operation(result.operation_id or "")
    before_hash = record.items[0].before_blob_hash
    assert before_hash is not None

    journal = service.history
    journal.blobs.path_for(before_hash).write_bytes(b"tampered bytes")

    with pytest.raises(HistoryUnavailableError) as raised:
        service.undo(result.operation_id)

    assert "missing or corrupt" in raised.value.message
    assert (service.root / "notes.txt").read_text(encoding="utf-8") == "new content\n"


def test_history_skip_cap_is_enforced_and_reported(tmp_path: Path) -> None:
    app = Filekeeper(home=tmp_path / "home")
    root = tmp_path / "workspace"
    root.mkdir()
    app.create_workspace("capped", root)
    app.config = Config(max_history_blob_bytes=64)
    app.registry.config = app.config
    service = app.workspace(name="capped")

    created = service.create_file("big.txt", "x" * 200)
    result = service.write_file("big.txt", "y" * 200)
    created_record = service.get_operation(created.operation_id or "")
    assert created_record.history_skipped == ("big.txt",)

    assert result.operation_id is not None
    assert result.undo_available is False
    assert "big.txt" in result.history_skipped
    assert any("history was skipped" in warning for warning in result.warnings)
    stored = {row[0] for row in _blob_hashes(service)}
    record = service.get_operation(result.operation_id)
    assert record.items[0].before_blob_hash not in stored

    with pytest.raises(HistoryUnavailableError):
        service.undo(result.operation_id)


def test_unchanged_files_do_not_get_blobs(service) -> None:
    result = service.copy_file("notes.txt", "copy.txt")
    record = service.get_operation(result.operation_id or "")

    assert [item.relative_path for item in record.items] == ["copy.txt"]


def test_undo_last_is_scoped_to_the_selected_workspace(tmp_path: Path) -> None:
    app = Filekeeper(home=tmp_path / "home")
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    app.create_workspace("first", first)
    app.create_workspace("second", second)
    first_service = app.workspace(name="first")
    second_service = app.workspace(name="second")

    first_service.create_file("one.txt", "one\n")
    second_service.create_file("two.txt", "two\n")

    undone = second_service.undo_last()

    assert (second / "two.txt").exists() is False
    assert (first / "one.txt").is_file()
    assert undone.path == "two.txt"


def test_history_filters_by_path_actor_and_task(service) -> None:
    service.write_file("notes.txt", "one\n", actor="writer", task_id="task-7")
    service.create_file("other.txt", "two\n", actor="other")

    by_path = service.list_history(path="notes.txt")
    by_actor = service.list_history(actor="writer")
    by_task = service.list_history(task_id="task-7")

    assert len(by_path) == 1
    assert by_path[0].target_path == "notes.txt"
    assert len(by_actor) == 1
    assert by_actor[0].actor == "writer"
    assert len(by_task) == 1
    assert by_task[0].task_id == "task-7"


def test_diff_operation_reports_only_changed_lines(service) -> None:
    (service.root / "diff.txt").write_text("one\ntwo\n", encoding="utf-8")
    result = service.write_file("diff.txt", "one\nthree\n")

    diff = service.diff_operation(result.operation_id or "")
    text = "".join(entry.unified_diff for entry in diff.items)

    assert "-two" in text
    assert "+three" in text
    assert text.count("one") <= 4


def test_first_mutation_creates_the_state_directory_in_a_git_repository(
    tmp_path: Path,
) -> None:
    app = Filekeeper(home=tmp_path / "home")
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".git").mkdir()
    app.create_workspace("repo", root)
    service = app.workspace(name="repo")

    assert not (root / ".filekeeper").exists()
    service.create_file("readme.txt", "hello\n")

    assert (root / ".filekeeper").is_dir()
    assert (root / ".filekeeper" / "state.sqlite3").is_file()
    assert ".filekeeper/" in (root / ".gitignore").read_text(encoding="utf-8")


def _blob_hashes(service) -> list[tuple[str]]:
    connection = sqlite3.connect(service.history.database_path)
    try:
        return list(connection.execute("SELECT hash FROM blobs").fetchall())
    finally:
        connection.close()


def test_undo_record_does_not_claim_history_was_skipped(service) -> None:
    result = service.write_file("notes.txt", "replacement\n")

    undone = service.undo(result.operation_id)
    record = service.get_operation(undone.operation_id or "")

    assert record.operation_type == "undo"
    assert record.history_skipped == ()
    assert "history_skipped" not in record.metadata


def test_a_copy_journal_entry_names_its_source(service) -> None:
    """The durable record of a copy must say what it copied from."""

    result = service.copy_file("notes.txt", "copy.txt")
    record = service.get_operation(result.operation_id or "")

    assert record.operation_type == "copy_file"
    # target_path keeps its existing meaning: the path the operation targets.
    assert record.target_path == "copy.txt"
    assert record.metadata["source_path"] == "notes.txt"
    assert "notes.txt" in (record.summary or "")
    assert "copy.txt" in (record.summary or "")
    # Rule: caller-relative paths only, never a resolved host path.
    assert str(service.root) not in (record.summary or "")
    assert str(service.root) not in str(record.metadata)


def test_a_move_journal_entry_names_its_source_like_a_copy(service) -> None:
    """Copy and move carry the same structured source field."""

    result = service.move_file("notes.txt", "renamed.txt")
    record = service.get_operation(result.operation_id or "")

    assert record.operation_type == "move_file"
    assert record.target_path == "renamed.txt"
    assert record.metadata["source_path"] == "notes.txt"
    assert "notes.txt" in (record.summary or "")
    assert "renamed.txt" in (record.summary or "")
    assert str(service.root) not in (record.summary or "")
    assert str(service.root) not in str(record.metadata)


def test_a_refused_undo_leaves_a_record_that_is_not_undoable(service) -> None:
    """A refusal is journaled with its code and paths, but stays off the undo stack."""

    copied = service.copy_file("notes.txt", "copied.txt")
    moved = service.move_file("copied.txt", "copied-renamed.txt")

    with pytest.raises(UndoConflictError) as raised:
        service.undo(copied.operation_id)

    assert raised.value.code == "UNDO_CONFLICT"

    refusal = service.list_history()[0]
    assert refusal.operation_type == "undo_refused"
    assert refusal.status == "failed"
    assert refusal.target_path == "copied.txt"
    assert refusal.items == ()
    assert refusal.metadata["undone_operation"] == copied.operation_id
    assert refusal.metadata["conflict_code"] == "UNDO_CONFLICT"
    assert refusal.metadata["conflicting_paths"] == ["copied.txt"]
    assert str(service.root) not in (refusal.summary or "")

    # The refusal did not become the undo stack's next target: undo-last still
    # resolves to the move, the most recent real mutation.
    undone = service.undo_last()
    assert undone.summary == f"Undid operation {moved.operation_id}."
    assert (service.root / "copied.txt").is_file()

    # An explicit undo of the refusal row is refused too, not executed. That
    # second refusal is not new evidence: the first row already states the
    # conflict and that nothing changed, so no second refusal row is written.
    with pytest.raises(UndoConflictError):
        service.undo(refusal.operation_id)
    assert [record.operation_type for record in service.list_history()].count("undo_refused") == 1


def test_undoing_a_refusal_record_writes_no_second_refusal_row(service) -> None:
    """A refusal of a refusal is not new evidence, so it adds no ledger row.

    The coded error is unchanged; only the duplicate record is suppressed. A
    refusal of a real operation still records, which the test above proves.
    """

    copied = service.copy_file("notes.txt", "copied.txt")
    service.move_file("copied.txt", "copied-renamed.txt")

    with pytest.raises(UndoConflictError) as first:
        service.undo(copied.operation_id)
    refusal_rows = [
        record for record in service.list_history() if record.operation_type == "undo_refused"
    ]
    assert len(refusal_rows) == 1
    refusal_id = refusal_rows[0].operation_id

    rows_before = len(service.list_history())
    with pytest.raises(UndoConflictError) as second:
        service.undo(refusal_id)

    assert second.value.code == "UNDO_CONFLICT"
    assert second.value.message == "operation did not take effect; there is nothing to undo"
    assert len(service.list_history()) == rows_before
    assert first.value.code == "UNDO_CONFLICT"


def test_an_old_format_journal_row_still_reads_and_undoes(service) -> None:
    """A row written before source metadata existed degrades to an empty dict."""

    result = service.copy_file("notes.txt", "legacy.txt")
    connection = sqlite3.connect(service.history.database_path)
    try:
        with connection:
            connection.execute(
                "UPDATE operations SET metadata_json = '{}' WHERE id = ?",
                (result.operation_id,),
            )
    finally:
        connection.close()

    record = service.get_operation(result.operation_id or "")
    assert record.metadata == {}
    assert record.history_skipped == ()
    assert "source_path" not in record.metadata
    assert service.list_history()[0].operation_id == result.operation_id
    diff = service.diff_operation(result.operation_id or "")
    assert diff.operation_id == result.operation_id

    service.undo(result.operation_id)
    assert not (service.root / "legacy.txt").exists()
