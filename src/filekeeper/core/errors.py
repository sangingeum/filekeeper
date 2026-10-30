"""Structured errors with stable, documented codes.

Every failure surfaced by Core is a :class:`FilekeeperError`. The code is the
stable part of the contract: adapters (CLI, MCP) map codes to exit codes and to
error payloads, and callers may branch on the code without parsing prose.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any


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
    ) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        if retryable is not None:
            self.retryable = retryable
        self.details: dict[str, Any] = details or {}

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

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, details=details)


class AlreadyExistsError(FilekeeperError):
    code = ErrorCode.ALREADY_EXISTS


class ContentConflictError(FilekeeperError):
    code = ErrorCode.CONTENT_CONFLICT
    retryable = True


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
