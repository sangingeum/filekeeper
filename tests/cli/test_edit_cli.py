"""CLI coverage for the editing primitives."""

from __future__ import annotations

import json
from pathlib import Path

from filekeeper.cli import exit_codes
from filekeeper.core.diffing import unified_diff_text
from filekeeper.core.safety.hashing import hash_bytes


def _parse(out: str) -> dict:
    return json.loads(out)


def _register(run_cli, workspace_root: Path) -> None:
    run_cli("workspace", "create", "project", str(workspace_root))


def test_replace_text_reports_the_stage_and_operation_id(run_cli, workspace_root: Path) -> None:
    _register(run_cli, workspace_root)
    code, out, err = run_cli(
        "file",
        "replace-text",
        "notes.txt",
        "--old",
        "beta",
        "--new",
        "BETA",
        "--first",
        "--workspace",
        "project",
    )

    assert code == 0, err
    assert "match_stage: exact" in out
    assert "match_count: 1" in out
    assert "operation_id: op_" in out
    assert (workspace_root / "notes.txt").read_text(encoding="utf-8") == ("alpha\nBETA\ngamma\n")


def test_replace_text_json_carries_the_same_stage(run_cli, workspace_root: Path) -> None:
    _register(run_cli, workspace_root)
    code, out, err = run_cli(
        "file",
        "replace-text",
        "notes.txt",
        "--old",
        "beta",
        "--new",
        "BETA",
        "--all",
        "--json",
        "--workspace",
        "project",
    )

    assert code == 0, err
    payload = _parse(out)
    assert payload["match_stage"] == "exact"
    assert payload["match_count"] == 1
    assert payload["operation_id"].startswith("op_")


def test_replace_text_dry_run_prints_a_diff_and_no_operation_id(
    run_cli, workspace_root: Path
) -> None:
    _register(run_cli, workspace_root)
    before = (workspace_root / "notes.txt").read_bytes()

    code, out, err = run_cli(
        "file",
        "replace-text",
        "notes.txt",
        "--old",
        "beta",
        "--new",
        "BETA",
        "--first",
        "--dry-run",
        "--workspace",
        "project",
    )

    assert code == 0, err
    assert "diff:" in out
    assert "-beta" in out
    assert "+BETA" in out
    assert "operation_id: none" in out
    assert (workspace_root / "notes.txt").read_bytes() == before


def test_line_editing_commands_each_record_an_operation(run_cli, workspace_root: Path) -> None:
    _register(run_cli, workspace_root)
    commands = [
        ("replace-lines", "notes.txt", "--start", "2", "--end", "2", "--replacement", "B2\n"),
        ("insert-lines", "notes.txt", "--position", "end", "--content", "delta\n"),
        ("delete-lines", "notes.txt", "--start", "1", "--end", "1"),
    ]
    for command in commands:
        code, out, err = run_cli("file", *command, "--workspace", "project")
        assert code == 0, err
        assert "operation_id: op_" in out


def test_apply_patch_command_records_one_operation(run_cli, workspace_root: Path) -> None:
    _register(run_cli, workspace_root)
    patch = unified_diff_text(
        (workspace_root / "notes.txt").read_text(encoding="utf-8"),
        "alpha\nBETA\ngamma\n",
        fromfile="a/notes.txt",
        tofile="b/notes.txt",
    )
    patch_file = workspace_root / "change.patch"
    patch_file.write_text(patch, encoding="utf-8")

    code, out, err = run_cli(
        "file",
        "apply-patch",
        str(patch_file),
        "--workspace",
        "project",
    )

    assert code == 0, err
    assert "patch_files: 1" in out
    assert "hunk 1: applied" in out
    assert "operation_id: op_" in out
    assert (workspace_root / "notes.txt").read_text(encoding="utf-8") == ("alpha\nBETA\ngamma\n")


def test_apply_patch_refuses_a_hunk_that_matches_only_elsewhere(
    run_cli, workspace_root: Path
) -> None:
    """The refusal is a distinct reason, its own exit code and no mutation."""

    _register(run_cli, workspace_root)
    target = workspace_root / "stale.txt"
    target.write_text("one\ntwo\nthree\nfour\nfive\nsix\nunique-context\n", encoding="utf-8")
    before = target.read_bytes()
    patch_file = workspace_root / "stale.patch"
    patch_file.write_text(
        "--- a/stale.txt\n+++ b/stale.txt\n@@ -4,1 +4,1 @@\n-unique-context\n+CHANGED\n",
        encoding="utf-8",
    )

    code, out, err = run_cli(
        "file",
        "apply-patch",
        str(patch_file),
        "--json",
        "--workspace",
        "project",
    )

    assert code == exit_codes.PATCH_FAILED, err
    payload = _parse(out)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "PATCH_FAILED"
    details = payload["error"]["details"]
    assert details["reason"] == "preimage_elsewhere"
    assert details["declared_start"] == 4
    assert details["positions"] == [7]
    assert target.read_bytes() == before


def test_apply_patch_expected_hash_refuses_a_stale_patch(run_cli, workspace_root: Path) -> None:
    _register(run_cli, workspace_root)
    target = workspace_root / "notes.txt"
    digest = hash_bytes(target.read_bytes())
    patch = unified_diff_text(
        target.read_text(encoding="utf-8"),
        "alpha\nBETA\ngamma\n",
        fromfile="a/notes.txt",
        tofile="b/notes.txt",
    )
    patch_file = workspace_root / "guarded.patch"
    patch_file.write_text(patch, encoding="utf-8")
    before = target.read_bytes()

    code, _out, err = run_cli(
        "file",
        "apply-patch",
        str(patch_file),
        "--expected-hash",
        "sha256:" + "0" * 64,
        "--workspace",
        "project",
    )
    assert code == exit_codes.CONFLICT, err
    assert "CONTENT_CONFLICT" in err
    assert target.read_bytes() == before

    code, _out, err = run_cli(
        "file",
        "apply-patch",
        str(patch_file),
        "--expected-hash",
        digest,
        "--workspace",
        "project",
    )
    assert code == 0, err
    assert target.read_text(encoding="utf-8") == "alpha\nBETA\ngamma\n"
