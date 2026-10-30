"""Workspace boundaries: traversal, absolute paths, symlinks, special files."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from filekeeper.core.errors import (
    AbsolutePathOutsideWorkspaceError,
    ErrorCode,
    PathNotFoundError,
    PathOutsideWorkspaceError,
    SpecialFileRefusedError,
    StatePathError,
    SymlinkEscapeRefusedError,
    SymlinkWriteRefusedError,
    ValidationError,
)
from filekeeper.core.paths import resolve_path


def test_relative_path_resolves_inside(workspace_root: Path) -> None:
    resolved = resolve_path(workspace_root, "sub/nested.md", must_exist=True)
    assert resolved.relative == "sub/nested.md"
    assert resolved.absolute == workspace_root / "sub" / "nested.md"


def test_absolute_path_inside_is_accepted(workspace_root: Path) -> None:
    resolved = resolve_path(workspace_root, workspace_root / "notes.txt", must_exist=True)
    assert resolved.relative == "notes.txt"


def test_absolute_path_outside_is_rejected(workspace_root: Path, tmp_path: Path) -> None:
    with pytest.raises(AbsolutePathOutsideWorkspaceError) as raised:
        resolve_path(workspace_root, tmp_path / "elsewhere.txt", must_exist=True)
    assert raised.value.code is ErrorCode.ABSOLUTE_PATH_OUTSIDE_WORKSPACE


def test_parent_traversal_is_rejected(workspace_root: Path) -> None:
    with pytest.raises(PathOutsideWorkspaceError) as raised:
        resolve_path(workspace_root, "../escape.txt")
    assert raised.value.code is ErrorCode.PATH_OUTSIDE_WORKSPACE


def test_embedded_traversal_is_rejected_even_when_it_stays_inside(
    workspace_root: Path,
) -> None:
    with pytest.raises(PathOutsideWorkspaceError):
        resolve_path(workspace_root, "sub/../notes.txt")


def test_absolute_traversal_that_escapes_is_rejected(workspace_root: Path) -> None:
    with pytest.raises(AbsolutePathOutsideWorkspaceError) as raised:
        resolve_path(workspace_root, str(workspace_root / ".." / "outside.txt"))
    assert raised.value.code is ErrorCode.ABSOLUTE_PATH_OUTSIDE_WORKSPACE


def test_symlink_pointing_outside_is_rejected(workspace_root: Path, tmp_path: Path) -> None:
    outside = tmp_path / "secret.txt"
    outside.write_text("secret\n", encoding="utf-8")
    link = workspace_root / "link.txt"
    os.symlink(outside, link)

    with pytest.raises(SymlinkEscapeRefusedError) as raised:
        resolve_path(workspace_root, "link.txt", must_exist=True)
    assert raised.value.code is ErrorCode.SYMLINK_ESCAPE_REFUSED

    with pytest.raises(SymlinkEscapeRefusedError) as write_raised:
        resolve_path(workspace_root, "link.txt", for_write=True)
    assert write_raised.value.code is ErrorCode.SYMLINK_ESCAPE_REFUSED


def test_symlink_pointing_inside_can_be_read_but_not_written(workspace_root: Path) -> None:
    link = workspace_root / "alias.txt"
    os.symlink(workspace_root / "notes.txt", link)

    readable = resolve_path(workspace_root, "alias.txt", must_exist=True)
    assert readable.absolute == (workspace_root / "notes.txt").resolve()

    with pytest.raises(SymlinkWriteRefusedError):
        resolve_path(workspace_root, "alias.txt", for_write=True)


def test_missing_path_reports_not_found(workspace_root: Path) -> None:
    with pytest.raises(PathNotFoundError):
        resolve_path(workspace_root, "absent.txt", must_exist=True)

    unresolved = resolve_path(workspace_root, "absent.txt")
    assert unresolved.exists is False
    assert unresolved.relative == "absent.txt"


def test_special_file_is_rejected(workspace_root: Path) -> None:
    fifo = workspace_root / "pipe"
    if not hasattr(os, "mkfifo"):  # pragma: no cover - Windows
        pytest.skip("named pipes are not available on this platform")
    os.mkfifo(fifo)

    with pytest.raises(SpecialFileRefusedError):
        resolve_path(workspace_root, "pipe", must_exist=True)


def test_state_directory_is_refused(workspace_root: Path, service) -> None:
    service.state_database_path()
    assert (workspace_root / ".filekeeper").is_dir()

    with pytest.raises(StatePathError):
        resolve_path(
            workspace_root,
            ".filekeeper/state.sqlite3",
            state_dir=workspace_root / ".filekeeper",
        )


def test_empty_and_nul_paths_are_rejected(workspace_root: Path) -> None:
    with pytest.raises(ValidationError):
        resolve_path(workspace_root, "")
    with pytest.raises(ValidationError):
        resolve_path(workspace_root, "notes\x00.txt")


def test_root_must_be_a_directory(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    with pytest.raises(ValidationError):
        resolve_path(missing, "file.txt")
