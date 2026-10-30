"""Directory creation, moving and deletion.

Deleting a tree never happens without both ``recursive`` and
``confirm_recursive``: a recursive delete destroys content that this release
cannot restore (history lands later), so the confirmation is a separate,
explicit argument rather than an inference from "it had children".
"""

from __future__ import annotations

import os
import shutil

from ..errors import (
    AlreadyExistsError,
    NotADirectoryError as FilekeeperNotADirectoryError,
    PathNotFoundError,
    StorageError,
    ValidationError,
)
from ..models import MutationResult, OperationItemResult
from ..paths import ensure_parent_directory, resolve_path
from ..safety.atomic import fsync_directory
from ..workspace import WorkspaceHandle


def _state_dir(handle: WorkspaceHandle) -> str:
    return str(handle.state_dir)


def create_directory(
    handle: WorkspaceHandle,
    path: str,
    *,
    parents: bool = False,
    dry_run: bool = False,
) -> MutationResult:
    """Create a directory. ``parents`` also creates missing ancestors."""

    resolved = resolve_path(
        handle.root, path, for_write=True, state_dir=handle.state_dir
    )
    if resolved.exists:
        raise AlreadyExistsError(
            "path already exists", details={"path": resolved.relative}
        )
    target = resolved.absolute
    if not parents and not target.parent.is_dir():
        raise ValidationError(
            "parent directory does not exist; pass parents=True to create ancestors",
            details={"path": resolved.relative},
        )

    if not dry_run:
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
            raise StorageError(
                f"cannot create directory: {exc}", details={"path": resolved.relative}
            ) from exc
        fsync_directory(target.parent)
        if parents:
            fsync_directory(target)

    return MutationResult(
        workspace=handle.name,
        path=resolved.relative,
        changed=True,
        before_hash=None,
        after_hash=None,
        summary=f"{'Would create' if dry_run else 'Created'} directory {resolved.relative}.",
        dry_run=dry_run,
        items=(OperationItemResult(path=resolved.relative, change="created"),),
        affected_count=1,
    )


def move_directory(
    handle: WorkspaceHandle,
    source: str,
    destination: str,
    *,
    dry_run: bool = False,
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
            details={"source": source_resolved.relative, "destination": destination_resolved.relative},
        )

    if not dry_run:
        try:
            os.replace(source_path, destination_path)
        except OSError as exc:
            raise StorageError(
                f"cannot move directory: {exc}; moving across filesystems is not supported",
                details={"source": source_resolved.relative, "destination": destination_resolved.relative},
            ) from exc
        fsync_directory(source_path.parent)
        fsync_directory(destination_path.parent)

    return MutationResult(
        workspace=handle.name,
        path=destination_resolved.relative,
        changed=True,
        before_hash=None,
        after_hash=None,
        summary=(
            f"{'Would move' if dry_run else 'Moved'} directory "
            f"{source_resolved.relative} to {destination_resolved.relative}."
        ),
        dry_run=dry_run,
        items=(
            OperationItemResult(path=source_resolved.relative, change="moved"),
            OperationItemResult(path=destination_resolved.relative, change="created"),
        ),
        affected_count=1,
    )


def _count_entries(root) -> int:
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
            "directory is not empty; pass recursive=True to delete its contents",
            details={"path": resolved.relative, "entries": entries},
        )
    if entries > 0 and not confirm_recursive:
        raise ValidationError(
            "recursive deletion requires explicit confirmation (confirm_recursive=True); "
            "this release cannot undo a deleted tree",
            details={"path": resolved.relative, "entries": entries},
        )

    if not dry_run:
        try:
            if recursive:
                shutil.rmtree(target)
            else:
                target.rmdir()
        except OSError as exc:
            raise StorageError(
                f"cannot delete directory: {exc}", details={"path": resolved.relative}
            ) from exc
        fsync_directory(target.parent)

    return MutationResult(
        workspace=handle.name,
        path=resolved.relative,
        changed=True,
        before_hash=None,
        after_hash=None,
        summary=(
            f"{'Would delete' if dry_run else 'Deleted'} directory "
            f"{resolved.relative} ({entries} entries)."
        ),
        dry_run=dry_run,
        items=(OperationItemResult(path=resolved.relative, change="deleted"),),
        affected_count=entries,
        warnings=(
            "A directory tree is deleted as a sequence of filesystem calls: the "
            "operation is recoverable only once history lands, and is never atomic "
            "across the tree.",
        )
        if entries > 0
        else (),
    )
