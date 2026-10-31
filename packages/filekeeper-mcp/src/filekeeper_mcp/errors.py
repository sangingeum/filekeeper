"""Structured protocol errors.

Every failure reaches the client as data, never as a Python traceback. The Core
error code and the caller's own path survive; host filesystem paths do not. A
resolved host path is useful to nobody outside this machine and can leak the shape
of the tree the server is confined to.

Scrubbing works on path boundaries, not substrings. A location is replaced only
where it starts a token and ends at a separator or the end of the token, so a
relative path such as ``tmp/notes/missing.txt`` keeps every slash and a message
that legitimately contains ``a/b`` is untouched. A location that is itself a bare
separator — the workspace root ``/`` — is never scrubbed: replacing it would
destroy every slash in the payload.

The envelope is the same one the CLI emits: ``{"ok": false, "error": {...}}``
with no fields beside ``ok`` and ``error`` at the top level. The caller's path
travels inside ``error.details.path``, where Core already records it. Every
value in ``details`` is scrubbed, not just ``path``: Core renders state
locations workspace- or home-relative, so scrubbing a relative value leaves it
readable and a resolved host path never survives anywhere in the envelope.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

import mcp.types as types

from filekeeper import CallerSurface, ErrorCode, FilekeeperError

from .payloads import tool_result

# Path-like strings that begin a token. Relative paths such as ``src/main.py`` are
# left alone because the character before the slash is part of a word.
_ABSOLUTE_POSIX = re.compile(r"(?<![\w:>/])/(?:[^\s\"\',;:()\[\]]+)")
_ABSOLUTE_WINDOWS = re.compile(r"(?<!<)\b[A-Za-z]:\\[^\s\"\',;:()\[\]]+")

# Detail keys that only ever carry a server-side location.
_LOCATION_KEYS = frozenset({"root", "home", "database", "path_absolute"})


def _location_pattern(location: str) -> re.Pattern[str]:
    """A pattern matching one location as a whole path token."""

    escaped = re.escape(location)
    return re.compile(r"(?<![\w./\\-])" + escaped + r"(?=$|[\s\"',;:)\]}/\\])")


def scrub_text(text: str, host_paths: Iterable[str]) -> str:
    """Remove host locations from a message while keeping its meaning."""

    cleaned = text
    locations: list[str] = []
    for location in host_paths:
        candidate = location.rstrip("/\\") if location else ""
        # A bare separator (the workspace root ``/``) and one-character names are
        # never locations: replacing either would corrupt unrelated text.
        if len(candidate) < 2:
            continue
        locations.append(candidate)
    for location in sorted(set(locations), key=len, reverse=True):
        cleaned = _location_pattern(location).sub("<host-path>", cleaned)
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
) -> dict[str, Any]:
    """The stable error envelope: ``ok`` plus ``error.code/message/retryable/details``.

    Nothing is duplicated at the top level: a client reads every error the same
    way it reads a CLI JSON error.
    """

    error = error.for_surface(CallerSurface.MCP)
    locations = tuple(host_paths)
    details: dict[str, Any] = {}
    for key, value in error.details.items():
        if key in _LOCATION_KEYS:
            continue
        details[key] = _scrub_value(value, locations)
    error_body: dict[str, Any] = {
        "code": str(error.code),
        "message": scrub_text(error.message, locations),
        "retryable": error.retryable,
    }
    if caller_path is not None:
        details.setdefault("path", caller_path)
    if details:
        error_body["details"] = details
    return {"ok": False, "error": error_body}


def error_result(
    error: FilekeeperError,
    *,
    host_paths: Iterable[str],
    caller_path: str | None = None,
) -> types.CallToolResult:
    payload = error_payload(error, host_paths=host_paths, caller_path=caller_path)
    return tool_result(payload, is_error=True)


def internal_error_result() -> types.CallToolResult:
    """A crash rendered as data. The exception text stays in the server log."""

    error = FilekeeperError(
        "the operation failed unexpectedly; nothing about the request was changed "
        "beyond what an earlier partial result reported"
    )
    return error_result(error, host_paths=())


__all__ = [
    "ErrorCode",
    "error_payload",
    "error_result",
    "internal_error_result",
    "scrub_text",
]
