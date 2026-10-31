"""Where durable state goes when the workspace root cannot host it.

A root at ``/`` is the deployment that exposed these failures, but the same
condition holds for any read-only checkout. The rules under test:

* an unusable project directory falls back to the global store and says so —
  including when the configuration file came from ``keeper init``;
* an explicit ``state_location`` is honoured, and its failure is a
  ``STORAGE_ERROR`` naming the state directory in a caller-relative form;
* no public error envelope carries an absolute host path, in any detail value;
* the failure happens before the filesystem is touched;
* a dry run touches no state at all.
"""

from __future__ import annotations

import json
import os
import tomllib
from pathlib import Path

import pytest

from filekeeper.core.config import Config
from filekeeper.core.errors import ErrorCode, StorageError
from filekeeper.core.service import Filekeeper, init_home


def _deny_writes(directory: Path) -> None:
    if os.geteuid() == 0:  # pragma: no cover - root ignores mode bits
        pytest.skip("permission bits do not deny root")
    directory.chmod(0o555)


def _allow_writes(directory: Path) -> None:
    directory.chmod(0o755)


def _project_local_config() -> Config:
    """The default limits with ``state_location`` chosen explicitly."""

    return Config(state_location="project", state_location_explicit=True)


def _assert_no_host_paths(details: dict[str, object], *host_roots: Path) -> None:
    """Every detail value is free of the resolved host paths in this deployment."""

    rendered = json.dumps(details)
    for host_root in host_roots:
        assert str(host_root) not in rendered, rendered


def test_an_unwritable_root_falls_back_to_the_global_store_with_a_warning(
    home: Path, tmp_path: Path
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    (root / "notes.txt").write_text("readable\n", encoding="utf-8")
    _deny_writes(root)
    try:
        app = Filekeeper(home=home)
        service = app.workspace(root=root)

        assert service.handle.workspace_id is not None
        assert service.handle.state_dir == home / "workspaces" / service.handle.workspace_id
        assert service.handle.state_warning is not None
        assert "global" in service.handle.state_warning

        # Reads need no journal and keep working on a read-only tree.
        assert service.read_file("notes.txt").content == "readable\n"
        assert not (root / ".filekeeper").exists()
    finally:
        _allow_writes(root)


def test_a_generated_config_still_reaches_the_global_fallback(home: Path, tmp_path: Path) -> None:
    """``keeper init`` must not turn the built-in default into an explicit choice."""

    init_home(home)
    generated = home / "config.toml"
    with generated.open("rb") as handle:
        section = tomllib.load(handle)["filekeeper"]
    assert "state_location" not in section
    assert "state_location" in generated.read_text(encoding="utf-8")

    root = tmp_path / "project"
    root.mkdir()
    _deny_writes(root)
    try:
        # A fresh process reads the generated file, exactly as a CLI call does.
        app = Filekeeper(home=home)
        assert app.config.state_location_explicit is False
        service = app.workspace(root=root)

        assert service.handle.state_dir.is_relative_to(home)
        assert service.handle.state_warning is not None
        assert "global" in service.handle.state_warning
    finally:
        _allow_writes(root)


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write anywhere")
def test_the_filesystem_root_falls_back_and_history_survives_a_second_instance(
    home: Path, tmp_path: Path
) -> None:
    app = Filekeeper(home=home)
    service = app.workspace(root=Path("/"))

    assert service.handle.state_dir.is_relative_to(home)
    assert service.handle.state_warning is not None

    relative = str(tmp_path.relative_to("/")) + "/created-by-root.txt"
    created = service.create_file(relative, "hi\n")
    assert created.operation_id is not None

    second = Filekeeper(home=home).workspace(root=Path("/"))
    assert second.handle.workspace_id == service.handle.workspace_id
    records = second.list_history()
    assert [record.operation_id for record in records] == [created.operation_id]

    second.undo(created.operation_id)
    assert not (tmp_path / "created-by-root.txt").exists()


def test_an_explicit_project_location_fails_with_a_relative_state_directory(
    home: Path, tmp_path: Path
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    _deny_writes(root)
    config = _project_local_config()
    try:
        app = Filekeeper(home=home, config=config)
        app.registry.config = config
        service = app.workspace(root=root)
        assert service.handle.state_warning is None

        with pytest.raises(StorageError) as raised:
            service.create_file("new.txt", "content\n")
    finally:
        _allow_writes(root)

    error = raised.value
    assert error.code is ErrorCode.STORAGE_ERROR
    assert error.details["state_dir"] == ".filekeeper"
    _assert_no_host_paths(error.details, root, home)
    assert str(root) not in error.message
    assert str(home) not in error.message
    assert not (root / "new.txt").exists()


def test_an_impossible_fallback_reports_the_state_directory_relative_to_home(
    home: Path, tmp_path: Path
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    _deny_writes(root)
    # ``workspaces`` is a file, so the global store cannot be created either.
    home.mkdir(parents=True)
    (home / "workspaces").write_text("not a directory\n", encoding="utf-8")
    try:
        app = Filekeeper(home=home)
        service = app.workspace(root=root)
        assert service.handle.state_warning is not None

        with pytest.raises(StorageError) as raised:
            service.create_file("new.txt", "content\n")
    finally:
        _allow_writes(root)

    error = raised.value
    assert error.code is ErrorCode.STORAGE_ERROR
    assert error.details["state_dir"] == f"workspaces/{service.handle.workspace_id}"
    _assert_no_host_paths(error.details, root, home)
    assert "cannot create state directory" in error.message
    assert str(home) not in error.message


def test_a_dry_run_touches_no_state_even_when_the_journal_cannot_be_written(
    home: Path, tmp_path: Path
) -> None:
    """A preview needs no journal, so an unwritable location cannot block it."""

    root = tmp_path / "project"
    root.mkdir()
    _deny_writes(root)
    config = _project_local_config()
    try:
        app = Filekeeper(home=home, config=config)
        app.registry.config = config
        service = app.workspace(root=root)

        result = service.create_file("new.txt", "content\n", dry_run=True)

        assert result.dry_run is True
        assert result.operation_id is None
        assert not (root / ".filekeeper").exists()
        assert not (root / "new.txt").exists()
    finally:
        _allow_writes(root)


def test_a_mutation_that_cannot_journal_leaves_no_partial_change(
    home: Path, tmp_path: Path
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    # A regular file where the state directory belongs: no journal is possible.
    (root / ".filekeeper").write_text("not a directory\n", encoding="utf-8")
    config = _project_local_config()
    app = Filekeeper(home=home, config=config)
    app.registry.config = config
    service = app.workspace(root=root)

    with pytest.raises(StorageError):
        service.create_directory("made-by-a-failed-call")

    assert not (root / "made-by-a-failed-call").exists()
