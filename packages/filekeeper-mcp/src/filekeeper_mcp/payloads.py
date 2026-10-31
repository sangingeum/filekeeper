"""Conversion of Core values into JSON-ready protocol payloads.

Rendering only: nothing here reads, resolves or writes a filesystem path. The
shapes deliberately mirror the Core dataclasses field for field, so a client sees
exactly what the library returned.

A successful tool result carries its payload once. ``structured_content`` is the
protocol's structured channel and the contract; the older convention of mirroring
the same object into a ``TextContent`` block doubled the bytes of every result
without adding information, so no text copy is emitted on success.

A failing result is different. Some clients read an error only from its text
blocks and fall back to a fixed sentence when there are none, which would erase
the code, message and details an agent needs to act on. An error therefore also
carries the same JSON in one ``TextContent`` block, serialized from the very
object placed in ``structured_content`` so the two channels cannot drift.
"""

from __future__ import annotations

import dataclasses
import json
from enum import Enum
from pathlib import Path
from typing import Any

import mcp.types as types


def jsonable(value: Any) -> Any:
    """Convert dataclasses, enums, paths, tuples and mappings into JSON values."""

    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: jsonable(getattr(value, field.name))
            for field in dataclasses.fields(value)
        }
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    return value


def success_payload(result: Any) -> dict[str, Any]:
    """Wrap one Core result in the stable success envelope."""

    payload: dict[str, Any] = {"ok": True}
    converted = jsonable(result)
    if isinstance(converted, dict):
        payload.update(converted)
    else:  # pragma: no cover - every tool result is an object
        payload["result"] = converted
    return payload


def tool_result(payload: dict[str, Any], *, is_error: bool = False) -> types.CallToolResult:
    """Build a result carrying the payload once, or twice for an error.

    Success results stay single-channel: the object is the contract and the
    duplicate only doubled the bytes. An error additionally carries one text
    block built from that same object, because the error branch of some clients
    reads text blocks only and would otherwise replace the code and message with
    a fixed sentence. Both channels are rendered from this one mapping, so they
    cannot disagree.
    """

    content: list[types.ContentBlock] = []
    if is_error:
        content.append(
            types.TextContent(
                type="text",
                text=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            )
        )
    return types.CallToolResult(
        content=content,
        structured_content=payload,
        is_error=is_error,
    )
