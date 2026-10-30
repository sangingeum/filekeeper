"""The published JSON shape: one success shape, one error shape.

Both documents below come from the same operation family (a guarded whole-file
write), so a client can branch on ``ok`` and read the rest of the document the
same way in either case. Field-by-field, not merely "the key exists".
"""

from __future__ import annotations

import json
from pathlib import Path

from filekeeper.cli import exit_codes

STALE_HASH = "sha256:" + "0" * 64


def _json_object(out: str) -> dict:
    payload = json.loads(out)
    assert isinstance(payload, dict)
    return payload


def test_a_success_document_is_flat_and_carries_operation_id(
    run_cli, workspace_root: Path
) -> None:
    run_cli("workspace", "create", "project", str(workspace_root))

    code, out, _ = run_cli("file", "write", "notes.txt", "--content", "changed\n", "--json")
    assert code == exit_codes.SUCCESS
    payload = _json_object(out)

    assert payload["ok"] is True
    assert payload["workspace"] == "project"
    assert payload["path"] == "notes.txt"
    assert payload["changed"] is True
    assert payload["before_hash"].startswith("sha256:")
    assert payload["after_hash"].startswith("sha256:")
    assert payload["operation_id"].startswith("op_")
    assert payload["undo_available"] is True
    assert payload["dry_run"] is False
    assert payload["warnings"] == []
    assert payload["items"][0]["path"] == "notes.txt"
    assert payload["items"][0]["change"] == "modified"
    assert "error" not in payload


def test_an_error_document_uses_the_same_top_level_shape(
    run_cli, workspace_root: Path
) -> None:
    run_cli("workspace", "create", "project", str(workspace_root))

    code, out, _ = run_cli(
        "file",
        "write",
        "notes.txt",
        "--content",
        "changed\n",
        "--expected-hash",
        STALE_HASH,
        "--json",
    )
    assert code == exit_codes.CONFLICT == 5
    payload = _json_object(out)

    # Exactly two top-level fields: no duplicated path or workspace.
    assert set(payload) == {"ok", "error"}
    assert payload["ok"] is False
    error = payload["error"]
    assert set(error) == {"code", "message", "retryable", "details"}
    assert error["code"] == "CONTENT_CONFLICT"
    assert error["retryable"] is False
    assert error["details"]["path"] == "notes.txt"
    assert error["details"]["expected_hash"] == STALE_HASH
    assert error["details"]["actual_hash"].startswith("sha256:")
    assert error["details"]["recovery"] == (
        "re-read the target, then retry against its current hash"
    )
    assert "path" not in payload
    assert "workspace" not in payload
    assert (workspace_root / "notes.txt").read_text(encoding="utf-8") == (
        "alpha\nbeta\ngamma\n"
    )
