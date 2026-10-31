"""Workspace-bound path resolution.

Every operation goes through :func:`resolve_path`. It is the single place where
"which file is this?" is decided, and it enforces four rules:

1. Relative paths resolve against the workspace root; absolute paths are accepted
   only when they land inside the workspace after canonical resolution.
2. Relative paths containing a ``..`` segment are rejected outright with
   :class:`PathOutsideWorkspaceError`. They are never needed for legitimate
   workspace work, and rejecting them is cheaper to audit than proving each
   normalisation correct.
3. Symlinks are resolved. A link whose target leaves the workspace is refused
   with :class:`SymlinkEscapeRefusedError`, and a symlink is never a write
   target: the link itself would be replaced.
4. An absolute path that resolves outside the workspace is refused with
   :class:`AbsolutePathOutsideWorkspaceError`.
5. Device files, sockets and FIFOs are refused for normal file operations, as are
   paths inside Filekeeper's own state directory.

Errors name the caller's path, never the resolved host path, so adapter output
stays free of absolute paths.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .errors import (
    AbsolutePathOutsideWorkspaceError,
    NotAFileError,
    PathNotFoundError,
    PathOutsideWorkspaceError,
    SpecialFileRefusedError,
    StatePathError,
    SymlinkEscapeRefusedError,
    SymlinkWriteRefusedError,
    ValidationError,
)
from .errors import (
    NotADirectoryError as FilekeeperNotADirectoryError,
)

PathKind = Literal["file", "directory", "symlink", "other"]


def reported_path(root: Path, relative: str) -> str:
    """Render a workspace-relative path for a caller.

    A relative path is the normal form and is returned byte-for-byte. The one
    exception is a workspace whose root is the filesystem root: there the
    relative form cannot be told apart from a repository-relative path, so the
    leading separator is restored and the reported string is the absolute path.
    The empty relative path is rendered as the root itself.
    """

    if Path(root) == Path("/"):
        return "/" + relative
    return relative or "."


@dataclass(frozen=True, slots=True)
class ResolvedPath:
    """A path proven to be inside the workspace.

    ``absolute`` is the canonical target (symlinks resolved) and is what
    operations act on. ``link_path`` is the path as located in the workspace,
    which differs only when the final component is a symlink; metadata calls need
    it to stat the link itself instead of its target.

    ``reported`` is the string a caller sees; ``relative`` stays the form
    operations and the journal use.
    """

    root: Path
    absolute: Path
    relative: str
    exists: bool
    is_symlink: bool
    link_path: Path

    @property
    def reported(self) -> str:
        return reported_path(self.root, self.relative)

    @property
    def kind(self) -> PathKind:
        if self.is_symlink:
            return "symlink"
        if self.absolute.is_dir():
            return "directory"
        if self.absolute.is_file():
            return "file"
        return "other"


def _reject_traversal(raw: str) -> None:
    normalised = raw.replace("\\", "/")
    for segment in normalised.split("/"):
        if segment == "..":
            raise PathOutsideWorkspaceError(
                "path traversal with '..' is not allowed; use a workspace-relative path",
                details={"path": raw},
            )


def _kind_of(path: Path) -> PathKind:
    if path.is_symlink():
        return "symlink"
    if path.is_dir():
        return "directory"
    if path.is_file():
        return "file"
    return "other"


def _escapes_through_symlink(joined: Path, root_canonical: Path) -> bool:
    """True when a symlink at or below the workspace root points outside it."""

    try:
        relative = Path(os.path.abspath(joined)).relative_to(root_canonical)
    except ValueError:
        return False
    current = root_canonical
    for part in relative.parts:
        current = current / part
        if not current.is_symlink():
            continue
        target = Path(os.path.realpath(current))
        if target != root_canonical and root_canonical not in target.parents:
            return True
    return False


def _reject_special_file(path: Path, *, display: str) -> None:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return
    mode = info.st_mode
    if stat.S_ISREG(mode) or stat.S_ISDIR(mode) or stat.S_ISLNK(mode):
        return
    raise SpecialFileRefusedError(
        "only regular files and directories are supported; device files, sockets "
        "and FIFOs are refused",
        details={"path": display},
    )


def resolve_path(
    root: Path,
    user_path: str | Path,
    *,
    for_write: bool = False,
    must_exist: bool = False,
    expect: Literal["file", "directory"] | None = None,
    state_dir: Path | None = None,
) -> ResolvedPath:
    """Resolve ``user_path`` inside ``root`` or raise a boundary error."""

    root_canonical = Path(root).resolve()
    if not root_canonical.is_dir():
        raise ValidationError(
            "workspace root does not exist or is not a directory",
            details={"root": str(root)},
        )

    raw = str(user_path)
    if "\x00" in raw:
        raise ValidationError("path contains a NUL byte", details={"path": "<contains NUL>"})
    if raw.strip() == "":
        raise ValidationError("path must not be empty")

    candidate = Path(raw)
    if candidate.is_absolute():
        joined = candidate
    else:
        _reject_traversal(raw)
        joined = root_canonical / candidate

    # realpath resolves symlinks in the existing prefix and is well-defined for
    # paths that do not exist yet.
    canonical = Path(os.path.realpath(joined))

    if canonical != root_canonical and root_canonical not in canonical.parents:
        if _escapes_through_symlink(joined, root_canonical):
            raise SymlinkEscapeRefusedError(
                "symlink target resolves outside the workspace",
                details={"path": raw},
            )
        if candidate.is_absolute():
            raise AbsolutePathOutsideWorkspaceError(
                "absolute path resolves outside the workspace",
                details={"path": raw},
            )
        raise PathOutsideWorkspaceError(
            "path resolves outside the workspace",
            details={"path": raw},
        )

    if state_dir is not None:
        state_canonical = Path(os.path.realpath(state_dir))
        if canonical == state_canonical or state_canonical in canonical.parents:
            raise StatePathError(
                "path points inside Filekeeper's own state directory",
                details={"path": raw},
            )

    is_link = joined.is_symlink()
    exists = os.path.lexists(joined)
    link_location = Path(os.path.abspath(joined))

    if is_link and for_write:
        raise SymlinkWriteRefusedError(
            "writes to symlinks are refused; write to the link target explicitly",
            details={"path": raw},
        )

    if exists:
        _reject_special_file(joined, display=raw)

    reported = link_location if is_link else canonical
    relative = reported.relative_to(root_canonical).as_posix()
    resolved = ResolvedPath(
        root=root_canonical,
        absolute=canonical,
        relative=relative if relative != "." else "",
        exists=exists and canonical.exists(),
        is_symlink=is_link,
        link_path=link_location,
    )

    if must_exist and not resolved.exists:
        raise PathNotFoundError("path does not exist", details={"path": raw})

    if expect is not None and resolved.exists:
        kind = resolved.kind
        if expect == "file" and kind not in {"file", "symlink"}:
            raise NotAFileError("path is not a regular file", details={"path": raw})
        if expect == "file" and kind == "symlink":
            # A symlink that resolves to a file is acceptable for reads.
            if not resolved.absolute.is_file():
                raise NotAFileError("path is not a regular file", details={"path": raw})
        if expect == "directory" and not resolved.absolute.is_dir():
            raise FilekeeperNotADirectoryError("path is not a directory", details={"path": raw})

    return resolved


def ensure_parent_directory(resolved: ResolvedPath) -> None:
    """Raise a validation error when the destination's parent is missing."""

    if not resolved.absolute.parent.is_dir():
        raise ValidationError(
            "parent directory does not exist; create it first",
            details={"path": resolved.relative or "."},
        )
