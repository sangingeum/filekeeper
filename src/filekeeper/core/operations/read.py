"""Reading files and inspecting metadata.

Line numbers are 1-based and inclusive, everywhere: Core, CLI, docs and tests.
A bounded read never silently returns a prefix of the requested range: either the
whole range is returned or the result says it was truncated and where to resume.
"""

from __future__ import annotations

import fnmatch
import os
from datetime import UTC, datetime
from pathlib import Path

from ..errors import (
    FilekeeperError,
    NotAFileError,
    ValidationError,
    storage_failure,
)
from ..models import (
    DirectoryEntry,
    DirectoryListing,
    EntryKind,
    ErrorDetail,
    ExistsResult,
    FileReadResult,
    PathInfo,
    ReadManyEntry,
)
from ..paths import reported_path, resolve_path
from ..safety.hashing import hash_file
from ..safety.textdata import (
    BINARY_SAMPLE_SIZE,
    TextProfile,
    decode_bytes,
    looks_binary,
)
from ..workspace import WorkspaceHandle

_INFO_SAMPLE_SIZE = 65536


def _relative(handle: WorkspaceHandle, path: Path) -> str:
    return path.relative_to(handle.root).as_posix()


def _read_window(
    handle: WorkspaceHandle,
    path: str,
    *,
    max_bytes: int | None,
    encoding: str | None,
) -> tuple[str, int, bool, TextProfile]:
    resolved = resolve_path(
        handle.root,
        path,
        must_exist=True,
        expect="file",
        state_dir=handle.state_dir,
    )
    if resolved.absolute.is_dir():
        raise NotAFileError("path is a directory, not a file", details={"path": path})

    limit = handle.config.max_read_bytes if max_bytes is None else max_bytes
    if limit <= 0:
        raise ValidationError("max_bytes must be positive", details={"max_bytes": limit})
    limit = min(limit, handle.config.max_read_bytes)

    try:
        size = resolved.absolute.stat().st_size
        with resolved.absolute.open("rb") as file_handle:
            data = file_handle.read(limit)
    except OSError as exc:
        raise storage_failure("read", resolved.relative, exc) from exc
    truncated = size > len(data)

    text, profile = decode_bytes(data, encoding=encoding, partial=truncated, path=resolved.relative)
    return text, size, truncated, profile


def read_file(
    handle: WorkspaceHandle,
    path: str,
    *,
    start_line: int | None = None,
    end_line: int | None = None,
    max_bytes: int | None = None,
    encoding: str | None = None,
) -> FileReadResult:
    """Read the whole file or an inclusive 1-based line range."""

    if start_line is not None and start_line < 1:
        raise ValidationError(
            "start_line is 1-based; line numbers below 1 do not exist",
            details={"start_line": start_line},
        )
    if end_line is not None and start_line is not None and end_line < start_line:
        raise ValidationError(
            "end_line must be greater than or equal to start_line",
            details={"start_line": start_line, "end_line": end_line},
        )
    if end_line is not None and start_line is None:
        raise ValidationError("end_line requires start_line")

    text, size, truncated, profile = _read_window(
        handle, path, max_bytes=max_bytes, encoding=encoding
    )
    lines = text.splitlines(keepends=True)
    loaded_line_count = len(lines)
    total_lines: int | None = None if truncated else loaded_line_count

    first = start_line if start_line is not None else 1
    if first > loaded_line_count and loaded_line_count > 0:
        raise ValidationError(
            "start_line is beyond the end of the file",
            details={"start_line": first, "total_lines": total_lines},
        )

    if end_line is None:
        last = loaded_line_count
    else:
        if total_lines is not None and end_line > total_lines:
            raise ValidationError(
                "end_line is beyond the end of the file",
                details={"end_line": end_line, "total_lines": total_lines},
            )
        last = min(end_line, loaded_line_count)

    selected = lines[first - 1 : last] if last >= first else []
    content = "".join(selected)

    resolved = resolve_path(
        handle.root, path, must_exist=True, expect="file", state_dir=handle.state_dir
    )
    next_start_line = last + 1 if truncated and last == loaded_line_count else None

    return FileReadResult(
        path=resolved.reported,
        content=content,
        start_line=first,
        end_line=last,
        returned_line_count=len(selected),
        total_lines=total_lines,
        size_bytes=size,
        content_hash=hash_file(resolved.absolute, display_path=resolved.relative),
        encoding=profile.encoding,
        newline=profile.newline,
        has_bom=profile.has_bom,
        truncated=truncated,
        truncation_reason=(
            "read stopped at the byte safety limit; the file is larger than max_read_bytes"
            if truncated
            else None
        ),
        next_start_line=next_start_line,
    )


def read_multiple_files(
    handle: WorkspaceHandle,
    paths: list[str] | tuple[str, ...],
    *,
    max_bytes_per_file: int | None = None,
    start_line: int | None = None,
    end_line: int | None = None,
) -> tuple[ReadManyEntry, ...]:
    """Read several files in one call. A failure is returned, never raised."""

    if not paths:
        raise ValidationError("at least one path is required")
    entries: list[ReadManyEntry] = []
    for path in paths:
        try:
            result = read_file(
                handle,
                path,
                start_line=start_line,
                end_line=end_line,
                max_bytes=max_bytes_per_file,
            )
        except FilekeeperError as exc:
            entries.append(
                ReadManyEntry(
                    path=str(path),
                    ok=False,
                    error=ErrorDetail(
                        code=str(exc.code),
                        message=exc.message,
                        retryable=exc.retryable,
                        details=exc.details,
                    ),
                )
            )
        else:
            entries.append(ReadManyEntry(path=result.path, ok=True, result=result))
    return tuple(entries)


def list_directory(
    handle: WorkspaceHandle,
    path: str = ".",
    *,
    recursive: bool = False,
    glob: str | None = None,
    max_entries: int | None = None,
) -> DirectoryListing:
    """List entries. Symlinks are reported, never followed during recursion."""

    limit = handle.config.max_directory_entries if max_entries is None else max_entries
    if limit <= 0:
        raise ValidationError("max_entries must be positive", details={"max_entries": limit})
    limit = min(limit, handle.config.max_directory_entries)

    resolved = resolve_path(
        handle.root, path, must_exist=True, expect="directory", state_dir=handle.state_dir
    )
    base = resolved.absolute
    # Compare canonical state path so a differently spelled base still hides it.
    canonical_state_dir = Path(os.path.realpath(handle.state_dir))

    entries: list[DirectoryEntry] = []
    truncated = False

    def matches(relative: str, name: str) -> bool:
        if glob is None:
            return True
        target = relative if "/" in glob else name
        return fnmatch.fnmatchcase(target, glob)

    def add(directory: Path, depth: int) -> None:
        nonlocal truncated
        if truncated:
            return
        try:
            children = sorted(directory.iterdir(), key=lambda item: item.name)
        except OSError as exc:
            raise storage_failure(
                "list directory", _relative(handle, directory) or ".", exc
            ) from exc
        for child in children:
            if len(entries) >= limit:
                truncated = True
                return
            is_symlink = child.is_symlink()
            if (
                not is_symlink
                and child.is_dir()
                and Path(os.path.realpath(child)) == (canonical_state_dir)
            ):
                continue
            if is_symlink:
                kind: EntryKind = "symlink"
            elif child.is_dir():
                kind = "directory"
            elif child.is_file():
                kind = "file"
            else:
                kind = "other"
            relative = _relative(handle, child)
            if matches(relative, child.name):
                try:
                    size_bytes = child.lstat().st_size
                except OSError as exc:
                    raise storage_failure("inspect", relative, exc) from exc
                entries.append(
                    DirectoryEntry(
                        path=reported_path(handle.root, relative),
                        name=child.name,
                        kind=kind,
                        size_bytes=size_bytes,
                        is_symlink=is_symlink,
                    )
                )
            if recursive and kind == "directory" and depth + 1 <= handle.config.max_recursion_depth:
                add(child, depth + 1)

    add(base, 0)
    return DirectoryListing(
        path=resolved.reported,
        entries=tuple(entries),
        truncated=truncated,
        max_entries=limit,
    )


def get_file_info(handle: WorkspaceHandle, path: str) -> PathInfo:
    """Metadata plus, for regular files, a hash and text characteristics."""

    resolved = resolve_path(handle.root, path, must_exist=True, state_dir=handle.state_dir)
    target = resolved.absolute
    link_path = resolved.link_path
    try:
        info = link_path.lstat()
    except OSError as exc:
        raise storage_failure("inspect", resolved.relative, exc) from exc

    if resolved.is_symlink:
        kind: EntryKind = "symlink"
    elif target.is_dir():
        kind = "directory"
    elif target.is_file():
        kind = "file"
    else:
        kind = "other"

    base = PathInfo(
        path=resolved.reported,
        kind=kind,
        size_bytes=info.st_size,
        mode=info.st_mode & 0o7777,
        modified_at=datetime.fromtimestamp(info.st_mtime, UTC).replace(microsecond=0).isoformat(),
        is_symlink=resolved.is_symlink,
        symlink_target=str(link_path.readlink()) if resolved.is_symlink else None,
    )

    if kind != "file":
        return base

    try:
        with target.open("rb") as file_handle:
            sample = file_handle.read(BINARY_SAMPLE_SIZE)
    except OSError as exc:
        raise storage_failure("read", resolved.relative, exc) from exc

    is_text = not looks_binary(sample)
    encoding: str | None = None
    newline: str | None = None
    line_count: int | None = None
    if is_text:
        try:
            with target.open("rb") as file_handle:
                payload = file_handle.read(_INFO_SAMPLE_SIZE)
            text, profile = decode_bytes(payload, path=resolved.relative)
        except FilekeeperError:
            is_text = False
        else:
            encoding = profile.encoding
            newline = profile.newline
            if info.st_size <= _INFO_SAMPLE_SIZE:
                line_count = len(text.splitlines())

    return PathInfo(
        path=base.path,
        kind=base.kind,
        size_bytes=base.size_bytes,
        mode=base.mode,
        modified_at=base.modified_at,
        is_symlink=base.is_symlink,
        symlink_target=base.symlink_target,
        is_text=is_text,
        content_hash=hash_file(target, display_path=resolved.relative),
        line_count=line_count,
        encoding=encoding,
        newline=newline,
    )


def get_file_hash(handle: WorkspaceHandle, path: str) -> str:
    resolved = resolve_path(
        handle.root, path, must_exist=True, expect="file", state_dir=handle.state_dir
    )
    return hash_file(resolved.absolute, display_path=resolved.relative)


def file_exists(handle: WorkspaceHandle, path: str) -> ExistsResult:
    resolved = resolve_path(handle.root, path, state_dir=handle.state_dir)
    if not resolved.exists:
        return ExistsResult(path=resolved.reported, exists=False)
    return ExistsResult(path=resolved.reported, exists=True, kind=resolved.kind)
