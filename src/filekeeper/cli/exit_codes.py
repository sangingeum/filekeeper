"""Stable process exit codes.

These numbers are part of the published contract: scripts and agents may branch
on them. They are documented in ``README.md`` and must not be renumbered.
"""

from __future__ import annotations

from ..core.errors import ErrorCode, FilekeeperError

SUCCESS = 0
GENERAL_ERROR = 1
INVALID_ARGUMENT = 2
PATH_NOT_FOUND = 3
ACCESS_DENIED = 4
CONFLICT = 5
PATCH_FAILED = 7
PARTIAL_RECOVERY = 8
STORAGE_ERROR = 9
SENSITIVE_PATH_REFUSED = 10

_BY_CODE: dict[str, int] = {
    str(ErrorCode.INVALID_ARGUMENT): INVALID_ARGUMENT,
    str(ErrorCode.NOT_A_FILE): INVALID_ARGUMENT,
    str(ErrorCode.NOT_A_DIRECTORY): INVALID_ARGUMENT,
    str(ErrorCode.LIMIT_EXCEEDED): INVALID_ARGUMENT,
    str(ErrorCode.INVALID_PATTERN): INVALID_ARGUMENT,
    str(ErrorCode.NO_WORKSPACE_SELECTED): INVALID_ARGUMENT,
    str(ErrorCode.ALREADY_EXISTS): INVALID_ARGUMENT,
    str(ErrorCode.BINARY_CONTENT_REFUSED): INVALID_ARGUMENT,
    str(ErrorCode.PATH_NOT_FOUND): PATH_NOT_FOUND,
    str(ErrorCode.WORKSPACE_NOT_FOUND): PATH_NOT_FOUND,
    str(ErrorCode.PATH_OUTSIDE_WORKSPACE): ACCESS_DENIED,
    str(ErrorCode.PATH_IS_STATE_PATH): ACCESS_DENIED,
    str(ErrorCode.SYMLINK_WRITE_REFUSED): ACCESS_DENIED,
    str(ErrorCode.SPECIAL_FILE_REFUSED): ACCESS_DENIED,
    str(ErrorCode.SENSITIVE_PATH_REFUSED): SENSITIVE_PATH_REFUSED,
    str(ErrorCode.CONTENT_CONFLICT): CONFLICT,
    str(ErrorCode.STORAGE_ERROR): STORAGE_ERROR,
    str(ErrorCode.MIGRATION_ERROR): STORAGE_ERROR,
    str(ErrorCode.WORKSPACE_EXISTS): INVALID_ARGUMENT,
    str(ErrorCode.NOT_IMPLEMENTED_YET): GENERAL_ERROR,
    str(ErrorCode.INTERNAL_ERROR): GENERAL_ERROR,
}

EXIT_CODE_DOCUMENTATION: tuple[tuple[int, str], ...] = (
    (SUCCESS, "success"),
    (GENERAL_ERROR, "unexpected error"),
    (INVALID_ARGUMENT, "invalid arguments or validation error"),
    (PATH_NOT_FOUND, "path or workspace not found"),
    (ACCESS_DENIED, "permission or workspace-boundary violation"),
    (CONFLICT, "concurrency conflict (stale expected hash)"),
    (PATCH_FAILED, "patch could not be applied (reserved)"),
    (PARTIAL_RECOVERY, "partial rollback or recovery (reserved)"),
    (STORAGE_ERROR, "storage or database error"),
    (SENSITIVE_PATH_REFUSED, "write refused: target looks like credential material"),
)


def exit_code_for(error: FilekeeperError) -> int:
    return _BY_CODE.get(str(error.code), GENERAL_ERROR)
