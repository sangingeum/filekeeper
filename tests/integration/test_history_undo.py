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

    result = service.delete_directory(
        "tree", recursive=True, confirm_recursive=True
    )
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
