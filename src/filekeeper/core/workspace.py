"""Workspace registration, settings, and per-workspace state location.

A workspace is a registered directory that bounds every operation. Registration
lives in the home database; the workspace's own state directory lives in the tree
by default so that history and blobs travel with the checkout.

When the tree cannot host that directory — a read-only checkout, or a workspace
root at ``/`` — the deployment still works: the state directory falls back to the
global store and the response says so. An operator who named ``state_location``
explicitly gets the failure instead, because that choice is theirs to make.
"""

from __future__ import annotations

import os
import re
import sqlite3
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from . import database
from .config import STATE_DIR_NAME, Config
from .errors import (
    StorageError,
    ValidationError,
    WorkspaceExistsError,
    WorkspaceNotFoundError,
)
from .models import Workspace

_NAME_PATTERN: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
DEFAULT_WORKSPACE_KEY: Final = "default_workspace"
STATE_DATABASE_NAME: Final = "state.sqlite3"
GITIGNORE_ENTRY: Final = f"{STATE_DIR_NAME}/"

# Names the fallback without naming a host path: the message is rendered to
# callers who are confined to the workspace.
GLOBAL_FALLBACK_WARNING: Final = (
    "the workspace root cannot host a state directory, so history is kept in "
    "the global Filekeeper state store instead of a project-local directory"
)


@dataclass(frozen=True, slots=True)
class WorkspaceHandle:
    """Everything an operation needs to know about where it is running."""

    name: str
    root: Path
    workspace_id: str | None
    config: Config
    state_dir: Path
    state_warning: str | None = None


@dataclass(frozen=True, slots=True)
class StateLocation:
    """A resolved state directory plus the warning that explains a fallback."""

    directory: Path
    warning: str | None = None


def global_workspace_state_dir(workspace_id: str, home: Path) -> Path:
    """The global store's directory for one workspace id."""

    return Path(home) / "workspaces" / workspace_id


def display_state_location(state_dir: Path, *, root: Path | None, home: Path | None) -> str:
    """Render a state directory without naming an absolute host path.

    A public error envelope is read by a caller confined to the workspace, so a
    resolved host path there tells them nothing they can use and describes the
    host tree. The workspace-relative form is preferred; a global store outside
    the workspace is rendered relative to the home directory; anything else falls
    back to prose. The value stays meaningful, which is what the operator needs
    in order to fix the deployment.
    """

    for base in (root, home):
        if base is None:
            continue
        try:
            relative = Path(state_dir).relative_to(Path(base)).as_posix()
        except ValueError:
            continue
        return relative or "."
    return "the global Filekeeper state store"


def _directory_usable(directory: Path) -> bool:
    """Whether ``directory`` exists and is writable, or can be created."""

    try:
        if directory.exists():
            return directory.is_dir() and os.access(directory, os.W_OK | os.X_OK)
        parent = directory.parent
        return parent.is_dir() and os.access(parent, os.W_OK | os.X_OK)
    except OSError:  # pragma: no cover - unreadable ancestors
        return False


def resolve_state_location(workspace: Workspace, config: Config, home: Path) -> StateLocation:
    """Where this workspace's journal and blobs live, after checking usability.

    Project-local is the default: ``<root>/.filekeeper``. Global mode keeps one
    directory per workspace id under the home store, so projects can still be
    attributed and pruned individually. When the root cannot host the project
    directory and the operator did not choose a location explicitly, the global
    store is used and the returned warning says so.
    """

    if config.state_location == "global":
        return StateLocation(global_workspace_state_dir(workspace.id, home))
    project_local = Path(workspace.root_path) / STATE_DIR_NAME
    if _directory_usable(project_local):
        return StateLocation(project_local)
    if config.state_location_explicit:
        # The operator asked for project-local state; do not redirect it.
        return StateLocation(project_local)
    return StateLocation(global_workspace_state_dir(workspace.id, home), GLOBAL_FALLBACK_WARNING)


def workspace_state_dir(workspace: Workspace, config: Config, home: Path) -> Path:
    """The state directory only, without the fallback explanation."""

    return resolve_state_location(workspace, config, home).directory


def _unattributed_history_note(workspace_id: str, state_dir: Path) -> str | None:
    """Name earlier per-process transient journals that cannot be adopted.

    Before this release a transient workspace drew a random id per process, so
    every process wrote its own directory. Those directories carry no record of
    their root, which makes silent adoption unsafe; they are left untouched and
    their existence is reported instead of being hidden.
    """

    if not workspace_id.startswith("ws-transient-"):
        return None
    try:
        siblings = [
            entry
            for entry in state_dir.parent.iterdir()
            if entry.is_dir()
            and entry != state_dir
            and entry.name.startswith("ws-transient-")
            and (entry / STATE_DATABASE_NAME).is_file()
        ]
    except OSError:  # pragma: no cover - unreadable store
        return None
    if not siblings:
        return None
    noun = "directory" if len(siblings) == 1 else "directories"
    return (
        f"{len(siblings)} earlier per-process state {noun} remain in the global store; "
        "their history is not attributed to this workspace"
    )


def handle_for(workspace: Workspace, config: Config, home: Path) -> WorkspaceHandle:
    location = resolve_state_location(workspace, config, home)
    warning = location.warning
    legacy = _unattributed_history_note(workspace.id, location.directory)
    if legacy is not None:
        warning = legacy if warning is None else f"{warning}; {legacy}"
    return WorkspaceHandle(
        name=workspace.name,
        root=Path(workspace.root_path),
        workspace_id=workspace.id,
        config=config,
        state_dir=location.directory,
        state_warning=warning,
    )


def ensure_state_directory(
    state_dir: Path, *, root: Path | None = None, home: Path | None = None
) -> Path:
    """Create the state directory and its migrated database; return the DB path.

    Every filesystem failure here is a :class:`StorageError` naming the state
    directory the operator chose. It is never an ``INTERNAL_ERROR``, and neither
    the message nor the details carry a resolved host path: the location is
    rendered workspace-relative (or home-relative for the global store).
    """

    display = display_state_location(state_dir, root=root, home=home)
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        state_dir.chmod(stat.S_IRWXU)
    except OSError as exc:
        raise StorageError(
            f"cannot create state directory: {exc.strerror or type(exc).__name__}",
            details={"state_dir": display},
        ) from exc
    if root is not None and state_dir.parent == root:
        # Only a project-local directory belongs in the tree's ignore rules.
        ensure_gitignore_entry(root)
    database_path = state_dir / STATE_DATABASE_NAME
    connection = database.open_workspace_database(database_path, display=display)
    connection.close()
    return database_path


def _inside_git_repository(root: Path) -> bool:
    return any((candidate / ".git").exists() for candidate in (root, *root.parents))


def ensure_gitignore_entry(root: Path) -> str | None:
    """Make sure the state directory is ignored, when the tree is a git repository.

    Returns a note describing what was done, or ``None`` when nothing was needed.
    Outside a repository nothing is written: creating a ``.gitignore`` in an
    arbitrary directory would be noise.
    """

    if not _inside_git_repository(root):
        return None
    gitignore = root / ".gitignore"
    try:
        if gitignore.exists():
            existing = gitignore.read_text(encoding="utf-8")
            lines = {line.strip() for line in existing.splitlines()}
            if GITIGNORE_ENTRY in lines or STATE_DIR_NAME in lines:
                return None
            separator = "" if existing.endswith(("\n", "")) else "\n"
            with gitignore.open("a", encoding="utf-8") as handle:
                handle.write(f"{separator}{GITIGNORE_ENTRY}\n")
            return f"added {GITIGNORE_ENTRY} to .gitignore"
        gitignore.write_text(f"{GITIGNORE_ENTRY}\n", encoding="utf-8")
    except OSError as exc:
        return f"could not update .gitignore ({exc}); add {GITIGNORE_ENTRY} manually"
    return f"created .gitignore with {GITIGNORE_ENTRY}"


class WorkspaceRegistry:
    """The home-scoped registry of workspaces and the default workspace setting."""

    def __init__(self, home: Path, config: Config | None = None) -> None:
        self.home = Path(home)
        self.config = config or Config()
        self.database_path = self.home / "metadata.sqlite3"

    def _connect(self) -> sqlite3.Connection:
        try:
            self.home.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise StorageError(
                f"cannot create the Filekeeper home directory: "
                f"{exc.strerror or type(exc).__name__}",
                details={"state_dir": "the Filekeeper home directory"},
            ) from exc
        try:
            self.home.chmod(stat.S_IRWXU)
        except OSError:  # pragma: no cover - restricted filesystems
            pass
        return database.open_registry_database(self.database_path)

    # -- workspaces ---------------------------------------------------------
    def create(self, name: str, root_path: str | Path) -> Workspace:
        if not _NAME_PATTERN.match(name):
            raise ValidationError(
                "workspace name must start with a letter or digit and contain only "
                "letters, digits, '.', '_' or '-' (maximum 64 characters)",
                details={"name": name},
            )
        root = Path(root_path).expanduser().resolve()
        if not root.is_dir():
            raise ValidationError(
                "workspace root must be an existing directory", details={"root": str(root_path)}
            )
        now = database.utc_now()
        workspace = Workspace(
            id=f"ws_{database.uuid_token()}",
            name=name,
            root_path=str(root),
            created_at=now,
            updated_at=now,
        )
        existing = self._find_by_name_or_root(name, str(root))
        if existing is not None:
            raise WorkspaceExistsError(
                f"a workspace already covers this name or root: {existing.name}",
                details={"name": name, "existing_workspace": existing.name},
            )
        connection = self._connect()
        try:
            with connection:
                connection.execute(
                    "INSERT INTO workspaces (id, name, root_path, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        workspace.id,
                        workspace.name,
                        workspace.root_path,
                        workspace.created_at,
                        workspace.updated_at,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            existing = self._find_by_name_or_root(name, str(root))
            if existing is not None:
                raise WorkspaceExistsError(
                    f"a workspace already covers this name or root: {existing.name}",
                    details={"name": name, "existing_workspace": existing.name},
                ) from exc
            raise StorageError(f"cannot register workspace: {exc}") from exc
        finally:
            connection.close()
        return workspace

    def _find_by_name_or_root(self, name: str, root: str) -> Workspace | None:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM workspaces WHERE name = ? OR root_path = ? LIMIT 1",
                (name, root),
            ).fetchone()
        finally:
            connection.close()
        return _row_to_workspace(row) if row is not None else None

    def list(self) -> tuple[Workspace, ...]:
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT * FROM workspaces ORDER BY name COLLATE NOCASE"
            ).fetchall()
        finally:
            connection.close()
        return tuple(_row_to_workspace(row) for row in rows)

    def get(self, name_or_id: str) -> Workspace:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM workspaces WHERE name = ? OR id = ? LIMIT 1",
                (name_or_id, name_or_id),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise WorkspaceNotFoundError(
                "no workspace with that name or id", details={"workspace": name_or_id}
            )
        return _row_to_workspace(row)

    def find_by_root(self, root_path: str | Path) -> Workspace | None:
        canonical = str(Path(root_path).expanduser().resolve())
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM workspaces WHERE root_path = ? LIMIT 1", (canonical,)
            ).fetchone()
        finally:
            connection.close()
        return _row_to_workspace(row) if row is not None else None

    def remove(self, name_or_id: str, *, delete_files: bool = False) -> Workspace:
        if delete_files:
            raise ValidationError(
                "removing a workspace never deletes its files; this release has no "
                "file-deleting removal mode",
                details={"workspace": name_or_id},
            )
        workspace = self.get(name_or_id)
        connection = self._connect()
        try:
            with connection:
                connection.execute("DELETE FROM workspaces WHERE id = ?", (workspace.id,))
                connection.execute("DELETE FROM settings WHERE key = ?", (DEFAULT_WORKSPACE_KEY,))
        finally:
            connection.close()
        return workspace

    # -- settings -----------------------------------------------------------
    def set_default(self, name_or_id: str) -> Workspace:
        workspace = self.get(name_or_id)
        connection = self._connect()
        try:
            with connection:
                connection.execute(
                    "INSERT INTO settings (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (DEFAULT_WORKSPACE_KEY, workspace.name),
                )
        finally:
            connection.close()
        return workspace

    def get_default_name(self) -> str | None:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT value FROM settings WHERE key = ?", (DEFAULT_WORKSPACE_KEY,)
            ).fetchone()
        finally:
            connection.close()
        return None if row is None else str(row["value"])

    def get_default(self) -> Workspace | None:
        name = self.get_default_name()
        if name is None:
            return None
        try:
            return self.get(name)
        except WorkspaceNotFoundError:
            return None

    # -- state directory ----------------------------------------------------
    def state_dir_for(self, workspace: Workspace) -> Path:
        return workspace_state_dir(workspace, self.config, self.home)

    def ensure_state_dir(self, workspace: Workspace) -> Path:
        """Create the state directory and its migrated database; return the DB path.

        Called before the first mutation that will need durable state. It also
        makes sure a git repository will not track a project-local directory.
        """

        return ensure_state_directory(
            self.state_dir_for(workspace),
            root=Path(workspace.root_path),
            home=self.home,
        )


def _row_to_workspace(row: sqlite3.Row) -> Workspace:
    root_path = str(row["root_path"])
    return Workspace(
        id=str(row["id"]),
        name=str(row["name"]),
        root_path=root_path,
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        root_exists=Path(root_path).is_dir(),
    )
