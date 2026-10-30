"""Conversion of Core values into JSON-ready protocol payloads.

Rendering only: nothing here reads, resolves or writes a filesystem path. The
shapes deliberately mirror the Core dataclasses field for field, so a client sees
exactly what the library returned.
"""

from __future__ import annotations

import dataclasses
import json
from enum import Enum
from pathlib import Path
from typing import Any


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


def compact_json(payload: dict[str, Any]) -> str:
    """The text content that accompanies structured content.

    Kept compact on purpose: the structured payload is the contract, and this is
    the fallback for hosts that only render text.
    """

    return json.dumps(jsonable(payload), ensure_ascii=False, separators=(",", ":"))
