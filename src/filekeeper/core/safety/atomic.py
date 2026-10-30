"""The atomic single-file write primitive.

The guarantee: on return, the destination holds either its complete previous
content or the complete new content. Never a prefix, never a mixture. That holds
across process death because the visible change is a single ``rename`` within one
directory, and it holds across machine crash when ``fsync`` is enabled because
both the temporary file and the parent directory are flushed first.

Failure before the rename leaves the destination untouched and removes the
temporary file; the caller can retry.
"""

from __future__ import annotations

import contextlib
import os
import stat
import tempfile
from pathlib import Path

from ..errors import StorageError
from .hashing import hash_bytes, hash_file

_TEMP_PREFIX = ".keeper-tmp-"


def _default_file_mode() -> int:
    """The mode a plain ``open()`` would produce for a new file under this umask."""

    current = os.umask(0)
    os.umask(current)
    return 0o666 & ~current


def _fsync_directory(directory: Path) -> None:
    """Flush a directory entry. Unsupported on some platforms; that is not an error."""

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(directory, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:  # pragma: no cover - e.g. filesystems that reject dir fsync
        pass
    finally:
        os.close(descriptor)


def atomic_write_bytes(
    destination: Path,
    data: bytes,
    *,
    mode: int | None = None,
    fsync: bool = True,
    display_path: str | None = None,
) -> str:
    """Replace ``destination`` with ``data`` atomically; return the new content hash.

    ``mode`` sets the resulting permission bits. When omitted, an existing
    destination keeps its mode and a new file gets the umask-derived default.
    ``display_path`` is the caller-facing path used in error details; the
    resolved host path and the temporary name are never reported. The final
    content is read back and hashed; a mismatch is a storage error.
    """

    details = {"path": display_path} if display_path is not None else {}

    if destination.is_symlink():
        raise StorageError(
            "refusing to replace a symlink atomically; the link itself would be "
            "replaced, not its target",
            details=details,
        )
    parent = destination.parent
    if not parent.is_dir():
        raise StorageError("destination directory does not exist", details=details)

    existing_mode: int | None = None
    try:
        existing_mode = stat.S_IMODE(destination.stat().st_mode)
    except FileNotFoundError:
        existing_mode = None

    temporary_path: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(prefix=_TEMP_PREFIX, dir=parent)
        temporary_path = Path(temporary_name)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            if fsync:
                os.fsync(handle.fileno())
        effective_mode = mode if mode is not None else existing_mode
        if effective_mode is None:
            # tempfile.mkstemp creates 0600; a new file should follow the umask.
            effective_mode = _default_file_mode()
        os.chmod(temporary_path, effective_mode)
        os.replace(temporary_path, destination)
    except OSError as exc:
        # ``str(exc)`` carries the temporary path and errno context; only the
        # standard reason phrase is safe to show a caller.
        raise StorageError(
            f"atomic write failed: {exc.strerror or type(exc).__name__}",
            details=details,
        ) from exc
    finally:
        if temporary_path is not None:
            with contextlib.suppress(FileNotFoundError):
                temporary_path.unlink()

    if fsync:
        _fsync_directory(parent)

    expected = hash_bytes(data)
    actual = hash_file(destination)
    if expected != actual:
        raise StorageError(
            "content verification failed after atomic write",
            details={**details, "expected_hash": expected, "actual_hash": actual},
        )
    return actual


def fsync_directory(directory: Path) -> None:
    """Public wrapper used after rename/unlink by non-atomic operations."""

    _fsync_directory(directory)
