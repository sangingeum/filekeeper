"""One successful call per advertised tool, asserted against the Core shape.

The point is delegation, not re-testing Core: the payload must be the Core result
field for field, with the ``ok`` envelope added and nothing renamed.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

from harness import ServerTarget, SessionResult, exercise

from filekeeper.core.diffing import unified_diff_text
from filekeeper.core.models import (
    DirectoryListing,
    FileReadResult,
    MutationResult,
    PathInfo,
)


def _core_fields(model: type[Any]) -> set[str]:
    return {field.name for field in dataclasses.fields(model)}


def _patch(path: str, before: str, after: str) -> str:
    return unified_diff_text(before, after, fromfile=f"a/{path}", tofile=f"b/{path}")


def _creation_hash(result: SessionResult) -> dict[str, Any]:
    return {"expected_hash": result.by_name("file_create").structured["after_hash"]}


def _operation_id(result: SessionResult, tool: str) -> dict[str, Any]:
    return {"operation_id": result.by_name(tool).structured["operation_id"]}


def test_every_read_tool_delegates_and_keeps_the_core_shape(target: ServerTarget) -> None:
    result = exercise(
        target,
        [
            ("workspace_list", {}),
            ("workspace_info", {}),
            ("file_read", {"path": "notes.txt", "start_line": 2, "end_line": 3}),
            ("file_read_many", {"paths": ["notes.txt", "sub/nested.md"]}),
            ("directory_list", {"path": ".", "recursive": True}),
            ("file_info", {"path": "notes.txt"}),
            ("file_search", {"path": ".", "glob": "*.md"}),
            ("content_search", {"path": ".", "query": "beta"}),
        ],
    )

    listing = result.by_name("workspace_list")
    assert listing.is_error is False
    assert listing.structured["ok"] is True
    assert listing.structured["workspaces"] == []

    info = result.by_name("workspace_info")
    assert info.structured["selected"] == "workspace"
    assert info.structured["registered"] is False

    read = result.by_name("file_read")
    assert set(read.structured) == _core_fields(FileReadResult) | {"ok"}
    assert read.structured["content"] == "beta\ngamma\n"
    assert read.structured["start_line"] == 2
    assert read.structured["end_line"] == 3
    assert read.structured["total_lines"] == 3

    many = result.by_name("file_read_many")
    assert many.structured["ok"] is True
    assert many.structured["count"] == 2
    assert [entry["path"] for entry in many.structured["files"]] == [
        "notes.txt",
        "sub/nested.md",
    ]
    assert all(entry["ok"] for entry in many.structured["files"])

    directory = result.by_name("directory_list")
    assert set(directory.structured) == _core_fields(DirectoryListing) | {"ok"}
    kinds = {entry["path"]: entry["kind"] for entry in directory.structured["entries"]}
    assert kinds["notes.txt"] == "file"
    assert kinds["sub"] == "directory"

    metadata = result.by_name("file_info")
    assert set(metadata.structured) == _core_fields(PathInfo) | {"ok"}
    assert metadata.structured["content_hash"].startswith("sha256:")
    assert metadata.structured["line_count"] == 3

    paths = result.by_name("file_search")
    assert paths.structured["paths"] == ["sub/nested.md"]

    search = result.by_name("content_search")
    assert search.structured["matches"][0]["path"] == "notes.txt"
    assert search.structured["matches"][0]["line_number"] == 2
    assert search.structured["matches"][0]["line"] == "beta"


def test_every_mutation_and_edit_tool_delegates_with_undo_metadata(
    target: ServerTarget,
) -> None:
    # The earlier edits leave exactly this content in place before the patch.
    patch = _patch("notes.txt", "ALPHA\nBETA\ngamma\n", "ALPHA\nBETA\nGAMMA\n")

    result = exercise(
        target,
        [
            ("file_create", {"path": "created.txt", "content": "one\ntwo\n"}),
            (
                "file_write",
                lambda previous: {
                    "path": "created.txt",
                    "content": "one\nTWO\n",
                    **_creation_hash(previous),
                },
            ),
            ("file_copy", {"source": "created.txt", "destination": "copied.txt"}),
            ("file_move", {"source": "copied.txt", "destination": "moved.txt"}),
            ("file_delete", {"path": "moved.txt"}),
            ("directory_create", {"path": "newdir"}),
            ("directory_delete", {"path": "newdir"}),
            (
                "file_replace_text",
                {"path": "notes.txt", "old": "beta", "new": "BETA", "match": "first"},
            ),
            (
                "file_replace_lines",
                {"path": "notes.txt", "start_line": 1, "end_line": 1, "replacement": "ALPHA\n"},
            ),
            (
                "file_insert_lines",
                {"path": "notes.txt", "content": "inserted\n", "line": 1, "position": "after"},
            ),
            ("file_delete_lines", {"path": "notes.txt", "start_line": 2, "end_line": 2}),
            ("file_apply_patch", {"patch": patch}),
            ("history_list", {"limit": 50}),
            ("history_get", lambda previous: _operation_id(previous, "file_apply_patch")),
            ("history_diff", lambda previous: _operation_id(previous, "file_apply_patch")),
            (
                "undo_operation",
                lambda previous: _operation_id(previous, "file_apply_patch"),
            ),
            ("undo_last", {}),
        ],
    )

    for name in (
        "file_create",
        "file_write",
        "file_copy",
        "file_move",
        "file_delete",
        "directory_create",
        "directory_delete",
        "file_replace_text",
        "file_replace_lines",
        "file_insert_lines",
        "file_delete_lines",
        "file_apply_patch",
        "undo_operation",
        "undo_last",
    ):
        reply = result.by_name(name)
        assert reply.is_error is False, (name, reply.structured)
        assert set(reply.structured) == _core_fields(MutationResult) | {"ok"}, name
        assert reply.structured["operation_id"], name
        assert reply.structured["undo_available"] is True, name

    created = result.by_name("file_create").structured
    assert created["changed"] is True
    assert created["after_hash"].startswith("sha256:")

    replaced = result.by_name("file_replace_text").structured
    assert replaced["match_stage"] == "exact"
    assert replaced["match_count"] == 1

    patched = result.by_name("file_apply_patch").structured
    assert patched["patch_files"][0]["path"] == "notes.txt"
    assert patched["patch_files"][0]["hunks"][0]["status"] == "applied"

    history = result.by_name("history_list").structured
    assert history["count"] == 12
    assert history["operations"][0]["operation_id"]

    record = result.by_name("history_get").structured["operation"]
    assert record["operation_type"] == "apply_patch"

    diff = result.by_name("history_diff").structured
    assert diff["files"][0]["unified_diff"]

    # undo_operation restored the pre-patch bytes; undo_last then undid that undo,
    # which is the supported way to go forward again.
    assert (target.root / "notes.txt").read_text(encoding="utf-8") == "ALPHA\nBETA\nGAMMA\n"


def test_workspace_info_reports_the_package_version(target: ServerTarget) -> None:
    """The version is readable over MCP and matches the built package version.

    The expectation comes from the distribution metadata (the single source of
    truth a release bumps), never from a literal in the test.
    """

    import tomllib

    pyproject = (
        Path(__file__).resolve().parents[2] / "packages" / "filekeeper-mcp" / "pyproject.toml"
    )
    with pyproject.open("rb") as handle:
        expected = str(tomllib.load(handle)["project"]["version"])

    result = exercise(target, [("workspace_info", {})])
    payload = result.by_name("workspace_info").structured

    assert payload["ok"] is True
    assert payload["version"] == expected
    assert payload["selected"] == "workspace"


def test_workspace_list_reports_a_registered_workspace(tmp_path: Path) -> None:
    from harness import ServerTarget

    from filekeeper import Filekeeper

    root = tmp_path / "registered"
    root.mkdir()
    home = tmp_path / "home"
    app = Filekeeper(home=home)
    app.create_workspace("project", root)

    result = exercise(ServerTarget(root=root, home=home), [("workspace_list", {})])
    payload = result.by_name("workspace_list").structured

    assert [workspace["name"] for workspace in payload["workspaces"]] == ["project"]
    assert payload["workspaces"][0]["root_exists"] is True
    assert payload["default_workspace"] is None


def test_workspace_list_flags_a_registration_whose_root_is_gone(tmp_path: Path) -> None:
    import shutil

    from harness import ServerTarget

    from filekeeper import Filekeeper

    root = tmp_path / "gone"
    root.mkdir()
    home = tmp_path / "home"
    Filekeeper(home=home).create_workspace("stale", root)
    shutil.rmtree(root)

    result = exercise(
        ServerTarget(root=None, home=home),
        [("workspace_list", {}), ("workspace_info", {"workspace": "stale"})],
    )
    listing = result.by_name("workspace_list").structured
    [entry] = listing["workspaces"]
    assert entry["name"] == "stale"
    assert entry["root_exists"] is False
    assert any(
        "stale" in warning and "no longer exists" in warning for warning in listing["warnings"]
    )

    info = result.by_name("workspace_info").structured
    assert info["workspace"]["root_exists"] is False
    assert any("stale" in warning and "no longer exists" in warning for warning in info["warnings"])


def test_history_list_and_undo_share_one_operation_key(target: ServerTarget) -> None:
    result = exercise(
        target,
        [
            ("file_write", {"path": "notes.txt", "content": "changed\n"}),
            ("history_list", {}),
            (
                "history_get",
                lambda seen: {
                    "operation_id": seen.by_name("history_list").structured["operations"][0][
                        "operation_id"
                    ]
                },
            ),
            (
                "undo_operation",
                lambda seen: {
                    "operation_id": seen.by_name("history_list").structured["operations"][0][
                        "operation_id"
                    ]
                },
            ),
        ],
    )

    listing = result.by_name("history_list").structured
    operation = listing["operations"][0]
    assert "id" not in operation
    operation_id = operation["operation_id"]
    assert operation_id.startswith("op_")

    shown = result.by_name("history_get").structured["operation"]
    assert "id" not in shown
    assert shown["operation_id"] == operation_id

    undone = result.by_name("undo_operation").structured
    assert undone["ok"] is True
    assert undone["operation_id"] != operation_id
    assert (target.root / "notes.txt").read_text(encoding="utf-8") == ("alpha\nbeta\ngamma\n")
