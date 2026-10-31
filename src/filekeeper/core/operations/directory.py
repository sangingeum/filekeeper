"""Directory creation, moving and deletion.

Deleting a tree never happens without both ``recursive`` and
``confirm_recursive``: the confirmation is a separate, explicit argument rather
than an inference from "it had children". With history in place the deleted
bytes are recoverable through ``keeper history undo``, but the removal itself is
still a sequence of filesystem calls and is never atomic across the tree.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

from ..errors import (
    AlreadyExistsError,
    CallerSurface,
    FilekeeperError,
    PathNotFoundError,
    StorageError,
    ValidationError,
    override_hint,
    storage_failure,
)
from ..errors import (
    NotADirectoryError as FilekeeperNotADirectoryError,
)
from ..history.journal import OperationJournal, PendingItem
from ..models import MutationResult, OperationItemResult, as_dry_run
from ..paths import ensure_parent_directory, resolve_path
from ..safety.atomic import fsync_directory
from ..safety.hashing import hash_file
from ..workspace import WorkspaceHandle

DIRECTORY_PARENTS_HINT = override_hint(
    core="pass parents=True to create ancestors",
    cli="pass --parents to create ancestors",
    mcp="pass parents=true to create ancestors",
)

DIRECTORY_DELETE_HINT = override_hint(
    core="pass recursive=True and confirm_recursive=True to delete its contents",
    cli="pass --recursive --confirm-recursive to delete its contents",
    mcp="pass recursive=true and confirm_recursive=true to delete its contents",
)

DIRECTORY_CONFIRM_HINT = override_hint(
    core="pass confirm_recursive=True to acknowledge the tree deletion",
    cli="pass --confirm-recursive to acknowledge the tree deletion",
    mcp="pass confirm_recursive=true to acknowledge the tree deletion",
)


def _mode(path: Path) -> int | None:
    try:
        return stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return None


def _walk_tree(root: Path) -> list[Path]:
    """Every directory and file under ``root``, parents before children."""

    found: list[Path] = [root]
    for current, directories, files in os.walk(root, followlinks=False):
        base = Path(current)
        for name in sorted(directories):
            found.append(base / name)
        for name in sorted(files):
            found.append(base / name)
    return found


def _file_bytes(path: Path, cap: int, *, display_path: str) -> tuple[str | None, bytes | None]:
    try:
        size = path.stat().st_size
    except OSError:
        return None, None
    if size > cap:
        try:
            return hash_file(path, display_path=display_path), None
        except (OSError, FilekeeperError):
            return None, None
    try:
        data = path.read_bytes()
    except OSError:
        return None, None
    from ..safety.hashing import hash_bytes

    return hash_bytes(data), data


def _remove_file(path: Path) -> None:
    """Remove one file. A separate function so failure injection has a seam."""

    path.unlink()


def create_directory(
    handle: WorkspaceHandle,
    path: str,
    *,
    parents: bool = False,
    dry_run: bool = False,
    journal: OperationJournal | None = None,
    actor: str | None = None,
    task_id: str | None = None,
) -> MutationResult:
    """Create a directory. ``parents`` also creates missing ancestors."""

    resolved = resolve_path(handle.root, path, for_write=True, state_dir=handle.state_dir)
    if resolved.exists:
        raise AlreadyExistsError("path already exists", details={"path": resolved.relative})
    target = resolved.absolute
    if not parents and not target.parent.is_dir():
        raise ValidationError(
            "parent directory does not exist; "
            f"{DIRECTORY_PARENTS_HINT.sentence(CallerSurface.CORE)}",
            details={"path": resolved.relative},
            override_hint=DIRECTORY_PARENTS_HINT,
        )

    created: list[Path] = [target]
    if parents:
        missing: list[Path] = []
        candidate = target.parent
        while candidate != handle.root and not candidate.exists():
            missing.append(candidate)
            candidate = candidate.parent
        created = [*reversed(missing), target]

    operation_id: str | None = None
    if not dry_run:
        if journal is not None:
            items = [
                PendingItem(
                    relative_path=directory.relative_to(handle.root).as_posix(),
                    item_type="directory",
                    before_exists=False,
                    after_exists=True,
                    after_mode=0o777 & ~_umask(),
                )
                for directory in created
            ]
            begun = journal.begin(
                operation_type="create_directory",
                target_path=resolved.relative,
                summary=f"Created directory {resolved.relative}.",
                items=items,
                actor=actor,
                task_id=task_id,
            )
            operation_id = begun.operation_id
        try:
            if parents:
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.mkdir()
        except FileExistsError as exc:
            raise AlreadyExistsError(
                "path already exists", details={"path": resolved.relative}
            ) from exc
        except OSError as exc:
            if journal is not None and operation_id is not None:
                journal.abort(operation_id)
            raise storage_failure("create directory", resolved.relative, exc) from exc
        fsync_directory(target.parent)
        if parents:
            fsync_directory(target)
        if journal is not None and operation_id is not None:
            journal.commit(operation_id)

    result = MutationResult(
        workspace=handle.name,
        path=resolved.reported,
        changed=True,
        before_hash=None,
        after_hash=None,
        summary=f"{'Would create' if dry_run else 'Created'} directory {resolved.relative}.",
        operation_id=operation_id,
        undo_available=not dry_run and operation_id is not None,
        dry_run=dry_run,
        items=(OperationItemResult(path=resolved.reported, change="created"),),
        affected_count=1,
    )
    if dry_run:
        return as_dry_run(result, would_change=True)
    return result


def _umask() -> int:
    current = os.umask(0)
    os.umask(current)
    return current


def move_directory(
    handle: WorkspaceHandle,
    source: str,
    destination: str,
    *,
    dry_run: bool = False,
    journal: OperationJournal | None = None,
    actor: str | None = None,
    task_id: str | None = None,
) -> MutationResult:
    """Rename a directory inside the workspace. Cross-device moves are refused."""

    source_resolved = resolve_path(
        handle.root,
        source,
        must_exist=True,
        expect="directory",
        for_write=True,
        state_dir=handle.state_dir,
    )
    if not source_resolved.absolute.is_dir():
        raise FilekeeperNotADirectoryError(
            "source is not a directory", details={"path": source_resolved.relative}
        )
    if source_resolved.relative == "":
        raise ValidationError("refusing to move the workspace root")

    destination_resolved = resolve_path(
        handle.root, destination, for_write=True, state_dir=handle.state_dir
    )
    if destination_resolved.exists:
        raise AlreadyExistsError(
            "destination already exists", details={"path": destination_resolved.relative}
        )
    ensure_parent_directory(destination_resolved)

    source_path = source_resolved.absolute
    destination_path = destination_resolved.absolute
    if destination_path == source_path or source_path in destination_path.parents:
        raise ValidationError(
            "cannot move a directory into itself",
            details={
                "source": source_resolved.relative,
                "destination": destination_resolved.relative,
            },
        )

    operation_id: str | None = None
    skipped: list[str] = []
    if not dry_run and journal is not None:
        cap = handle.config.max_history_blob_bytes
        items: list[PendingItem] = []
        for entry in _walk_tree(source_path):
            relative = entry.relative_to(handle.root).as_posix()
            suffix = entry.relative_to(source_path).as_posix()
            moved_relative = (
                destination_resolved.relative
                if suffix == "."
                else f"{destination_resolved.relative}/{suffix}"
            )
            if entry.is_dir():
                items.append(
                    PendingItem(
                        relative_path=relative,
                        item_type="directory",
                        before_exists=True,
                        after_exists=False,
                        before_mode=_mode(entry),
                    )
                )
                items.append(
                    PendingItem(
                        relative_path=moved_relative,
                        item_type="directory",
                        before_exists=False,
                        after_exists=True,
                        after_mode=_mode(entry),
                    )
                )
            else:
                digest, data = _file_bytes(entry, cap, display_path=relative)
                if data is None:
                    skipped.append(relative)
                items.append(
                    PendingItem(
                        relative_path=relative,
                        item_type="file",
                        before_exists=True,
                        after_exists=False,
                        before_bytes=data,
                        before_hash=digest,
                        before_mode=_mode(entry),
                    )
                )
                items.append(
                    PendingItem(
                        relative_path=moved_relative,
                        item_type="file",
                        before_exists=False,
                        after_exists=True,
                        after_bytes=data,
                        after_hash=digest,
                        after_mode=_mode(entry),
                    )
                )
        begun = journal.begin(
            operation_type="move_directory",
            target_path=destination_resolved.relative,
            summary=(
                f"Moved directory {source_resolved.relative} to {destination_resolved.relative}."
            ),
            items=items,
            actor=actor,
            task_id=task_id,
        )
        operation_id = begun.operation_id
        skipped = list(begun.skipped)

    try:
        if not dry_run:
            os.replace(source_path, destination_path)
            fsync_directory(source_path.parent)
            fsync_directory(destination_path.parent)
            if journal is not None and operation_id is not None:
                journal.commit(operation_id)
    except OSError as exc:
        if journal is not None and operation_id is not None:
            journal.abort(operation_id)
        raise StorageError(
            f"cannot move directory: {exc.strerror or type(exc).__name__}; moving across "
            "filesystems is not supported",
            details={
                "source": source_resolved.relative,
                "destination": destination_resolved.relative,
            },
        ) from exc

    result = MutationResult(
        workspace=handle.name,
        path=destination_resolved.reported,
        changed=True,
        before_hash=None,
        after_hash=None,
        summary=(
            f"{'Would move' if dry_run else 'Moved'} directory "
            f"{source_resolved.relative} to {destination_resolved.relative}."
        ),
        operation_id=operation_id,
        undo_available=not dry_run and operation_id is not None and not skipped,
        dry_run=dry_run,
        items=(
            OperationItemResult(path=source_resolved.reported, change="moved"),
            OperationItemResult(path=destination_resolved.reported, change="created"),
        ),
        affected_count=1,
        history_skipped=tuple(sorted(set(skipped))),
    )
    if dry_run:
        return as_dry_run(result, would_change=True)
    return result


def _count_entries(root: Path) -> int:
    total = 0
    for _, directories, files in os.walk(root, followlinks=False):
        total += len(directories) + len(files)
    return total


def delete_directory(
    handle: WorkspaceHandle,
    path: str,
    *,
    recursive: bool = False,
    confirm_recursive: bool = False,
    dry_run: bool = False,
    journal: OperationJournal | None = None,
    actor: str | None = None,
    task_id: str | None = None,
) -> MutationResult:
    """Delete an empty directory, or a whole tree with two explicit flags."""

    resolved = resolve_path(
        handle.root,
        path,
        must_exist=True,
        expect="directory",
        for_write=True,
        state_dir=handle.state_dir,
    )
    target = resolved.absolute
    if resolved.relative == "" or target == handle.root:
        raise ValidationError("refusing to delete the workspace root")

    if not target.is_dir():
        raise PathNotFoundError("directory no longer exists", details={"path": resolved.relative})

    entries = _count_entries(target)
    if entries > 0 and not recursive:
        raise ValidationError(
            f"directory is not empty; {DIRECTORY_DELETE_HINT.sentence(CallerSurface.CORE)}",
            details={"path": resolved.relative, "entries": entries},
            override_hint=DIRECTORY_DELETE_HINT,
        )
    if entries > 0 and not confirm_recursive:
        raise ValidationError(
            "recursive deletion requires explicit confirmation: "
            f"{DIRECTORY_CONFIRM_HINT.sentence(CallerSurface.CORE)}",
            details={"path": resolved.relative, "entries": entries},
            override_hint=DIRECTORY_CONFIRM_HINT,
        )

    operation_id: str | None = None
    warnings: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()
    if not dry_run and journal is not None:
        cap = handle.config.max_history_blob_bytes
        items: list[PendingItem] = []
        for entry in _walk_tree(target):
            relative = entry.relative_to(handle.root).as_posix()
            if entry.is_dir():
                items.append(
                    PendingItem(
                        relative_path=relative,
                        item_type="directory",
                        before_exists=True,
                        after_exists=False,
                        before_mode=_mode(entry),
                    )
                )
            else:
                digest, data = _file_bytes(entry, cap, display_path=relative)
                items.append(
                    PendingItem(
                        relative_path=relative,
                        item_type="file",
                        before_exists=True,
                        after_exists=False,
                        before_bytes=data,
                        before_hash=digest,
                        before_mode=_mode(entry),
                    )
                )
        begun = journal.begin(
            operation_type="delete_directory",
            target_path=resolved.relative,
            summary=f"Deleted directory {resolved.relative} ({entries} entries).",
            items=items,
            actor=actor,
            task_id=task_id,
        )
        operation_id = begun.operation_id
        skipped = begun.skipped
        warnings = begun.warnings

    if not dry_run:
        try:
            if recursive:
                # Files first, deepest first, so a failure part-way leaves a
                # truthful partial state that startup reconciliation can resolve.
                for entry in sorted(
                    _walk_tree(target),
                    key=lambda item: len(item.parts),
                    reverse=True,
                ):
                    if entry.is_dir():
                        entry.rmdir()
                    else:
                        _remove_file(entry)
            else:
                target.rmdir()
        except OSError as exc:
            # Leave the operation pending: reconciliation inspects the actual
            # state and reports the partial application honestly.
            raise storage_failure("delete directory", resolved.relative, exc) from exc
        fsync_directory(target.parent)
        if journal is not None and operation_id is not None:
            journal.commit(operation_id)

    result = MutationResult(
        workspace=handle.name,
        path=resolved.reported,
        changed=True,
        before_hash=None,
        after_hash=None,
        summary=(
            f"{'Would delete' if dry_run else 'Deleted'} directory "
            f"{resolved.relative} ({entries} entries)."
        ),
        operation_id=operation_id,
        undo_available=not dry_run and operation_id is not None and not skipped,
        dry_run=dry_run,
        items=(OperationItemResult(path=resolved.reported, change="deleted"),),
        affected_count=entries,
        warnings=(
            *warnings,
            "A directory tree is deleted as a sequence of filesystem calls: the "
            "deletion is recoverable through history, but it is never atomic "
            "across the tree.",
        )
        if entries > 0
        else warnings,
        history_skipped=skipped,
    )
    if dry_run:
        return as_dry_run(result, would_change=True)
    return result
