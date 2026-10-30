"""Whole-file mutations.

Every write goes through :func:`atomic_write_bytes`, so each single-file
replacement is atomic: after a failure the file holds either its old bytes or
its new bytes, never a truncated mix.

Each mutation is also recorded in the durable history before the filesystem is
touched: blobs first, then the pending operation, then the atomic replacement,
then the commit mark. A failure before the replacement marks the operation
failed; a failure after it leaves the operation pending for startup
reconciliation, which never discards the recoverable copy.

Copy and move touch two paths. Each individual step is a complete atomic
replacement or rename, but the pair is **not** atomic across both paths: it is
recoverable through history, never all-or-nothing.
"""

from __future__ import annotations

import codecs
import os
import stat
from pathlib import Path

from ..errors import (
    AlreadyExistsError,
    ContentConflictError,
    FilekeeperError,
    LimitExceededError,
    NotAFileError,
    SensitivePathRefusedError,
    StorageError,
    SymlinkWriteRefusedError,
    ValidationError,
    storage_failure,
)
from ..history.journal import OperationJournal, PendingItem
from ..models import MutationResult, OperationItemResult
from ..paths import ResolvedPath, ensure_parent_directory, resolve_path
from ..safety.atomic import atomic_write_bytes, fsync_directory
from ..safety.hashing import format_digest, hash_bytes, hash_file, hashes_match, normalize_hash
from ..safety.sensitive import sensitive_reason
from ..safety.textdata import decode_bytes, encode_text, looks_binary
from ..safety.vcs import ignore_warning
from ..workspace import WorkspaceHandle

SECRET_FLAG_HINT = (
    "pass allow_sensitive_write=True in Core, or the CLI flag "
    "--i-accept-secret-write-risk, if you really mean to write credential material"
)


def _default_file_mode() -> int:
    current = os.umask(0)
    os.umask(current)
    return 0o666 & ~current


def _snapshot(
    path: Path, cap: int, *, display_path: str
) -> tuple[str | None, bytes | None, bool]:
    """Return ``(hash, bytes, exists)`` for a file, without reading oversize content.

    ``bytes`` is ``None`` for an oversize file: the hash is still recorded, the
    content is not. That is what makes the history cap visible instead of silent.
    """

    try:
        info = path.stat()
    except FileNotFoundError:
        return None, None, False
    if info.st_size > cap:
        return hash_file(path, display_path=display_path), None, True
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise storage_failure("read", display_path, exc) from exc
    return hash_bytes(data), data, True


def _check_size(handle: WorkspaceHandle, data: bytes, path: str) -> None:
    if len(data) > handle.config.max_write_bytes:
        raise LimitExceededError(
            "content exceeds the configured write size limit",
            limit=handle.config.max_write_bytes,
            actual=len(data),
            details={"path": path},
        )


def _guard_sensitive(
    handle: WorkspaceHandle, resolved: ResolvedPath, *, allow_sensitive_write: bool
) -> tuple[str, ...]:
    reason = sensitive_reason(resolved.relative)
    if reason is not None and not allow_sensitive_write:
        raise SensitivePathRefusedError(
            f"refusing to write {resolved.relative}: it looks like credential or key "
            f"material ({reason}); {SECRET_FLAG_HINT}",
            details={"path": resolved.relative, "reason": reason},
        )
    warnings: list[str] = []
    warning = ignore_warning(handle.root, resolved.absolute, resolved.relative)
    if warning is not None:
        warnings.append(warning)
    if reason is not None:
        warnings.append(f"sensitive-path refusal was overridden for {resolved.relative} ({reason})")
    return tuple(warnings)


def _target_mode(resolved: ResolvedPath) -> int | None:
    try:
        return stat.S_IMODE(resolved.absolute.stat().st_mode)
    except FileNotFoundError:
        return None


def _encoding_for_existing(target: Path, *, encoding: str | None) -> tuple[str, bool]:
    """Return ``(encoding, has_bom)`` for rewriting an existing file."""

    try:
        raw = target.read_bytes()
    except OSError:
        return (encoding or "utf-8", False)
    try:
        _, profile = decode_bytes(raw, encoding=encoding, path=str(target.name))
    except FilekeeperError:
        if encoding is None:
            raise
        return (encoding, encoding == "utf-8" and raw.startswith(codecs.BOM_UTF8))
    return (profile.encoding, profile.has_bom)


def _after_write(
    handle: WorkspaceHandle,
    resolved: ResolvedPath,
    data: bytes,
    *,
    operation_type: str,
    change: str,
    before_hash: str | None,
    before_bytes: bytes | None,
    before_exists: bool,
    before_mode: int | None,
    dry_run: bool,
    warnings: tuple[str, ...],
    past_verb: str,
    dry_verb: str,
    journal: OperationJournal | None,
    actor: str | None,
    task_id: str | None,
) -> MutationResult:
    after_hash = hash_bytes(data)
    operation_id: str | None = None
    skipped: tuple[str, ...] = ()
    undo_available = False

    if not dry_run:
        pending = PendingItem(
            relative_path=resolved.relative,
            item_type="file",
            before_exists=before_exists,
            after_exists=True,
            before_bytes=before_bytes,
            after_bytes=data,
            before_hash=before_hash,
            after_hash=after_hash,
            before_mode=before_mode,
            after_mode=_target_mode(resolved) or _default_file_mode(),
        )
        if journal is not None:
            begun = journal.begin(
                operation_type=operation_type,
                target_path=resolved.relative,
                summary=f"{past_verb} {resolved.relative}.",
                items=[pending],
                actor=actor,
                task_id=task_id,
                before_hash=before_hash,
                after_hash=after_hash,
            )
            operation_id = begun.operation_id
            skipped = begun.skipped
            warnings = (*warnings, *begun.warnings)
        try:
            after_hash = atomic_write_bytes(
                resolved.absolute,
                data,
                mode=_target_mode(resolved),
                fsync=handle.config.fsync,
                display_path=resolved.relative,
            )
        except FilekeeperError:
            if journal is not None and operation_id is not None:
                journal.abort(operation_id)
            raise
        if journal is not None and operation_id is not None:
            journal.commit(operation_id)
            undo_available = not skipped

    return MutationResult(
        workspace=handle.name,
        path=resolved.relative,
        changed=True,
        before_hash=before_hash,
        after_hash=after_hash,
        summary=(
            f"{dry_verb} {resolved.relative}." if dry_run else f"{past_verb} {resolved.relative}."
        ),
        operation_id=operation_id,
        undo_available=undo_available,
        dry_run=dry_run,
        warnings=warnings,
        items=(
            OperationItemResult(
                path=resolved.relative,
                change=change,  # type: ignore[arg-type]
                before_hash=before_hash,
                after_hash=after_hash,
                size_bytes=len(data),
            ),
        ),
        affected_count=1,
        history_skipped=skipped,
    )


def create_file(
    handle: WorkspaceHandle,
    path: str,
    content: str,
    *,
    expected_absent: bool = True,
    encoding: str = "utf-8",
    allow_sensitive_write: bool = False,
    dry_run: bool = False,
    journal: OperationJournal | None = None,
    actor: str | None = None,
    task_id: str | None = None,
) -> MutationResult:
    """Create a file. Refuses an existing target unless ``expected_absent`` is False."""

    resolved = resolve_path(handle.root, path, for_write=True, state_dir=handle.state_dir)
    if resolved.exists and resolved.absolute.is_dir():
        raise NotAFileError("path is a directory", details={"path": resolved.relative})
    if resolved.exists and expected_absent:
        raise AlreadyExistsError(
            "file already exists; pass expected_absent=False to overwrite",
            details={"path": resolved.relative},
        )
    warnings = _guard_sensitive(handle, resolved, allow_sensitive_write=allow_sensitive_write)
    ensure_parent_directory(resolved)

    before_hash, before_bytes, before_exists = _snapshot(
        resolved.absolute,
        handle.config.max_history_blob_bytes,
        display_path=resolved.relative,
    )
    before_mode = _target_mode(resolved) if before_exists else None
    data = encode_text(content, encoding=encoding, has_bom=False)
    _check_size(handle, data, resolved.relative)

    return _after_write(
        handle,
        resolved,
        data,
        operation_type="create_file",
        change="modified" if before_exists else "created",
        before_hash=before_hash,
        before_bytes=before_bytes,
        before_exists=before_exists,
        before_mode=before_mode,
        dry_run=dry_run,
        warnings=warnings,
        past_verb="Created" if not before_exists else "Overwrote",
        dry_verb="Would create" if not before_exists else "Would overwrite",
        journal=journal,
        actor=actor,
        task_id=task_id,
    )


def write_file(
    handle: WorkspaceHandle,
    path: str,
    content: str,
    *,
    expected_hash: str | None = None,
    encoding: str | None = None,
    allow_sensitive_write: bool = False,
    dry_run: bool = False,
    journal: OperationJournal | None = None,
    actor: str | None = None,
    task_id: str | None = None,
) -> MutationResult:
    """Replace a file's complete content, optionally guarded by its current hash.

    The existing file's encoding and byte-order mark are preserved unless an
    encoding is named explicitly. Line endings inside ``content`` are written
    exactly as given: they are never normalised.
    """

    resolved = resolve_path(handle.root, path, for_write=True, state_dir=handle.state_dir)
    if resolved.exists and resolved.absolute.is_dir():
        raise NotAFileError("path is a directory", details={"path": resolved.relative})
    warnings = _guard_sensitive(handle, resolved, allow_sensitive_write=allow_sensitive_write)
    ensure_parent_directory(resolved)

    before_hash, before_bytes, before_exists = _snapshot(
        resolved.absolute,
        handle.config.max_history_blob_bytes,
        display_path=resolved.relative,
    )
    before_mode = _target_mode(resolved) if before_exists else None
    if expected_hash is not None:
        wanted = normalize_hash(expected_hash)
        if before_hash is None:
            raise ContentConflictError(
                "file does not exist, so it cannot match the expected hash",
                details={"path": resolved.relative, "expected_hash": wanted},
            )
        if not hashes_match(wanted, before_hash):
            raise ContentConflictError(
                "file content changed since it was read; refusing to overwrite",
                details={
                    "path": resolved.relative,
                    "expected_hash": wanted,
                    "actual_hash": before_hash,
                },
            )

    if before_exists:
        effective_encoding, has_bom = _encoding_for_existing(resolved.absolute, encoding=encoding)
    else:
        effective_encoding, has_bom = encoding or "utf-8", False

    data = encode_text(content, encoding=effective_encoding, has_bom=has_bom)
    _check_size(handle, data, resolved.relative)

    return _after_write(
        handle,
        resolved,
        data,
        operation_type="write_file",
        change="modified" if before_exists else "created",
        before_hash=before_hash,
        before_bytes=before_bytes,
        before_exists=before_exists,
        before_mode=before_mode,
        dry_run=dry_run,
        warnings=warnings,
        past_verb="Wrote",
        dry_verb="Would write",
        journal=journal,
        actor=actor,
        task_id=task_id,
    )


def copy_file(
    handle: WorkspaceHandle,
    source: str,
    destination: str,
    *,
    expected_destination_hash: str | None = None,
    allow_sensitive_write: bool = False,
    dry_run: bool = False,
    journal: OperationJournal | None = None,
    actor: str | None = None,
    task_id: str | None = None,
) -> MutationResult:
    """Copy a file's bytes to a new path. Never overwrites an unguarded destination."""

    source_resolved = resolve_path(
        handle.root,
        source,
        must_exist=True,
        expect="file",
        state_dir=handle.state_dir,
    )
    if source_resolved.absolute.is_dir():
        raise NotAFileError("source is a directory", details={"path": source_resolved.relative})

    destination_resolved = resolve_path(
        handle.root, destination, for_write=True, state_dir=handle.state_dir
    )
    if destination_resolved.absolute == source_resolved.absolute:
        raise ValidationError("source and destination are the same path")

    destination_before: str | None = None
    destination_bytes: bytes | None = None
    destination_exists = False
    if destination_resolved.exists:
        if destination_resolved.absolute.is_dir():
            raise NotAFileError(
                "destination is a directory", details={"path": destination_resolved.relative}
            )
        destination_before, destination_bytes, destination_exists = _snapshot(
            destination_resolved.absolute,
            handle.config.max_history_blob_bytes,
            display_path=destination_resolved.relative,
        )
        if expected_destination_hash is None:
            raise AlreadyExistsError(
                "destination already exists; pass expected_destination_hash to "
                "replace it deliberately",
                details={"path": destination_resolved.relative},
            )
        wanted = normalize_hash(expected_destination_hash)
        if not hashes_match(wanted, destination_before or ""):
            raise ContentConflictError(
                "destination changed since it was read; refusing to overwrite",
                details={
                    "path": destination_resolved.relative,
                    "expected_hash": wanted,
                    "actual_hash": destination_before,
                },
            )

    warnings = _guard_sensitive(
        handle,
        destination_resolved,
        allow_sensitive_write=allow_sensitive_write,
    )
    ensure_parent_directory(destination_resolved)

    try:
        data = source_resolved.absolute.read_bytes()
    except OSError as exc:
        raise StorageError(
            f"cannot read source: {exc}", details={"path": source_resolved.relative}
        ) from exc
    _check_size(handle, data, destination_resolved.relative)

    result = _after_write(
        handle,
        destination_resolved,
        data,
        operation_type="copy_file",
        change="modified" if destination_exists else "created",
        before_hash=destination_before,
        before_bytes=destination_bytes,
        before_exists=destination_exists,
        before_mode=_target_mode(destination_resolved) if destination_exists else None,
        dry_run=dry_run,
        warnings=warnings,
        past_verb="Copied",
        dry_verb="Would copy",
        journal=journal,
        actor=actor,
        task_id=task_id,
    )
    item = OperationItemResult(
        path=source_resolved.relative,
        change="unchanged",
        before_hash=hash_bytes(data),
        after_hash=hash_bytes(data),
        size_bytes=len(data),
    )
    return MutationResult(
        workspace=result.workspace,
        path=result.path,
        changed=True,
        before_hash=result.before_hash,
        after_hash=result.after_hash,
        summary=(
            f"{'Would copy' if dry_run else 'Copied'} {source_resolved.relative} "
            f"to {destination_resolved.relative}."
        ),
        operation_id=result.operation_id,
        undo_available=result.undo_available,
        dry_run=dry_run,
        warnings=result.warnings,
        items=(*result.items, item),
        affected_count=1,
        history_skipped=result.history_skipped,
    )


def move_file(
    handle: WorkspaceHandle,
    source: str,
    destination: str,
    *,
    overwrite: bool = False,
    allow_sensitive_write: bool = False,
    dry_run: bool = False,
    journal: OperationJournal | None = None,
    actor: str | None = None,
    task_id: str | None = None,
) -> MutationResult:
    """Rename a file. Refuses an existing destination unless ``overwrite`` is set."""

    source_resolved = resolve_path(
        handle.root,
        source,
        must_exist=True,
        expect="file",
        state_dir=handle.state_dir,
    )
    if source_resolved.absolute.is_dir():
        raise ValidationError(
            "source is a directory; use the directory move operation",
            details={"path": source_resolved.relative},
        )
    source_hash = hash_file(source_resolved.absolute)
    source_mode = _target_mode(source_resolved)
    cap = handle.config.max_history_blob_bytes
    source_bytes: bytes | None
    try:
        source_bytes = (
            source_resolved.absolute.read_bytes()
            if source_resolved.absolute.stat().st_size <= cap
            else None
        )
    except OSError as exc:
        raise StorageError(
            f"cannot read source: {exc}", details={"path": source_resolved.relative}
        ) from exc

    destination_resolved = resolve_path(
        handle.root, destination, for_write=True, state_dir=handle.state_dir
    )
    if destination_resolved.absolute == source_resolved.absolute:
        raise ValidationError("source and destination are the same path")
    if destination_resolved.exists and destination_resolved.absolute.is_dir():
        raise NotAFileError(
            "destination is a directory", details={"path": destination_resolved.relative}
        )
    if destination_resolved.exists and not overwrite:
        raise AlreadyExistsError(
            "destination already exists; pass overwrite=True to replace it",
            details={"path": destination_resolved.relative},
        )
    warnings = _guard_sensitive(
        handle,
        destination_resolved,
        allow_sensitive_write=allow_sensitive_write,
    )
    ensure_parent_directory(destination_resolved)
    destination_before, destination_bytes, destination_exists = _snapshot(
        destination_resolved.absolute,
        cap,
        display_path=destination_resolved.relative,
    )

    operation_id: str | None = None
    skipped: tuple[str, ...] = ()
    if not dry_run:
        if journal is not None:
            begun = journal.begin(
                operation_type="move_file",
                target_path=destination_resolved.relative,
                summary=(
                    f"Moved {source_resolved.relative} to {destination_resolved.relative}."
                ),
                items=[
                    PendingItem(
                        relative_path=source_resolved.relative,
                        item_type="file",
                        before_exists=True,
                        after_exists=False,
                        before_bytes=source_bytes,
                        before_hash=source_hash,
                        before_mode=source_mode,
                    ),
                    PendingItem(
                        relative_path=destination_resolved.relative,
                        item_type="file",
                        before_exists=destination_exists,
                        after_exists=True,
                        before_bytes=destination_bytes,
                        before_hash=destination_before,
                        after_bytes=source_bytes,
                        after_hash=source_hash,
                        before_mode=_target_mode(destination_resolved)
                        if destination_exists
                        else None,
                        after_mode=source_mode,
                    ),
                ],
                actor=actor,
                task_id=task_id,
                before_hash=destination_before,
                after_hash=source_hash,
            )
            operation_id = begun.operation_id
            skipped = begun.skipped
            warnings = (*warnings, *begun.warnings)
        try:
            os.replace(source_resolved.absolute, destination_resolved.absolute)
        except OSError as exc:
            if journal is not None and operation_id is not None:
                journal.abort(operation_id)
            raise StorageError(
                f"cannot move file: {exc.strerror or type(exc).__name__}; moving across "
                "filesystems is not supported",
                details={
                    "source": source_resolved.relative,
                    "destination": destination_resolved.relative,
                },
            ) from exc
        fsync_directory(source_resolved.absolute.parent)
        fsync_directory(destination_resolved.absolute.parent)
        after_hash = hash_file(destination_resolved.absolute)
        if after_hash != source_hash:
            if journal is not None and operation_id is not None:
                journal.abort(operation_id)
            raise StorageError(
                "moved file content does not match its source",
                details={"path": destination_resolved.relative},
            )
        if journal is not None and operation_id is not None:
            journal.commit(operation_id)
    else:
        after_hash = source_hash

    return MutationResult(
        workspace=handle.name,
        path=destination_resolved.relative,
        changed=True,
        before_hash=source_hash,
        after_hash=after_hash,
        summary=(
            f"{'Would move' if dry_run else 'Moved'} {source_resolved.relative} "
            f"to {destination_resolved.relative}."
        ),
        operation_id=operation_id,
        undo_available=not dry_run and operation_id is not None and not skipped,
        dry_run=dry_run,
        warnings=warnings,
        items=(
            OperationItemResult(
                path=source_resolved.relative,
                change="moved",
                before_hash=source_hash,
                after_hash=None,
            ),
            OperationItemResult(
                path=destination_resolved.relative,
                change="created",
                before_hash=destination_before,
                after_hash=after_hash,
            ),
        ),
        affected_count=1,
        history_skipped=skipped,
    )


def delete_file(
    handle: WorkspaceHandle,
    path: str,
    *,
    expected_hash: str | None = None,
    dry_run: bool = False,
    journal: OperationJournal | None = None,
    actor: str | None = None,
    task_id: str | None = None,
) -> MutationResult:
    """Delete a file, optionally guarded by its current hash."""

    resolved = resolve_path(
        handle.root,
        path,
        must_exist=True,
        expect="file",
        state_dir=handle.state_dir,
    )
    if resolved.absolute.is_dir():
        raise NotAFileError(
            "path is a directory; use the directory delete operation",
            details={"path": resolved.relative},
        )
    before_hash, before_bytes, _ = _snapshot(
        resolved.absolute,
        handle.config.max_history_blob_bytes,
        display_path=resolved.relative,
    )
    if expected_hash is not None:
        wanted = normalize_hash(expected_hash)
        if not hashes_match(wanted, before_hash or ""):
            raise ContentConflictError(
                "file content changed since it was read; refusing to delete",
                details={
                    "path": resolved.relative,
                    "expected_hash": wanted,
                    "actual_hash": before_hash,
                },
            )

    operation_id: str | None = None
    skipped: tuple[str, ...] = ()
    if not dry_run:
        if journal is not None:
            begun = journal.begin(
                operation_type="delete_file",
                target_path=resolved.relative,
                summary=f"Deleted {resolved.relative}.",
                items=[
                    PendingItem(
                        relative_path=resolved.relative,
                        item_type="file",
                        before_exists=True,
                        after_exists=False,
                        before_bytes=before_bytes,
                        before_hash=before_hash,
                        before_mode=_target_mode(resolved),
                    )
                ],
                actor=actor,
                task_id=task_id,
                before_hash=before_hash,
            )
            operation_id = begun.operation_id
            skipped = begun.skipped
        try:
            resolved.absolute.unlink()
        except OSError as exc:
            if journal is not None and operation_id is not None:
                journal.abort(operation_id)
            raise StorageError(
                f"cannot delete file: {exc}", details={"path": resolved.relative}
            ) from exc
        fsync_directory(resolved.absolute.parent)
        if journal is not None and operation_id is not None:
            journal.commit(operation_id)

    return MutationResult(
        workspace=handle.name,
        path=resolved.relative,
        changed=True,
        before_hash=before_hash,
        after_hash=None,
        summary=f"{'Would delete' if dry_run else 'Deleted'} {resolved.relative}.",
        operation_id=operation_id,
        undo_available=not dry_run and operation_id is not None and not skipped,
        dry_run=dry_run,
        items=(
            OperationItemResult(
                path=resolved.relative,
                change="deleted",
                before_hash=before_hash,
                after_hash=None,
            ),
        ),
        affected_count=1,
        history_skipped=skipped,
    )


# Re-exported for adapters that need to format a hash without importing safety.
__all__ = [
    "SECRET_FLAG_HINT",
    "copy_file",
    "create_file",
    "delete_file",
    "format_digest",
    "move_file",
    "write_file",
]

# Keep static analyzers honest about intentionally public helpers.
_ = (looks_binary, SymlinkWriteRefusedError, stat.S_IMODE)
