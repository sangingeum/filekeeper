"""Unreadable targets on the mutation and undo surfaces: relative paths only.

Copy, move and undo need to read or hash a file that may be unreadable. The
failure is an environment condition, not a bug: ``STORAGE_ERROR`` (exit 9), a
message naming the workspace-relative path, and no resolved host path anywhere
in the payload — message or ``details``.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

from filekeeper.cli import exit_codes

ABSOLUTE_PATH = re.compile(r"(?<![\w.-])/[^\s'\"]+")


def _deny(path: Path) -> None:
    if os.geteuid() == 0:  # pragma: no cover - root ignores mode bits
        pytest.skip("permission bits do not deny root")
    os.chmod(path, 0o000)


def _restore(path: Path, mode: int) -> None:
    os.chmod(path, mode)


def _assert_no_host_path(out: str, workspace_root: Path) -> None:
    assert str(workspace_root) not in out, out
    assert ABSOLUTE_PATH.search(out) is None, out


def _assert_details_are_relative(details: object, workspace_root: Path) -> None:
    """Every string in ``details`` is caller-facing, never a host path."""

    if isinstance(details, dict):
        for value in details.values():
            _assert_details_are_relative(value, workspace_root)
    elif isinstance(details, list):
        for value in details:
            _assert_details_are_relative(value, workspace_root)
    elif isinstance(details, str):
        assert str(workspace_root) not in details, details
        # A bare file name or a relative path; nothing rooted at '/'.
        assert not details.startswith("/"), details


def test_copy_from_an_unreadable_source_reports_a_relative_path(
    run_cli, workspace_root: Path
) -> None:
    run_cli("workspace", "create", "project", str(workspace_root))
    source = workspace_root / "secret.txt"
    source.write_text("secret\n", encoding="utf-8")
    _deny(source)
    try:
        code, out, _ = run_cli("--json", "file", "copy", "secret.txt", "dst.txt")
    finally:
        _restore(source, 0o600)

    payload = json.loads(out)
    assert code == exit_codes.STORAGE_ERROR == 9
    assert payload["error"]["code"] == "STORAGE_ERROR"
    assert "secret.txt" in payload["error"]["message"]
    assert "[Errno" not in payload["error"]["message"]
    _assert_no_host_path(out, workspace_root)
    _assert_details_are_relative(payload["error"].get("details", {}), workspace_root)
    assert not (workspace_root / "dst.txt").exists()


def test_move_from_an_unreadable_source_reports_a_relative_path(
    run_cli, workspace_root: Path
) -> None:
    run_cli("workspace", "create", "project", str(workspace_root))
    source = workspace_root / "secret.txt"
    source.write_text("secret\n", encoding="utf-8")
    _deny(source)
    try:
        code, out, _ = run_cli("--json", "file", "move", "secret.txt", "dst.txt")
    finally:
        _restore(source, 0o600)

    payload = json.loads(out)
    assert code == exit_codes.STORAGE_ERROR == 9
    assert payload["error"]["code"] == "STORAGE_ERROR"
    assert "secret.txt" in payload["error"]["message"]
    _assert_no_host_path(out, workspace_root)
    details = payload["error"].get("details", {})
    _assert_details_are_relative(details, workspace_root)
    assert details["path"] == "secret.txt"
    assert not (workspace_root / "dst.txt").exists()


def test_undo_over_an_unreadable_target_reports_a_relative_path(
    run_cli, workspace_root: Path
) -> None:
    run_cli("workspace", "create", "project", str(workspace_root))
    code, out, _ = run_cli("--json", "file", "write", "secret.txt", "--content", "changed\n")
    assert code == exit_codes.SUCCESS
    operation_id = json.loads(out)["operation_id"]

    target = workspace_root / "secret.txt"
    _deny(target)
    try:
        code, out, _ = run_cli("--json", "history", "undo", operation_id)
    finally:
        _restore(target, 0o600)

    payload = json.loads(out)
    assert code == exit_codes.STORAGE_ERROR == 9
    assert payload["error"]["code"] == "STORAGE_ERROR"
    assert "secret.txt" in payload["error"]["message"]
    _assert_no_host_path(out, workspace_root)
    details = payload["error"].get("details", {})
    _assert_details_are_relative(details, workspace_root)
    assert details["path"] == "secret.txt"
