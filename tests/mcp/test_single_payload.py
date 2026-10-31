"""Each tool result carries its payload once, not twice.

``structured_content`` is the contract. A result carries one payload when the
structured object arrives without a text mirror; an unconditional text copy
doubled the bytes of every call.
"""

from __future__ import annotations

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


def test_error_envelope_carries_exactly_one_payload(target: ServerTarget) -> None:
    result = exercise(target, [("file_read", {"path": "absent.txt"})])

    reply = result.by_name("file_read")
    assert reply.is_error is True
    assert _payload_count(reply) == 1
    assert reply.text == ""
    assert set(reply.structured) == {"ok", "error"}
    assert reply.structured["error"]["code"] == "PATH_NOT_FOUND"
