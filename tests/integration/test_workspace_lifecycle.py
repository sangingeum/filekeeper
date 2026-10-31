"""Registration, default selection, state directory and .gitignore handling."""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

from filekeeper.core.errors import (
    NoWorkspaceSelectedError,
    ValidationError,
    WorkspaceNotFoundError,
)


def test_register_list_and_lookup(app, workspace_root: Path) -> None:
    workspace = app.create_workspace("project", workspace_root)

    assert app.list_workspaces() == (workspace,)
    assert app.get_workspace("project") == workspace
    assert app.get_workspace(workspace.id) == workspace


def test_default_workspace_selection(app, workspace_root: Path, tmp_path: Path) -> None:
    app.create_workspace("alpha", workspace_root)
    other = tmp_path / "other"
    other.mkdir()
    app.create_workspace("beta", other)

    app.use_workspace("beta")
    assert app.default_workspace() is not None
    assert app.workspace().name == "beta"
    assert app.workspace(name="alpha").name == "alpha"
    assert app.workspace(root=workspace_root).name == "alpha"


def test_no_workspace_selected_is_an_error(app) -> None:
    with pytest.raises(NoWorkspaceSelectedError):
        app.workspace()


def test_transient_root_is_confined_like_a_workspace(app, workspace_root: Path) -> None:
    service = app.workspace(root=workspace_root)
    assert service.registered is False
    assert service.read_file("notes.txt").content == "alpha\nbeta\ngamma\n"

    with pytest.raises(ValidationError):
        app.workspace(root=workspace_root / "missing")


def test_workspace_name_validation(app, workspace_root: Path) -> None:
    with pytest.raises(ValidationError):
        app.create_workspace("bad name!", workspace_root)
    with pytest.raises(ValidationError):
        app.create_workspace("missing-root", workspace_root / "absent")


def test_unknown_workspace_is_reported(app) -> None:
    with pytest.raises(WorkspaceNotFoundError):
        app.get_workspace("nothing")
    with pytest.raises(WorkspaceNotFoundError):
        app.remove_workspace("nothing")


def test_removal_never_deletes_files(app, workspace_root: Path) -> None:
    app.create_workspace("project", workspace_root)
    removed = app.remove_workspace("project")

    assert removed.name == "project"
    assert (workspace_root / "notes.txt").is_file()
    assert app.list_workspaces() == ()


def test_removal_with_delete_files_is_refused(app, workspace_root: Path) -> None:
    app.create_workspace("project", workspace_root)
    with pytest.raises(ValidationError):
        app.remove_workspace("project", delete_files=True)


def test_a_missing_root_is_reported_not_pruned(app, workspace_root: Path) -> None:
    app.create_workspace("project", workspace_root)
    app.use_workspace("project")
    shutil.rmtree(workspace_root)

    listed = app.list_workspaces()
    assert len(listed) == 1
    assert listed[0].root_exists is False
    assert app.get_workspace("project").root_exists is False
    assert app.default_workspace() is not None, "a stale registration is still offered by default"


def test_removing_a_stale_workspace_clears_the_default(app, workspace_root: Path) -> None:
    app.create_workspace("project", workspace_root)
    app.use_workspace("project")
    shutil.rmtree(workspace_root)

    removed = app.remove_workspace("project")

    assert removed.name == "project"
    assert app.list_workspaces() == ()
    assert app.registry.get_default_name() is None
    assert not workspace_root.exists()


def test_state_directory_is_project_local_and_migrated(app, workspace_root: Path) -> None:
    app.create_workspace("project", workspace_root)
    service = app.workspace(name="project")
    database_path = service.state_database_path()

    assert database_path == workspace_root / ".filekeeper" / "state.sqlite3"
    assert database_path.is_file()

    connection = sqlite3.connect(database_path)
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    finally:
        connection.close()
    assert {"operations", "operation_items", "blobs", "locks"} <= tables


def test_global_state_location_uses_the_home_store(app, workspace_root: Path, home: Path) -> None:
    app.config = type(app.config)(
        state_location="global",
        max_read_bytes=app.config.max_read_bytes,
        max_write_bytes=app.config.max_write_bytes,
        max_search_results=app.config.max_search_results,
        max_directory_entries=app.config.max_directory_entries,
        max_recursion_depth=app.config.max_recursion_depth,
        max_line_length=app.config.max_line_length,
        fsync=app.config.fsync,
    )
    app.registry.config = app.config
    workspace = app.create_workspace("project", workspace_root)

    assert app.registry.state_dir_for(workspace) == home / "workspaces" / workspace.id


def test_gitignore_entry_is_added_once_for_repositories(app, workspace_root: Path) -> None:
    (workspace_root / ".git").mkdir()
    workspace = app.create_workspace("project", workspace_root)

    app.registry.ensure_state_dir(workspace)
    app.registry.ensure_state_dir(workspace)

    content = (workspace_root / ".gitignore").read_text(encoding="utf-8")
    assert content.count(".filekeeper/") == 1


def test_gitignore_is_not_created_outside_a_repository(app, workspace_root: Path) -> None:
    workspace = app.create_workspace("project", workspace_root)
    app.registry.ensure_state_dir(workspace)

    assert not (workspace_root / ".gitignore").exists()
