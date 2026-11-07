"""CLI behaviour: command tree, JSON output, exit codes, end-to-end flow."""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path

from filekeeper.cli import exit_codes


def test_help_succeeds(run_cli) -> None:
    code, out, _ = run_cli("--help")
    assert code == exit_codes.SUCCESS
    assert "workspace" in out
    assert "1-based" in out


def test_version(run_cli) -> None:
    code, out, _ = run_cli("--version")
    assert code == exit_codes.SUCCESS
    assert out.startswith("keeper ")


def test_init_creates_home_and_database(run_cli, home: Path) -> None:
    code, out, _ = run_cli("init")

    assert code == exit_codes.SUCCESS
    assert (home / "config.toml").is_file()
    assert (home / "metadata.sqlite3").is_file()
    assert "initialized" in out


def test_end_to_end_workspace_flow(run_cli, workspace_root: Path) -> None:
    assert run_cli("init")[0] == exit_codes.SUCCESS
    assert run_cli("workspace", "create", "project", str(workspace_root))[0] == exit_codes.SUCCESS

    code, out, _ = run_cli("workspace", "list")
    assert code == exit_codes.SUCCESS
    assert "project" in out

    assert run_cli("workspace", "use", "project")[0] == exit_codes.SUCCESS
    assert run_cli("workspace", "show", "project")[0] == exit_codes.SUCCESS

    code, out, _ = run_cli("file", "create", "todo.md", "--content", "# Todo\n")
    assert code == exit_codes.SUCCESS
    assert "created" in out

    code, out, _ = run_cli("file", "read", "todo.md")
    assert code == exit_codes.SUCCESS
    assert out == "# Todo\n"

    code, out, _ = run_cli("--json", "file", "read", "todo.md")
    payload = json.loads(out)
    assert payload["ok"] is True
    assert payload["path"] == "todo.md"
    assert payload["content"] == "# Todo\n"
    assert payload["start_line"] == 1

    code, out, _ = run_cli("file", "hash", "todo.md")
    assert code == exit_codes.SUCCESS
    assert out.startswith("sha256:")

    assert run_cli("file", "info", "todo.md")[0] == exit_codes.SUCCESS
    assert run_cli("file", "exists", "todo.md")[0] == exit_codes.SUCCESS
    assert run_cli("file", "exists", "absent.md")[0] == exit_codes.PATH_NOT_FOUND

    assert run_cli("dir", "create", "docs", "--parents")[0] == exit_codes.SUCCESS
    assert run_cli("workspace", "remove", "project")[0] == exit_codes.SUCCESS
    assert (workspace_root / "todo.md").is_file()


def test_json_flag_after_subcommand(run_cli, workspace_root: Path) -> None:
    run_cli("workspace", "create", "project", str(workspace_root))
    code, out, _ = run_cli("file", "list", ".", "--json")
    payload = json.loads(out)
    assert code == exit_codes.SUCCESS
    assert {entry["name"] for entry in payload["entries"]} == {"notes.txt", "sub"}


def test_line_range_flags(run_cli, workspace_root: Path) -> None:
    run_cli("workspace", "create", "project", str(workspace_root))
    code, out, _ = run_cli(
        "--root",
        str(workspace_root),
        "file",
        "read",
        "notes.txt",
        "--from-line",
        "2",
        "--to-line",
        "2",
    )
    assert code == exit_codes.SUCCESS
    assert out == "beta\n"


def test_missing_path_exits_three(run_cli, workspace_root: Path) -> None:
    run_cli("workspace", "create", "project", str(workspace_root))
    code, _, err = run_cli("file", "read", "nope.txt")
    assert code == exit_codes.PATH_NOT_FOUND
    assert "PATH_NOT_FOUND" in err


def test_workspace_escape_exits_four(run_cli, workspace_root: Path) -> None:
    run_cli("workspace", "create", "project", str(workspace_root))
    code, _, err = run_cli("file", "read", "../outside.txt")
    assert code == exit_codes.ACCESS_DENIED
    assert "PATH_OUTSIDE_WORKSPACE" in err


def test_stale_hash_write_exits_five_and_does_not_modify(run_cli, workspace_root: Path) -> None:
    run_cli("workspace", "create", "project", str(workspace_root))
    stale = "sha256:" + "2" * 64

    code, _, err = run_cli(
        "file", "write", "notes.txt", "--content", "clobber\n", "--expected-hash", stale
    )
    assert code == exit_codes.CONFLICT
    assert "CONTENT_CONFLICT" in err
    assert (workspace_root / "notes.txt").read_text(encoding="utf-8") == "alpha\nbeta\ngamma\n"


def test_secret_refusal_exits_ten_and_override_works(run_cli, workspace_root: Path) -> None:
    run_cli("workspace", "create", "project", str(workspace_root))

    code, _, err = run_cli("file", "create", ".env", "--content", "TOKEN=1\n")
    assert code == exit_codes.SENSITIVE_PATH_REFUSED
    assert "SENSITIVE_PATH_REFUSED" in err
    assert not (workspace_root / ".env").exists()

    code, _, _ = run_cli(
        "file", "create", ".env", "--content", "TOKEN=1\n", "--i-accept-secret-write-risk"
    )
    assert code == exit_codes.SUCCESS
    assert (workspace_root / ".env").read_text(encoding="utf-8") == "TOKEN=1\n"


def test_secret_refusal_names_the_cli_flag_only(run_cli, workspace_root: Path) -> None:
    """The CLI's hint names its own flag, never Core's keyword or MCP's argument."""

    run_cli("workspace", "create", "project", str(workspace_root))
    code, out, _ = run_cli("--json", "file", "create", ".env", "--content", "TOKEN=1\n")
    payload = json.loads(out)

    assert code == exit_codes.SENSITIVE_PATH_REFUSED
    message = payload["error"]["message"]
    assert "--i-accept-secret-write-risk" in message
    assert "allow_sensitive_write" not in message
    assert "i_accept_secret_write_risk" not in message
    assert set(payload["error"]["details"]) == {"path", "reason"}


def test_recursive_delete_needs_both_flags(run_cli, workspace_root: Path) -> None:
    run_cli("workspace", "create", "project", str(workspace_root))

    code, _, err = run_cli("dir", "delete", "sub", "--recursive")
    assert code == exit_codes.INVALID_ARGUMENT
    assert "confirmation" in err
    assert (workspace_root / "sub" / "nested.md").exists()

    code, _, _ = run_cli("dir", "delete", "sub", "--recursive", "--confirm-recursive")
    assert code == exit_codes.SUCCESS
    assert not (workspace_root / "sub").exists()


def test_dry_run_reports_without_writing(run_cli, workspace_root: Path) -> None:
    run_cli("workspace", "create", "project", str(workspace_root))
    code, out, _ = run_cli("file", "write", "notes.txt", "--content", "x\n", "--dry-run")

    assert code == exit_codes.SUCCESS
    assert "Would write" in out
    assert (workspace_root / "notes.txt").read_text(encoding="utf-8") == "alpha\nbeta\ngamma\n"


def test_json_error_shape(run_cli, workspace_root: Path) -> None:
    run_cli("workspace", "create", "project", str(workspace_root))
    code, out, _ = run_cli("--json", "file", "read", "absent.txt")
    payload = json.loads(out)

    assert code == exit_codes.PATH_NOT_FOUND
    assert payload["ok"] is False
    assert payload["error"]["code"] == "PATH_NOT_FOUND"
    assert payload["error"]["retryable"] is False


def test_no_workspace_selected_exits_two(run_cli) -> None:
    code, _, err = run_cli("file", "read", "anything.txt")
    assert code == exit_codes.INVALID_ARGUMENT
    assert "NO_WORKSPACE_SELECTED" in err


def test_search_and_grep_commands(run_cli, workspace_root: Path) -> None:
    run_cli("workspace", "create", "project", str(workspace_root))

    code, out, _ = run_cli("file", "search", ".", "*.md")
    assert code == exit_codes.SUCCESS
    assert out.strip() == "sub/nested.md"

    code, out, _ = run_cli("file", "grep", ".", "beta")
    assert code == exit_codes.SUCCESS
    assert out.strip() == "notes.txt:2:1: beta"


def test_stdin_content_is_used_when_provided(run_cli, workspace_root: Path, monkeypatch) -> None:
    run_cli("workspace", "create", "project", str(workspace_root))
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    class FakeStdin:
        def isatty(self) -> bool:
            return False

        def read(self) -> str:
            return "from stdin\n"

    monkeypatch.setattr("sys.stdin", FakeStdin())
    code, _, _ = run_cli("file", "create", "piped.txt")
    assert code == exit_codes.SUCCESS
    assert (workspace_root / "piped.txt").read_text(encoding="utf-8") == "from stdin\n"


def test_traversal_refusal_exits_four(run_cli, workspace_root: Path) -> None:
    run_cli("workspace", "create", "project", str(workspace_root))
    code, _, err = run_cli("file", "read", "../outside.txt")
    assert code == exit_codes.ACCESS_DENIED
    assert "PATH_OUTSIDE_WORKSPACE" in err


def test_absolute_path_outside_exits_six(run_cli, workspace_root: Path, tmp_path: Path) -> None:
    run_cli("workspace", "create", "project", str(workspace_root))
    outside = tmp_path / "elsewhere.txt"
    outside.write_text("secret\n", encoding="utf-8")

    code, _, err = run_cli("file", "read", str(outside))
    assert code == exit_codes.ABSOLUTE_PATH_OUTSIDE_WORKSPACE
    assert "ABSOLUTE_PATH_OUTSIDE_WORKSPACE" in err
    assert "PATH_OUTSIDE_WORKSPACE" not in err.replace("ABSOLUTE_PATH_OUTSIDE_WORKSPACE", "")


def test_symlink_escape_refusal_exits_eleven(run_cli, workspace_root: Path, tmp_path: Path) -> None:
    run_cli("workspace", "create", "project", str(workspace_root))
    outside = tmp_path / "target.txt"
    outside.write_text("secret\n", encoding="utf-8")
    os.symlink(outside, workspace_root / "escape.txt")

    code, _, err = run_cli("file", "read", "escape.txt")
    assert code == exit_codes.SYMLINK_ESCAPE_REFUSED
    assert "SYMLINK_ESCAPE_REFUSED" in err
    assert outside.read_text(encoding="utf-8") == "secret\n"


def test_read_only_directory_write_is_a_storage_error(run_cli, workspace_root: Path) -> None:
    run_cli("workspace", "create", "project", str(workspace_root))
    read_only = workspace_root / "locked"
    read_only.mkdir()
    os.chmod(read_only, 0o555)
    try:
        code, _, err = run_cli("file", "create", "locked/blocked.txt", "--content", "hello\n")
    finally:
        os.chmod(read_only, 0o755)

    assert code == exit_codes.STORAGE_ERROR
    assert "STORAGE_ERROR" in err
    assert "Permission denied" in err
    # The message line never embeds a host path. The details deliberately do:
    # naming the resolved target and the workspace root is what lets a caller see
    # where the write went instead of guessing.
    assert re.search(r"(?<![\w.-])/[^\s'\"]+", err.splitlines()[0]) is None, err
    assert f"resolved_path: {workspace_root.resolve() / 'locked' / 'blocked.txt'}" in err
    assert f"workspace_root: {workspace_root.resolve()}" in err
    assert ".keeper-tmp-" not in err
    assert not (read_only / "blocked.txt").exists()


def test_non_empty_directory_advice_names_the_cli_flags(run_cli, workspace_root: Path) -> None:
    run_cli("workspace", "create", "project", str(workspace_root))

    code, _, err = run_cli("dir", "delete", "sub")
    assert code == exit_codes.INVALID_ARGUMENT
    assert "--recursive --confirm-recursive" in err
    assert "recursive=True" not in err


def test_recursive_delete_warning_states_recoverable_but_not_atomic(
    run_cli, workspace_root: Path
) -> None:
    run_cli("workspace", "create", "project", str(workspace_root))

    code, out, _ = run_cli("dir", "delete", "sub", "--recursive", "--confirm-recursive")
    assert code == exit_codes.SUCCESS
    assert "recoverable through history" in out
    assert "never atomic across the tree" in out
    assert "operation_id: op_" in out
    assert "recoverable only once history lands" not in out


def test_workspace_list_and_show_mark_a_missing_root(run_cli, workspace_root: Path) -> None:
    run_cli("workspace", "create", "project", str(workspace_root))
    run_cli("workspace", "use", "project")
    shutil.rmtree(workspace_root)

    code, out, _ = run_cli("--json", "workspace", "list")
    assert code == exit_codes.SUCCESS
    payload = json.loads(out)
    [entry] = payload["workspaces"]
    assert entry["root_exists"] is False
    assert payload["warnings"] == [
        "workspace 'project' is registered at a root that no longer exists"
    ]

    code, out, _ = run_cli("workspace", "list")
    assert code == exit_codes.SUCCESS
    assert "root missing" in out

    code, out, _ = run_cli("--json", "workspace", "show", "project")
    assert code == exit_codes.SUCCESS
    assert json.loads(out)["workspace"]["root_exists"] is False

    code, out, _ = run_cli("workspace", "show", "project")
    assert code == exit_codes.SUCCESS
    assert "root_exists: false" in out


def test_workspace_remove_accepts_a_stale_registration(run_cli, workspace_root: Path) -> None:
    run_cli("workspace", "create", "project", str(workspace_root))
    run_cli("workspace", "use", "project")
    shutil.rmtree(workspace_root)

    code, out, _ = run_cli("workspace", "remove", "project")
    assert code == exit_codes.SUCCESS
    assert "its files were not touched" in out

    code, out, _ = run_cli("--json", "workspace", "list")
    payload = json.loads(out)
    assert payload["workspaces"] == []
    assert payload["default_workspace"] is None
