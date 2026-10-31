"""Unreadable targets on the read path: storage error, caller-facing path only.

A permission failure while opening or listing a target is an environment
failure, not a bug: it must map to ``STORAGE_ERROR`` (exit 9), the same mapping
the write path already uses. The message names the workspace-relative path and
must never carry the resolved host path.
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


def _assert_no_host_path(text: str, workspace_root: Path) -> None:
    assert str(workspace_root) not in text, text
    assert ABSOLUTE_PATH.search(text) is None, text


def test_unreadable_file_read_is_a_storage_error(run_cli, workspace_root: Path) -> None:
    run_cli("workspace", "create", "project", str(workspace_root))
    target = workspace_root / "noperm.txt"
    target.write_text("secret\n", encoding="utf-8")
    _deny(target)
    try:
        code, out, _ = run_cli("--json", "file", "read", "noperm.txt")
    finally:
        _restore(target, 0o600)

    payload = json.loads(out)
    assert code == exit_codes.STORAGE_ERROR == 9
    assert payload["error"]["code"] == "STORAGE_ERROR"
    assert payload["error"]["retryable"] is False
    assert "noperm.txt" in payload["error"]["message"]
    _assert_no_host_path(out, workspace_root)


def test_unreadable_file_in_read_many_is_a_storage_error(run_cli, workspace_root: Path) -> None:
    run_cli("workspace", "create", "project", str(workspace_root))
    target = workspace_root / "noperm.txt"
    target.write_text("secret\n", encoding="utf-8")
    _deny(target)
    try:
        code, out, _ = run_cli("--json", "file", "read-many", "notes.txt", "noperm.txt")
    finally:
        _restore(target, 0o600)

    payload = json.loads(out)
    assert code == exit_codes.STORAGE_ERROR == 9
    assert payload["ok"] is False
    failed = payload["files"][1]
    assert failed["ok"] is False
    assert failed["error"]["code"] == "STORAGE_ERROR"
    assert "noperm.txt" in failed["error"]["message"]
    _assert_no_host_path(out, workspace_root)


def test_unreadable_directory_listing_is_a_storage_error(run_cli, workspace_root: Path) -> None:
    run_cli("workspace", "create", "project", str(workspace_root))
    locked = workspace_root / "locked"
    locked.mkdir()
    (locked / "inner.txt").write_text("hidden\n", encoding="utf-8")
    _deny(locked)
    try:
        code, out, _ = run_cli("--json", "file", "list", "locked")
    finally:
        _restore(locked, 0o700)

    payload = json.loads(out)
    assert code == exit_codes.STORAGE_ERROR == 9
    assert payload["error"]["code"] == "STORAGE_ERROR"
    assert "locked" in payload["error"]["message"]
    _assert_no_host_path(out, workspace_root)


def test_unreadable_file_info_and_hash_are_storage_errors(run_cli, workspace_root: Path) -> None:
    run_cli("workspace", "create", "project", str(workspace_root))
    target = workspace_root / "noperm.txt"
    target.write_text("secret\n", encoding="utf-8")
    _deny(target)
    try:
        info_code, info_out, _ = run_cli("--json", "file", "info", "noperm.txt")
        hash_code, hash_out, _ = run_cli("--json", "file", "hash", "noperm.txt")
    finally:
        _restore(target, 0o600)

    for code, out in ((info_code, info_out), (hash_code, hash_out)):
        payload = json.loads(out)
        assert code == exit_codes.STORAGE_ERROR == 9
        assert payload["error"]["code"] == "STORAGE_ERROR"
        assert "noperm.txt" in payload["error"]["message"]
        _assert_no_host_path(out, workspace_root)


def test_unreadable_content_search_root_is_a_storage_error(run_cli, workspace_root: Path) -> None:
    run_cli("workspace", "create", "project", str(workspace_root))
    locked = workspace_root / "locked"
    locked.mkdir()
    (locked / "inner.txt").write_text("hidden\n", encoding="utf-8")
    _deny(locked)
    try:
        code, out, _ = run_cli("--json", "file", "grep", "locked", "hidden")
    finally:
        _restore(locked, 0o700)

    payload = json.loads(out)
    assert code == exit_codes.STORAGE_ERROR == 9
    assert payload["error"]["code"] == "STORAGE_ERROR"
    assert "locked" in payload["error"]["message"]
    _assert_no_host_path(out, workspace_root)


def test_a_genuine_internal_fault_still_maps_to_internal_error(
    run_cli, workspace_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The narrowing must not swallow real bugs: an unexpected OSError stays exit 1."""

    run_cli("workspace", "create", "project", str(workspace_root))

    def explode(self, path: str) -> str:  # pragma: no cover - never actually runs logic
        raise OSError("simulated internal fault")

    monkeypatch.setattr("filekeeper.core.service.FilekeeperService.get_file_hash", explode)

    code, out, _ = run_cli("--json", "file", "hash", "notes.txt")
    payload = json.loads(out)
    assert code == exit_codes.GENERAL_ERROR == 1
    assert payload["error"]["code"] == "INTERNAL_ERROR"
