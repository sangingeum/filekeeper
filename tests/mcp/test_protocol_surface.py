"""Handshake, tools/list and the published schema.

The session is a real stdio subprocess, so this is the protocol as a host sees
it, not an in-process approximation.
"""

from __future__ import annotations

from harness import ServerTarget, exercise

from filekeeper_mcp.tools import TOOL_BINDINGS

READ_ONLY_TOOLS = {
    "workspace_list",
    "workspace_info",
    "file_read",
    "file_read_many",
    "directory_list",
    "file_info",
    "file_search",
    "content_search",
    "history_list",
    "history_get",
    "history_diff",
}

DESTRUCTIVE_TOOLS = {
    "file_create",
    "file_write",
    "file_copy",
    "file_move",
    "file_delete",
    "directory_delete",
    "file_replace_text",
    "file_replace_lines",
    "file_delete_lines",
    "file_apply_patch",
    "undo_operation",
    "undo_last",
}


def test_handshake_publishes_every_tool_with_schema_and_annotations(
    target: ServerTarget,
) -> None:
    result = exercise(target, [])

    advertised = {tool.name: tool for tool in result.tools}
    assert set(advertised) == {name for name, _ in TOOL_BINDINGS}
    assert len(advertised) == 25

    for name, tool in advertised.items():
        assert tool.description, f"{name} has no description"
        assert len(tool.description) > 60, name
        assert tool.input_schema["type"] == "object"
        assert tool.annotations is not None, name
        annotations = tool.annotations
        assert annotations.read_only_hint is not None, name
        assert annotations.destructive_hint is not None, name
        assert annotations.idempotent_hint is not None, name
        assert annotations.open_world_hint is False, name


def test_readers_are_read_only_and_destructive_tools_say_so(target: ServerTarget) -> None:
    advertised = {tool.name: tool for tool in exercise(target, []).tools}

    for name in READ_ONLY_TOOLS:
        annotations = advertised[name].annotations
        assert annotations is not None
        assert annotations.read_only_hint is True, name
        assert annotations.destructive_hint is False, name
        assert annotations.idempotent_hint is True, name

    for name in DESTRUCTIVE_TOOLS:
        annotations = advertised[name].annotations
        assert annotations is not None
        assert annotations.read_only_hint is False, name
        assert annotations.destructive_hint is True, name

    # A whole-file write replaces content and appends history, so it is neither
    # read-only nor idempotent.
    write = advertised["file_write"].annotations
    assert write is not None
    assert write.read_only_hint is False
    assert write.idempotent_hint is False

    # Creating a directory is additive: nothing existing is replaced.
    create_directory = advertised["directory_create"].annotations
    assert create_directory is not None
    assert create_directory.destructive_hint is False


def test_schemas_require_the_arguments_a_caller_cannot_guess(target: ServerTarget) -> None:
    advertised = {tool.name: tool for tool in exercise(target, []).tools}

    required = {
        name: set(tool.input_schema.get("required", []))
        for name, tool in advertised.items()
    }
    assert required["file_read"] == {"path"}
    assert required["file_replace_text"] == {"path", "old", "new", "match"}
    assert required["file_apply_patch"] == {"patch"}
    assert required["undo_operation"] == {"operation_id"}
    assert "start_line" in advertised["file_replace_lines"].input_schema["properties"]
    assert "expected_hash" in advertised["file_write"].input_schema["properties"]
    assert "dry_run" in advertised["file_write"].input_schema["properties"]


def test_replace_text_schema_publishes_the_match_policy(target: ServerTarget) -> None:
    advertised = {tool.name: tool for tool in exercise(target, []).tools}

    match = advertised["file_replace_text"].input_schema["properties"]["match"]
    assert set(match["enum"]) == {"first", "all"}
    assert len(match["enum"]) == 2
    assert match.get("description") == "replacement policy: 'first' or 'all'"
