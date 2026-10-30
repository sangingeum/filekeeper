"""Errors arrive as stable codes and data, never as prose-only failures.

Each case is a real protocol call. The assertions cover the three things a host
can rely on: the Core code, the retryable flag, and the absence of a resolved
host path or a stack trace anywhere in the payload.
"""

from __future__ import annotations

import json
from pathlib import Path

from harness import ServerTarget, ToolReply, exercise

FAKE_HASH = "sha256:" + "0" * 64


def _assert_clean(reply: ToolReply, target: ServerTarget) -> None:
    rendered = json.dumps(reply.structured)
    assert reply.is_error is True
    assert "Traceback" not in rendered
    assert "Traceback" not in reply.text
    assert str(target.root) not in rendered
    assert str(target.home) not in rendered
    assert ".filekeeper" not in rendered
    assert str(target.root) not in reply.text


def test_a_stale_hash_is_not_retryable_and_names_the_recovery_path(
    target: ServerTarget,
) -> None:
    result = exercise(
        target,
        [
            ("file_read", {"path": "notes.txt"}),
            (
                "file_write",
                {"path": "notes.txt", "content": "replaced\n", "expected_hash": FAKE_HASH},
            ),
        ],
    )

    conflict = result.by_name("file_write")
    _assert_clean(conflict, target)
    assert conflict.code == "CONTENT_CONFLICT"
    assert conflict.error["retryable"] is False
    assert conflict.error["details"]["recovery"] == (
        "re-read the target, then retry against its current hash"
    )
    assert conflict.structured["error"]["details"]["path"] == "notes.txt"
    assert set(conflict.structured) == {"ok", "error"}
    assert (target.root / "notes.txt").read_text(encoding="utf-8") == "alpha\nbeta\ngamma\n"


def test_a_credential_shaped_target_is_refused_and_an_override_warns(
    target: ServerTarget,
) -> None:
    result = exercise(
        target,
        [
            ("file_write", {"path": ".env", "content": "TOKEN=1\n"}),
            (
                "file_write",
                {
                    "path": ".env",
                    "content": "TOKEN=1\n",
                    "i_accept_secret_write_risk": True,
                },
            ),
        ],
    )

    refused, overridden = result.replies
    _assert_clean(refused, target)
    assert refused.code == "SENSITIVE_PATH_REFUSED"
    assert refused.error["retryable"] is False
    assert refused.structured["error"]["details"]["path"] == ".env"

    assert overridden.is_error is False
    assert overridden.structured["ok"] is True
    assert any(
        "overridden" in warning for warning in overridden.structured["warnings"]
    ), overridden.structured["warnings"]
    assert (target.root / ".env").read_text(encoding="utf-8") == "TOKEN=1\n"


def test_boundary_escapes_keep_their_distinct_codes(target: ServerTarget) -> None:
    outside = target.root.parent / "escape.txt"
    result = exercise(
        target,
        [
            ("file_read", {"path": "../escape.txt"}),
            ("file_read", {"path": str(outside)}),
            ("file_read", {"path": "sub/../../escape.txt"}),
        ],
    )

    traversal, absolute, nested = result.replies
    for reply in (traversal, absolute, nested):
        _assert_clean(reply, target)

    assert traversal.code == "PATH_OUTSIDE_WORKSPACE"
    assert absolute.code == "ABSOLUTE_PATH_OUTSIDE_WORKSPACE"
    assert nested.code == "PATH_OUTSIDE_WORKSPACE"


def test_a_storage_failure_reports_storage_error_without_a_host_path(
    target: ServerTarget,
) -> None:
    # The first mutation creates .filekeeper/ and its blob store.
    exercise(target, [("file_write", {"path": "notes.txt", "content": "first\n"})])

    blob_directory = target.root / ".filekeeper" / "blobs"
    assert blob_directory.is_dir()
    blob_directory.rename(target.root / ".filekeeper" / "blobs-disabled")
    blob_directory.write_text("not a directory\n", encoding="utf-8")

    result = exercise(
        target, [("file_write", {"path": "notes.txt", "content": "second\n"})]
    )

    failed = result.by_name("file_write")
    _assert_clean(failed, target)
    assert failed.code == "STORAGE_ERROR"
    assert failed.structured["error"]["details"]["path"] == "notes.txt"
    assert (target.root / "notes.txt").read_text(encoding="utf-8") == "first\n"


def test_unknown_operations_and_missing_paths_keep_core_codes(target: ServerTarget) -> None:
    result = exercise(
        target,
        [
            ("history_get", {"operation_id": "op_missing"}),
            ("file_read", {"path": "absent.txt"}),
            ("undo_operation", {"operation_id": "op_missing"}),
        ],
    )

    missing_operation, missing_path, undo = result.replies
    assert missing_operation.code == "OPERATION_NOT_FOUND"
    assert missing_path.code == "PATH_NOT_FOUND"
    assert undo.code == "OPERATION_NOT_FOUND"
    for reply in result.replies:
        rendered = json.dumps(reply.structured)
        assert "Traceback" not in rendered
        assert str(target.root) not in rendered


def test_startup_without_a_workspace_refuses_scoped_tools_cleanly(tmp_path: Path) -> None:
    # No --root and no registration: tools/list still works, scoped calls refuse.
    from harness import ServerTarget

    empty_home = tmp_path / "empty-home"
    target = ServerTarget(root=None, home=empty_home)
    result = exercise(target, [("file_read", {"path": "notes.txt"})])

    assert len(result.tools) == 25
    refusal = result.by_name("file_read")
    assert refusal.is_error is True
    assert refusal.code == "NO_WORKSPACE_SELECTED"
    assert str(empty_home) not in json.dumps(refusal.structured)
