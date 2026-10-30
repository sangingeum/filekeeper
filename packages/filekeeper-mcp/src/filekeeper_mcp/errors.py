"""Structured protocol errors.

Every failure reaches the client as data, never as a Python traceback. The Core
error code and the caller's own path survive; host filesystem paths do not. A
resolved host path is useful to nobody outside this machine and can leak the shape
of the tree the server is confined to.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

import mcp.types as types

from filekeeper import ErrorCode, FilekeeperError

from .payloads import compact_json

# Path-like strings that begin a token. Relative paths such as ``src/main.py`` are
# left alone because the character before the slash is part of a word.
_ABSOLUTE_POSIX = re.compile(r"(?<![\w:/])/(?:[^\s\"',;:()\[\]]+)")
_ABSOLUTE_WINDOWS = re.compile(r"\b[A-Za-z]:\\[^\s\"',;:()\[\]]+")

# Detail keys that only ever carry a server-side location.
_LOCATION_KEYS = frozenset({"root", "state_dir", "home", "database", "path_absolute"})


def scrub_text(text: str, host_paths: Iterable[str]) -> str:
    """Remove host locations from a message while keeping its meaning."""

    cleaned = text
    for location in sorted(host_paths, key=len, reverse=True):
        if location:
            cleaned = cleaned.replace(location, "<host-path>")
    cleaned = _ABSOLUTE_WINDOWS.sub("<host-path>", cleaned)
    return _ABSOLUTE_POSIX.sub("<host-path>", cleaned)


def _scrub_value(value: Any, host_paths: Iterable[str]) -> Any:
    if isinstance(value, str):
        return scrub_text(value, host_paths)
    if isinstance(value, dict):
        return {str(key): _scrub_value(item, host_paths) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub_value(item, host_paths) for item in value]
    return value


def error_payload(
    error: FilekeeperError,
    *,
    host_paths: Iterable[str],
    caller_path: str | None = None,
    workspace: str | None = None,
) -> dict[str, Any]:
    """The stable error envelope: code, message, retryable, sanitized details."""

    locations = tuple(host_paths)
    details = {
        key: _scrub_value(value, locations)
        for key, value in error.details.items()
        if key not in _LOCATION_KEYS
    }
    error_body: dict[str, Any] = {
        "code": str(error.code),
        "message": scrub_text(error.message, locations),
        "retryable": error.retryable,
    }
    if details:
        error_body["details"] = details
    payload: dict[str, Any] = {"ok": False, "error": error_body}
    if caller_path is not None:
        payload["path"] = caller_path
    if workspace is not None:
        payload["workspace"] = workspace
    return payload


def error_result(
    error: FilekeeperError,
    *,
    host_paths: Iterable[str],
    caller_path: str | None = None,
    workspace: str | None = None,
) -> types.CallToolResult:
    payload = error_payload(
        error, host_paths=host_paths, caller_path=caller_path, workspace=workspace
    )
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=compact_json(payload))],
        structured_content=payload,
        is_error=True,
    )


def internal_error_result(
    *, caller_path: str | None = None, workspace: str | None = None
) -> types.CallToolResult:
    """A crash rendered as data. The exception text stays in the server log."""

    error = FilekeeperError(
        "the operation failed unexpectedly; nothing about the request was changed "
        "beyond what an earlier partial result reported"
    )
    return error_result(
        error,
        host_paths=(),
        caller_path=caller_path,
        workspace=workspace,
    )


__all__ = [
    "ErrorCode",
    "error_payload",
    "error_result",
    "internal_error_result",
    "scrub_text",
]
