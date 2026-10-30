"""A patch that cannot remove its target reports workspace-relative paths only.

Deleting a file through ``apply-patch`` needs write permission on the parent
directory. When that directory is read-only the unlink fails, the files already
changed are rolled back, and the rollback itself cannot restore the file it
never managed to delete. The result is ``PARTIAL_RECOVERY`` (exit 8); every
string in the error details, ``cause`` included, must name workspace-relative
paths and never a resolved host path or a raw ``[Errno ...]`` repr.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

from filekeeper.cli import exit_codes


def _assert_no_absolute_path(value: object) -> None:
    """Walk every value in ``details``; no string may be rooted at ``/``."""

    if isinstance(value, dict):
        for entry in value.values():
            _assert_no_absolute_path(entry)
    elif isinstance(value, (list, tuple)):
        for entry in value:
            _assert_no_absolute_path(entry)
    elif isinstance(value, str):
        assert "[Errno" not in value, value
        for token in value.split():
            assert not token.startswith("/"), value


def test_deleting_inside_a_read_only_directory_reports_relative_paths(
    run_cli, workspace_root: Path
) -> None:
    run_cli("workspace", "create", "project", str(workspace_root))
    target_dir = workspace_root / "ro"
    target_dir.mkdir()
    deleted = target_dir / "del.txt"
    deleted.write_text("gone\n", encoding="utf-8")

    patch_file = workspace_root / "delete.patch"
    patch_file.write_text(
        "--- a/ro/del.txt\n+++ /dev/null\n@@ -1 +0,0 @@\n-gone\n",
        encoding="utf-8",
    )

    original_mode = stat.S_IMODE(target_dir.stat().st_mode)
    os.chmod(target_dir, 0o555)
    try:
        code, out, err = run_cli(
            "--json",
            "file",
            "apply-patch",
            str(patch_file),
            "--workspace",
            "project",
        )
    finally:
        os.chmod(target_dir, original_mode)

    payload = json.loads(out)
    assert code == exit_codes.PARTIAL_RECOVERY == 8, err
    error = payload["error"]
    assert error["code"] == "PARTIAL_RECOVERY"

    details = error.get("details", {})
    assert details["not_restored"] == ["ro/del.txt"]
    assert "ro/del.txt" in details["cause"]
    assert str(workspace_root) not in out
    _assert_no_absolute_path(details)
    assert deleted.exists()
