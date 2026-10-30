"""Schema creation, migration idempotence, and refusal of a newer schema."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from filekeeper.core import database
from filekeeper.core.errors import MigrationError, WorkspaceExistsError


def _table_names(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {str(row["name"]) for row in rows}


def test_registry_database_has_workspaces_and_settings(tmp_path: Path) -> None:
    connection = database.open_registry_database(tmp_path / "registry.sqlite3")
    try:
        tables = _table_names(connection)
    finally:
        connection.close()
    assert {"workspaces", "settings", "schema_migrations"} <= tables


def test_workspace_database_has_all_history_tables(tmp_path: Path) -> None:
    connection = database.open_workspace_database(tmp_path / "state.sqlite3")
    try:
        tables = _table_names(connection)
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        connection.close()
    assert {
        "operations",
        "operation_items",
        "blobs",
        "locks",
        "schema_migrations",
    } <= tables
    assert str(journal_mode).lower() == "wal"


def test_migrations_are_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    first = database.open_workspace_database(path)
    version_after_first = database.migrate_workspace(first)
    first.close()

    second = database.open_workspace_database(path)
    version_after_second = database.migrate_workspace(second)
    rows = second.execute("SELECT COUNT(*) AS n FROM schema_migrations").fetchone()["n"]
    second.close()

    assert version_after_first == version_after_second == database.WORKSPACE_SCHEMA_VERSION
    assert rows == database.WORKSPACE_SCHEMA_VERSION


def test_newer_schema_is_refused(tmp_path: Path) -> None:
    connection = database.connect(tmp_path / "future.sqlite3")
    try:
        connection.execute(
            "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        connection.execute("INSERT INTO schema_migrations (version, applied_at) VALUES (99, 'now')")
        connection.commit()
        with pytest.raises(MigrationError):
            database.migrate_workspace(connection)
    finally:
        connection.close()


def test_duplicate_workspace_registration_is_refused(app, workspace_root: Path) -> None:
    app.create_workspace("project", workspace_root)
    with pytest.raises(WorkspaceExistsError):
        app.create_workspace("project", workspace_root)
    with pytest.raises(WorkspaceExistsError):
        app.create_workspace("other-name", workspace_root)
