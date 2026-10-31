"""The ``keeper history`` command family and its stable JSON contract."""

from __future__ import annotations

import json
from pathlib import Path

from filekeeper.cli import exit_codes


def _operation_id(out: str) -> str:
    for line in out.splitlines():
        if line.startswith("operation_id: "):
            return line.split(": ", 1)[1]
    raise AssertionError(f"no operation id in output: {out!r}")


def test_history_list_show_and_diff_have_a_stable_json_contract(
    run_cli, workspace_root: Path
) -> None:
    run_cli("workspace", "create", "project", str(workspace_root))
    code, out, _ = run_cli(
        "file", "write", "notes.txt", "--content", "alpha\nbeta\ndelta\n", "--json"
    )
    assert code == exit_codes.SUCCESS
    operation_id = json.loads(out)["operation_id"]
    assert operation_id.startswith("op_")

    code, out, _ = run_cli("history", "list", "--json")
    assert code == exit_codes.SUCCESS
    listing = json.loads(out)
    assert listing["ok"] is True
    assert listing["count"] >= 1
    assert listing["operations"][0]["operation_id"] == operation_id

    code, out, _ = run_cli("history", "show", operation_id, "--json")
    assert code == exit_codes.SUCCESS
    operation = json.loads(out)["operation"]
    assert operation["operation_type"] == "write_file"
    assert operation["status"] == "committed"
    assert operation["items"][0]["relative_path"] == "notes.txt"

    code, out, _ = run_cli("history", "diff", operation_id, "--json")
    assert code == exit_codes.SUCCESS
    diff_payload = json.loads(out)
    assert diff_payload["operation_id"] == operation_id
    unified = "".join(entry["unified_diff"] for entry in diff_payload["files"])
    assert "-gamma" in unified
    assert "+delta" in unified


def test_history_list_filters_by_path_and_actor(run_cli, workspace_root: Path) -> None:
    run_cli("workspace", "create", "project", str(workspace_root))
    run_cli("file", "write", "notes.txt", "--content", "one\n", "--actor", "writer")
    run_cli("file", "create", "other.txt", "--content", "two\n", "--actor", "other")

    code, out, _ = run_cli("history", "list", "--path", "notes.txt", "--json")
    assert code == exit_codes.SUCCESS
    listing = json.loads(out)
    assert listing["count"] == 1
    assert listing["operations"][0]["target_path"] == "notes.txt"

    code, out, _ = run_cli("history", "list", "--actor", "writer", "--json")
    assert code == exit_codes.SUCCESS
    assert json.loads(out)["count"] == 1


def test_history_diff_prints_a_unified_diff_not_the_file_body(
    run_cli, workspace_root: Path
) -> None:
    run_cli("workspace", "create", "project", str(workspace_root))
    code, out, _ = run_cli("file", "write", "notes.txt", "--content", "alpha\nbeta\ndelta\n")
    operation_id = _operation_id(out)

    code, out, _ = run_cli("history", "diff", operation_id)

    assert code == exit_codes.SUCCESS
    assert out.startswith("--- a/notes.txt")
    assert "-gamma" in out
    assert "+delta" in out


def test_history_undo_restores_the_file(run_cli, workspace_root: Path) -> None:
    run_cli("workspace", "create", "project", str(workspace_root))
    original = (workspace_root / "notes.txt").read_text(encoding="utf-8")
    code, out, _ = run_cli("file", "write", "notes.txt", "--content", "changed\n")
    operation_id = _operation_id(out)
    assert (workspace_root / "notes.txt").read_text(encoding="utf-8") == "changed\n"

    code, out, _ = run_cli("history", "undo", operation_id, "--json")

    assert code == exit_codes.SUCCESS
    assert json.loads(out)["ok"] is True
    assert (workspace_root / "notes.txt").read_text(encoding="utf-8") == original


def test_history_undo_conflict_exits_with_its_own_code(run_cli, workspace_root: Path) -> None:
    run_cli("workspace", "create", "project", str(workspace_root))
    code, out, _ = run_cli("file", "write", "notes.txt", "--content", "changed\n")
    operation_id = _operation_id(out)
    (workspace_root / "notes.txt").write_text("later\n", encoding="utf-8")

    code, out, _ = run_cli("history", "undo", operation_id, "--json")

    assert code == exit_codes.UNDO_CONFLICT == 12
    payload = json.loads(out)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "UNDO_CONFLICT"
    assert (workspace_root / "notes.txt").read_text(encoding="utf-8") == "later\n"


def test_history_undo_last_stays_inside_the_selected_workspace(
    run_cli, workspace_root: Path, tmp_path: Path
) -> None:
    other = tmp_path / "other"
    other.mkdir()
    (other / "keep.txt").write_text("keep\n", encoding="utf-8")
    run_cli("workspace", "create", "project", str(workspace_root))
    run_cli("workspace", "create", "other", str(other))
    run_cli("--workspace", "other", "file", "write", "keep.txt", "--content", "other\n")

    code, _out, _ = run_cli(
        "--workspace", "project", "file", "create", "here.txt", "--content", "x\n"
    )
    assert code == exit_codes.SUCCESS

    code, _out, _ = run_cli("history", "undo-last", "--workspace", "project", "--json")

    assert code == exit_codes.SUCCESS
    assert not (workspace_root / "here.txt").exists()
    assert (other / "keep.txt").read_text(encoding="utf-8") == "other\n"


def test_history_show_of_an_unknown_operation_is_a_path_error(
    run_cli, workspace_root: Path
) -> None:
    run_cli("workspace", "create", "project", str(workspace_root))
    run_cli("file", "write", "notes.txt", "--content", "one\n")

    code, out, _ = run_cli("history", "show", "op_missing", "--json")

    assert code == exit_codes.PATH_NOT_FOUND
    assert json.loads(out)["error"]["code"] == "OPERATION_NOT_FOUND"


def test_history_diff_keeps_lines_apart_when_the_file_has_no_final_newline(
    run_cli, workspace_root: Path
) -> None:
    run_cli("workspace", "create", "project", str(workspace_root))
    (workspace_root / "plain.txt").write_text("alpha", encoding="utf-8")
    code, out, _ = run_cli("file", "write", "plain.txt", "--content", "beta")
    operation_id = _operation_id(out)

    code, out, _ = run_cli("history", "diff", operation_id, "--json")
    assert code == exit_codes.SUCCESS
    unified = json.loads(out)["files"][0]["unified_diff"]

    assert "-alpha\n" in unified
    assert "+beta\n" in unified
    assert all(line.endswith("\n") for line in unified.splitlines(keepends=True))
    assert unified.count("\\ No newline at end of file") == 2

    code, out, _ = run_cli("history", "diff", operation_id)
    assert code == exit_codes.SUCCESS
    lines = out.splitlines()
    assert "-alpha" in lines
    assert "+beta" in lines


def test_history_list_and_undo_use_the_same_operation_key(
    run_cli, workspace_root: Path
) -> None:
    run_cli("workspace", "create", "project", str(workspace_root))
    run_cli("file", "write", "notes.txt", "--content", "changed\n")

    code, out, _ = run_cli("history", "list", "--json")
    assert code == exit_codes.SUCCESS
    listing = json.loads(out)
    operation = listing["operations"][0]
    assert "id" not in operation
    operation_id = operation["operation_id"]
    assert operation_id.startswith("op_")

    code, out, _ = run_cli("history", "show", operation_id, "--json")
    assert code == exit_codes.SUCCESS
    shown = json.loads(out)["operation"]
    assert "id" not in shown
    assert shown["operation_id"] == operation_id

    code, out, _ = run_cli("history", "undo", operation_id, "--json")
    assert code == exit_codes.SUCCESS
    result = json.loads(out)
    assert result["ok"] is True
    assert result["operation_id"].startswith("op_")
    assert (workspace_root / "notes.txt").read_text(encoding="utf-8") == (
        "alpha\nbeta\ngamma\n"
    )


def test_history_undo_refuses_a_copy_whose_destination_was_moved_away(
    run_cli, workspace_root: Path
) -> None:
    run_cli("workspace", "create", "project", str(workspace_root))
    code, _out, _ = run_cli("dir", "create", "src")
    assert code == exit_codes.SUCCESS
    code, _out, _ = run_cli("file", "create", "src/a.txt", "--content", "alpha", "--json")
    assert code == exit_codes.SUCCESS
    code, out, _ = run_cli("file", "copy", "src/a.txt", "src/b.txt", "--json")
    assert code == exit_codes.SUCCESS
    copy_id = json.loads(out)["operation_id"]
    code, _out, _ = run_cli("file", "move", "src/b.txt", "src/b-renamed.txt", "--json")
    assert code == exit_codes.SUCCESS

    code, out, _ = run_cli("history", "undo", copy_id, "--json")

    assert code == exit_codes.UNDO_CONFLICT == 12
    payload = json.loads(out)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "UNDO_CONFLICT"
    assert "src/b.txt" in payload["error"]["message"]
    assert payload["error"]["details"]["conflicting_paths"] == "src/b.txt"
    assert not (workspace_root / "src" / "b.txt").exists()
    assert (workspace_root / "src" / "b-renamed.txt").read_text(encoding="utf-8") == "alpha"
