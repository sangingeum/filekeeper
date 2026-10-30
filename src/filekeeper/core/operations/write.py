"""Whole-file mutations.

Every write goes through :func:`atomic_write_bytes`, so each single-file mutation
in this release is already crash-safe: after a failure the file holds either its
old bytes or its new bytes, never a truncated mix. History recording arrives in a
later release; the result objects already carry the fields it will fill
(``operation_id`` is ``None``, ``undo_available`` is ``False``).

Copy and move touch two paths. They are recoverable in the sense that each
individual step is a complete atomic replacement or rename, but they are **not**
atomic across the pair, and nothing in this release can roll them back.
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
)
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
    change: str,
    before_hash: str | None,
    dry_run: bool,
    warnings: tuple[str, ...],
    past_verb: str,
    dry_verb: str,
) -> MutationResult:
    after_hash = hash_bytes(data)
    if not dry_run:
        mode = _target_mode(resolved)
        after_hash = atomic_write_bytes(
            resolved.absolute, data, mode=mode, fsync=handle.config.fsync
        )
    return MutationResult(
        workspace=handle.name,
        path=resolved.relative,
        changed=True,
        before_hash=before_hash,
        after_hash=after_hash,
        summary=(
            f"{dry_verb} {resolved.relative}." if dry_run else f"{past_verb} {resolved.relative}."
        ),
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

    before_hash = hash_file(resolved.absolute) if resolved.exists else None
    data = encode_text(content, encoding=encoding, has_bom=False)
    _check_size(handle, data, resolved.relative)

    return _after_write(
        handle,
        resolved,
        data,
        change="modified" if resolved.exists else "created",
        before_hash=before_hash,
        dry_run=dry_run,
        warnings=warnings,
        past_verb="Created" if not resolved.exists else "Overwrote",
        dry_verb="Would create" if not resolved.exists else "Would overwrite",
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

    before_hash = hash_file(resolved.absolute) if resolved.exists else None
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

    if resolved.exists:
        effective_encoding, has_bom = _encoding_for_existing(resolved.absolute, encoding=encoding)
    else:
        effective_encoding, has_bom = encoding or "utf-8", False

    data = encode_text(content, encoding=effective_encoding, has_bom=has_bom)
    _check_size(handle, data, resolved.relative)

    return _after_write(
        handle,
        resolved,
        data,
        change="modified" if resolved.exists else "created",
        before_hash=before_hash,
        dry_run=dry_run,
        warnings=warnings,
        past_verb="Wrote",
        dry_verb="Would write",
    )


def copy_file(
    handle: WorkspaceHandle,
    source: str,
    destination: str,
    *,
    expected_destination_hash: str | None = None,
    allow_sensitive_write: bool = False,
    dry_run: bool = False,
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
    if destination_resolved.exists:
        if destination_resolved.absolute.is_dir():
            raise NotAFileError(
                "destination is a directory", details={"path": destination_resolved.relative}
            )
        destination_before = hash_file(destination_resolved.absolute)
        if expected_destination_hash is None:
            raise AlreadyExistsError(
                "destination already exists; pass expected_destination_hash to "
                "replace it deliberately",
                details={"path": destination_resolved.relative},
            )
        wanted = normalize_hash(expected_destination_hash)
        if not hashes_match(wanted, destination_before):
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
        change="modified" if destination_resolved.exists else "created",
        before_hash=destination_before,
        dry_run=dry_run,
        warnings=warnings,
        past_verb="Copied",
        dry_verb="Would copy",
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
        dry_run=dry_run,
        warnings=result.warnings,
        items=(*result.items, item),
        affected_count=1,
    )


def move_file(
    handle: WorkspaceHandle,
    source: str,
    destination: str,
    *,
    overwrite: bool = False,
    allow_sensitive_write: bool = False,
    dry_run: bool = False,
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
    destination_before = (
        hash_file(destination_resolved.absolute) if destination_resolved.exists else None
    )

    if not dry_run:
        try:
            os.replace(source_resolved.absolute, destination_resolved.absolute)
        except OSError as exc:
            raise StorageError(
                f"cannot move file: {exc}; moving across filesystems is not supported",
                details={
                    "source": source_resolved.relative,
                    "destination": destination_resolved.relative,
                },
            ) from exc
        fsync_directory(source_resolved.absolute.parent)
        fsync_directory(destination_resolved.absolute.parent)
        after_hash = hash_file(destination_resolved.absolute)
        if after_hash != source_hash:
            raise StorageError(
                "moved file content does not match its source",
                details={"path": destination_resolved.relative},
            )
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
    )


def delete_file(
    handle: WorkspaceHandle,
    path: str,
    *,
    expected_hash: str | None = None,
    dry_run: bool = False,
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
    before_hash = hash_file(resolved.absolute)
    if expected_hash is not None:
        wanted = normalize_hash(expected_hash)
        if not hashes_match(wanted, before_hash):
            raise ContentConflictError(
                "file content changed since it was read; refusing to delete",
                details={
                    "path": resolved.relative,
                    "expected_hash": wanted,
                    "actual_hash": before_hash,
                },
            )

    if not dry_run:
        try:
            resolved.absolute.unlink()
        except OSError as exc:
            raise StorageError(
                f"cannot delete file: {exc}", details={"path": resolved.relative}
            ) from exc
        fsync_directory(resolved.absolute.parent)

    return MutationResult(
        workspace=handle.name,
        path=resolved.relative,
        changed=True,
        before_hash=before_hash,
        after_hash=None,
        summary=f"{'Would delete' if dry_run else 'Deleted'} {resolved.relative}.",
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
