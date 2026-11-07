"""Structured errors with stable, documented codes.

Every failure surfaced by Core is a :class:`FilekeeperError`. The code is the
stable part of the contract: adapters (CLI, MCP) map codes to exit codes and to
error payloads, and callers may branch on the code without parsing prose.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from .paths import ResolvedPath


class ErrorCode(StrEnum):
    """Stable error codes. Adding a code is a compatible change; renaming one is not."""

    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    PATH_NOT_FOUND = "PATH_NOT_FOUND"
    NOT_A_FILE = "NOT_A_FILE"
    NOT_A_DIRECTORY = "NOT_A_DIRECTORY"
    PATH_OUTSIDE_WORKSPACE = "PATH_OUTSIDE_WORKSPACE"
    ABSOLUTE_PATH_OUTSIDE_WORKSPACE = "ABSOLUTE_PATH_OUTSIDE_WORKSPACE"
    SYMLINK_ESCAPE_REFUSED = "SYMLINK_ESCAPE_REFUSED"
    PATH_IS_STATE_PATH = "PATH_IS_STATE_PATH"
    SYMLINK_WRITE_REFUSED = "SYMLINK_WRITE_REFUSED"
    SPECIAL_FILE_REFUSED = "SPECIAL_FILE_REFUSED"
    SENSITIVE_PATH_REFUSED = "SENSITIVE_PATH_REFUSED"
    ALREADY_EXISTS = "ALREADY_EXISTS"
    CONTENT_CONFLICT = "CONTENT_CONFLICT"
    NO_MATCH = "NO_MATCH"
    MATCH_COUNT_MISMATCH = "MATCH_COUNT_MISMATCH"
    PATCH_FAILED = "PATCH_FAILED"
    PARTIAL_RECOVERY = "PARTIAL_RECOVERY"
    BINARY_CONTENT_REFUSED = "BINARY_CONTENT_REFUSED"
    LIMIT_EXCEEDED = "LIMIT_EXCEEDED"
    INVALID_PATTERN = "INVALID_PATTERN"
    WORKSPACE_NOT_FOUND = "WORKSPACE_NOT_FOUND"
    WORKSPACE_EXISTS = "WORKSPACE_EXISTS"
    NO_WORKSPACE_SELECTED = "NO_WORKSPACE_SELECTED"
    STORAGE_ERROR = "STORAGE_ERROR"
    MIGRATION_ERROR = "MIGRATION_ERROR"
    OPERATION_NOT_FOUND = "OPERATION_NOT_FOUND"
    UNDO_CONFLICT = "UNDO_CONFLICT"
    HISTORY_UNAVAILABLE = "HISTORY_UNAVAILABLE"
    NOT_IMPLEMENTED_YET = "NOT_IMPLEMENTED_YET"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class CallerSurface(StrEnum):
    """The interface a caller reached Core through.

    A refusal that offers an override must name the argument the caller actually
    has. Core renders the sentence for ``CORE``; each adapter re-renders the same
    error for its own surface before returning it, so no message ever names
    another interface's argument.
    """

    CORE = "core"
    CLI = "cli"
    MCP = "mcp"


@dataclass(frozen=True, slots=True)
class OverrideHint:
    """One "pass X to override" instruction, worded for each caller surface."""

    sentences: Mapping[CallerSurface, str]

    def sentence(self, surface: CallerSurface) -> str:
        return self.sentences[surface]


def override_hint(*, core: str, cli: str, mcp: str) -> OverrideHint:
    """Build a hint whose sentence names each surface's own argument."""

    return OverrideHint({CallerSurface.CORE: core, CallerSurface.CLI: cli, CallerSurface.MCP: mcp})


class FilekeeperError(Exception):
    """Base error carrying a stable code, a human message, and optional details."""

    code: ErrorCode = ErrorCode.INTERNAL_ERROR
    retryable: bool = False

    def __init__(
        self,
        message: str,
        *,
        code: ErrorCode | None = None,
        retryable: bool | None = None,
        details: dict[str, Any] | None = None,
        override_hint: OverrideHint | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        if retryable is not None:
            self.retryable = retryable
        self.details: dict[str, Any] = details or {}
        # Not serialised: the hint only lets an adapter re-word the message.
        self.override_hint = override_hint

    def for_surface(self, surface: CallerSurface) -> FilekeeperError:
        """Return this error worded for the interface that received it.

        ``code``, ``retryable`` and ``details`` are carried over untouched; only
        the message's override sentence changes. An error with no hint, or a
        request for the surface Core already rendered, is returned as is.
        """

        hint = self.override_hint
        if hint is None or surface is CallerSurface.CORE:
            return self
        core_sentence = hint.sentence(CallerSurface.CORE)
        if core_sentence not in self.message:
            return self
        adapted = copy.copy(self)
        adapted.message = self.message.replace(core_sentence, hint.sentence(surface))
        adapted.args = (adapted.message,)
        return adapted

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": str(self.code),
            "message": self.message,
            "retryable": self.retryable,
        }
        if self.details:
            payload["details"] = dict(self.details)
        return payload

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


class ValidationError(FilekeeperError):
    code = ErrorCode.INVALID_ARGUMENT


class PathNotFoundError(FilekeeperError):
    code = ErrorCode.PATH_NOT_FOUND


class NotAFileError(FilekeeperError):
    code = ErrorCode.NOT_A_FILE


class NotADirectoryError(FilekeeperError):
    code = ErrorCode.NOT_A_DIRECTORY


class PathOutsideWorkspaceError(FilekeeperError):
    """A relative path used ``..`` traversal."""

    code = ErrorCode.PATH_OUTSIDE_WORKSPACE


class AbsolutePathOutsideWorkspaceError(PathOutsideWorkspaceError):
    """An absolute path resolved outside the selected workspace."""

    code = ErrorCode.ABSOLUTE_PATH_OUTSIDE_WORKSPACE


class SymlinkEscapeRefusedError(PathOutsideWorkspaceError):
    """A symlink inside the workspace resolved to a target outside it."""

    code = ErrorCode.SYMLINK_ESCAPE_REFUSED


class StatePathError(FilekeeperError):
    """A path points inside Filekeeper's own state directory."""

    code = ErrorCode.PATH_IS_STATE_PATH


class SymlinkWriteRefusedError(FilekeeperError):
    code = ErrorCode.SYMLINK_WRITE_REFUSED


class SpecialFileRefusedError(FilekeeperError):
    code = ErrorCode.SPECIAL_FILE_REFUSED


class SensitivePathRefusedError(FilekeeperError):
    """The target looks like credential or key material and was refused."""

    code = ErrorCode.SENSITIVE_PATH_REFUSED

    def __init__(
        self,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        override_hint: OverrideHint | None = None,
    ) -> None:
        super().__init__(message, details=details, override_hint=override_hint)


class AlreadyExistsError(FilekeeperError):
    code = ErrorCode.ALREADY_EXISTS


class ContentConflictError(FilekeeperError):
    """The guarded write's expected hash or content no longer matches.

    The identical request cannot succeed, so ``retryable`` is ``False``. The
    recovery path is stated in the payload: read the current state again and
    issue the write against the hash that read returned.
    """

    code = ErrorCode.CONTENT_CONFLICT
    retryable = False

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {
            "recovery": "re-read the target, then retry against its current hash"
        }
        if details:
            payload.update(details)
        super().__init__(message, details=payload)


class NoMatchError(FilekeeperError):
    """No occurrence of the requested text was found; nothing was modified."""

    code = ErrorCode.NO_MATCH


class MatchCountMismatchError(FilekeeperError):
    """The requested text matched a different number of times than expected."""

    code = ErrorCode.MATCH_COUNT_MISMATCH


class PatchFailedError(FilekeeperError):
    """A patch was rejected or could not be applied; the pre-patch state is intact."""

    code = ErrorCode.PATCH_FAILED


class PartialRecoveryError(FilekeeperError):
    """A failed multi-file application could not be fully rolled back."""

    code = ErrorCode.PARTIAL_RECOVERY


class BinaryContentRefusedError(FilekeeperError):
    code = ErrorCode.BINARY_CONTENT_REFUSED


class LimitExceededError(FilekeeperError):
    code = ErrorCode.LIMIT_EXCEEDED

    def __init__(
        self, message: str, *, limit: int, actual: int, details: dict[str, Any] | None = None
    ) -> None:
        payload: dict[str, Any] = {"limit": limit, "actual": actual}
        if details:
            payload.update(details)
        super().__init__(message, details=payload)


class InvalidPatternError(FilekeeperError):
    code = ErrorCode.INVALID_PATTERN


class WorkspaceNotFoundError(FilekeeperError):
    code = ErrorCode.WORKSPACE_NOT_FOUND


class WorkspaceExistsError(FilekeeperError):
    code = ErrorCode.WORKSPACE_EXISTS


class NoWorkspaceSelectedError(FilekeeperError):
    code = ErrorCode.NO_WORKSPACE_SELECTED


class StorageError(FilekeeperError):
    code = ErrorCode.STORAGE_ERROR


class MigrationError(FilekeeperError):
    code = ErrorCode.MIGRATION_ERROR


class OperationNotFoundError(FilekeeperError):
    """No operation with that id is recorded in this workspace."""

    code = ErrorCode.OPERATION_NOT_FOUND


class UndoConflictError(FilekeeperError):
    """Undo would destroy a later change; nothing was modified."""

    code = ErrorCode.UNDO_CONFLICT


class HistoryUnavailableError(FilekeeperError):
    """The bytes needed to undo are missing, corrupt or were never stored."""

    code = ErrorCode.HISTORY_UNAVAILABLE


class NotImplementedYetError(FilekeeperError, NotImplementedError):
    """A published API surface whose implementation is deliberately a later release.

    It subclasses :class:`NotImplementedError` as well, so callers that guard the
    standard exception also catch it. It is never a silent no-op.
    """

    code = ErrorCode.NOT_IMPLEMENTED_YET


def location_details(absolute: str | Path, root: str | Path) -> dict[str, str]:
    """Name where a caller's path resolved and the workspace root it resolved in.

    A caller cannot otherwise tell where its path went: on a workspace rooted at
    the filesystem root a relative path resolves against ``/``, so ``runs/x``
    becomes ``/runs/x``. Both fields stay in ``details`` so the message stays
    short and the diagnosis takes one step.
    """

    return {"resolved_path": str(absolute), "workspace_root": str(root)}


def storage_failure(
    action: str,
    path: str,
    exc: OSError,
    *,
    location: ResolvedPath | None = None,
) -> StorageError:
    """Build the :class:`StorageError` for a failed filesystem call on ``path``.

    ``path`` is the caller-facing path: the workspace-relative form for anything
    inside a workspace. An ``OSError``'s own message embeds the resolved host
    path, so only its standard reason phrase is carried into the message.

    Guaranteed: the message never names a workspace file by its resolved host
    path. When the caller passes the ``location`` it resolved, ``details`` gains
    ``resolved_path`` and ``workspace_root`` — the diagnosis without the leak of
    a host path into prose. Every construction site that has a workspace path in
    hand passes the relative form, and :func:`filekeeper.core.safety.hashing.hash_file`
    never falls back to the resolved path.

    Not covered: locations the operator chooses rather than workspace content —
    the state directory and its database, and the configuration file. Those
    errors report the location as the operator supplied it, and their message
    still carries only the reason phrase.
    """

    details: dict[str, Any] = {"path": path}
    if location is not None:
        details.update(location_details(location.absolute, location.root))
    reason = exc.strerror or type(exc).__name__
    return StorageError(f"cannot {action} {path}: {reason}", details=details)
