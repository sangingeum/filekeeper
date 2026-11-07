"""Every path-resolution refusal names the resolved target and the workspace root.

The details payload is where a caller learns *where* its path went: on a
workspace rooted at the filesystem root a relative path resolves against ``/``,
so ``runs/x`` becomes ``/runs/x``. The message stays short, and the fields make
the diagnosis one step instead of a guess.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from filekeeper.core.errors import (
    AbsolutePathOutsideWorkspaceError,
    StorageError,
    ValidationError,
)
from filekeeper.core.service import Filekeeper


def test_a_missing_parent_names_the_resolved_path_under_a_root_workspace(
    home: Path, tmp_path: Path
) -> None:
    service = Filekeeper(home=home).workspace(root="/")

    with pytest.raises(ValidationError) as caught:
        service.create_file("runs/captures/note.txt", "hello\n")

    details = caught.value.details
    assert details["path"] == "runs/captures/note.txt"
    assert details["resolved_path"] == "/runs/captures/note.txt"
    assert details["workspace_root"] == "/"


def test_a_missing_parent_on_directory_create_names_the_root_workspace(
    home: Path, tmp_path: Path
) -> None:
    service = Filekeeper(home=home).workspace(root="/")

    with pytest.raises(ValidationError) as caught:
        service.create_directory("runs/captures")

    details = caught.value.details
    assert details["path"] == "runs/captures"
    assert details["resolved_path"] == "/runs/captures"
    assert details["workspace_root"] == "/"


def test_a_permission_failure_names_the_resolved_target_and_the_root(
    service, workspace_root: Path
) -> None:
    locked = workspace_root / "locked"
    locked.mkdir()
    os.chmod(locked, 0o555)
    try:
        with pytest.raises(StorageError) as caught:
            service.create_file("locked/blocked.txt", "hello\n")
    finally:
        os.chmod(locked, 0o755)

    details = caught.value.details
    assert details["path"] == "locked/blocked.txt"
    assert details["resolved_path"] == str(locked / "blocked.txt")
    assert details["workspace_root"] == str(workspace_root.resolve())
    assert not (locked / "blocked.txt").exists()


def test_an_absolute_path_outside_the_workspace_names_the_resolved_target(
    service, workspace_root: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("not mine\n", encoding="utf-8")

    with pytest.raises(AbsolutePathOutsideWorkspaceError) as caught:
        service.read_file(str(outside))

    details = caught.value.details
    assert details["path"] == str(outside)
    assert details["resolved_path"] == str(outside.resolve())
    assert details["workspace_root"] == str(workspace_root.resolve())


def test_the_workspace_root_reports_its_own_writability(service, workspace_root: Path) -> None:
    assert service.root_writable is True
    assert service.root == workspace_root.resolve()
