"""SQLite connection setup and forward-only migrations.

There are two migration chains because there are two stores:

* the **registry** database (``<home>/metadata.sqlite3``) holds ``workspaces``
  and ``settings`` — data that spans workspaces and cannot live inside one;
* the **workspace** database (``<workspace>/.filekeeper/state.sqlite3``, or the
  shared location in global mode) holds ``operations``, ``operation_items``,
  ``blobs`` and ``locks`` — per-workspace state that travels with the tree.

Both chains are ordered, numbered, and applied at most once. A database whose
recorded version is ahead of this code is refused rather than guessed at.
"""

from __future__ import annotations

import os
import sqlite3
import stat
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from .errors import MigrationError, StorageError

REGISTRY_SCHEMA_VERSION = 1
WORKSPACE_SCHEMA_VERSION = 1

_REGISTRY_MIGRATIONS: tuple[tuple[int, tuple[str, ...]], ...] = (
    (
        1,
        (
            """
            CREATE TABLE workspaces (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                root_path TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            "CREATE UNIQUE INDEX idx_workspaces_root_path ON workspaces(root_path)",
            """
            CREATE TABLE settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """,
        ),
    ),
)

_WORKSPACE_MIGRATIONS: tuple[tuple[int, tuple[str, ...]], ...] = (
    (
        1,
        (
            """
            CREATE TABLE operations (
                id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL,
                operation_type TEXT NOT NULL,
                target_path TEXT,
                status TEXT NOT NULL CHECK (
                    status IN ('pending', 'committed', 'undone', 'failed', 'partially_undone')
                ),
                created_at TEXT NOT NULL,
                actor TEXT,
                task_id TEXT,
                summary TEXT,
                before_hash TEXT,
                after_hash TEXT,
                metadata_json TEXT
            )
            """,
            "CREATE INDEX idx_operations_workspace_time ON operations(workspace_id, created_at)",
            "CREATE INDEX idx_operations_target_path ON operations(target_path)",
            "CREATE INDEX idx_operations_task_id ON operations(task_id)",
            """
            CREATE TABLE operation_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                operation_id TEXT NOT NULL REFERENCES operations(id) ON DELETE CASCADE,
                relative_path TEXT NOT NULL,
                item_type TEXT NOT NULL,
                before_blob_hash TEXT,
                after_blob_hash TEXT,
                before_exists INTEGER NOT NULL DEFAULT 0,
                after_exists INTEGER NOT NULL DEFAULT 0,
                before_mode INTEGER,
                after_mode INTEGER
            )
            """,
            "CREATE INDEX idx_operation_items_operation ON operation_items(operation_id)",
            "CREATE INDEX idx_operation_items_path ON operation_items(relative_path)",
            """
            CREATE TABLE blobs (
                hash TEXT PRIMARY KEY,
                size INTEGER NOT NULL,
                created_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE locks (
                workspace_id TEXT NOT NULL,
                relative_path TEXT NOT NULL,
                owner TEXT NOT NULL,
                token TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (workspace_id, relative_path)
            )
            """,
            "CREATE INDEX idx_locks_expires_at ON locks(expires_at)",
        ),
    ),
)

_MIGRATION_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
)
"""


def utc_now() -> str:
    """Current UTC time as an ISO-8601 string with second precision."""

    return datetime.now(UTC).replace(microsecond=0).isoformat()


def uuid_token() -> str:
    """A short, URL-safe, collision-resistant identifier fragment."""

    return uuid.uuid4().hex[:16]


def connect(path: Path) -> sqlite3.Connection:
    """Open a SQLite database with durable, foreign-key-enforcing settings."""

    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, stat.S_IRWXU)
    except OSError:  # pragma: no cover - restricted filesystems
        pass
    try:
        connection = sqlite3.connect(path, timeout=10.0)
    except sqlite3.Error as exc:
        raise StorageError(f"cannot open database: {exc}", details={"path": str(path)}) from exc
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode = WAL")
    # Full synchronous mode: a committed record survives an OS crash, which the
    # recovery story depends on.
    connection.execute("PRAGMA synchronous = FULL")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


def _current_version(connection: sqlite3.Connection) -> int:
    connection.execute(_MIGRATION_TABLE_SQL)
    row = connection.execute("SELECT MAX(version) AS version FROM schema_migrations").fetchone()
    if row is None or row["version"] is None:
        return 0
    return int(row["version"])


def migrate(
    connection: sqlite3.Connection,
    migrations: Sequence[tuple[int, tuple[str, ...]]],
    *,
    label: str,
) -> int:
    """Apply every pending migration in order; return the resulting version."""

    current = _current_version(connection)
    target = max((version for version, _ in migrations), default=0)
    if current > target:
        raise MigrationError(
            f"{label} database schema version {current} is newer than this release "
            f"supports ({target}); upgrade Filekeeper",
            details={"current": current, "supported": target},
        )

    for version, statements in migrations:
        if version <= current:
            continue
        try:
            with connection:
                for statement in statements:
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                    (version, utc_now()),
                )
        except sqlite3.Error as exc:
            raise MigrationError(
                f"cannot migrate {label} database to version {version}: {exc}",
                details={"label": label, "version": version},
            ) from exc
        current = version
    return current


def migrate_registry(connection: sqlite3.Connection) -> int:
    return migrate(connection, _REGISTRY_MIGRATIONS, label="registry")


def migrate_workspace(connection: sqlite3.Connection) -> int:
    return migrate(connection, _WORKSPACE_MIGRATIONS, label="workspace")


def open_registry_database(path: Path) -> sqlite3.Connection:
    connection = connect(path)
    migrate_registry(connection)
    return connection


def open_workspace_database(path: Path) -> sqlite3.Connection:
    connection = connect(path)
    migrate_workspace(connection)
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:  # pragma: no cover - restricted filesystems
        pass
    return connection
