"""The public Core API.

Two objects make up the surface:

* :class:`Filekeeper` — home-scoped: initialisation, workspace registration and
  selection. It is the entry point for adapters.
* :class:`FilekeeperService` — workspace-scoped: every filesystem operation.

Both layers are plain Python with no adapter concerns. The CLI and the future MCP
server are thin: parse, call, render. The mutating methods already return the
result shape that the history layer will fill, so recording history later changes
values inside results, never their fields.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from . import database, operations
from .config import (
    CONFIG_FILENAME,
    DEFAULT_CONFIG_TOML,
    STATE_DIR_NAME,
    Config,
    load_config,
    resolve_home,
)
from .errors import (
    CallerSurface,
    NoWorkspaceSelectedError,
    ValidationError,
    override_hint,
)
from .history.blobs import BlobStore
from .history.journal import OperationJournal
from .history.repository import diff_operation as read_diff
from .history.repository import get_operation as read_operation
from .history.repository import list_history as read_history
from .history.undo import undo_last as perform_undo_last
from .history.undo import undo_operation as perform_undo
from .models import (
    ContentSearchResult,
    DirectoryListing,
    ExistsResult,
    FileReadResult,
    MutationResult,
    OperationDiff,
    OperationRecord,
    PathInfo,
    PathSearchResult,
    ReadManyEntry,
    Workspace,
)
from .workspace import (
    STATE_DATABASE_NAME,
    WorkspaceHandle,
    WorkspaceRegistry,
    ensure_state_directory,
    handle_for,
)

TRANSIENT_ID_PREFIX = "ws-transient-"
TRANSIENT_ID_LENGTH = 16

WORKSPACE_SELECTION_HINT = override_hint(
    core="select a workspace explicitly or register a default",
    cli="pass --workspace or --root, or register a default with 'keeper workspace use'",
    mcp="start the server with --root PATH or register a default workspace",
)


def transient_workspace_id(root: str | Path) -> str:
    """A stable id for the transient workspace rooted at ``root``.

    Durable state is keyed by workspace id, so the id must survive a process
    restart: it is derived from the canonical absolute root rather than drawn
    at random.
    """

    canonical = str(Path(root).expanduser().resolve())
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"{TRANSIENT_ID_PREFIX}{digest[:TRANSIENT_ID_LENGTH]}"


def init_home(home: Path | None = None) -> Path:
    """Create the home directory, default configuration and registry database."""

    resolved_home = resolve_home(home)
    resolved_home.mkdir(parents=True, exist_ok=True)
    config_file = resolved_home / CONFIG_FILENAME
    if not config_file.exists():
        config_file.write_text(DEFAULT_CONFIG_TOML, encoding="utf-8")
    connection = database.open_registry_database(resolved_home / "metadata.sqlite3")
    connection.close()
    return resolved_home


class Filekeeper:
    """Home-scoped facade: configuration, workspaces, and service construction."""

    def __init__(
        self,
        *,
        home: str | Path | None = None,
        config: Config | None = None,
    ) -> None:
        self.home = resolve_home(home)
        self.config = config or load_config(self.home)
        self.registry = WorkspaceRegistry(self.home, self.config)

    # -- lifecycle ----------------------------------------------------------
    def init(self) -> Path:
        return init_home(self.home)

    # -- workspace registration --------------------------------------------
    def create_workspace(self, name: str, root: str | Path) -> Workspace:
        return self.registry.create(name, root)

    def list_workspaces(self) -> tuple[Workspace, ...]:
        return self.registry.list()

    def get_workspace(self, name_or_id: str) -> Workspace:
        return self.registry.get(name_or_id)

    def remove_workspace(self, name_or_id: str, *, delete_files: bool = False) -> Workspace:
        return self.registry.remove(name_or_id, delete_files=delete_files)

    def use_workspace(self, name_or_id: str) -> Workspace:
        return self.registry.set_default(name_or_id)

    def default_workspace(self) -> Workspace | None:
        return self.registry.get_default()

    # -- selection ----------------------------------------------------------
    def workspace(
        self,
        *,
        name: str | None = None,
        root: str | Path | None = None,
    ) -> FilekeeperService:
        """Select a workspace: explicit name, then explicit root, then the default.

        An explicit root that is not registered is used as a transient workspace:
        it is confined exactly like a registered one, but nothing is recorded. The
        selection is never guessed when several workspaces exist.
        """

        if name is not None and root is not None:
            raise ValidationError("select a workspace by name or by root, not both")

        if name is not None:
            workspace = self.registry.get(name)
            return FilekeeperService(handle_for(workspace, self.config, self.home), self)

        if root is not None:
            candidate = Path(root).expanduser().resolve()
            if not candidate.is_dir():
                raise ValidationError(
                    "workspace root must be an existing directory",
                    details={"root": str(root)},
                )
            registered = self.registry.find_by_root(candidate)
            if registered is not None:
                return FilekeeperService(handle_for(registered, self.config, self.home), self)
            transient = Workspace(
                id=transient_workspace_id(candidate),
                name=candidate.name or "workspace",
                root_path=str(candidate),
                created_at=database.utc_now(),
                updated_at=database.utc_now(),
            )
            return FilekeeperService(
                handle_for(transient, self.config, self.home), self, registered=False
            )

        default = self.registry.get_default()
        if default is None:
            workspaces = self.registry.list()
            if len(workspaces) == 1:
                # A single registered workspace is not an arbitrary choice; using it
                # keeps the common one-project setup frictionless without ever
                # guessing between several.
                return FilekeeperService(handle_for(workspaces[0], self.config, self.home), self)
            available = ", ".join(workspace.name for workspace in workspaces)
            raise NoWorkspaceSelectedError(
                "no workspace selected: "
                f"{WORKSPACE_SELECTION_HINT.sentence(CallerSurface.CORE)}"
                + (f"; known workspaces: {available}" if available else ""),
                details={"known_workspaces": available},
                override_hint=WORKSPACE_SELECTION_HINT,
            )
        return FilekeeperService(handle_for(default, self.config, self.home), self)


class FilekeeperService:
    """Workspace-scoped operations. All paths are workspace-relative or inside the root."""

    def __init__(
        self,
        handle: WorkspaceHandle,
        owner: Filekeeper,
        *,
        registered: bool = True,
    ) -> None:
        self.handle = handle
        self.owner = owner
        self.registered = registered
        self._journal: OperationJournal | None = None

    @property
    def name(self) -> str:
        return self.handle.name

    @property
    def root(self) -> Path:
        return self.handle.root

    @property
    def root_writable(self) -> bool:
        """Whether this process can write into the workspace root right now.

        A point-in-time answer, not a guarantee: it lets a caller tell a
        misconfigured root from a later permission failure before attempting a
        write. A root that is not a directory is never writable.
        """

        return self.root.is_dir() and os.access(self.root, os.W_OK | os.X_OK)

    @property
    def config(self) -> Config:
        return self.handle.config

    # -- state --------------------------------------------------------------
    def state_database_path(self) -> Path:
        """Create (if needed) and return this workspace's state database.

        The database is created on the first mutation that needs durable history,
        which is also when ``.filekeeper/`` is created and gitignored.
        """

        if self.registered and self.handle.workspace_id is not None:
            workspace = self.owner.registry.get(self.handle.name)
            return self.owner.registry.ensure_state_dir(workspace)
        return ensure_state_directory(self.handle.state_dir, root=self.root, home=self.owner.home)

    def blob_store(self) -> BlobStore:
        return BlobStore(self.state_database_path().parent / "blobs", fsync=self.config.fsync)

    def _journal_for(self, dry_run: bool) -> OperationJournal | None:
        """The journal a mutation will record into, or ``None`` for a dry run.

        A dry run has nothing to journal, so it touches no state at all: it does
        not create the state directory, open the database or write a blob. That
        makes a preview readable even where the real operation would be refused —
        which is the point of a preview, and why a preview that failed on state
        creation was not a preview.
        """

        if dry_run:
            return None
        return self.history

    # -- history ------------------------------------------------------------
    def _open_journal(self, *, create: bool) -> OperationJournal | None:
        """Return the journal, reconciling pending work once per service instance.

        Reading history never creates the state directory: a workspace that was
        never mutated simply has no history.
        """

        if self._journal is not None:
            return self._journal
        database_path = self.handle.state_dir / STATE_DATABASE_NAME
        if not create and not database_path.exists():
            return None
        if create:
            database_path = self.state_database_path()
        journal = OperationJournal(
            database_path,
            root=self.root,
            workspace_id=self.handle.workspace_id or "local",
            config=self.config,
        )
        journal.reconcile()
        self._journal = journal
        return journal

    @property
    def history(self) -> OperationJournal:
        journal = self._open_journal(create=True)
        assert journal is not None
        return journal

    def reconcile_history(self) -> tuple[str, ...]:
        """Resolve operations left pending by an interrupted run."""

        return self.history.reconcile()

    def get_operation(self, operation_id: str) -> OperationRecord:
        journal = self._open_journal(create=False)
        if journal is None:
            from .errors import OperationNotFoundError

            raise OperationNotFoundError(
                "no operation with that id in this workspace",
                details={"operation_id": operation_id},
            )
        return read_operation(journal, operation_id)

    def list_history(
        self,
        *,
        path: str | None = None,
        actor: str | None = None,
        task_id: str | None = None,
        limit: int = 50,
    ) -> tuple[OperationRecord, ...]:
        journal = self._open_journal(create=False)
        if journal is None:
            return ()
        return read_history(journal, path=path, actor=actor, task_id=task_id, limit=limit)

    def diff_operation(self, operation_id: str) -> OperationDiff:
        journal = self._open_journal(create=False)
        if journal is None:
            from .errors import OperationNotFoundError

            raise OperationNotFoundError(
                "no operation with that id in this workspace",
                details={"operation_id": operation_id},
            )
        return read_diff(journal, operation_id)

    def undo(
        self,
        operation_id: str,
        *,
        actor: str | None = None,
        task_id: str | None = None,
    ) -> MutationResult:
        return perform_undo(
            self.history,
            operation_id,
            workspace_name=self.name,
            actor=actor,
            task_id=task_id,
        )

    def undo_last(
        self,
        *,
        actor: str | None = None,
        task_id: str | None = None,
    ) -> MutationResult:
        return perform_undo_last(
            self.history, workspace_name=self.name, actor=actor, task_id=task_id
        )

    # -- read and inspection ------------------------------------------------
    def read_file(
        self,
        path: str,
        *,
        start_line: int | None = None,
        end_line: int | None = None,
        max_bytes: int | None = None,
        encoding: str | None = None,
    ) -> FileReadResult:
        return operations.read.read_file(
            self.handle,
            path,
            start_line=start_line,
            end_line=end_line,
            max_bytes=max_bytes,
            encoding=encoding,
        )

    def read_multiple_files(
        self,
        paths: list[str] | tuple[str, ...],
        *,
        max_bytes_per_file: int | None = None,
        start_line: int | None = None,
        end_line: int | None = None,
    ) -> tuple[ReadManyEntry, ...]:
        return operations.read.read_multiple_files(
            self.handle,
            paths,
            max_bytes_per_file=max_bytes_per_file,
            start_line=start_line,
            end_line=end_line,
        )

    def list_directory(
        self,
        path: str = ".",
        *,
        recursive: bool = False,
        glob: str | None = None,
        max_entries: int | None = None,
    ) -> DirectoryListing:
        return operations.read.list_directory(
            self.handle,
            path,
            recursive=recursive,
            glob=glob,
            max_entries=max_entries,
        )

    def get_file_info(self, path: str) -> PathInfo:
        return operations.read.get_file_info(self.handle, path)

    def get_file_hash(self, path: str) -> str:
        return operations.read.get_file_hash(self.handle, path)

    def file_exists(self, path: str) -> ExistsResult:
        return operations.read.file_exists(self.handle, path)

    def search_files(
        self, path: str, glob: str, *, max_results: int | None = None
    ) -> PathSearchResult:
        return operations.search.search_files(self.handle, path, glob, max_results=max_results)

    def search_content(
        self,
        path: str,
        query: str,
        *,
        regex: bool = False,
        glob: str | None = None,
        max_results: int | None = None,
        case_sensitive: bool = True,
    ) -> ContentSearchResult:
        return operations.search.search_content(
            self.handle,
            path,
            query,
            regex=regex,
            glob=glob,
            max_results=max_results,
            case_sensitive=case_sensitive,
        )

    # -- mutations ----------------------------------------------------------
    def create_file(
        self,
        path: str,
        content: str,
        *,
        expected_absent: bool = True,
        encoding: str = "utf-8",
        allow_sensitive_write: bool = False,
        dry_run: bool = False,
        actor: str | None = None,
        task_id: str | None = None,
    ) -> MutationResult:
        return operations.write.create_file(
            self.handle,
            path,
            content,
            expected_absent=expected_absent,
            encoding=encoding,
            allow_sensitive_write=allow_sensitive_write,
            dry_run=dry_run,
            journal=self._journal_for(dry_run),
            actor=actor,
            task_id=task_id,
        )

    def write_file(
        self,
        path: str,
        content: str,
        *,
        expected_hash: str | None = None,
        encoding: str | None = None,
        allow_sensitive_write: bool = False,
        dry_run: bool = False,
        actor: str | None = None,
        task_id: str | None = None,
    ) -> MutationResult:
        return operations.write.write_file(
            self.handle,
            path,
            content,
            expected_hash=expected_hash,
            encoding=encoding,
            allow_sensitive_write=allow_sensitive_write,
            dry_run=dry_run,
            journal=self._journal_for(dry_run),
            actor=actor,
            task_id=task_id,
        )

    def copy_file(
        self,
        source: str,
        destination: str,
        *,
        expected_destination_hash: str | None = None,
        allow_sensitive_write: bool = False,
        dry_run: bool = False,
        actor: str | None = None,
        task_id: str | None = None,
    ) -> MutationResult:
        return operations.write.copy_file(
            self.handle,
            source,
            destination,
            expected_destination_hash=expected_destination_hash,
            allow_sensitive_write=allow_sensitive_write,
            dry_run=dry_run,
            journal=self._journal_for(dry_run),
            actor=actor,
            task_id=task_id,
        )

    def move_file(
        self,
        source: str,
        destination: str,
        *,
        overwrite: bool = False,
        allow_sensitive_write: bool = False,
        dry_run: bool = False,
        actor: str | None = None,
        task_id: str | None = None,
    ) -> MutationResult:
        return operations.write.move_file(
            self.handle,
            source,
            destination,
            overwrite=overwrite,
            allow_sensitive_write=allow_sensitive_write,
            dry_run=dry_run,
            journal=self._journal_for(dry_run),
            actor=actor,
            task_id=task_id,
        )

    def delete_file(
        self,
        path: str,
        *,
        expected_hash: str | None = None,
        dry_run: bool = False,
        actor: str | None = None,
        task_id: str | None = None,
    ) -> MutationResult:
        return operations.write.delete_file(
            self.handle,
            path,
            expected_hash=expected_hash,
            dry_run=dry_run,
            journal=self._journal_for(dry_run),
            actor=actor,
            task_id=task_id,
        )

    # -- text editing -------------------------------------------------------
    def replace_text(
        self,
        path: str,
        old: str,
        new: str,
        *,
        match: str,
        expected_hash: str | None = None,
        expected_content: str | None = None,
        expected_match_count: int | None = None,
        allow_sensitive_write: bool = False,
        dry_run: bool = False,
        actor: str | None = None,
        task_id: str | None = None,
    ) -> MutationResult:
        return operations.edit.replace_text(
            self.handle,
            path,
            old,
            new,
            match=match,
            expected_hash=expected_hash,
            expected_content=expected_content,
            expected_match_count=expected_match_count,
            allow_sensitive_write=allow_sensitive_write,
            dry_run=dry_run,
            journal=self._journal_for(dry_run),
            actor=actor,
            task_id=task_id,
        )

    def replace_lines(
        self,
        path: str,
        start_line: int,
        end_line: int,
        replacement: str,
        *,
        expected_content: str | None = None,
        expected_hash: str | None = None,
        dry_run: bool = False,
        actor: str | None = None,
        task_id: str | None = None,
    ) -> MutationResult:
        return operations.edit.replace_lines(
            self.handle,
            path,
            start_line,
            end_line,
            replacement,
            expected_content=expected_content,
            expected_hash=expected_hash,
            dry_run=dry_run,
            journal=self._journal_for(dry_run),
            actor=actor,
            task_id=task_id,
        )

    def insert_lines(
        self,
        path: str,
        line: int | None,
        content: str,
        *,
        position: str = "after",
        expected_content: str | None = None,
        expected_hash: str | None = None,
        dry_run: bool = False,
        actor: str | None = None,
        task_id: str | None = None,
    ) -> MutationResult:
        return operations.edit.insert_lines(
            self.handle,
            path,
            line,
            content,
            position=position,
            expected_content=expected_content,
            expected_hash=expected_hash,
            dry_run=dry_run,
            journal=self._journal_for(dry_run),
            actor=actor,
            task_id=task_id,
        )

    def delete_lines(
        self,
        path: str,
        start_line: int,
        end_line: int,
        *,
        expected_content: str | None = None,
        expected_hash: str | None = None,
        dry_run: bool = False,
        actor: str | None = None,
        task_id: str | None = None,
    ) -> MutationResult:
        return operations.edit.delete_lines(
            self.handle,
            path,
            start_line,
            end_line,
            expected_content=expected_content,
            expected_hash=expected_hash,
            dry_run=dry_run,
            journal=self._journal_for(dry_run),
            actor=actor,
            task_id=task_id,
        )

    def apply_patch(
        self,
        patch_text: str,
        *,
        expected_hash: str | None = None,
        allow_sensitive_write: bool = False,
        dry_run: bool = False,
        actor: str | None = None,
        task_id: str | None = None,
    ) -> MutationResult:
        return operations.edit.apply_patch(
            self.handle,
            patch_text,
            expected_hash=expected_hash,
            allow_sensitive_write=allow_sensitive_write,
            dry_run=dry_run,
            journal=self._journal_for(dry_run),
            actor=actor,
            task_id=task_id,
        )

    def create_directory(
        self,
        path: str,
        *,
        parents: bool = False,
        dry_run: bool = False,
        actor: str | None = None,
        task_id: str | None = None,
    ) -> MutationResult:
        return operations.directory.create_directory(
            self.handle,
            path,
            parents=parents,
            dry_run=dry_run,
            journal=self._journal_for(dry_run),
            actor=actor,
            task_id=task_id,
        )

    def move_directory(
        self,
        source: str,
        destination: str,
        *,
        dry_run: bool = False,
        actor: str | None = None,
        task_id: str | None = None,
    ) -> MutationResult:
        return operations.directory.move_directory(
            self.handle,
            source,
            destination,
            dry_run=dry_run,
            journal=self._journal_for(dry_run),
            actor=actor,
            task_id=task_id,
        )

    def delete_directory(
        self,
        path: str,
        *,
        recursive: bool = False,
        confirm_recursive: bool = False,
        dry_run: bool = False,
        actor: str | None = None,
        task_id: str | None = None,
    ) -> MutationResult:
        return operations.directory.delete_directory(
            self.handle,
            path,
            recursive=recursive,
            confirm_recursive=confirm_recursive,
            dry_run=dry_run,
            journal=self._journal_for(dry_run),
            actor=actor,
            task_id=task_id,
        )


__all__ = [
    "STATE_DIR_NAME",
    "Filekeeper",
    "FilekeeperService",
    "init_home",
]
