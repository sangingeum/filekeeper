"""Mutations: create, write, copy, move, delete, directories, failure behaviour."""

from __future__ import annotations

import codecs
import os
from pathlib import Path

import pytest

from filekeeper.core.errors import (
    AlreadyExistsError,
    ContentConflictError,
    LimitExceededError,
    PathOutsideWorkspaceError,
    StorageError,
    SymlinkWriteRefusedError,
    ValidationError,
)


def test_create_file_returns_the_history_shaped_result(service) -> None:
    result = service.create_file("todo.md", "# Todo\n")

    assert result.changed is True
    assert result.path == "todo.md"
    assert result.before_hash is None
    assert result.after_hash is not None
    assert result.after_hash.startswith("sha256:")
    assert result.operation_id is not None
    assert result.operation_id.startswith("op_")
    assert result.undo_available is True
    assert result.items[0].change == "created"
    assert (service.root / "todo.md").read_text(encoding="utf-8") == "# Todo\n"


def test_create_file_refuses_an_existing_target(service) -> None:
    with pytest.raises(AlreadyExistsError):
        service.create_file("notes.txt", "overwrite\n")
    assert (service.root / "notes.txt").read_text(encoding="utf-8") == "alpha\nbeta\ngamma\n"


def test_create_file_can_overwrite_when_asked(service) -> None:
    result = service.create_file("notes.txt", "replaced\n", expected_absent=False)
    assert result.items[0].change == "modified"
    assert (service.root / "notes.txt").read_text(encoding="utf-8") == "replaced\n"


def test_create_file_requires_an_existing_parent(service) -> None:
    with pytest.raises(ValidationError):
        service.create_file("missing/dir/file.txt", "x\n")


def test_write_file_accepts_a_matching_expected_hash(service) -> None:
    current = service.get_file_hash("notes.txt")
    result = service.write_file("notes.txt", "new\n", expected_hash=current)

    assert result.before_hash == current
    assert (service.root / "notes.txt").read_text(encoding="utf-8") == "new\n"


def test_stale_expected_hash_is_refused_without_modifying_the_file(service) -> None:
    stale = "sha256:" + "0" * 64
    before = (service.root / "notes.txt").read_bytes()

    with pytest.raises(ContentConflictError) as caught:
        service.write_file("notes.txt", "clobber\n", expected_hash=stale)

    assert (service.root / "notes.txt").read_bytes() == before
    error = caught.value
    assert error.retryable is False
    assert error.details["recovery"] == (
        "re-read the target, then retry against its current hash"
    )
    assert error.details["expected_hash"] == stale
    assert str(error.details["actual_hash"]).startswith("sha256:")

    # The declared recovery path works: re-read, then write against the new hash.
    current = service.get_file_hash("notes.txt")
    retried = service.write_file("notes.txt", "clobber\n", expected_hash=current)
    assert retried.before_hash == current
    assert (service.root / "notes.txt").read_text(encoding="utf-8") == "clobber\n"


def test_write_preserves_a_utf8_bom(service) -> None:
    target = service.root / "bom.txt"
    target.write_bytes(codecs.BOM_UTF8 + b"hello\n")

    service.write_file("bom.txt", "goodbye\n")

    assert target.read_bytes() == codecs.BOM_UTF8 + b"goodbye\n"


def test_write_preserves_crlf_content_as_given(service) -> None:
    target = service.root / "windows.txt"
    target.write_bytes(b"one\r\ntwo\r\n")

    service.write_file("windows.txt", "three\r\nfour\r\n")

    assert target.read_bytes() == b"three\r\nfour\r\n"


def test_write_refuses_a_symlink_target(service) -> None:
    os.symlink(service.root / "notes.txt", service.root / "alias.txt")

    with pytest.raises(SymlinkWriteRefusedError):
        service.write_file("alias.txt", "clobber\n")
    assert (service.root / "notes.txt").read_text(encoding="utf-8") == "alpha\nbeta\ngamma\n"


def test_write_outside_the_workspace_is_refused(service, tmp_path: Path) -> None:
    with pytest.raises(PathOutsideWorkspaceError):
        service.write_file(str(tmp_path / "escape.txt"), "x\n")


def test_write_size_limit_is_enforced(small_limits) -> None:
    with pytest.raises(LimitExceededError):
        small_limits.create_file("big.txt", "x" * 2048)


def test_write_failure_before_rename_leaves_the_old_content(
    service, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_replace(source: str, destination: str) -> None:
        raise OSError("simulated crash before rename")

    monkeypatch.setattr("filekeeper.core.safety.atomic.os.replace", fail_replace)

    with pytest.raises(StorageError):
        service.write_file("notes.txt", "replacement\n")

    assert (service.root / "notes.txt").read_text(encoding="utf-8") == "alpha\nbeta\ngamma\n"
    assert not list((service.root).glob(".keeper-tmp-*"))


def test_copy_file(service) -> None:
    result = service.copy_file("notes.txt", "copy.txt")

    assert (service.root / "copy.txt").read_text(encoding="utf-8") == "alpha\nbeta\ngamma\n"
    assert result.items[0].change == "created"


def test_copy_refuses_an_unguarded_existing_destination(service) -> None:
    (service.root / "existing.txt").write_text("keep\n", encoding="utf-8")
    with pytest.raises(AlreadyExistsError):
        service.copy_file("notes.txt", "existing.txt")
    assert (service.root / "existing.txt").read_text(encoding="utf-8") == "keep\n"


def test_copy_replaces_a_destination_only_with_a_matching_hash(service) -> None:
    (service.root / "existing.txt").write_text("old\n", encoding="utf-8")
    digest = service.get_file_hash("existing.txt")

    service.copy_file("notes.txt", "existing.txt", expected_destination_hash=digest)
    assert (service.root / "existing.txt").read_text(encoding="utf-8") == "alpha\nbeta\ngamma\n"


def test_move_file(service) -> None:
    result = service.move_file("notes.txt", "renamed.txt")

    assert not (service.root / "notes.txt").exists()
    assert (service.root / "renamed.txt").read_text(encoding="utf-8") == "alpha\nbeta\ngamma\n"
    assert result.items[0].change == "moved"


def test_move_refuses_an_existing_destination_unless_overwriting(service) -> None:
    (service.root / "occupied.txt").write_text("occupied\n", encoding="utf-8")
    with pytest.raises(AlreadyExistsError):
        service.move_file("notes.txt", "occupied.txt")

    service.move_file("notes.txt", "occupied.txt", overwrite=True)
    assert (service.root / "occupied.txt").read_text(encoding="utf-8") == "alpha\nbeta\ngamma\n"


def test_delete_file(service) -> None:
    service.delete_file("notes.txt")
    assert not (service.root / "notes.txt").exists()


def test_delete_with_a_stale_hash_is_refused(service) -> None:
    stale = "sha256:" + "1" * 64
    with pytest.raises(ContentConflictError):
        service.delete_file("notes.txt", expected_hash=stale)
    assert (service.root / "notes.txt").exists()


def test_create_directory_variants(service) -> None:
    with pytest.raises(ValidationError):
        service.create_directory("a/b/c")
    service.create_directory("a/b/c", parents=True)
    assert (service.root / "a" / "b" / "c").is_dir()

    with pytest.raises(AlreadyExistsError):
        service.create_directory("a")


def test_delete_directory_requires_recursion_and_confirmation(service) -> None:
    with pytest.raises(ValidationError):
        service.delete_directory("sub")
    with pytest.raises(ValidationError):
        service.delete_directory("sub", recursive=True)

    result = service.delete_directory("sub", recursive=True, confirm_recursive=True)
    assert not (service.root / "sub").exists()
    assert result.items[0].change == "deleted"


def test_delete_directory_refuses_an_empty_directory_without_recursion(service) -> None:
    service.create_directory("empty")
    result = service.delete_directory("empty")
    assert result.changed is True
    assert not (service.root / "empty").exists()


def test_delete_directory_refuses_the_workspace_root(service) -> None:
    with pytest.raises(ValidationError):
        service.delete_directory(".", recursive=True, confirm_recursive=True)


def test_move_directory(service) -> None:
    service.move_directory("sub", "moved")
    assert not (service.root / "sub").exists()
    assert (service.root / "moved" / "nested.md").is_file()


def test_move_directory_into_itself_is_refused(service) -> None:
    with pytest.raises(ValidationError):
        service.move_directory("sub", "sub/inner")


def test_dry_run_changes_nothing(service) -> None:
    result = service.write_file("notes.txt", "never written\n", dry_run=True)

    assert result.dry_run is True
    assert (service.root / "notes.txt").read_text(encoding="utf-8") == "alpha\nbeta\ngamma\n"

    dry = service.delete_file("notes.txt", dry_run=True)
    assert dry.changed is True
    assert (service.root / "notes.txt").exists()
