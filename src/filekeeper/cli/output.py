"""Rendering: human-readable text by default, one stable JSON object with --json.

Nothing here decides behaviour; it prints what Core returned. JSON payloads are
documented in ``README.md``: always an object, always with ``ok``, and errors
carry ``error.code`` / ``error.message`` / ``error.retryable``.
"""

from __future__ import annotations

import dataclasses
import json
import sys
from enum import Enum
from pathlib import Path
from typing import Any

from ..core.errors import FilekeeperError
from ..core.models import (
    ContentSearchResult,
    DirectoryListing,
    ExistsResult,
    FileReadResult,
    MutationResult,
    PathInfo,
    PathSearchResult,
    ReadManyEntry,
    Workspace,
)


def jsonable(value: Any) -> Any:
    """Convert dataclasses, enums, tuples and paths into JSON-ready values."""

    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {field.name: jsonable(getattr(value, field.name)) for field in dataclasses.fields(value)}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    return value


def print_json(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(jsonable(payload), ensure_ascii=False, indent=2, sort_keys=False))
    sys.stdout.write("\n")


def print_error(error: FilekeeperError, *, as_json: bool) -> None:
    if as_json:
        print_json({"ok": False, "error": error.to_dict()})
        return
    sys.stderr.write(f"error: {error.code}: {error.message}\n")
    for key, value in error.details.items():
        sys.stderr.write(f"  {key}: {value}\n")


def result_payload(result: Any) -> dict[str, Any]:
    payload = jsonable(result)
    merged: dict[str, Any] = {"ok": True}
    merged.update(payload)
    return merged


# -- human renderers -------------------------------------------------------
def read(result: FileReadResult) -> str:
    return result.content


def read_many(entries: tuple[ReadManyEntry, ...]) -> str:
    chunks: list[str] = []
    for entry in entries:
        chunks.append(f"== {entry.path} ==")
        if entry.ok and entry.result is not None:
            chunks.append(entry.result.content.rstrip("\n"))
        else:
            code = entry.error.code if entry.error else "UNKNOWN"
            message = entry.error.message if entry.error else "read failed"
            chunks.append(f"[{code}] {message}")
    return "\n".join(chunks)


def listing(result: DirectoryListing) -> str:
    lines: list[str] = []
    for entry in result.entries:
        marker = "/" if entry.kind == "directory" else "@" if entry.kind == "symlink" else ""
        lines.append(f"{entry.kind:<9} {entry.size_bytes:>10}  {entry.path}{marker}")
    if result.truncated:
        lines.append(f"... truncated at {result.max_entries} entries")
    return "\n".join(lines)


def info(result: PathInfo) -> str:
    lines = [
        f"path: {result.path}",
        f"kind: {result.kind}",
        f"size_bytes: {result.size_bytes}",
        f"mode: {result.mode:04o}",
        f"modified_at: {result.modified_at}",
    ]
    if result.is_symlink:
        lines.append(f"symlink_target: {result.symlink_target}")
    if result.content_hash is not None:
        lines.append(f"content_hash: {result.content_hash}")
    if result.is_text is not None:
        lines.append(f"is_text: {result.is_text}")
    if result.line_count is not None:
        lines.append(f"line_count: {result.line_count}")
    if result.encoding is not None:
        lines.append(f"encoding: {result.encoding}")
    if result.newline is not None:
        lines.append(f"newline: {json.dumps(result.newline)}")
    return "\n".join(lines)


def exists(result: ExistsResult) -> str:
    if not result.exists:
        return f"{result.path}: absent"
    return f"{result.path}: present ({result.kind})"


def path_search(result: PathSearchResult) -> str:
    lines = list(result.paths)
    if result.truncated:
        lines.append("... truncated; narrow the glob or raise the result limit")
    return "\n".join(lines)


def content_search(result: ContentSearchResult) -> str:
    lines = [f"{match.path}:{match.line_number}:{match.column}: {match.line}" for match in result.matches]
    if result.truncated:
        lines.append("... truncated; narrow the query or raise the result limit")
    return "\n".join(lines)


def mutation(result: MutationResult) -> str:
    lines: list[str] = []
    for item in result.items:
        lines.append(f"{item.change:<9} {item.path}")
    lines.append(result.summary)
    if result.after_hash:
        lines.append(f"after_hash: {result.after_hash}")
    if result.before_hash and result.before_hash != result.after_hash:
        lines.append(f"before_hash: {result.before_hash}")
    lines.append("operation_id: none (history is not recorded in this release)")
    for warning in result.warnings:
        lines.append(f"warning: {warning}")
    return "\n".join(lines)


def workspace_summary(workspace: Workspace, *, is_default: bool = False) -> str:
    suffix = "  (default)" if is_default else ""
    return f"{workspace.name}\t{workspace.root_path}\t{workspace.id}{suffix}"


def workspace_detail(workspace: Workspace, *, is_default: bool = False) -> str:
    return "\n".join(
        [
            f"name: {workspace.name}",
            f"id: {workspace.id}",
            f"root: {workspace.root_path}",
            f"created_at: {workspace.created_at}",
            f"updated_at: {workspace.updated_at}",
            f"default: {is_default}",
        ]
    )
