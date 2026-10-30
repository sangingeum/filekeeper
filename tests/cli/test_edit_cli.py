"""CLI coverage for the editing primitives."""

from __future__ import annotations

import json
from pathlib import Path

from filekeeper.core.diffing import unified_diff_text


def _parse(out: str) -> dict:
    return json.loads(out)


def _register(run_cli, workspace_root: Path) -> None:
    run_cli("workspace", "create", "project", str(workspace_root))


def test_replace_text_reports_the_stage_and_operation_id(
    run_cli, workspace_root: Path
) -> None:
    _register(run_cli, workspace_root)
    code, out, err = run_cli(
        "file", "replace-text", "notes.txt",
        "--old", "beta", "--new", "BETA", "--first",
        "--workspace", "project",
    )

    assert code == 0, err
    assert "match_stage: exact" in out
    assert "match_count: 1" in out
    assert "operation_id: op_" in out
    assert (workspace_root / "notes.txt").read_text(encoding="utf-8") == (
        "alpha\nBETA\ngamma\n"
    )


def test_replace_text_json_carries_the_same_stage(
    run_cli, workspace_root: Path
) -> None:
    _register(run_cli, workspace_root)
    code, out, err = run_cli(
        "file", "replace-text", "notes.txt",
        "--old", "beta", "--new", "BETA", "--all", "--json",
        "--workspace", "project",
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
        "file", "replace-text", "notes.txt",
        "--old", "beta", "--new", "BETA", "--first", "--dry-run",
        "--workspace", "project",
    )

    assert code == 0, err
    assert "diff:" in out
    assert "-beta" in out
    assert "+BETA" in out
    assert "operation_id: none" in out
    assert (workspace_root / "notes.txt").read_bytes() == before


def test_line_editing_commands_each_record_an_operation(
    run_cli, workspace_root: Path
) -> None:
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


def test_apply_patch_command_records_one_operation(
    run_cli, workspace_root: Path
) -> None:
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
        "file", "apply-patch", str(patch_file),
        "--workspace", "project",
    )

    assert code == 0, err
    assert "patch_files: 1" in out
    assert "hunk 1: applied" in out
    assert "operation_id: op_" in out
    assert (workspace_root / "notes.txt").read_text(encoding="utf-8") == (
        "alpha\nBETA\ngamma\n"
    )
