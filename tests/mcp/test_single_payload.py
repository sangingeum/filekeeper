"""Each successful tool result carries its payload once, not twice.

``structured_content`` is the contract. A success carries one payload when the
structured object arrives without a text mirror; an unconditional text copy
doubled the bytes of every call. An error is the exception: a text-only client
must still be able to read its code, so it carries a text fallback as well.
"""

from __future__ import annotations

import json

from harness import ServerTarget, ToolReply, exercise


def _payload_count(reply: ToolReply) -> int:
    """Count the channels that carry the payload: structured plus non-empty text."""

    return (1 if isinstance(reply.structured, dict) else 0) + (1 if reply.text else 0)


def test_success_result_carries_exactly_one_payload(target: ServerTarget) -> None:
    result = exercise(target, [("file_read", {"path": "notes.txt"})])

    reply = result.by_name("file_read")
    assert reply.is_error is False
    assert _payload_count(reply) == 1
    assert reply.text == ""
    assert reply.structured["ok"] is True
    assert reply.structured["path"] == "notes.txt"


def test_error_carries_a_text_fallback_beside_the_structured_payload(
    target: ServerTarget,
) -> None:
    result = exercise(target, [("file_read", {"path": "absent.txt"})])

    reply = result.by_name("file_read")
    assert reply.is_error is True
    assert _payload_count(reply) == 2
    assert json.loads(reply.text) == reply.structured
    assert set(reply.structured) == {"ok", "error"}
    assert reply.structured["error"]["code"] == "PATH_NOT_FOUND"
